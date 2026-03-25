"""
bot.py — BTC 15-Minute Trading Engine (plugin).
Regime gating, trade execution, sell target management.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import json
import time
import logging
import traceback
import threading
from datetime import datetime, timezone, timedelta
from collections import Counter

from config import ET, CT, KALSHI_FEE_RATE
from regime import (
    compute_coarse_label, score_spread,
    get_live_price,
)
from db import (
    get_config, set_config, get_all_config,
    get_plugin_state, update_plugin_state,
    get_pending_commands, complete_command, cancel_command, flush_pending_commands,
    get_latest_regime_snapshot,
    insert_log, now_utc,
    insert_bankroll_snapshot,
)
from plugins.btc_15m.market_db import (
    upsert_market, update_market_outcome,
    insert_trade, update_trade, get_trade, get_recent_trades, delete_trades,
    insert_price_point, insert_live_price,
    get_strategy_risk, update_regime_stats, recompute_all_stats,
    get_skipped_trades_needing_result, backfill_skipped_result,
    refresh_all_coarse_regime_stats, refresh_all_hourly_stats,
    get_observation_count,
)
from plugins.btc_15m.strategy import (
    MarketObserver, backfill_observation_results,
    get_recommendation, BtcFairValueModel,
    run_simulation_batch, compute_btc_probability_surface,
    compute_feature_importance,
)
from plugins.btc_15m.notifications import (
    notify_trade_result, notify_buy, notify_observed,
    notify_error, notify_new_regime, notify_regime_classified,
    notify_trade_update, notify_early_exit,
    notify_balance_anomaly,
)

log = logging.getLogger("btc_15m")

PLUGIN_ID = "btc_15m"

# ═══════════════════════════════════════════════════════════════
#  Module-level state (initialized in run_loop)
# ═══════════════════════════════════════════════════════════════
_observer = None
_fair_value_model = None
_fv_btc_open = None
_fv_market_ticker = None
_fv_last_btc_fetch = 0
_fv_last_btc_price = None
_skip_first_market = True


# ═══════════════════════════════════════════════════════════════
#  DB LOGGER
# ═══════════════════════════════════════════════════════════════

def blog(level: str, msg: str, category: str = "btc_15m"):
    """Log to both Python logger and database for dashboard display."""
    getattr(log, level.lower(), log.info)(msg)
    try:
        insert_log(level.upper(), msg, category)
    except Exception:
        pass


def fpnl(val: float) -> str:
    """Format P&L with sign before dollar: +$5.00 or -$5.00"""
    return f"+${val:.2f}" if val >= 0 else f"-${abs(val):.2f}"


# ═══════════════════════════════════════════════════════════════
#  MARKET DISCOVERY (moved from old kalshi.py)
# ═══════════════════════════════════════════════════════════════

def find_current_market(client) -> dict | None:
    """Find the currently active 15-min BTC market."""
    et = datetime.now(ET)
    close_minute = ((et.minute // 15) + 1) * 15
    close_hour = et.hour
    day_offset = 0

    if close_minute >= 60:
        close_minute -= 60
        close_hour += 1
        if close_hour >= 24:
            close_hour -= 24
            day_offset = 1

    close_et = et.replace(
        hour=close_hour, minute=close_minute,
        second=0, microsecond=0
    )
    if day_offset:
        close_et += timedelta(days=1)

    ticker = _build_ticker(close_et)
    return _fetch_market_safe(client, ticker)


def _build_ticker(close_et: datetime) -> str:
    """Build the Kalshi ticker string from close time in ET."""
    _MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
               "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
    mon = _MONTHS[close_et.month - 1]
    event_ticker = f"KXBTC15M-{close_et.strftime('%y')}{mon}{close_et.strftime('%d%H%M')}"
    return f"{event_ticker}-{close_et.minute:02d}"


def _fetch_market_safe(client, ticker: str) -> dict | None:
    """Fetch market, return None if not found or already closed."""
    market = client.get_market(ticker)
    if not market.get("ticker"):
        return None
    close_str = market.get("close_time", "")
    if close_str:
        close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
        if close_dt < datetime.now(timezone.utc):
            return None
    return market


# ═══════════════════════════════════════════════════════════════
#  CONFIG HELPERS
# ═══════════════════════════════════════════════════════════════

def load_config() -> dict:
    """Load plugin config from DB, merging with defaults."""
    from plugins.btc_15m.plugin import Btc15mPlugin
    defaults = Btc15mPlugin().get_default_config()
    stored = get_all_config(namespace=PLUGIN_ID)
    cfg = {**defaults}
    prefix = f"{PLUGIN_ID}."
    for k, v in stored.items():
        short_key = k[len(prefix):] if k.startswith(prefix) else k
        if short_key in cfg:
            cfg[short_key] = v
    return cfg


def save_config_updates(updates: dict):
    """Write config updates with plugin namespace."""
    for k, v in updates.items():
        set_config(f"{PLUGIN_ID}.{k}", v)


def get_trading_mode(cfg: dict) -> str:
    """Get trading mode. No legacy boolean fallback needed."""
    mode = cfg.get("trading_mode", "")
    if mode in ("observe", "shadow", "hybrid", "auto", "manual"):
        return mode
    return "manual"


# ═══════════════════════════════════════════════════════════════
#  STATE HELPERS
# ═══════════════════════════════════════════════════════════════

def _get_state() -> dict:
    """Get plugin state dict."""
    ps = get_plugin_state(PLUGIN_ID)
    return ps.get("state", {})


def _update_state(data: dict):
    """Merge data into plugin state."""
    update_plugin_state(PLUGIN_ID, {"state": data})


def _update_status(status: str = None, detail: str = None, state_data: dict = None):
    """Update status/detail and optionally merge state data."""
    d = {}
    if status:
        d["status"] = status
    if detail:
        d["status_detail"] = detail
    if state_data:
        d["state"] = state_data
    if d:
        update_plugin_state(PLUGIN_ID, d)


# ═══════════════════════════════════════════════════════════════
#  BANKROLL & SIZING
# ═══════════════════════════════════════════════════════════════

def get_effective_bankroll_cents(client, cfg: dict) -> int:
    """Cash balance minus locked bankroll, in cents."""
    raw = client.get_balance_cents()
    locked_c = int(float(cfg.get("locked_bankroll", 0)) * 100)
    return max(raw - locked_c, 0)


def get_r1_bet_dollars(cfg: dict, bankroll: float, edge_pct: float = None) -> float:
    """Compute bet size based on bet mode (flat/percent/edge_scaled)."""
    mode = cfg.get("bet_mode", "flat")
    if mode == "percent":
        base = (cfg.get("bet_size", 5.0) / 100) * bankroll
    elif mode == "edge_scaled":
        base = cfg.get("bet_size", 50.0)
        tiers = cfg.get("edge_tiers", [
            {"min_edge": 0, "multiplier": 0.5},
            {"min_edge": 2, "multiplier": 1.0},
            {"min_edge": 5, "multiplier": 1.5},
            {"min_edge": 10, "multiplier": 2.0},
        ])
        if isinstance(tiers, str):
            tiers = json.loads(tiers)
        multiplier = 0.5
        if edge_pct is not None:
            for tier in sorted(tiers, key=lambda t: t["min_edge"], reverse=True):
                if edge_pct >= tier["min_edge"]:
                    multiplier = tier["multiplier"]
                    break
        base = base * multiplier
    else:
        base = cfg.get("bet_size", 50.0)
    return base


def check_balance_safety(client, cfg: dict, bet_dollars: float, entry_price_c: int) -> tuple:
    """Check if we can afford this bet. Returns (safe, reason)."""
    bankroll_c = get_effective_bankroll_cents(client, cfg)
    bankroll = bankroll_c / 100
    shares = client.calc_shares_for_dollars(bet_dollars, entry_price_c)
    est_cost = shares * entry_price_c / 100 + client.estimate_fees(shares, entry_price_c)
    if est_cost > bankroll:
        reason = (f"Insufficient bankroll: need ~${est_cost:.2f} but have "
                  f"${bankroll:.2f}. Stopping.")
        blog("WARNING", reason)
        _update_status("stopped", "Bankroll insufficient",
                       {"auto_trading": 0, "trades_remaining": 0,
                        "bankroll_cents": bankroll_c})
        return False, reason
    return True, ""


# ═══════════════════════════════════════════════════════════════
#  STRATEGY KEY & REGIME GATING
# ═══════════════════════════════════════════════════════════════

def build_strategy_key(cfg: dict) -> str:
    """Map bot settings to strategy key: side:timing:entry_max:sell_target"""
    side_rule = cfg.get("strategy_side", "cheaper")
    if side_rule not in ("cheaper", "yes", "no", "model"):
        side_rule = "cheaper"

    delay = float(cfg.get("entry_delay_minutes", 0))
    if delay >= 8:
        timing = "late"
    elif delay >= 4:
        timing = "mid"
    else:
        timing = "early"

    max_price = int(cfg.get("entry_price_max_c", 45))
    entry_max = round(max_price / 5) * 5
    entry_max = max(5, min(entry_max, 95))

    sell_raw = cfg.get("sell_target_c", 0)
    if sell_raw and int(sell_raw) > 0:
        sell_target = round(int(sell_raw) / 5) * 5
        sell_target = max(10, min(sell_target, 99))
        if sell_target > 95:
            sell_target = 99
    else:
        sell_target = "hold"

    return f"{side_rule}:{timing}:{entry_max}:{sell_target}"


def _base_regime_label(label: str) -> str:
    """Strip modifiers to get base regime label."""
    base = label
    for prefix in ("thin_", "squeeze_"):
        if base.startswith(prefix):
            base = base[len(prefix):]
    for suffix in ("_accel", "_decel"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
    return base


def _get_regime_filter(regime_label: str, filters: dict) -> dict:
    """Look up per-regime filters with base-label fallback."""
    rf = filters.get(regime_label, {})
    if not rf:
        base = _base_regime_label(regime_label)
        if base != regime_label:
            rf = filters.get(base, {})
    return rf


def check_regime_gate(cfg: dict, regime_label: str,
                      strategy_risk: dict = None,
                      coarse_regime: str = None) -> dict:
    """Determine if we should trade based on strategy risk in this regime."""
    if not regime_label:
        regime_label = "unknown"

    if strategy_risk:
        risk_level = strategy_risk.get("risk_level", "unknown")
        win_rate = strategy_risk.get("win_rate", 0)
        sample_n = strategy_risk.get("sample_size", 0)
        ev = strategy_risk.get("ev_per_trade_c")
        setup = strategy_risk.get("setup_key", "")
        risk_score = strategy_risk.get("risk_score", 0)
        ev_str = f", EV {ev:+.1f}¢" if ev is not None else ""
        src = f"from {setup}" if setup else "no data"
        info_str = (f"'{regime_label}' strategy {risk_level} "
                    f"(score={risk_score:.0f}, WR={win_rate:.0%}, n={sample_n}{ev_str}, {src})")
    else:
        risk_level = "unknown"
        info_str = f"'{regime_label}' (unknown)"

    # Trade-all bypass
    if cfg.get("auto_strat_trade_all", False):
        blog("INFO", f"Trade-all active for '{regime_label}' — bypassing risk gate")
        return {
            "should_trade": True, "is_data_collection": False,
            "reason": f"Regime {info_str} — trade-all",
            "risk_level": risk_level, "strategy_risk": strategy_risk,
        }

    # Quick-trade whitelist
    qt_regimes = cfg.get("quick_trade_regimes", [])
    if isinstance(qt_regimes, str):
        qt_regimes = json.loads(qt_regimes)
    if qt_regimes:
        base = _base_regime_label(regime_label)
        if regime_label in qt_regimes or base in qt_regimes:
            blog("INFO", f"Quick-trade active for '{regime_label}' — bypassing risk gate")
            return {
                "should_trade": True, "is_data_collection": False,
                "reason": f"Regime {info_str} — quick-trade",
                "risk_level": risk_level, "strategy_risk": strategy_risk,
            }
        else:
            blog("INFO", f"Quick-trade active — '{regime_label}' not in whitelist, skipping")
            return {
                "should_trade": False, "is_data_collection": False,
                "reason": f"Regime {info_str} — not in quick-trade whitelist",
                "risk_level": risk_level, "strategy_risk": strategy_risk,
            }

    # Override priority: exact fine → base → coarse → risk level
    overrides = cfg.get("regime_overrides", {})
    if isinstance(overrides, str):
        overrides = json.loads(overrides)
    risk_actions = cfg.get("risk_level_actions", {})
    if isinstance(risk_actions, str):
        risk_actions = json.loads(risk_actions)
    defaults = {"low": "normal", "moderate": "normal", "high": "normal",
                "terrible": "skip", "unknown": "skip"}

    action = overrides.get(regime_label, "default")
    if action == "_custom":
        action = "default"
    if action == "default":
        base = _base_regime_label(regime_label)
        if base != regime_label:
            action = overrides.get(base, "default")
            if action == "_custom":
                action = "default"
    if action == "default" and coarse_regime:
        action = overrides.get(coarse_regime, "default")
        if action == "_custom":
            action = "default"
    if action == "default":
        action = risk_actions.get(risk_level, defaults.get(risk_level, "normal"))
    if action == "data":
        action = "skip"
    if action == "_custom":
        action = risk_actions.get(risk_level, defaults.get(risk_level, "normal"))

    if action == "skip":
        return {
            "should_trade": False, "is_data_collection": False,
            "reason": f"Regime {info_str} — skipping",
            "risk_level": risk_level, "strategy_risk": strategy_risk,
        }
    return {
        "should_trade": True, "is_data_collection": False,
        "reason": f"Regime {info_str}",
        "risk_level": risk_level, "strategy_risk": strategy_risk,
    }


# ═══════════════════════════════════════════════════════════════
#  REGIME NOTIFICATIONS
# ═══════════════════════════════════════════════════════════════

def _update_regime_with_notify(regime_label: str):
    """Update regime stats and send notifications for changes."""
    if not regime_label:
        return
    try:
        result = update_regime_stats(regime_label)
        if not result:
            return
        if result.get("is_new") and result["total"] > 0:
            notify_new_regime(regime_label, result["total"])
        old_risk = result.get("old_risk")
        new_risk = result.get("new_risk")
        if old_risk and new_risk and old_risk != new_risk:
            notify_regime_classified(
                regime_label, new_risk,
                total=result["total"], win_rate=result["win_rate"],
                old_risk=old_risk,
            )
    except Exception as e:
        blog("WARNING", f"Regime stats/notify error: {e}")


# ═══════════════════════════════════════════════════════════════
#  SKIP/RESOLVE HELPERS
# ═══════════════════════════════════════════════════════════════

def _resolve_skip_inline(client, trade_id: int, ticker: str,
                         market_id: int = None):
    """Fetch market result after close and update trade record."""
    if not ticker or ticker == "n/a":
        return
    try:
        time.sleep(5)
        market_result = None
        for _ in range(12):
            market_result = client.get_market_result(ticker)
            if market_result:
                break
            time.sleep(5)
        if not market_result:
            blog("INFO", f"Skip {trade_id}: no market result after 60s, backfill will handle")
            return
        update_trade(trade_id, {"market_result": market_result})
        blog("INFO", f"Skip {trade_id}: market result {market_result.upper()}")
        if market_id:
            try:
                update_market_outcome(market_id, market_result)
            except Exception:
                pass
    except Exception as e:
        blog("WARNING", f"Inline skip resolve error: {e}")


def _skip_wait_loop(client, cfg, close_dt, skip_trade_id, ticker,
                    regime_label, risk_level, reason,
                    track_side=False, resolve_inline=False,
                    initial_cheaper_side=None, market_id=None) -> bool:
    """Wait for skipped market to close, processing commands. Returns True if stopped early."""
    secs = (close_dt - datetime.now(timezone.utc)).total_seconds()
    if secs <= 0:
        if resolve_inline:
            _resolve_skip_inline(client, skip_trade_id, ticker, market_id=market_id)
        return False

    deadline = time.monotonic() + secs + 2
    cheaper_prices = []
    cheaper_sides = []
    stopped_early = False

    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        ctx = _trade_ctx()
        _update_status(detail=f"Observing {regime_label.replace('_', ' ')} — next in ~{_fmt_wait(remaining)}{ctx}")

        for cmd in get_pending_commands(PLUGIN_ID):
            cmd_type = cmd["command_type"]
            cmd_id = cmd["id"]
            params = json.loads(cmd.get("parameters") or "{}")
            if cmd_type == "stop":
                _update_status("stopped", "Stopped",
                               {"auto_trading": 0, "trades_remaining": 0})
                complete_command(cmd_id)
                stopped_early = True
                break
            elif cmd_type == "update_config":
                for k, v in params.items():
                    set_config(f"{PLUGIN_ID}.{k}", v)
                    if k in cfg:
                        cfg[k] = v
                complete_command(cmd_id)
            else:
                complete_command(cmd_id)

        if stopped_early:
            break

        if track_side:
            try:
                m_opp = client.get_market(ticker)
                csid, cprice = client.get_cheaper_side(m_opp)
                if cprice > 0:
                    cheaper_prices.append(cprice)
                    cheaper_sides.append(csid)
            except Exception:
                pass

        try:
            poll_live_market(client, cfg)
        except Exception:
            pass
        try:
            _update_state({"bankroll_cents": client.get_balance_cents()})
        except Exception:
            pass

        time.sleep(min(2, max(0, remaining)))

    if track_side and cheaper_sides and not stopped_early:
        most_common_side = Counter(cheaper_sides).most_common(1)[0][0]
        avg_cheaper_c = round(sum(cheaper_prices) / len(cheaper_prices), 1) if cheaper_prices else None
        try:
            update_trade(skip_trade_id, {
                "side": most_common_side,
                "avg_fill_price_c": int(avg_cheaper_c) if avg_cheaper_c else None,
            })
        except Exception:
            pass

    if resolve_inline and not stopped_early:
        _resolve_skip_inline(client, skip_trade_id, ticker, market_id=market_id)

    return stopped_early


# ═══════════════════════════════════════════════════════════════
#  TRADE CONTEXT
# ═══════════════════════════════════════════════════════════════

def _build_trade_context(client, cfg, state, market, snapshot, gate,
                         coarse_regime, prev_regime, hour_et, day_of_week,
                         vol_level=None, close_str=None):
    """Build common context dict for all trade inserts."""
    btc_price = get_live_price("BTC")
    eff_bankroll_c = get_effective_bankroll_cents(client, cfg)

    spread_c = None
    cheaper_side = cheaper_side_price_c = None
    yes_ask = no_ask = yes_bid = no_bid = None
    kalshi_volume = kalshi_oi = None
    if market:
        try:
            ya = market.get("yes_ask") or market.get("yes_price")
            na = market.get("no_ask") or market.get("no_price")
            yes_ask = ya
            no_ask = na
            yes_bid = market.get("yes_bid")
            no_bid = market.get("no_bid")
            kalshi_volume = market.get("volume")
            kalshi_oi = market.get("open_interest")
            if ya and na:
                spread_c = abs(ya - na) if ya < 90 and na < 90 else None
            cs, cp = client.get_cheaper_side(market)
            if cs and cp > 0:
                cheaper_side = cs
                cheaper_side_price_c = cp
        except Exception:
            pass

    mins_before_close = None
    if close_str:
        try:
            mins_before_close = round(client.minutes_until_close(close_str), 2)
        except Exception:
            pass

    return {
        "regime_label": snapshot.get("composite_label", "unknown") if snapshot else "unknown",
        "regime_risk_level": gate.get("risk_level", "unknown") if gate else "unknown",
        "regime_confidence": snapshot.get("regime_confidence") if snapshot else None,
        "vol_regime": vol_level,
        "trend_regime": snapshot.get("trend_regime") if snapshot else None,
        "volume_regime": snapshot.get("volume_regime") if snapshot else None,
        "coarse_regime": coarse_regime,
        "prev_regime_label": prev_regime,
        "trend_direction": snapshot.get("trend_direction") if snapshot else None,
        "trend_strength": snapshot.get("trend_strength") if snapshot else None,
        "bollinger_squeeze": snapshot.get("bollinger_squeeze", 0) if snapshot else 0,
        "trend_acceleration": snapshot.get("trend_acceleration") if snapshot else None,
        "volume_spike": snapshot.get("volume_spike", 0) if snapshot else 0,
        "bollinger_width": snapshot.get("bollinger_width_15m") if snapshot else None,
        "atr_15m": snapshot.get("atr_15m") if snapshot else None,
        "realized_vol": snapshot.get("realized_vol_15m") if snapshot else None,
        "ema_slope_15m": snapshot.get("ema_slope_15m") if snapshot else None,
        "ema_slope_1h": snapshot.get("ema_slope_1h") if snapshot else None,
        "btc_return_15m": snapshot.get("btc_return_15m") if snapshot else None,
        "btc_return_1h": snapshot.get("btc_return_1h") if snapshot else None,
        "btc_return_4h": snapshot.get("btc_return_4h") if snapshot else None,
        "btc_price_at_entry": btc_price,
        "yes_ask_at_entry": yes_ask,
        "no_ask_at_entry": no_ask,
        "yes_bid_at_entry": yes_bid,
        "no_bid_at_entry": no_bid,
        "kalshi_market_volume": kalshi_volume,
        "kalshi_open_interest": kalshi_oi,
        "spread_at_entry_c": spread_c,
        "spread_regime": score_spread(spread_c),
        "cheaper_side": cheaper_side,
        "cheaper_side_price_c": cheaper_side_price_c,
        "bankroll_at_entry_c": eff_bankroll_c,
        "hour_et": hour_et,
        "day_of_week": day_of_week,
        "minute_et": datetime.now(ET).minute,
        "minutes_before_close": mins_before_close,
        "market_close_time_utc": close_str,
        "trade_mode": get_trading_mode(cfg),
        "entry_delay_minutes": cfg.get("entry_delay_minutes", 0),
        "btc_distance_pct": (
            round((_fv_last_btc_price - _fv_btc_open) / _fv_btc_open * 100, 4)
            if _fv_btc_open and _fv_btc_open > 0 and _fv_last_btc_price
            else (snapshot.get("btc_return_15m") if snapshot else None)
        ),
    }


# ═══════════════════════════════════════════════════════════════
#  SHADOW TRADING
# ═══════════════════════════════════════════════════════════════

def _get_shadow_strategy(regime_label: str, hour_et: int = None,
                         vol_regime: int = None, trend_regime: int = None) -> dict | None:
    """Get best strategy for shadow trading — NO validation gates."""
    from plugins.btc_15m.market_db import get_strategy_for_setup
    candidates = []
    if regime_label and regime_label != "unknown":
        candidates.append(f"regime:{regime_label}")
    if vol_regime is not None and trend_regime is not None:
        try:
            coarse = compute_coarse_label(int(vol_regime), int(trend_regime))
            candidates.append(f"coarse_regime:{coarse}")
        except Exception:
            pass
    candidates.append("global:all")

    for setup in candidates:
        results = get_strategy_for_setup(setup, min_samples=5)
        if results:
            row = results[0]
            return {
                "setup_key": row.get("setup_key"),
                "strategy_key": row.get("strategy_key"),
                "side_rule": row.get("side_rule"),
                "entry_time_rule": row.get("entry_time_rule"),
                "entry_price_max": row.get("entry_price_max"),
                "sell_target": row.get("exit_rule"),
                "ev_per_trade_c": row.get("ev_per_trade_c"),
                "win_rate": row.get("win_rate"),
                "sample_size": row.get("sample_size"),
            }
    return None


def _place_shadow_trade(client, ticker: str, side: str, price_c: int,
                        market_id: int = None, regime_label: str = None,
                        snapshot_id: int = None, ctx: dict = None,
                        strategy_key: str = None) -> int | None:
    """Place a 1-contract shadow trade for execution data collection."""
    if not side or side == "n/a" or not price_c or price_c <= 0 or price_c >= 95:
        return None

    decision_time = time.time()
    decision_price_c = price_c

    try:
        resp = client.place_limit_order(ticker, side, 1, price_c, action="buy")
        order = resp.get("order", {})
        order_id = order.get("order_id")
        if not order_id:
            return None

        fill = None
        status = order.get("status", "")
        if status == "executed":
            fill = client.parse_fill(order)
        elif status == "resting":
            deadline = time.time() + 60
            fill = client.poll_until_filled(order_id, 1, deadline, interval=3)
        else:
            client.cancel_order(order_id)
            return None

        fill_count = fill.get("fill_count", 0) if fill else 0
        fill_time = time.time()
        latency_ms = int((fill_time - decision_time) * 1000)

        if fill_count == 0:
            try:
                client.cancel_order(order_id)
            except Exception:
                pass
            blog("DEBUG", f"Shadow trade: no fill on {ticker} {side}@{price_c}¢")
            return None

        avg_fill = fill.get("avg_price_c", price_c)
        cost_cents = fill.get("contract_cost_cents", 0)
        fees = fill.get("fees_dollars", 0)
        actual_cost = (cost_cents / 100) + fees

        trade_data = {
            "market_id": market_id,
            "regime_snapshot_id": snapshot_id,
            "ticker": ticker,
            "side": side,
            "shares_filled": 1,
            "avg_fill_price_c": avg_fill,
            "entry_price_c": price_c,
            "actual_cost": round(actual_cost, 4),
            "outcome": "open",
            "is_shadow": 1,
            "is_ignored": 1,
            "shadow_decision_price_c": decision_price_c,
            "shadow_fill_latency_ms": latency_ms,
            "regime_label": regime_label,
            "auto_strategy_key": strategy_key,
            "spread_at_entry_c": (ctx or {}).get("spread_at_entry_c"),
            "yes_ask_at_entry": (ctx or {}).get("yes_ask_at_entry"),
            "no_ask_at_entry": (ctx or {}).get("no_ask_at_entry"),
            "yes_bid_at_entry": (ctx or {}).get("yes_bid_at_entry"),
            "no_bid_at_entry": (ctx or {}).get("no_bid_at_entry"),
        }

        shadow_id = insert_trade(trade_data)
        slip = avg_fill - decision_price_c
        _strat_label = f" strat={strategy_key}" if strategy_key else ""
        blog("INFO", f"Shadow trade: {side}@{avg_fill}¢ "
                      f"(ask was {decision_price_c}¢, slip={slip:+d}¢, "
                      f"latency={latency_ms}ms{_strat_label}) [{ticker}]")
        return shadow_id

    except Exception as e:
        blog("DEBUG", f"Shadow trade failed: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
#  DISPLAY HELPERS
# ═══════════════════════════════════════════════════════════════

def marketStartTime(close_time_str: str) -> str:
    """Convert a market close time to its start time label in Central."""
    try:
        close_dt = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
        start_dt = close_dt - timedelta(minutes=15)
        ct = start_dt.astimezone(CT)
        return ct.strftime("%-I:%M %p CT")
    except Exception:
        return close_time_str


def _trade_ctx() -> str:
    """Build context suffix for status messages."""
    state = _get_state()
    parts = []
    rem = state.get("trades_remaining", 0)
    if rem and rem > 0:
        parts.append(f"{rem} trade{'s' if rem != 1 else ''} left")
    return (" · " + " · ".join(parts)) if parts else ""


def _fmt_wait(secs: float) -> str:
    """Format seconds as Xm Xs."""
    s = max(0, int(secs))
    return f"{s // 60}m {s % 60:02d}s"


# ═══════════════════════════════════════════════════════════════
#  LIVE MARKET POLLING
# ═══════════════════════════════════════════════════════════════

def poll_live_market(client, cfg: dict):
    """Poll current market info and write to plugin state for dashboard."""
    global _fv_btc_open, _fv_market_ticker, _fv_last_btc_fetch, _fv_last_btc_price
    try:
        market = find_current_market(client)
        if not market:
            _update_state({"live_market": None})
            return

        ticker = market["ticker"]
        close_str = market.get("close_time", "")
        mins_left = client.minutes_until_close(close_str) if close_str else 0
        side, price_c = client.get_cheaper_side(market)

        snapshot = get_latest_regime_snapshot("BTC")
        regime_label = snapshot.get("composite_label", "unknown") if snapshot else "unknown"

        if snapshot and regime_label != "unknown":
            try:
                snap_time = datetime.fromisoformat(
                    snapshot["captured_at"].replace("Z", "+00:00"))
                snap_age_s = (datetime.now(timezone.utc) - snap_time).total_seconds()
                if snap_age_s > 600:
                    regime_label = "unknown"
            except Exception:
                pass

        _poll_risk_level = "unknown"
        _poll_win_rate = 0
        _poll_sample_n = 0
        _poll_strat_risk = None
        try:
            _poll_cfg = load_config()
            _poll_strat_key = build_strategy_key(_poll_cfg)
            _poll_strat_risk = get_strategy_risk(regime_label, _poll_strat_key)
            if (_poll_strat_risk.get("risk_level") == "unknown"
                    and _poll_cfg.get("strategy_side") == "model"):
                _fb_key = _poll_strat_key.replace("model:", "cheaper:", 1)
                _fb = get_strategy_risk(regime_label, _fb_key)
                if _fb and _fb.get("risk_level") != "unknown":
                    _poll_strat_risk = _fb
            _poll_risk_level = _poll_strat_risk.get("risk_level", "unknown")
            _poll_win_rate = _poll_strat_risk.get("win_rate", 0)
            _poll_sample_n = _poll_strat_risk.get("sample_size", 0)
        except Exception:
            pass

        live_data = {
            "ticker": ticker,
            "close_time": close_str,
            "minutes_left": round(mins_left, 1),
            "cheaper_side": side,
            "cheaper_price_c": price_c,
            "yes_ask": market.get("yes_ask"),
            "no_ask": market.get("no_ask"),
            "yes_bid": market.get("yes_bid"),
            "no_bid": market.get("no_bid"),
            "regime_label": regime_label,
            "risk_level": _poll_risk_level,
            "regime_win_rate": _poll_win_rate,
            "regime_trades": _poll_sample_n,
            "btc_price": snapshot.get("btc_price") if snapshot else None,
            "vol_regime": snapshot.get("vol_regime") if snapshot else None,
            "trend_regime": snapshot.get("trend_regime") if snapshot else None,
            "volume_regime": snapshot.get("volume_regime") if snapshot else None,
        }

        if _fair_value_model:
            try:
                if ticker != _fv_market_ticker:
                    _fv_market_ticker = ticker
                    _btc_snap = snapshot.get("btc_price") if snapshot else None
                    if _btc_snap and _btc_snap > 0:
                        _fv_btc_open = _btc_snap
                        _fv_last_btc_price = _btc_snap
                        _fv_last_btc_fetch = time.time()

                _now_fv = time.time()
                if _fv_btc_open and _now_fv - _fv_last_btc_fetch >= 10:
                    _btc_f = get_live_price("BTC")
                    if _btc_f and _btc_f > 0:
                        _fv_last_btc_price = _btc_f
                        _fv_last_btc_fetch = _now_fv

                if _fv_btc_open and _fv_btc_open > 0 and _fv_last_btc_price:
                    _dist = (_fv_last_btc_price - _fv_btc_open) / _fv_btc_open * 100
                    _secs = max(0, 900 - mins_left * 60)
                    _rvol = snapshot.get("realized_vol_15m") if snapshot else None
                    _edge = _fair_value_model.compute_edge(
                        yes_ask_c=market.get("yes_ask") or 0,
                        no_ask_c=market.get("no_ask") or 0,
                        btc_distance_pct=_dist,
                        seconds_into_market=_secs,
                        realized_vol=_rvol,
                        vol_regime=snapshot.get("vol_regime") if snapshot else None,
                    )
                    live_data["fv_model"] = {
                        "btc_open": round(_fv_btc_open, 0),
                        "btc_now": round(_fv_last_btc_price, 0),
                        "btc_distance_pct": round(_dist, 4),
                        "fair_yes_c": _edge["model"]["fair_yes_c"],
                        "fair_no_c": _edge["model"]["fair_no_c"],
                        "yes_edge_pct": _edge["yes_edge_pct"],
                        "no_edge_pct": _edge["no_edge_pct"],
                        "yes_ev_c": _edge["yes_ev_c"],
                        "no_ev_c": _edge["no_ev_c"],
                        "recommended_side": _edge["recommended_side"],
                        "best_edge_pct": _edge["best_edge_pct"],
                        "source": _edge["model"]["source"],
                        "confidence": _edge["model"]["confidence"],
                    }
            except Exception:
                pass

        _update_state({"live_market": live_data})

        if _observer:
            market_data = {
                "yes_ask": market.get("yes_ask"),
                "no_ask": market.get("no_ask"),
                "yes_bid": market.get("yes_bid"),
                "no_bid": market.get("no_bid"),
                "btc_price": snapshot.get("btc_price") if snapshot else None,
                "volume": market.get("volume"),
                "open_interest": market.get("open_interest"),
            }
            _observer.tick(ticker, close_str, market_data, snapshot, _poll_strat_risk)

        try:
            insert_live_price(ticker, market.get("yes_ask"), market.get("no_ask"),
                              market.get("yes_bid"), market.get("no_bid"))
        except Exception:
            pass

    except Exception as e:
        log.debug(f"Live market poll error: {e}")


# ═══════════════════════════════════════════════════════════════
#  WAIT FOR NEXT MARKET
# ═══════════════════════════════════════════════════════════════

def wait_for_next_market(client, cfg: dict) -> dict | None:
    """Wait until a fresh market is available."""
    global _skip_first_market
    state = _get_state()
    last_ticker = state.get("last_ticker")

    current = find_current_market(client)
    if current:
        ticker = current["ticker"]
        close_str = current.get("close_time", "")

        if _skip_first_market and ticker != last_ticker:
            mins_left = client.minutes_until_close(close_str) if close_str else 0
            blog("INFO", f"Skipping mid-market {ticker} ({mins_left:.1f}m left) — "
                          f"waiting for next fresh market after start/restart")
        elif ticker != last_ticker:
            mins_left = client.minutes_until_close(close_str) if close_str else 0
            if mins_left > 12:
                blog("INFO", f"Fresh market available: {ticker} ({mins_left:.1f}m left)")
                _skip_first_market = False
                return current
            else:
                blog("INFO", f"Market {ticker} has only {mins_left:.1f}m left — waiting for next one")

        secs_left = client.minutes_until_close(close_str) * 60 if close_str else 30
        if secs_left > 0:
            blog("INFO", f"Waiting {secs_left:.0f}s for {ticker} to close")
            deadline = time.monotonic() + secs_left + 5

            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                ctx = _trade_ctx()
                skip_info = _get_state().get("active_skip")
                if skip_info and skip_info.get("ticker") == ticker:
                    skip_reason = skip_info.get("reason", "")
                    short_reason = skip_reason[:50] if skip_reason else "regime filter"
                    detail = f"Observing: {short_reason} — next in ~{_fmt_wait(remaining)}{ctx}"
                else:
                    detail = f"Next market in ~{_fmt_wait(remaining)}{ctx}"
                _update_status("waiting", detail)

                for cmd in get_pending_commands(PLUGIN_ID):
                    cmd_type = cmd["command_type"]
                    cmd_id = cmd["id"]
                    params = json.loads(cmd.get("parameters") or "{}")

                    if cmd_type == "stop":
                        _update_status("stopped", "Stopped",
                                       {"auto_trading": 0, "trades_remaining": 0})
                        complete_command(cmd_id)
                        blog("INFO", "Stop received while waiting — streak reset")
                        return None

                    if cmd_type == "start":
                        mode = params.get("mode", "continuous")
                        count = params.get("count", 1)
                        base = {"auto_trading": 1, "session_stopped_at": ""}
                        if mode == "single":
                            base["trades_remaining"] = 1
                        elif mode == "count":
                            base["trades_remaining"] = count
                        else:
                            base["trades_remaining"] = 0
                        _update_state(base)
                        _update_status("waiting", f"Restarted — {mode} — waiting")
                        complete_command(cmd_id, {"mode": mode})
                        blog("INFO", f"Start received during wait: mode={mode}")
                        return None

                    if cmd_type == "update_config":
                        for k, v in params.items():
                            set_config(f"{PLUGIN_ID}.{k}", v)
                            if k in cfg:
                                cfg[k] = v
                        complete_command(cmd_id)
                    else:
                        complete_command(cmd_id)

                try:
                    poll_live_market(client, cfg)
                except Exception:
                    pass
                try:
                    _update_state({"bankroll_cents": client.get_balance_cents()})
                except Exception:
                    pass

                time.sleep(min(2, max(0, remaining)))

    blog("INFO", "Polling for new market...")
    ctx = _trade_ctx()
    _update_status("searching", f"Starting — finding market{ctx}")

    for attempt in range(30):
        for cmd in get_pending_commands(PLUGIN_ID):
            cmd_type = cmd["command_type"]
            cmd_id = cmd["id"]
            params = json.loads(cmd.get("parameters") or "{}")
            if cmd_type == "stop":
                _update_status("stopped", "Stopped",
                               {"auto_trading": 0, "trades_remaining": 0})
                complete_command(cmd_id)
                return None
            elif cmd_type == "update_config":
                for k, v in params.items():
                    set_config(f"{PLUGIN_ID}.{k}", v)
                    if k in cfg:
                        cfg[k] = v
                complete_command(cmd_id)
            else:
                complete_command(cmd_id)

        new_market = find_current_market(client)
        if new_market:
            new_ticker = new_market["ticker"]
            if new_ticker != last_ticker:
                mins_left = client.minutes_until_close(
                    new_market.get("close_time", "")) if new_market.get("close_time") else 0
                if mins_left > 2:
                    blog("INFO", f"Found new market: {new_ticker} ({mins_left:.1f}m left)")
                    _skip_first_market = False
                    return new_market
        time.sleep(2)

    blog("WARNING", "Timed out waiting for new market")
    return None


# ═══════════════════════════════════════════════════════════════
#  EXECUTE CASH OUT
# ═══════════════════════════════════════════════════════════════
#  ORPHAN TRADE RECOVERY
# ═══════════════════════════════════════════════════════════════

def monitor_orphan_trade(client, cfg: dict):
    """Monitor an active trade surviving from a restart."""
    state = _get_state()
    active = state.get("active_trade")
    if not active:
        return

    close_str = active.get("close_time", "")
    if not close_str:
        _update_state({"active_trade": None})
        return

    close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
    secs = (close_dt - datetime.now(timezone.utc)).total_seconds()

    if secs < -5:
        _resolve_orphan_trade(client, active)
        return

    ticker = active["ticker"]
    side = active["side"]
    sell_order_id = active.get("sell_order_id")

    try:
        m = client.get_market(ticker)
        cur_bid = m.get(f"{side}_bid", 0) or 0

        sell_progress = 0
        if sell_order_id:
            sell_status = client.get_order(sell_order_id)
            sell_progress = sell_status.get("fill_count", 0)
            if sell_progress >= active.get("fill_count", 0):
                _resolve_orphan_trade(client, active, sell_filled=sell_progress)
                return

        active["current_bid"] = cur_bid
        active["sell_progress"] = sell_progress
        active["minutes_left"] = round(max(secs / 60, 0), 1)
        _update_state({"active_trade": active})
    except Exception:
        pass


def _resolve_orphan_trade(client, at: dict, sell_filled: int = None):
    """Resolve a trade after market close."""
    trade_id = at.get("trade_id")
    ticker = at.get("ticker")
    side = at.get("side")
    fill_count = at.get("fill_count", 0)
    actual_cost = at.get("actual_cost", 0)
    sell_price_c = at.get("sell_price_c", 0)

    # Guard against double-resolution
    if trade_id:
        existing = get_trade(trade_id)
        if existing and existing.get("outcome") in ("win", "loss", "cashed_out"):
            _update_state({"active_trade": None})
            return

    if sell_filled is None and at.get("sell_order_id"):
        try:
            sell_filled = client.get_order(at["sell_order_id"]).get("fill_count", 0)
        except Exception:
            sell_filled = 0

    market_result = None
    for _ in range(12):
        market_result = client.get_market_result(ticker)
        if market_result:
            break
        time.sleep(3)

    won = (market_result == side) if market_result else False
    gross = client.calc_gross(fill_count, sell_filled or 0, sell_price_c, won)
    pnl = gross - actual_cost
    trade_won = gross > actual_cost
    outcome = "win" if trade_won else "loss"

    if trade_id:
        update_trade(trade_id, {
            "outcome": outcome,
            "gross_proceeds": round(gross, 2),
            "pnl": round(pnl, 2),
            "sell_filled": sell_filled or 0,
            "exit_time_utc": now_utc(),
            "market_result": market_result,
            "exit_method": "market_expiry",
            "notes": "Resolved orphan trade",
        })

    regime_label = at.get("regime_label")
    if regime_label and regime_label != "unknown":
        _update_regime_with_notify(regime_label)

    state = _get_state()
    win_key = "lifetime_wins" if trade_won else "lifetime_losses"
    _update_state({
        "active_trade": None,
        "lifetime_pnl": (state.get("lifetime_pnl") or 0) + pnl,
        win_key: (state.get(win_key) or 0) + 1,
        "last_completed_trade": {
            "trade_id": trade_id, "ticker": ticker, "side": side,
            "outcome": outcome, "pnl": round(pnl, 2),
            "market_result": market_result,
            "regime_label": regime_label,
        },
    })
    _update_status("stopped", f"Resolved: {outcome.upper()} {fpnl(pnl)}")

    new_bankroll = client.get_balance_cents()
    _update_state({"bankroll_cents": new_bankroll})
    insert_bankroll_snapshot(new_bankroll, PLUGIN_ID, trade_id)

    blog("INFO", f"Orphan trade resolved: {outcome.upper()} {fpnl(pnl)} [{ticker}]")


def backfill_trade_market_results(client, limit: int = 20):
    """Backfill missing market_result for trades."""
    trades = get_skipped_trades_needing_result(limit)
    filled = 0
    for t in trades:
        ticker = t.get("ticker")
        trade_id = t.get("id")
        if not ticker:
            continue
        result = client.get_market_result(ticker)
        if result:
            backfill_skipped_result(trade_id, result)
            filled += 1
    return filled


# ═══════════════════════════════════════════════════════════════
#  COMMAND PROCESSING
# ═══════════════════════════════════════════════════════════════

def process_commands(client, cfg: dict) -> dict:
    """Process all pending commands for this plugin."""
    global _skip_first_market

    for cmd in get_pending_commands(PLUGIN_ID):
        cmd_type = cmd["command_type"]
        cmd_id = cmd["id"]
        params = json.loads(cmd.get("parameters") or "{}")

        try:
            if cmd_type == "start":
                mode = params.get("mode", "continuous")
                count = params.get("count", 1)
                state = _get_state()

                _skip_first_market = True
                base = {"auto_trading": 1}

                if mode == "single":
                    base["trades_remaining"] = 1
                elif mode == "count":
                    base["trades_remaining"] = count
                else:
                    base["trades_remaining"] = 0

                _update_state(base)
                _update_status("searching", f"Started — {mode}")
                complete_command(cmd_id, {"mode": mode})
                blog("INFO", f"Start command: mode={mode}")
                cfg = load_config()

            elif cmd_type == "stop":
                state = _get_state()
                active = state.get("active_trade")
                if active:
                    active["is_ignored"] = True
                    _update_state({"active_trade": active})
                    trade_id = active.get("trade_id")
                    if trade_id:
                        update_trade(trade_id, {"is_ignored": 1, "notes": "Stopped mid-trade — ignored"})
                if _observer:
                    _observer.discard()
                _update_state({"auto_trading": 0, "trades_remaining": 0})
                _update_status("stopped", "Stopped")
                complete_command(cmd_id)
                blog("INFO", "Stop command received")

            elif cmd_type == "update_config":
                for k, v in params.items():
                    set_config(f"{PLUGIN_ID}.{k}", v)
                    if k in cfg:
                        cfg[k] = v
                complete_command(cmd_id, {"updated": list(params.keys())})
                blog("INFO", f"Config updated: {list(params.keys())}")
                cfg = load_config()

            elif cmd_type == "dismiss_summary":
                _update_state({"last_completed_trade": None})
                complete_command(cmd_id)

            else:
                complete_command(cmd_id)

        except Exception as e:
            blog("ERROR", f"Command {cmd_type} failed: {e}\n{traceback.format_exc()}")
            cancel_command(cmd_id, str(e))

    return cfg


# ═══════════════════════════════════════════════════════════════
#  LOG CLEANUP
# ═══════════════════════════════════════════════════════════════

def _cleanup_logs(retention_days: int = 7):
    """Clean up log file and log_entries table."""
    from config import LOG_FILE
    try:
        import os
        if os.path.exists(LOG_FILE):
            size_mb = os.path.getsize(LOG_FILE) / (1024 * 1024)
            if size_mb > 5:
                with open(LOG_FILE, 'r') as f:
                    lines = f.readlines()
                kept = lines[-20000:]
                with open(LOG_FILE, 'w') as f:
                    f.write(f"--- Log rotated at {now_utc()} (was {size_mb:.1f}MB) ---\n")
                    f.writelines(kept)
                blog("INFO", f"Log rotated: {size_mb:.1f}MB → ~{len(kept)} lines")
    except Exception as e:
        log.warning(f"Log file rotation error: {e}")

    try:
        from db import get_conn
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with get_conn() as c:
            c.execute("DELETE FROM log_entries WHERE ts < ?", (cutoff,))
    except Exception as e:
        log.warning(f"Log DB cleanup error: {e}")


# ═══════════════════════════════════════════════════════════════
#  BACKGROUND COMPUTATION THREAD
# ═══════════════════════════════════════════════════════════════

def _background_worker(stop_event):
    """Run simulation batch, probability surface, feature importance."""
    last_sim = 0
    last_analysis = 0
    SIM_INTERVAL = 1800      # 30 min
    ANALYSIS_INTERVAL = 3600  # 60 min

    while not stop_event.is_set():
        now = time.time()

        if now - last_sim >= SIM_INTERVAL:
            try:
                blog("INFO", "Background: running simulation batch")
                run_simulation_batch()
                last_sim = time.time()
                blog("INFO", "Background: simulation batch complete")
            except Exception as e:
                blog("WARNING", f"Background sim error: {e}")
                last_sim = time.time()

        if now - last_analysis >= ANALYSIS_INTERVAL:
            try:
                blog("INFO", "Background: computing probability surface")
                compute_btc_probability_surface()
                compute_feature_importance()
                last_analysis = time.time()
                blog("INFO", "Background: analysis complete")
            except Exception as e:
                blog("WARNING", f"Background analysis error: {e}")
                last_analysis = time.time()

        stop_event.wait(30)


# ═══════════════════════════════════════════════════════════════
#  MAIN TRADE ENGINE
# ═══════════════════════════════════════════════════════════════

def run_trade(client, cfg: dict) -> bool:
    """Execute one trade cycle. Returns True if a trade was placed."""
    global _fv_btc_open, _fv_market_ticker, _fv_last_btc_fetch, _fv_last_btc_price

    state = _get_state()

    # Safety: if there's an active trade, resolve first
    existing = state.get("active_trade")
    if existing:
        blog("WARNING", "Active trade exists — resolving before starting fresh")
        close_str = existing.get("close_time", "")
        if close_str:
            close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
            secs = (close_dt - datetime.now(timezone.utc)).total_seconds()
            if secs < -5:
                _resolve_orphan_trade(client, existing)
            else:
                while secs > -5:
                    monitor_orphan_trade(client, cfg)
                    time.sleep(2)
                    secs = (close_dt - datetime.now(timezone.utc)).total_seconds()
                    if not _get_state().get("active_trade"):
                        break
                if _get_state().get("active_trade"):
                    _resolve_orphan_trade(client, _get_state()["active_trade"])
        else:
            _update_state({"active_trade": None})
        return False

    # ── 0. Pre-trade checks ───────────────────────────────────
    # Simple balance safety check
    bankroll_c = get_effective_bankroll_cents(client, cfg)
    bet_dollars = get_r1_bet_dollars(cfg, bankroll_c / 100)
    if bankroll_c / 100 < bet_dollars * 0.5:
        reason = f"Bankroll ${bankroll_c / 100:.2f} too low for ${bet_dollars:.2f} bet"
        blog("WARNING", reason)
        _update_status("stopped", reason, {"auto_trading": 0, "trades_remaining": 0})
        return False

    # ── 1. Find market ────────────────────────────────────────
    market = wait_for_next_market(client, cfg)
    if not market:
        st = get_plugin_state(PLUGIN_ID).get("status", "")
        if st != "stopped":
            _update_status("waiting", f"No market found — retrying{_trade_ctx()}")
            time.sleep(15)
        return False

    ticker = market["ticker"]
    close_str = market.get("close_time", "")
    close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
    mins_left = client.minutes_until_close(close_str)

    # Already traded this market?
    state = _get_state()
    if state.get("last_ticker") == ticker:
        secs = (close_dt - datetime.now(timezone.utc)).total_seconds()
        if secs > 0:
            ctx = _trade_ctx()
            skip_info = state.get("active_skip")
            if skip_info and skip_info.get("ticker") == ticker:
                short = (skip_info.get("reason") or "")[:50]
                detail = f"Observing: {short} — next in ~{_fmt_wait(secs)}{ctx}"
            else:
                detail = f"Next market in ~{_fmt_wait(secs)}{ctx}"
            _update_status("waiting", detail)
            time.sleep(min(secs + 2, 60))
        return False

    _trading_mode = get_trading_mode(cfg)
    blog("INFO", f"Market: {ticker} | {mins_left:.1f}m to close | mode={_trading_mode}")

    now_et = datetime.now(ET)
    market_id = upsert_market(
        ticker=ticker, close_time_utc=close_dt.isoformat(),
        hour_et=now_et.hour, minute_et=now_et.minute,
        day_of_week=now_et.weekday()
    )
    _update_state({"last_ticker": ticker, "active_skip": None, "active_shadow": None})

    # ── 2. Regime check ───────────────────────────────────────
    snapshot = get_latest_regime_snapshot("BTC")
    regime_label = snapshot.get("composite_label", "unknown") if snapshot else "unknown"
    snapshot_id = snapshot.get("id") if snapshot else None

    # Regime observation count
    _regime_obs_n = 0
    try:
        obs_counts = get_observation_count()
        _regime_obs_n = obs_counts.get("resolved", 0)
    except Exception:
        pass

    # Guard against stale regime data
    if snapshot and regime_label != "unknown":
        try:
            snap_time = datetime.fromisoformat(
                snapshot["captured_at"].replace("Z", "+00:00"))
            snap_age_s = (datetime.now(timezone.utc) - snap_time).total_seconds()
            if snap_age_s > 600:
                blog("WARNING", f"Regime snapshot is {snap_age_s / 60:.0f}m old — treating as unknown")
                regime_label = "unknown"
        except Exception:
            pass

    # ── 2-fv. Fair Value Model: capture BTC open ──
    if _fair_value_model and ticker != _fv_market_ticker:
        _fv_market_ticker = ticker
        _fv_btc_open = None
        try:
            btc_now = get_live_price("BTC")
            if btc_now and btc_now > 0:
                _fv_btc_open = btc_now
                _fv_last_btc_price = btc_now
                _fv_last_btc_fetch = time.time()
                blog("DEBUG", f"FV model: BTC open for {ticker} = ${btc_now:,.0f}")
        except Exception as e:
            blog("DEBUG", f"FV model: failed to capture BTC open: {e}")
            if snapshot and snapshot.get("btc_price"):
                _fv_btc_open = snapshot["btc_price"]
                _fv_last_btc_price = _fv_btc_open

    # ── 2a. Strategy key & risk ──
    _active_strategy_key = None
    _strategy_risk = None
    if _trading_mode in ("hybrid", "auto"):
        try:
            _peek_rec = get_recommendation(
                regime_label, now_et.hour,
                vol_regime=snapshot.get("vol_regime") if snapshot else None,
                trend_regime=snapshot.get("trend_regime") if snapshot else None,
            )
            if _peek_rec and _peek_rec.get("strategy_key"):
                _active_strategy_key = _peek_rec["strategy_key"]
        except Exception:
            pass
    if not _active_strategy_key:
        _active_strategy_key = build_strategy_key(cfg)

    try:
        _strategy_risk = get_strategy_risk(regime_label, _active_strategy_key)
        if (_strategy_risk and _strategy_risk.get("risk_level") == "unknown"
                and cfg.get("strategy_side") == "model"):
            _fallback_key = _active_strategy_key.replace("model:", "cheaper:", 1)
            _fb_risk = get_strategy_risk(regime_label, _fallback_key)
            if _fb_risk and _fb_risk.get("risk_level") != "unknown":
                _strategy_risk = _fb_risk
    except Exception:
        pass

    vol_level = snapshot.get("vol_regime") if snapshot else None
    gate = check_regime_gate(cfg, regime_label, strategy_risk=_strategy_risk,
                             coarse_regime=compute_coarse_label(
                                 snapshot.get("vol_regime", 3) if snapshot else 3,
                                 snapshot.get("trend_regime", 0) if snapshot else 0,
                                 snapshot.get("volume_regime") if snapshot else None,
                             ))

    # Observe/shadow mode override
    if _trading_mode in ("observe", "shadow") and gate["should_trade"]:
        gate = {
            "should_trade": False, "is_data_collection": False,
            "reason": "Observe-only mode" if _trading_mode == "observe" else "Shadow mode",
            "risk_level": gate["risk_level"], "strategy_risk": gate.get("strategy_risk"),
        }

    blog("INFO", f"Regime: {gate['reason']}")

    # ── 2b. Per-regime condition filters ──
    _regime_filters = cfg.get("regime_filters", {})
    if isinstance(_regime_filters, str):
        _regime_filters = json.loads(_regime_filters)
    _is_quick_trade = "quick-trade" in gate.get("reason", "") or "trade-all" in gate.get("reason", "")

    if gate["should_trade"] and not _is_quick_trade:
        rf = _get_regime_filter(regime_label, _regime_filters)
        if rf:
            skip_reason = None
            vol_min = rf.get("vol_min", 1)
            vol_max = rf.get("vol_max", 5)
            if vol_level is not None and (vol_level < vol_min or vol_level > vol_max):
                skip_reason = f"Vol {vol_level}/5 outside {vol_min}-{vol_max} for {regime_label}"
            blocked_hours = rf.get("blocked_hours", [])
            if blocked_hours and now_et.hour in blocked_hours:
                skip_reason = f"Hour {now_et.hour} ET blocked for {regime_label}"
            blocked_days = rf.get("blocked_days", [])
            if blocked_days and now_et.weekday() in blocked_days:
                day_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
                skip_reason = f"{day_names[now_et.weekday()]} blocked for {regime_label}"

            if skip_reason:
                gate = {
                    "should_trade": False, "is_data_collection": False,
                    "reason": skip_reason, "risk_level": gate["risk_level"],
                    "strategy_risk": gate.get("strategy_risk"),
                }
                blog("INFO", f"Regime filter: {skip_reason}")

    # ── 2c. Enrichment fields ──
    trend_level = snapshot.get("trend_regime", 0) if snapshot else 0
    volume_level = snapshot.get("volume_regime", 3) if snapshot else 3
    coarse_regime = compute_coarse_label(vol_level or 3, trend_level, volume_level)
    prev_regime = None  # No prev_regime tracking in plugin (simplified)
    trade_hour_et = now_et.hour
    trade_day_of_week = now_et.weekday()

    _ctx = _build_trade_context(
        client, cfg, state, market, snapshot, gate,
        coarse_regime, prev_regime, trade_hour_et, trade_day_of_week,
        vol_level=vol_level, close_str=close_str
    )
    _ctx["auto_strategy_key"] = _active_strategy_key
    _ctx["auto_strategy_setup"] = "manual"

    # ── 2d. Auto-strategy lookup ──
    auto_strat = None
    if gate["should_trade"] and _trading_mode in ("hybrid", "auto"):
        _as_min_n = int(cfg.get("auto_strategy_min_samples", 20))
        _as_min_ev = float(cfg.get("auto_strategy_min_ev_c", 0))
        _as_rec = None
        _as_rejection = {}
        try:
            _as_rec = get_recommendation(
                regime_label, now_et.hour,
                vol_regime=snapshot.get("vol_regime") if snapshot else None,
                trend_regime=snapshot.get("trend_regime") if snapshot else None,
                rejection_info=_as_rejection,
            )
        except Exception as e:
            blog("DEBUG", f"Auto-strategy lookup error: {e}")

        if _as_rec and _as_rec.get("ev_per_trade_c") is not None:
            _as_ev = _as_rec["ev_per_trade_c"]
            _as_n = _as_rec["sample_size"]
            _as_valid = _as_ev >= _as_min_ev and _as_n >= _as_min_n
        else:
            _as_ev = None
            _as_n = 0
            _as_valid = False

        if _as_valid:
            auto_strat = _as_rec
            _ctx["auto_strategy_key"] = _as_rec["strategy_key"]
            _ctx["auto_strategy_setup"] = _as_rec["setup_key"]
            _ctx["auto_strategy_ev_c"] = _as_rec["ev_per_trade_c"]
            _strat_label = (f"{_as_rec['side_rule'].upper()} "
                           f"{'hold' if _as_rec['sell_target'] == 'hold' else 'sell@' + str(_as_rec['sell_target']) + '¢'} "
                           f"{_as_rec['entry_time_rule']} ≤{_as_rec['entry_price_max']}¢")
            blog("INFO", f"Auto-strategy: {_strat_label} "
                         f"(EV {_as_ev:+.1f}¢, n={_as_n}, WR={_as_rec['win_rate']:.0%})")
        else:
            _rej_short = _as_rejection.get("short", "")
            if _as_rec and _as_ev is not None:
                _as_short = f"EV too low ({_as_ev:+.1f}¢)" if _as_ev < _as_min_ev else f"n={_as_n} < {_as_min_n} min"
                _skip_r = f"Auto-strategy: {_as_short}"
            elif _rej_short:
                _as_short = _rej_short
                _skip_r = f"Auto-strategy: {_rej_short}"
            elif _regime_obs_n > 0:
                _as_short = f"n={_regime_obs_n}, fails validation"
                _skip_r = f"Auto-strategy: {_regime_obs_n} obs but no strategy passes validation"
            else:
                _as_short = "no observations yet"
                _skip_r = f"Auto-strategy: no observations for {regime_label.replace('_', ' ')} yet"

            gate = {
                "should_trade": False, "is_data_collection": False,
                "reason": _skip_r, "risk_level": gate["risk_level"],
                "strategy_risk": gate.get("strategy_risk"),
                "_auto_skip_short": _as_short,
            }
            blog("INFO", _skip_r)

    # ── SKIP PATH ──
    if not gate["should_trade"]:
        initial_skip_side = "n/a"
        initial_skip_price = None
        try:
            skip_market = client.get_market(ticker)
            _cs, _cp = client.get_cheaper_side(skip_market)
            if _cp > 0:
                initial_skip_side = _cs
                initial_skip_price = _cp
        except Exception:
            pass

        skip_trade_id = insert_trade({
            **_ctx,
            "market_id": market_id, "regime_snapshot_id": snapshot_id,
            "ticker": ticker, "side": initial_skip_side,
            "avg_fill_price_c": initial_skip_price,
            "outcome": "skipped", "skip_reason": gate["reason"],
            "cheaper_side": initial_skip_side if initial_skip_side != "n/a" else _ctx.get("cheaper_side"),
            "cheaper_side_price_c": initial_skip_price or _ctx.get("cheaper_side_price_c"),
        })
        notify_observed(regime_label, gate["reason"])
        if _observer:
            _observer.mark_action("observed", skip_trade_id,
                                  market_id=market_id,
                                  strategy_key=_active_strategy_key, regime_label=regime_label)

        secs_to_close = (close_dt - datetime.now(timezone.utc)).total_seconds()
        risk_display = 'extreme' if gate['risk_level'] == 'terrible' else gate['risk_level']
        _as_short = gate.get("_auto_skip_short", "")
        if _as_short:
            _skip_short = _as_short
        elif "Observe-only" in gate["reason"]:
            _skip_short = ""
        elif "— skipping" in gate["reason"]:
            _skip_short = f"{risk_display} risk — skip"
        else:
            _skip_short = gate["reason"][:40]

        s = _get_state()
        _update_state({
            "session_skips": (s.get("session_skips") or 0) + 1,
            "active_skip": {
                "reason": gate["reason"], "skip_short": _skip_short,
                "regime_label": regime_label, "risk_level": gate["risk_level"],
                "ticker": ticker, "close_time": close_dt.isoformat(),
                "trade_id": skip_trade_id, "regime_obs_n": _regime_obs_n,
            },
        })
        _update_status("waiting",
                       f"Observing {regime_label.replace('_', ' ')} — next in ~{_fmt_wait(secs_to_close)}{_trade_ctx()}")

        # Shadow trading
        if _trading_mode in ("shadow", "hybrid"):
            try:
                _shadow_market = client.get_market(ticker)
                _sh_ctx = {
                    "spread_at_entry_c": abs((_shadow_market.get("yes_ask") or 0) - (_shadow_market.get("no_ask") or 0)),
                    "yes_ask_at_entry": _shadow_market.get("yes_ask"),
                    "no_ask_at_entry": _shadow_market.get("no_ask"),
                    "yes_bid_at_entry": _shadow_market.get("yes_bid"),
                    "no_bid_at_entry": _shadow_market.get("no_bid"),
                }
                _sh_rec = _get_shadow_strategy(
                    regime_label, hour_et=now_et.hour,
                    vol_regime=snapshot.get("vol_regime") if snapshot else None,
                    trend_regime=snapshot.get("trend_regime") if snapshot else None,
                )
                _sh_strat_key = None
                if _sh_rec:
                    _sh_side_rule = _sh_rec["side_rule"]
                    _sh_strat_key = _sh_rec["strategy_key"]
                    if _sh_side_rule in ("yes", "no"):
                        _sh_side = _sh_side_rule
                        _sh_price = _shadow_market.get(f"{_sh_side}_ask") or 0
                    elif _sh_side_rule == "model":
                        _sh_side, _sh_price = client.get_cheaper_side(_shadow_market)
                        if _fair_value_model and _fv_btc_open and _fv_btc_open > 0:
                            try:
                                _sh_btc = _fv_last_btc_price or _fv_btc_open
                                if _sh_btc and _sh_btc > 0:
                                    _sh_dist = (_sh_btc - _fv_btc_open) / _fv_btc_open * 100
                                    _sh_edge = _fair_value_model.compute_edge(
                                        yes_ask_c=_shadow_market.get("yes_ask") or 0,
                                        no_ask_c=_shadow_market.get("no_ask") or 0,
                                        btc_distance_pct=_sh_dist,
                                        seconds_into_market=0,
                                        vol_regime=snapshot.get("vol_regime") if snapshot else None,
                                    )
                                    if _sh_edge.get("recommended_side"):
                                        _sh_side = _sh_edge["recommended_side"]
                                        _sh_price = _shadow_market.get(f"{_sh_side}_ask") or 0
                            except Exception:
                                pass
                    else:
                        _sh_side, _sh_price = client.get_cheaper_side(_shadow_market)
                else:
                    _sh_side, _sh_price = client.get_cheaper_side(_shadow_market)

                _shadow_id = _place_shadow_trade(
                    client, ticker, _sh_side, _sh_price,
                    market_id=market_id, regime_label=regime_label,
                    snapshot_id=snapshot_id, ctx=_sh_ctx,
                    strategy_key=_sh_strat_key,
                )
                if _shadow_id:
                    try:
                        delete_trades([skip_trade_id])
                    except Exception:
                        pass
                    _update_state({
                        "active_shadow": {
                            "trade_id": _shadow_id, "side": _sh_side,
                            "price_c": _sh_price, "strategy_key": _sh_strat_key,
                        },
                    })
            except Exception as _she:
                blog("DEBUG", f"Shadow trade error: {_she}")

        _skip_wait_loop(
            client, cfg, close_dt, skip_trade_id, ticker,
            regime_label, gate["risk_level"], gate["reason"],
            track_side=True, resolve_inline=True,
            initial_cheaper_side=initial_skip_side, market_id=market_id,
        )
        _update_state({"active_shadow": None, "active_skip": None})
        return False

    # ── 2e. Strategy overrides ──
    _auto_side_rule = None
    _auto_sell_target = None
    _auto_strat_label = None
    if auto_strat:
        _auto_side_rule = auto_strat["side_rule"]
        _auto_sell_target = auto_strat["sell_target"]
        _sell_label = "hold" if _auto_sell_target == "hold" else f"sell@{_auto_sell_target}¢"
        _auto_strat_label = (f"{auto_strat['side_rule'].upper()} "
                            f"{_sell_label} "
                            f"{auto_strat['entry_time_rule']} "
                            f"≤{auto_strat['entry_price_max']}¢")
    else:
        manual_side = cfg.get("strategy_side", "cheaper")
        if manual_side and manual_side != "cheaper":
            _auto_side_rule = manual_side

    # ── 3. Entry delay + Poll for entry price ─────────────────
    entry_delay = cfg.get("entry_delay_minutes", 0)
    if auto_strat:
        _time_rule = auto_strat["entry_time_rule"]
        if _time_rule == "late":
            entry_delay = 10
        elif _time_rule == "mid":
            entry_delay = 5
        else:
            entry_delay = 0

    if entry_delay > 0:
        target_mins_left = 15 - entry_delay
        current_mins = client.minutes_until_close(close_str)
        wait_secs = max(0, (current_mins - target_mins_left) * 60)
        if wait_secs > 10:
            blog("INFO", f"Entry delay: waiting {wait_secs:.0f}s ({entry_delay}min into market)")
            delay_deadline = time.monotonic() + wait_secs
            while time.monotonic() < delay_deadline:
                rem = delay_deadline - time.monotonic()
                _update_status("waiting", f"Delaying entry: {_fmt_wait(rem)}{_trade_ctx()}")
                for cmd in get_pending_commands(PLUGIN_ID):
                    cmd_type = cmd["command_type"]
                    cmd_id = cmd["id"]
                    if cmd_type == "stop":
                        _update_status("stopped", "Stopped",
                                       {"auto_trading": 0, "trades_remaining": 0})
                        complete_command(cmd_id)
                        return False
                    elif cmd_type == "update_config":
                        params = json.loads(cmd.get("parameters") or "{}")
                        for k, v in params.items():
                            set_config(f"{PLUGIN_ID}.{k}", v)
                            if k in cfg:
                                cfg[k] = v
                        complete_command(cmd_id)
                    else:
                        complete_command(cmd_id)
                try:
                    poll_live_market(client, cfg)
                except Exception:
                    pass
                time.sleep(min(2, max(0, rem)))

    max_entry_c = cfg.get("entry_price_max_c", 42)
    min_entry_c = 1
    poll_interval = cfg.get("price_poll_interval", 2)
    fill_wait = 600
    min_mins = 0.5

    if auto_strat:
        max_entry_c = auto_strat["entry_price_max"]
        if _auto_side_rule in ("yes", "no", "model"):
            min_entry_c = max(min_entry_c, 1)

    _now_wall = time.time()
    _now_mono = time.monotonic()
    _close_wall = close_dt.timestamp()
    _mono_close = _now_mono + (_close_wall - _now_wall)

    price_deadline = min(
        _now_mono + fill_wait,
        _mono_close - (min_mins * 60),
    )

    market_label = marketStartTime(close_str)
    if auto_strat:
        _update_status("searching", f"Auto: {_auto_strat_label} — watching {market_label}{_trade_ctx()}")
    elif _auto_side_rule == "model":
        _update_status("searching", f"Model: scanning {market_label}{_trade_ctx()}")
    else:
        _update_status("searching", f"Watching {market_label} — price ≤ {max_entry_c}c{_trade_ctx()}")

    side_info = None
    stopped_early = False
    skip_reason = None
    poll_prices_seen = []
    poll_sides_seen = []
    _entry_model_edge = None
    _entry_model_ev = None
    _entry_model_source = None

    while time.monotonic() < price_deadline:
        for cmd in get_pending_commands(PLUGIN_ID):
            cmd_type = cmd["command_type"]
            cmd_id = cmd["id"]
            if cmd_type == "stop":
                _update_status("stopped", "Stopped",
                               {"auto_trading": 0, "trades_remaining": 0})
                complete_command(cmd_id)
                blog("INFO", "Stop received during price polling")
                stopped_early = True
                break
            elif cmd_type == "update_config":
                params = json.loads(cmd.get("parameters") or "{}")
                for k, v in params.items():
                    set_config(f"{PLUGIN_ID}.{k}", v)
                    if k in cfg:
                        cfg[k] = v
                complete_command(cmd_id)
            else:
                pass

        if stopped_early:
            break

        try:
            m = client.get_market(ticker)
        except Exception:
            time.sleep(poll_interval)
            continue

        side, price_c = client.get_cheaper_side(m)

        # Auto-strategy side override
        if _auto_side_rule:
            if _auto_side_rule in ("yes", "no"):
                side = _auto_side_rule
                price_c = m.get(f"{side}_ask") or 0
            elif _auto_side_rule == "model":
                _model_side = None
                _model_edge_val = 0
                _model_ev_val = 0
                if _fair_value_model and _fv_btc_open and _fv_btc_open > 0:
                    try:
                        _now_m = time.time()
                        if _now_m - _fv_last_btc_fetch >= 10:
                            _btc_m = get_live_price("BTC")
                            if _btc_m and _btc_m > 0:
                                _fv_last_btc_price = _btc_m
                                _fv_last_btc_fetch = _now_m
                        if _fv_last_btc_price and _fv_last_btc_price > 0:
                            _m_dist = (_fv_last_btc_price - _fv_btc_open) / _fv_btc_open * 100
                            _m_secs = max(0, 900 - client.minutes_until_close(close_str) * 60)
                            _m_rvol = snapshot.get("realized_vol_15m") if snapshot else None
                            _m_edge = _fair_value_model.compute_edge(
                                yes_ask_c=m.get("yes_ask") or 0,
                                no_ask_c=m.get("no_ask") or 0,
                                btc_distance_pct=_m_dist,
                                seconds_into_market=_m_secs,
                                realized_vol=_m_rvol,
                                vol_regime=snapshot.get("vol_regime") if snapshot else None,
                            )
                            _min_me = float(cfg.get("min_model_edge_pct", 3.0))
                            if _m_edge["recommended_side"] and _m_edge["best_edge_pct"] >= _min_me:
                                _model_side = _m_edge["recommended_side"]
                                _model_edge_val = _m_edge["best_edge_pct"]
                                _model_ev_val = _m_edge.get(f"{_model_side}_ev_c") or 0
                    except Exception:
                        pass
                if _model_side:
                    side = _model_side
                    price_c = m.get(f"{side}_ask") or 0
                    _entry_model_edge = _model_edge_val
                    _entry_model_ev = _model_ev_val
                    try:
                        _entry_model_source = _m_edge["model"]["source"]
                    except Exception:
                        _entry_model_source = "unknown"
                else:
                    price_c = 0
                    _entry_model_edge = None
                    _entry_model_ev = None
                    _entry_model_source = None

        if price_c > 0:
            poll_prices_seen.append(price_c)
            if price_c <= max_entry_c:
                poll_sides_seen.append(side)

        # Update live market for dashboard
        cur_stab = (max(poll_prices_seen) - min(poll_prices_seen)) if len(poll_prices_seen) >= 2 else None
        _live_market_data = {
            "ticker": ticker, "close_time": close_str,
            "minutes_left": round(client.minutes_until_close(close_str), 1),
            "cheaper_side": side, "cheaper_price_c": price_c,
            "yes_ask": m.get("yes_ask"), "no_ask": m.get("no_ask"),
            "yes_bid": m.get("yes_bid"), "no_bid": m.get("no_bid"),
            "regime_label": regime_label, "risk_level": gate["risk_level"],
            "regime_win_rate": _strategy_risk.get("win_rate", 0) if _strategy_risk else 0,
            "regime_trades": _strategy_risk.get("sample_size", 0) if _strategy_risk else 0,
            "btc_price": snapshot.get("btc_price") if snapshot else None,
            "vol_regime": snapshot.get("vol_regime") if snapshot else None,
            "trend_regime": snapshot.get("trend_regime") if snapshot else None,
            "volume_regime": snapshot.get("volume_regime") if snapshot else None,
            "stability_c": cur_stab, "regime_obs_n": _regime_obs_n,
        }
        if auto_strat:
            _live_market_data["auto_strategy"] = _auto_strat_label
            _live_market_data["auto_strategy_ev"] = auto_strat["ev_per_trade_c"]
        _live_market_data["strategy_key"] = _active_strategy_key

        # FV model display
        if _fair_value_model and _fv_btc_open and _fv_btc_open > 0:
            try:
                _now_fv = time.time()
                if _now_fv - _fv_last_btc_fetch >= 10:
                    _btc_fresh = get_live_price("BTC")
                    if _btc_fresh and _btc_fresh > 0:
                        _fv_last_btc_price = _btc_fresh
                        _fv_last_btc_fetch = _now_fv
                if _fv_last_btc_price and _fv_last_btc_price > 0:
                    _fv_dist = (_fv_last_btc_price - _fv_btc_open) / _fv_btc_open * 100
                    _fv_secs = max(0, 900 - client.minutes_until_close(close_str) * 60)
                    _fv_rvol = snapshot.get("realized_vol_15m") if snapshot else None
                    _fv_edge = _fair_value_model.compute_edge(
                        yes_ask_c=m.get("yes_ask") or 0, no_ask_c=m.get("no_ask") or 0,
                        btc_distance_pct=_fv_dist, seconds_into_market=_fv_secs,
                        realized_vol=_fv_rvol,
                        vol_regime=snapshot.get("vol_regime") if snapshot else None,
                    )
                    _live_market_data["fv_model"] = {
                        "btc_open": round(_fv_btc_open, 0),
                        "btc_now": round(_fv_last_btc_price, 0),
                        "btc_distance_pct": round(_fv_dist, 4),
                        "fair_yes_c": _fv_edge["model"]["fair_yes_c"],
                        "fair_no_c": _fv_edge["model"]["fair_no_c"],
                        "yes_edge_pct": _fv_edge["yes_edge_pct"],
                        "no_edge_pct": _fv_edge["no_edge_pct"],
                        "yes_ev_c": _fv_edge["yes_ev_c"],
                        "no_ev_c": _fv_edge["no_ev_c"],
                        "recommended_side": _fv_edge["recommended_side"],
                        "best_edge_pct": _fv_edge["best_edge_pct"],
                        "source": _fv_edge["model"]["source"],
                        "confidence": _fv_edge["model"]["confidence"],
                    }
            except Exception:
                pass

        _update_state({"live_market": _live_market_data})

        # Feed Observatory
        if _observer:
            _obs_data = {
                "yes_ask": m.get("yes_ask"), "no_ask": m.get("no_ask"),
                "yes_bid": m.get("yes_bid"), "no_bid": m.get("no_bid"),
                "btc_price": snapshot.get("btc_price") if snapshot else None,
                "volume": m.get("volume"), "open_interest": m.get("open_interest"),
            }
            _observer.tick(ticker, close_str, _obs_data, snapshot, _strategy_risk)

        if price_c > 0 and price_c >= min_entry_c and price_c <= max_entry_c:
            mins_now = client.minutes_until_close(close_str)
            if mins_now >= min_mins:
                side_info = (side, price_c)
                break

        if price_c > 0 and price_c < min_entry_c:
            skip_reason = f"Price {price_c}c below minimum {min_entry_c}c — too decided"
            blog("INFO", skip_reason)
            break

        if client.minutes_until_close(close_str) < min_mins:
            skip_reason = "Too close to market close"
            blog("INFO", skip_reason)
            break

        time.sleep(poll_interval)

    if stopped_early:
        return False

    if not side_info:
        if skip_reason:
            reason = skip_reason
        elif _auto_side_rule == "model":
            reason = f"Model found no edge ≥{float(cfg.get('min_model_edge_pct', 3.0)):.0f}%"
        else:
            reason = "Price never reached entry range"

        skip_side = "n/a"
        skip_avg_price = None
        if poll_sides_seen:
            skip_side = Counter(poll_sides_seen).most_common(1)[0][0]
            in_range = [p for p in poll_prices_seen if p <= max_entry_c]
            skip_avg_price = round(sum(in_range) / len(in_range)) if in_range else None

        price_skip_id = insert_trade({
            **_ctx, "market_id": market_id, "regime_snapshot_id": snapshot_id,
            "ticker": ticker, "side": skip_side, "avg_fill_price_c": skip_avg_price,
            "outcome": "skipped", "skip_reason": reason,
            "price_stability_c": (max(poll_prices_seen) - min(poll_prices_seen)) if len(poll_prices_seen) >= 2 else None,
            "num_price_samples": len(poll_prices_seen),
        })
        notify_observed(regime_label, reason)
        if _observer:
            _observer.mark_action("observed", price_skip_id, market_id=market_id,
                                  strategy_key=_active_strategy_key, regime_label=regime_label)
        s = _get_state()
        secs_to_close = (close_dt - datetime.now(timezone.utc)).total_seconds()
        _update_state({
            "session_skips": (s.get("session_skips") or 0) + 1,
            "active_skip": {
                "reason": reason, "skip_short": reason,
                "regime_label": regime_label, "risk_level": gate.get("risk_level", "unknown"),
                "ticker": ticker, "close_time": close_dt.isoformat(),
                "trade_id": price_skip_id, "regime_obs_n": _regime_obs_n,
            },
        })
        _update_status("waiting", f"Observing {regime_label.replace('_', ' ')} — next in ~{_fmt_wait(secs_to_close)}{_trade_ctx()}")

        _skip_wait_loop(
            client, cfg, close_dt, price_skip_id, ticker,
            regime_label, gate.get("risk_level", "unknown"), reason,
            resolve_inline=True, initial_cheaper_side=skip_side, market_id=market_id,
        )
        return False

    side, entry_price_c = side_info

    # Capture orderbook from last poll
    polled_ya = polled_na = polled_yb = polled_nb = None
    try:
        polled_ya = m.get("yes_ask") or 0
        polled_yb = m.get("yes_bid") or 0
        polled_na = m.get("no_ask") or 0
        polled_nb = m.get("no_bid") or 0
    except Exception:
        pass

    # ── Side filter ──
    rf_side = _get_regime_filter(regime_label, _regime_filters)
    blocked_sides = rf_side.get("blocked_sides", [])
    if not _is_quick_trade and blocked_sides and side in blocked_sides:
        reason = f"{side.upper()} side blocked for {regime_label}"
        blog("INFO", f"Regime filter: {reason}")
        side_skip_id = insert_trade({
            **_ctx, "market_id": market_id, "regime_snapshot_id": snapshot_id,
            "ticker": ticker, "side": side, "avg_fill_price_c": entry_price_c,
            "outcome": "skipped", "skip_reason": reason,
            "yes_ask_at_entry": polled_ya, "no_ask_at_entry": polled_na,
            "yes_bid_at_entry": polled_yb, "no_bid_at_entry": polled_nb,
        })
        if _observer:
            _observer.mark_action("observed", side_skip_id, market_id=market_id,
                                  strategy_key=_active_strategy_key, regime_label=regime_label)
        _skip_wait_loop(client, cfg, close_dt, side_skip_id, ticker,
                        regime_label, gate.get("risk_level", "unknown"), reason,
                        resolve_inline=True, market_id=market_id)
        return False

    # ── Spread filter ──
    try:
        if side == "yes":
            spread_at_entry_c = max(0, polled_ya - polled_yb) if polled_ya and polled_yb else None
        else:
            spread_at_entry_c = max(0, polled_na - polled_nb) if polled_na and polled_nb else None
    except Exception:
        spread_at_entry_c = None

    spread_regime_label = score_spread(spread_at_entry_c)
    rf_spread = _get_regime_filter(regime_label, _regime_filters)
    max_spread = rf_spread.get("max_spread_c", 0)
    if not _is_quick_trade and max_spread > 0 and spread_at_entry_c is not None and spread_at_entry_c > max_spread:
        reason = f"Spread {spread_at_entry_c}c > {max_spread}c max for {regime_label}"
        blog("INFO", f"Regime filter: {reason}")
        spread_skip_id = insert_trade({
            **_ctx, "market_id": market_id, "regime_snapshot_id": snapshot_id,
            "ticker": ticker, "side": side, "avg_fill_price_c": entry_price_c,
            "spread_at_entry_c": spread_at_entry_c, "spread_regime": spread_regime_label,
            "outcome": "skipped", "skip_reason": reason,
        })
        if _observer:
            _observer.mark_action("observed", spread_skip_id, market_id=market_id,
                                  strategy_key=_active_strategy_key, regime_label=regime_label)
        _skip_wait_loop(client, cfg, close_dt, spread_skip_id, ticker,
                        regime_label, gate.get("risk_level", "unknown"), reason,
                        resolve_inline=True, market_id=market_id)
        return False

    # ── 4. Bet sizing ──
    is_ignored = bool(cfg.get("ignore_mode", False))
    bankroll_c = get_effective_bankroll_cents(client, cfg)
    _update_state({"bankroll_cents": client.get_balance_cents()})

    bet_dollars = get_r1_bet_dollars(cfg, bankroll_c / 100)
    blog("INFO", f"Bet: ${bet_dollars:.2f} ({cfg.get('bet_mode', 'flat')})")
    shares = client.calc_shares_for_dollars(bet_dollars, entry_price_c)
    est_cost = shares * entry_price_c / 100
    est_fees = client.estimate_fees(shares, entry_price_c)

    stability_c = (max(poll_prices_seen) - min(poll_prices_seen)) if len(poll_prices_seen) >= 2 else None

    # Stability filter
    rf = _get_regime_filter(regime_label, _regime_filters)
    stab_max = rf.get("stability_max", 0) if rf else 0
    if not _is_quick_trade and stab_max > 0 and stability_c is not None and stability_c > stab_max:
        reason = f"Stability {stability_c}c > {stab_max}c max for {regime_label}"
        blog("INFO", f"Regime filter: {reason}")
        stab_skip_id = insert_trade({
            **_ctx, "market_id": market_id, "regime_snapshot_id": snapshot_id,
            "ticker": ticker, "side": side,
            "outcome": "skipped", "skip_reason": reason,
            "price_stability_c": stability_c,
        })
        if _observer:
            _observer.mark_action("observed", stab_skip_id, market_id=market_id,
                                  strategy_key=_active_strategy_key, regime_label=regime_label)
        _skip_wait_loop(client, cfg, close_dt, stab_skip_id, ticker,
                        regime_label, gate.get("risk_level", "unknown"), reason,
                        resolve_inline=True, market_id=market_id)
        return False

    # Edge-scaled sizing
    if cfg.get("bet_mode") == "edge_scaled":
        _sizing_edge = _entry_model_edge
        if _sizing_edge is None and _fair_value_model and _fv_btc_open and _fv_btc_open > 0:
            try:
                _btc_now = _fv_last_btc_price or _fv_btc_open
                if _btc_now and _btc_now > 0:
                    _s_dist = (_btc_now - _fv_btc_open) / _fv_btc_open * 100
                    _s_secs = max(0, 900 - client.minutes_until_close(close_str) * 60)
                    _s_edge = _fair_value_model.compute_edge(
                        yes_ask_c=polled_ya or 0, no_ask_c=polled_na or 0,
                        btc_distance_pct=_s_dist, seconds_into_market=_s_secs,
                        vol_regime=snapshot.get("vol_regime") if snapshot else None,
                    )
                    _sizing_edge = _s_edge.get(f"{side}_edge_pct")
                    if _sizing_edge is not None and _sizing_edge < 0:
                        _sizing_edge = 0
            except Exception:
                _sizing_edge = None
        if _sizing_edge is not None and _sizing_edge > 0:
            bet_dollars = get_r1_bet_dollars(cfg, bankroll_c / 100, edge_pct=_sizing_edge)
            shares = client.calc_shares_for_dollars(bet_dollars, entry_price_c)
            est_cost = shares * entry_price_c / 100
            est_fees = client.estimate_fees(shares, entry_price_c)
            blog("INFO", f"Edge-scaled: ${bet_dollars:.2f} ({shares} shares) for edge +{_sizing_edge:.1f}%")

    # Bankroll safety
    safe, reason = check_balance_safety(client, cfg, bet_dollars, entry_price_c)
    if not safe:
        safety_skip_id = insert_trade({
            **_ctx, "market_id": market_id, "regime_snapshot_id": snapshot_id,
            "ticker": ticker, "side": side,
            "outcome": "skipped", "skip_reason": reason,
            "bet_size_dollars": bet_dollars,
        })
        if _observer:
            _observer.mark_action("observed", safety_skip_id, market_id=market_id,
                                  strategy_key=_active_strategy_key, regime_label=regime_label)
        _skip_wait_loop(client, cfg, close_dt, safety_skip_id, ticker,
                        regime_label, gate.get("risk_level", "unknown"), reason,
                        resolve_inline=True, market_id=market_id)
        return False

    blog("INFO", f"Plan: {shares} {side.upper()} @ {entry_price_c}c (~${est_cost:.2f} + ~${est_fees:.2f} fees)")

    # ── 5. Place buy order(s) — adaptive entry ──
    buy_start_time = time.monotonic()
    _update_status("trading", f"Buying {shares} {side.upper()} @ {entry_price_c}c{_trade_ctx()}")

    fill_deadline = min(
        time.time() + fill_wait,
        close_dt.timestamp() - (min_mins * 60),
    )

    target_shares = shares
    total_filled = 0
    total_cost_cents = 0
    total_fees_cents = 0
    all_order_ids = []
    buy_price_c = entry_price_c
    buy_attempt = 0
    _buy_error = None

    adaptive = bool(cfg.get("adaptive_entry", False))
    if adaptive and spread_at_entry_c and spread_at_entry_c >= 4:
        buy_price_c = max(2, entry_price_c - 2)
        blog("INFO", f"Adaptive entry: starting at {buy_price_c}c (ask={entry_price_c}c, spread={spread_at_entry_c}c)")

    while total_filled < target_shares and time.time() < fill_deadline:
        remaining_shares = target_shares - total_filled
        buy_attempt += 1

        try:
            buy_resp = client.place_limit_order(
                ticker, side, remaining_shares, buy_price_c, action="buy"
            )
            buy_order = buy_resp.get("order", {})
            order_id = buy_order.get("order_id")
        except Exception as e:
            blog("ERROR", f"Buy attempt {buy_attempt} failed: {e}")
            _buy_error = str(e)
            break

        if not order_id:
            blog("ERROR", f"No order_id on attempt {buy_attempt}")
            _buy_error = "No order_id returned"
            break

        all_order_ids.append(order_id)
        status = buy_order.get("status", "")

        if status == "executed":
            fill = client.parse_fill(buy_order)
        elif status == "resting":
            attempt_deadline = fill_deadline
            if adaptive and buy_attempt == 1 and buy_price_c < entry_price_c:
                attempt_deadline = min(fill_deadline, time.time() + 20)
            _poll_int = cfg.get("order_poll_interval", 3)
            fill = None
            while time.time() < attempt_deadline:
                time.sleep(_poll_int)
                _elapsed = int(time.monotonic() - buy_start_time)
                _update_status("trading",
                               f"Buying {remaining_shares} {side.upper()} @ {buy_price_c}c (filling… {_elapsed}s){_trade_ctx()}")
                try:
                    _fm = client.get_market(ticker)
                    if _observer:
                        _observer.tick(ticker, close_str, {
                            "yes_ask": _fm.get("yes_ask"), "no_ask": _fm.get("no_ask"),
                            "yes_bid": _fm.get("yes_bid"), "no_bid": _fm.get("no_bid"),
                            "btc_price": snapshot.get("btc_price") if snapshot else None,
                            "volume": _fm.get("volume"), "open_interest": _fm.get("open_interest"),
                        }, snapshot, _strategy_risk)
                except Exception:
                    pass
                _order = client.get_order(order_id)
                _st = _order.get("status", "")
                if _st in ("executed", "canceled", "expired"):
                    fill = client.parse_fill(_order)
                    break
            if fill is None:
                fill = client.parse_fill(client.get_order(order_id))
        else:
            blog("WARNING", f"Unexpected buy status: {status}")
            _buy_error = f"Unexpected order status: {status}"
            break

        filled_now = fill["fill_count"]
        total_filled += filled_now
        if filled_now > 0:
            total_cost_cents += fill["contract_cost_cents"]
            total_fees_cents += int(fill["fees_dollars"] * 100)

        if total_filled < target_shares and time.time() < fill_deadline:
            try:
                client.cancel_order(order_id)
            except Exception:
                pass
            try:
                m = client.get_market(ticker)
                new_price = m.get(f"{side}_ask", 0) or 0
                if new_price >= min_entry_c and new_price <= max_entry_c:
                    buy_price_c = new_price
                else:
                    _other = "no" if side == "yes" else "yes"
                    _other_price = m.get(f"{_other}_ask", 0) or 0
                    if _other_price >= min_entry_c and _other_price <= max_entry_c:
                        side = _other
                        buy_price_c = _other_price
                    else:
                        _found = False
                        while time.time() < fill_deadline:
                            time.sleep(poll_interval)
                            try:
                                _rm = client.get_market(ticker)
                                if _observer:
                                    _observer.tick(ticker, close_str, {
                                        "yes_ask": _rm.get("yes_ask"), "no_ask": _rm.get("no_ask"),
                                        "yes_bid": _rm.get("yes_bid"), "no_bid": _rm.get("no_bid"),
                                        "btc_price": snapshot.get("btc_price") if snapshot else None,
                                        "volume": _rm.get("volume"), "open_interest": _rm.get("open_interest"),
                                    }, snapshot, _strategy_risk)
                                for _cs in ("yes", "no"):
                                    _cp = _rm.get(f"{_cs}_ask", 0) or 0
                                    if _cp >= min_entry_c and _cp <= max_entry_c:
                                        side = _cs
                                        buy_price_c = _cp
                                        _found = True
                                        break
                                if _found:
                                    break
                            except Exception:
                                continue
                        if not _found:
                            break
            except Exception:
                break
            time.sleep(1)

    if total_filled == 0:
        _is_error = _buy_error is not None
        if _is_error:
            blog("ERROR", f"Buy failed with error — {_buy_error}")
            _outcome = "error"
            _reason = f"Order error: {_buy_error[:200]}"
            notify_error(f"Buy failed: {_buy_error[:100]}")
        else:
            blog("INFO", "Buy did not fill — cancelling")
            _outcome = "no_fill"
            _reason = "No fill — order cancelled"
        for oid in all_order_ids:
            try:
                client.cancel_order(oid)
            except Exception:
                pass
        insert_trade({
            **_ctx, "market_id": market_id, "regime_snapshot_id": snapshot_id,
            "ticker": ticker, "side": side, "entry_price_c": entry_price_c,
            "outcome": _outcome, "buy_order_id": all_order_ids[0] if all_order_ids else None,
            "skip_reason": _reason, "bet_size_dollars": bet_dollars,
        })
        _update_status(detail=_reason)
        secs = (close_dt - datetime.now(timezone.utc)).total_seconds()
        if secs > 0:
            time.sleep(secs + 2)
        return False

    fill_count = total_filled
    actual_cost = (total_cost_cents + total_fees_cents) / 100
    avg_price_c = round(total_cost_cents / total_filled) if total_filled > 0 else buy_price_c
    fees_paid = total_fees_cents / 100
    buy_order_id = all_order_ids[0]

    blog("INFO", f"Filled: {fill_count} @ ~{avg_price_c}c | cost=${actual_cost:.2f} (fees=${fees_paid:.2f})")
    notify_buy(side, fill_count, avg_price_c, actual_cost, regime_label)

    # ── 7. Sell price ──
    is_hold_to_expiry = False
    sell_price_c = 99

    if _auto_sell_target is not None:
        if _auto_sell_target == "hold":
            is_hold_to_expiry = True
        else:
            sell_price_c = min(int(_auto_sell_target), 99)
    elif _auto_sell_target is None:
        manual_sell = int(cfg.get("sell_target_c", 0) or 0)
        if manual_sell > 0:
            sell_price_c = min(manual_sell, 99)
        else:
            is_hold_to_expiry = True

    if is_hold_to_expiry:
        sell_price_c = 99
        expected_gross = fill_count * 100 / 100
        expected_profit = expected_gross - actual_cost
    else:
        expected_gross = fill_count * sell_price_c / 100
        expected_profit = expected_gross - actual_cost

    # Dynamic sell init
    _dynamic_sell_active = False
    _dynamic_sell_adjustments = 0
    _dynamic_sell_initial = None
    _dynamic_sell_floor = int(cfg.get("dynamic_sell_floor_c", 3))
    if (bool(cfg.get("dynamic_sell_enabled", False))
            and _fair_value_model and _fv_btc_open and _fv_btc_open > 0
            and not is_hold_to_expiry):
        try:
            _fv_now = _fv_last_btc_price or get_live_price("BTC")
            if _fv_now and _fv_now > 0:
                _ds_dist = (_fv_now - _fv_btc_open) / _fv_btc_open * 100
                _ds_secs = max(0, 900 - client.minutes_until_close(close_str) * 60)
                _ds_rvol = snapshot.get("realized_vol_15m") if snapshot else None
                _ds_model = _fair_value_model.get_yes_probability(
                    _ds_dist, _ds_secs, _ds_rvol,
                    vol_regime=snapshot.get("vol_regime") if snapshot else None)
                _ds_fv = _ds_model["fair_yes_c"] if side == "yes" else _ds_model["fair_no_c"]
                _ds_target = max(int(_ds_fv) - 1, avg_price_c + 1)
                _ds_target = min(_ds_target, 99)
                if _ds_target != sell_price_c:
                    sell_price_c = _ds_target
                _dynamic_sell_active = True
                _dynamic_sell_initial = sell_price_c
                expected_gross = fill_count * sell_price_c / 100
                expected_profit = expected_gross - actual_cost
                blog("INFO", f"Dynamic sell enabled: initial target {sell_price_c}c (FV {_ds_fv:.1f}c)")
        except Exception:
            pass

    # ── 8. Place sell order ──
    sell_order_id = None
    if is_hold_to_expiry:
        blog("INFO", f"Holding {fill_count} {side.upper()} to expiry")
    else:
        try:
            sell_resp = client.place_limit_order(
                ticker, side, fill_count, sell_price_c, action="sell"
            )
            sell_order = sell_resp.get("order", {})
            sell_order_id = sell_order.get("order_id")
            blog("INFO", f"Sell placed: {fill_count}x {side} @ {sell_price_c}c")
        except Exception as e:
            blog("ERROR", f"Sell order failed: {e} — holding to close")

    # ── 9. Save trade + state ──
    btc_price = get_live_price("BTC")
    mins_at_entry = client.minutes_until_close(close_str)
    fill_duration_s = round(time.monotonic() - buy_start_time, 1)

    trade_id = insert_trade({
        **_ctx,
        "market_id": market_id, "regime_snapshot_id": snapshot_id,
        "ticker": ticker, "side": side,
        "entry_price_c": entry_price_c, "entry_time_utc": now_utc(),
        "minutes_before_close": round(mins_at_entry, 2),
        "shares_ordered": shares, "shares_filled": fill_count,
        "actual_cost": round(actual_cost, 2), "fees_paid": round(fees_paid, 2),
        "avg_fill_price_c": avg_price_c,
        "buy_order_id": buy_order_id,
        "sell_price_c": sell_price_c, "sell_order_id": sell_order_id,
        "outcome": "open", "is_data_collection": 0, "is_ignored": int(is_ignored),
        "price_stability_c": stability_c,
        "spread_at_entry_c": spread_at_entry_c, "spread_regime": spread_regime_label,
        "yes_ask_at_entry": polled_ya, "no_ask_at_entry": polled_na,
        "yes_bid_at_entry": polled_yb, "no_bid_at_entry": polled_nb,
        "bet_size_dollars": round(bet_dollars, 2),
        "fill_duration_seconds": fill_duration_s,
        "num_price_samples": len(poll_prices_seen),
        "model_edge_at_entry": _entry_model_edge,
        "model_ev_at_entry": _entry_model_ev,
        "model_source_at_entry": _entry_model_source,
    })

    active_trade = {
        "trade_id": trade_id, "ticker": ticker, "side": side,
        "fill_count": fill_count, "actual_cost": round(actual_cost, 2),
        "avg_price_c": avg_price_c, "sell_price_c": sell_price_c,
        "sell_order_id": sell_order_id, "buy_order_id": buy_order_id,
        "close_time": close_str, "entry_time": now_utc(),
        "regime_label": regime_label, "risk_level": gate["risk_level"],
        "regime_win_rate": _strategy_risk.get("win_rate", 0) if _strategy_risk else 0,
        "regime_trades": _strategy_risk.get("sample_size", 0) if _strategy_risk else 0,
        "regime_obs_n": _regime_obs_n,
        "vol_regime": snapshot.get("vol_regime") if snapshot else None,
        "btc_price": btc_price,
        "expected_profit": round(expected_profit, 2),
        "auto_strategy": _auto_strat_label,
        "auto_strategy_ev": auto_strat["ev_per_trade_c"] if auto_strat else None,
        "strategy_key": _active_strategy_key,
        "is_hold_to_expiry": is_hold_to_expiry,
        "model_edge": _entry_model_edge, "model_ev": _entry_model_ev,
        "dynamic_sell": _dynamic_sell_active,
        "dynamic_initial": _dynamic_sell_initial,
        "dynamic_adjustments": 0, "dynamic_fv": None,
    }

    _update_state({"active_trade": active_trade, "bankroll_cents": client.get_balance_cents()})
    _update_status("trading", f"{fill_count} {side.upper()} ~{avg_price_c}c → {'hold' if is_hold_to_expiry else f'sell@{sell_price_c}c'}")

    if _observer:
        _observer.mark_action("traded", trade_id, market_id=market_id,
                              strategy_key=_active_strategy_key, regime_label=regime_label)

    # ── 10. Monitor until close ──
    high_water_c = avg_price_c
    low_water_c = avg_price_c
    osc_count = 0
    last_direction = None
    secs_to_close = (close_dt - datetime.now(timezone.utc)).total_seconds()
    if secs_to_close > 0:
        blog("INFO", f"Monitoring for {secs_to_close:.0f}s...")
        end_time = time.monotonic() + secs_to_close + 2
        poll_s = cfg.get("price_poll_interval", 2)
        last_db_write = 0
        _last_trade_notify = 0

        while time.monotonic() < end_time:
            # Commands
            for cmd in get_pending_commands(PLUGIN_ID):
                cmd_type = cmd["command_type"]
                cmd_id = cmd["id"]
                params = json.loads(cmd.get("parameters") or "{}")

                if cmd_type == "stop":
                    _update_state({"auto_trading": 0, "trades_remaining": 0})
                    active_trade["is_ignored"] = True
                    _update_state({"active_trade": active_trade})
                    if trade_id:
                        update_trade(trade_id, {"is_ignored": 1, "notes": "Stopped mid-trade — ignored"})
                    complete_command(cmd_id)

                if cmd_type == "update_config":
                    for k, v in params.items():
                        set_config(f"{PLUGIN_ID}.{k}", v)
                        if k in cfg:
                            cfg[k] = v
                    complete_command(cmd_id)
                else:
                    pass

            sleep_secs = min(poll_s, end_time - time.monotonic())
            if sleep_secs > 0:
                time.sleep(sleep_secs)

            try:
                m = client.get_market(ticker)
                cur_bid = m.get(f"{side}_bid", 0) or 0
                cur_ask = m.get(f"{side}_ask", 0) or 0

                if cur_bid > high_water_c:
                    high_water_c = cur_bid
                if cur_bid > 0 and cur_bid < low_water_c:
                    low_water_c = cur_bid

                direction = (1 if cur_bid > avg_price_c else -1 if cur_bid < avg_price_c else 0)
                if last_direction and direction and direction != last_direction:
                    osc_count += 1
                if direction:
                    last_direction = direction

                if _observer:
                    try:
                        _observer.tick(ticker, close_str, {
                            "yes_ask": m.get("yes_ask"), "no_ask": m.get("no_ask"),
                            "yes_bid": m.get("yes_bid"), "no_bid": m.get("no_bid"),
                            "btc_price": snapshot.get("btc_price") if snapshot else None,
                            "volume": m.get("volume"), "open_interest": m.get("open_interest"),
                        }, snapshot, _strategy_risk)
                    except Exception:
                        pass

                sell_progress = 0
                if sell_order_id:
                    sell_status = client.get_order(sell_order_id)
                    sell_progress = sell_status.get("fill_count", 0)
                    if sell_progress >= fill_count:
                        blog("INFO", f"Sell fully filled! {sell_progress}/{fill_count}")
                        active_trade["current_bid"] = cur_bid
                        active_trade["high_water_c"] = high_water_c
                        active_trade["sell_progress"] = sell_progress
                        active_trade["minutes_left"] = 0
                        _update_state({"active_trade": active_trade})
                        break

                secs_left = end_time - time.monotonic()
                mins_rem = max(secs_left / 60, 0)

                # Dynamic sell
                if (_dynamic_sell_active and sell_order_id
                        and _fair_value_model and _fv_btc_open and _fv_btc_open > 0
                        and mins_rem > 0.5):
                    try:
                        _ds_now = time.time()
                        if _ds_now - _fv_last_btc_fetch >= 10:
                            _ds_btc = get_live_price("BTC")
                            if _ds_btc and _ds_btc > 0:
                                _fv_last_btc_price = _ds_btc
                                _fv_last_btc_fetch = _ds_now
                        if _fv_last_btc_price and _fv_last_btc_price > 0:
                            _ds_dist = (_fv_last_btc_price - _fv_btc_open) / _fv_btc_open * 100
                            _ds_secs = max(0, 900 - mins_rem * 60)
                            _ds_rvol = snapshot.get("realized_vol_15m") if snapshot else None
                            _ds_prob = _fair_value_model.get_yes_probability(
                                _ds_dist, _ds_secs, _ds_rvol,
                                vol_regime=snapshot.get("vol_regime") if snapshot else None)
                            _ds_fv_now = _ds_prob["fair_yes_c"] if side == "yes" else _ds_prob["fair_no_c"]
                            _ds_new_target = max(int(_ds_fv_now) - 1, avg_price_c + 1)
                            _ds_new_target = min(_ds_new_target, 99)
                            _ds_diff = _ds_new_target - sell_price_c

                            if abs(_ds_diff) >= _dynamic_sell_floor and sell_progress == 0:
                                try:
                                    client.cancel_order(sell_order_id)
                                    _ds_resp = client.place_limit_order(
                                        ticker, side, fill_count - sell_progress,
                                        _ds_new_target, action="sell"
                                    )
                                    _ds_oid = _ds_resp.get("order", {}).get("order_id")
                                    if _ds_oid:
                                        sell_order_id = _ds_oid
                                        sell_price_c = _ds_new_target
                                        active_trade["sell_order_id"] = _ds_oid
                                        active_trade["sell_price_c"] = _ds_new_target
                                        _dynamic_sell_adjustments += 1
                                        active_trade["dynamic_adjustments"] = _dynamic_sell_adjustments
                                        active_trade["dynamic_fv"] = round(_ds_fv_now, 1)
                                        blog("INFO", f"Dynamic sell #{_dynamic_sell_adjustments}: "
                                                      f"FV={_ds_fv_now:.1f}c → sell@{_ds_new_target}c")
                                except Exception as _ds_err:
                                    blog("WARNING", f"Dynamic sell adjust failed: {_ds_err}")
                            else:
                                active_trade["dynamic_fv"] = round(_ds_fv_now, 1)

                            # Early exit via model
                            if (_ds_fv_now < avg_price_c - 5 and mins_rem < 7
                                    and cur_bid > 0 and cur_bid < avg_price_c
                                    and sell_progress == 0):
                                blog("INFO", f"Dynamic sell: FV {_ds_fv_now:.1f}c << entry {avg_price_c}c — cutting losses at {cur_bid}c")
                                try:
                                    client.cancel_order(sell_order_id)
                                    _ds_exit = client.place_limit_order(
                                        ticker, side, fill_count, cur_bid, action="sell"
                                    )
                                    _ds_exit_oid = _ds_exit.get("order", {}).get("order_id")
                                    if _ds_exit_oid:
                                        sell_order_id = _ds_exit_oid
                                        sell_price_c = cur_bid
                                        active_trade["sell_order_id"] = _ds_exit_oid
                                        active_trade["sell_price_c"] = cur_bid
                                        _dynamic_sell_active = False
                                        update_trade(trade_id, {"is_early_exit": 1, "early_exit_price_c": cur_bid})
                                        est_pnl = fill_count * cur_bid / 100 - actual_cost
                                        notify_early_exit(cur_bid, est_pnl, regime_label, mins_left=mins_rem)
                                except Exception:
                                    pass
                    except Exception:
                        pass

                # Trailing stop
                trailing_pct = float(cfg.get("trailing_stop_pct", 0))
                if trailing_pct > 0 and sell_order_id and sell_price_c > avg_price_c:
                    target_range = sell_price_c - avg_price_c
                    progress = (high_water_c - avg_price_c) / target_range if target_range > 0 else 0
                    if progress >= (trailing_pct / 100):
                        trail_buffer = max(2, int(target_range * 0.15))
                        trail_floor = high_water_c - trail_buffer
                        if cur_bid > 0 and cur_bid <= trail_floor and trail_floor > avg_price_c:
                            blog("INFO", f"Trailing stop: bid {cur_bid}c <= floor {trail_floor}c (HWM {high_water_c}c)")
                            try:
                                client.cancel_order(sell_order_id)
                                exit_resp = client.place_limit_order(
                                    ticker, side, fill_count - sell_progress,
                                    cur_bid, action="sell"
                                )
                                exit_oid = exit_resp.get("order", {}).get("order_id")
                                if exit_oid:
                                    sell_order_id = exit_oid
                                    sell_price_c = cur_bid
                                    active_trade["sell_order_id"] = exit_oid
                                    active_trade["sell_price_c"] = cur_bid
                                    update_trade(trade_id, {"is_early_exit": 1, "early_exit_price_c": cur_bid})
                            except Exception as e:
                                blog("WARNING", f"Trailing stop exit failed: {e}")

                # Early exit EV
                early_exit_enabled = bool(cfg.get("early_exit_ev", False))
                if (early_exit_enabled and sell_order_id and mins_rem < 2
                        and cur_bid > 0 and cur_bid < avg_price_c):
                    hold_ev_c = cur_bid
                    time_haircut = max(0.7, mins_rem / 2)
                    adjusted_hold_ev = hold_ev_c * time_haircut
                    if cur_bid > adjusted_hold_ev + 2:
                        blog("INFO", f"Early exit: sell@{cur_bid}c > hold EV {adjusted_hold_ev:.0f}c")
                        try:
                            client.cancel_order(sell_order_id)
                            exit_resp = client.place_limit_order(
                                ticker, side, fill_count - sell_progress,
                                cur_bid, action="sell"
                            )
                            exit_oid = exit_resp.get("order", {}).get("order_id")
                            if exit_oid:
                                sell_order_id = exit_oid
                                sell_price_c = cur_bid
                                active_trade["sell_order_id"] = exit_oid
                                active_trade["sell_price_c"] = cur_bid
                                update_trade(trade_id, {"is_early_exit": 1, "early_exit_price_c": cur_bid})
                        except Exception:
                            pass

                # Price point logging (throttled ~5s)
                now_mono = time.monotonic()
                if now_mono - last_db_write >= 5:
                    last_db_write = now_mono
                    insert_price_point(trade_id, {
                        "minutes_left": round(mins_rem, 2),
                        "yes_bid": m.get("yes_bid"), "yes_ask": m.get("yes_ask"),
                        "no_bid": m.get("no_bid"), "no_ask": m.get("no_ask"),
                        "our_side_bid": cur_bid, "our_side_ask": cur_ask,
                        "btc_price": get_live_price("BTC"),
                    })
                    try:
                        poll_live_market(client, cfg)
                    except Exception:
                        pass

                active_trade["current_bid"] = cur_bid
                active_trade["high_water_c"] = high_water_c
                active_trade["sell_progress"] = sell_progress
                active_trade["minutes_left"] = round(mins_rem, 1)
                _update_state({"active_trade": active_trade})
                _update_status("trading")

                # Minute-by-minute notification
                if time.monotonic() - _last_trade_notify >= 60 and mins_rem > 0.5:
                    _last_trade_notify = time.monotonic()
                    try:
                        notify_trade_update(
                            side=side, cur_bid=cur_bid, avg_price_c=avg_price_c,
                            sell_price_c=sell_price_c, mins_left=mins_rem,
                            fill_count=fill_count, actual_cost=actual_cost,
                            regime_label=regime_label,
                        )
                    except Exception:
                        pass

            except Exception:
                pass

    # ── 11. Resolve outcome ──
    sell_filled = 0
    if sell_order_id:
        sell_filled = client.get_order(sell_order_id).get("fill_count", 0)

    if sell_filled >= fill_count:
        gross = fill_count * sell_price_c / 100
        pnl = gross - actual_cost
        trade_won = pnl > 0
        outcome = "win" if trade_won else "loss"

        market_result = None
        try:
            market_result = client.get_market_result(ticker)
        except Exception:
            pass

        btc_exit = get_live_price("BTC")
        btc_entry = active_trade.get("btc_price")
        btc_move = None
        if btc_exit and btc_entry and btc_entry > 0:
            btc_move = round((btc_exit - btc_entry) / btc_entry * 100, 4)

        blog("INFO", f"SELL FILLED — {outcome.upper()} | cost=${actual_cost:.2f} gross=${gross:.2f} pnl={fpnl(pnl)}")

        entry_time_str = active_trade.get("entry_time")
        time_to_target_s = None
        if entry_time_str:
            try:
                ent = datetime.fromisoformat(entry_time_str.replace("Z", "+00:00"))
                time_to_target_s = round((datetime.now(timezone.utc) - ent).total_seconds(), 1)
            except Exception:
                pass

        update_trade(trade_id, {
            "outcome": outcome, "gross_proceeds": round(gross, 2), "pnl": round(pnl, 2),
            "sell_filled": sell_filled, "exit_price_c": sell_price_c, "exit_time_utc": now_utc(),
            "price_high_water_c": high_water_c, "price_low_water_c": low_water_c,
            "pct_progress_toward_target": 100.0, "oscillation_count": osc_count,
            "market_result": market_result, "btc_price_at_exit": btc_exit,
            "btc_move_pct": btc_move, "exit_method": "sell_fill",
            "time_to_target_seconds": time_to_target_s,
        })
    else:
        _update_status("trading", f"Resolving {ticker}...")
        time.sleep(3)

        market_result = None
        for _ in range(10):
            market_result = client.get_market_result(ticker)
            if market_result:
                break
            time.sleep(3)

        won = (market_result == side) if market_result else False
        gross = client.calc_gross(fill_count, sell_filled, sell_price_c, won)
        pnl = gross - actual_cost
        trade_won = gross > actual_cost
        outcome = "win" if trade_won else "loss"

        pct_progress = 0.0
        if sell_price_c > avg_price_c:
            pct_progress = ((high_water_c - avg_price_c) / (sell_price_c - avg_price_c)) * 100
            pct_progress = max(0, min(100, pct_progress))

        blog("INFO", f"Result: {outcome.upper()} | market={market_result} | cost=${actual_cost:.2f} gross=${gross:.2f} pnl={fpnl(pnl)}")

        if market_result:
            update_market_outcome(market_id, market_result)

        btc_exit = get_live_price("BTC")
        btc_entry = active_trade.get("btc_price")
        btc_move = None
        if btc_exit and btc_entry and btc_entry > 0:
            btc_move = round((btc_exit - btc_entry) / btc_entry * 100, 4)

        update_trade(trade_id, {
            "outcome": outcome, "gross_proceeds": round(gross, 2), "pnl": round(pnl, 2),
            "sell_filled": sell_filled,
            "exit_price_c": sell_price_c if sell_filled > 0 else (100 if won else 0),
            "exit_time_utc": now_utc(),
            "price_high_water_c": high_water_c, "price_low_water_c": low_water_c,
            "pct_progress_toward_target": round(pct_progress, 1),
            "oscillation_count": osc_count, "market_result": market_result,
            "btc_price_at_exit": btc_exit, "btc_move_pct": btc_move,
            "exit_method": "market_expiry",
        })

    # ── 12. Update stats ──
    state = _get_state()
    new_bankroll = client.get_balance_cents()

    win_key = "lifetime_wins" if trade_won else "lifetime_losses"
    _update_state({
        "lifetime_pnl": (state.get("lifetime_pnl") or 0) + pnl,
        win_key: (state.get(win_key) or 0) + 1,
        "bankroll_cents": new_bankroll,
    })

    insert_bankroll_snapshot(new_bankroll, PLUGIN_ID, trade_id)

    if is_ignored:
        blog("INFO", f"IGNORED trade ({outcome} {fpnl(pnl)}) — not counted in stats")
        _update_state({"active_trade": None})
        _update_status("searching", f"Last: {outcome} ({fpnl(pnl)}) [IGNORED]")
        return True

    if trade_won:
        blog("INFO", f"WIN +${pnl:.2f}")
        notify_trade_result("win", pnl, side, ticker, regime_label)
        _update_status(detail=f"WIN {fpnl(pnl)} in {regime_label}")
    else:
        blog("INFO", f"LOSS {fpnl(pnl)}")
        notify_trade_result("loss", pnl, side, ticker, regime_label)
        _update_status(detail=f"LOSS {fpnl(pnl)}")

    if regime_label:
        _update_regime_with_notify(regime_label)

    summary = {
        "trade_id": trade_id, "ticker": ticker, "side": side,
        "outcome": outcome, "pnl": round(pnl, 2),
        "actual_cost": round(actual_cost, 2), "gross": round(gross, 2),
        "avg_price_c": avg_price_c, "sell_price_c": sell_price_c,
        "fill_count": fill_count, "sell_filled": sell_filled,
        "high_water_c": high_water_c, "market_result": market_result,
        "regime_label": regime_label, "risk_level": gate["risk_level"],
    }

    _update_state({"active_trade": None, "last_completed_trade": summary})

    return True


# ═══════════════════════════════════════════════════════════════
#  MAIN LOOP
# ═══════════════════════════════════════════════════════════════

def run_loop(client, stop_event):
    """Main trading loop called by plugin.run()."""
    global _observer, _fair_value_model, _skip_first_market
    global _fv_btc_open, _fv_market_ticker, _fv_last_btc_fetch, _fv_last_btc_price

    blog("INFO", "BTC 15m trading engine starting...")

    # Initialize Observatory and Fair Value Model
    _observer = MarketObserver()
    try:
        _fair_value_model = BtcFairValueModel()
        blog("INFO", f"Fair value model loaded")
    except Exception as e:
        blog("WARNING", f"Fair value model not available: {e}")
        _fair_value_model = None

    # Connect to Kalshi
    try:
        balance = client.get_balance_cents()
        blog("INFO", f"Kalshi connected — balance: ${balance / 100:.2f}")
        _update_state({"bankroll_cents": balance})
    except Exception as e:
        blog("ERROR", f"Kalshi connection failed: {e}")
        return

    # Flush stale commands
    flush_pending_commands(PLUGIN_ID)

    # Orphan trade recovery
    state = _get_state()
    active = state.get("active_trade")
    if active:
        blog("INFO", f"Found orphan trade: {active.get('ticker')}")
        close_str = active.get("close_time", "")
        if close_str:
            close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
            secs = (close_dt - datetime.now(timezone.utc)).total_seconds()
            if secs < -5:
                _resolve_orphan_trade(client, active)
            else:
                # Check if buy actually filled
                buy_oid = active.get("buy_order_id")
                if buy_oid:
                    try:
                        order = client.get_order(buy_oid)
                        if order.get("fill_count", 0) == 0:
                            blog("INFO", "Orphan trade has 0 fills — cancelling and clearing")
                            try:
                                client.cancel_order(buy_oid)
                            except Exception:
                                pass
                            if active.get("sell_order_id"):
                                try:
                                    client.cancel_order(active["sell_order_id"])
                                except Exception:
                                    pass
                            _update_state({"active_trade": None})
                        else:
                            blog("INFO", f"Orphan trade has {order['fill_count']} fills — monitoring")
                            while secs > -5 and not stop_event.is_set():
                                monitor_orphan_trade(client, load_config())
                                time.sleep(2)
                                if not _get_state().get("active_trade"):
                                    break
                                secs = (close_dt - datetime.now(timezone.utc)).total_seconds()
                            if _get_state().get("active_trade"):
                                _resolve_orphan_trade(client, _get_state()["active_trade"])
                    except Exception:
                        _resolve_orphan_trade(client, active)
        else:
            _update_state({"active_trade": None})

    # Reset transient state
    _skip_first_market = True
    _update_state({
        "active_shadow": None, "active_skip": None,
    })
    _update_status("stopped", "Ready")

    # Recompute stats from history
    try:
        recompute_all_stats()
    except Exception:
        pass

    # Backfill results
    try:
        backfill_trade_market_results(client)
    except Exception:
        pass
    try:
        backfill_observation_results(client)
    except Exception:
        pass

    # Start background computation thread
    bg_stop = threading.Event()
    bg_thread = threading.Thread(target=_background_worker, args=(bg_stop,), daemon=True)
    bg_thread.start()

    # Auto-start data collection for observe/shadow/hybrid
    cfg = load_config()
    _trading_mode = get_trading_mode(cfg)
    if _trading_mode in ("observe", "shadow", "hybrid"):
        _update_state({"auto_trading": 1})
        _update_status("searching", f"Auto-started in {_trading_mode} mode")
        blog("INFO", f"Auto-started data collection in {_trading_mode} mode")

    # ── Main loop ──
    _consecutive_errors = 0
    _last_backfill = time.monotonic()
    _last_cleanup = time.monotonic()
    _was_auto = False

    try:
        while not stop_event.is_set():
            cfg = process_commands(client, cfg)
            state = _get_state()
            should_run = bool(state.get("auto_trading", 0))

            # Detect transitions
            if should_run and not _was_auto:
                _update_state({"auto_trading_since": now_utc()})
            elif not should_run and _was_auto:
                _update_state({"session_stopped_at": now_utc()})
            _was_auto = should_run

            # Periodic backfill (every 5 min)
            now_mono = time.monotonic()
            if now_mono - _last_backfill >= 300:
                _last_backfill = now_mono
                try:
                    backfill_trade_market_results(client)
                except Exception:
                    pass
                try:
                    backfill_observation_results(client)
                except Exception:
                    pass
                try:
                    refresh_all_coarse_regime_stats()
                    refresh_all_hourly_stats()
                except Exception:
                    pass

            # Periodic log cleanup (every 6 hours)
            if now_mono - _last_cleanup >= 21600:
                _last_cleanup = now_mono
                try:
                    _cleanup_logs()
                except Exception:
                    pass

            if not should_run:
                # Idle mode
                active = state.get("active_trade")
                if active:
                    monitor_orphan_trade(client, cfg)
                else:
                    try:
                        poll_live_market(client, cfg)
                    except Exception:
                        pass
                    if _trading_mode in ("observe", "shadow", "hybrid"):
                        _update_status("stopped", f"Data collection paused — {_trading_mode} mode")
                time.sleep(1)
                _consecutive_errors = 0
                continue

            # Auto-trading ON
            active = state.get("active_trade")
            if active and active.get("is_ignored"):
                monitor_orphan_trade(client, cfg)
                time.sleep(1)
                continue

            try:
                traded = run_trade(client, cfg)
                _consecutive_errors = 0

                if traded:
                    state = _get_state()
                    if not state.get("auto_trading"):
                        continue

                    tr = state.get("trades_remaining", 0)
                    if tr and tr > 0:
                        tr -= 1
                        _update_state({"trades_remaining": tr})
                        if tr <= 0:
                            _update_state({"auto_trading": 0, "trades_remaining": 0})
                            _update_status("stopped", "Trade count reached")
                            blog("INFO", "Trade count reached — stopping")

            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as e:
                _consecutive_errors += 1
                blog("ERROR", f"Trade error: {e}\n{traceback.format_exc()}")
                if _consecutive_errors >= 5:
                    blog("ERROR", "5 consecutive errors — stopping auto-trading")
                    _update_state({"auto_trading": 0, "trades_remaining": 0})
                    _update_status("stopped", "Stopped: too many errors")
                    _consecutive_errors = 0
                time.sleep(15)

            time.sleep(2)

    except KeyboardInterrupt:
        blog("INFO", "Shutting down (KeyboardInterrupt)")
        _update_state({"auto_trading": 0})
    except SystemExit:
        blog("INFO", "Shutting down (SIGTERM — service restart)")
    finally:
        # Observatory discard
        if _observer:
            _observer.discard()
        # Stop background thread
        bg_stop.set()
        bg_thread.join(timeout=5)
        _update_status("stopped", "Stopped")
        blog("INFO", "Bot stopped")
