"""
bot.py — BTC 15-minute trading engine foundations.
Market discovery, bankroll sizing, state helpers.
"""

import json
import logging
from datetime import datetime, timezone, timedelta

from config import ET, KALSHI_FEE_RATE
from db import (
    get_config, set_config, get_all_config,
    get_plugin_state, update_plugin_state,
    insert_log, now_utc,
    get_latest_regime_snapshot,
)
from kalshi import KalshiClient
from regime import get_live_price, compute_coarse_label, score_spread
from plugins.btc_15m.market_db import (
    upsert_market, update_market_outcome,
    insert_trade, update_trade, get_trade, get_recent_trades,
    insert_price_point, insert_live_price,
    update_regime_stats, recompute_all_stats,
    get_regime_risk, get_strategy_risk,
    get_skipped_trades_needing_result, backfill_skipped_result,
    get_prev_regime_label,
)
from plugins.btc_15m.strategy import (
    MarketObserver, backfill_observation_results,
    get_recommendation, BtcFairValueModel,
)
from plugins.btc_15m.notifications import (
    notify_trade_result, notify_max_loss, notify_bankroll_limit,
    notify_error, notify_buy, notify_observed,
    notify_new_regime, notify_regime_classified,
    notify_trade_update, notify_early_exit,
)

log = logging.getLogger("bot")

PLUGIN_ID = "btc_15m"


# ═══════════════════════════════════════════════════════════════
#  DB LOGGER
# ═══════════════════════════════════════════════════════════════

def blog(level: str, msg: str, category: str = "bot"):
    """Log to both Python logger and database for dashboard display."""
    getattr(log, level.lower(), log.info)(msg)
    try:
        insert_log(level.upper(), msg, category, source=PLUGIN_ID)
    except Exception:
        pass


def fpnl(val: float) -> str:
    """Format P&L with sign before dollar: +$5.00 or -$5.00"""
    return f"+${val:.2f}" if val >= 0 else f"-${abs(val):.2f}"


# ═══════════════════════════════════════════════════════════════
#  STATE HELPERS
# ═══════════════════════════════════════════════════════════════

def _get_state() -> dict:
    """Get plugin state from platform DB."""
    return get_plugin_state(PLUGIN_ID)


def _update_state(data: dict):
    """Merge data into plugin state."""
    update_plugin_state(PLUGIN_ID, data)


def _update_status(status: str, detail: str = "", **extra):
    """Update status fields in plugin state."""
    data = {"status": status, "status_detail": detail}
    data.update(extra)
    _update_state(data)


def _get_cfg() -> dict:
    """Load plugin config with btc_15m. prefix, strip prefix for callers."""
    raw = get_all_config(namespace="btc_15m.")
    return {k.replace("btc_15m.", ""): v for k, v in raw.items()}


# ═══════════════════════════════════════════════════════════════
#  MODULE-LEVEL STATE
# ═══════════════════════════════════════════════════════════════

_observer = None
_fair_value_model = None

# BTC open price tracking for current market (reset on new ticker)
_fv_btc_open = None          # BTC price at market open
_fv_market_ticker = None     # Ticker we captured the open for
_fv_last_btc_fetch = 0       # Throttle BTC price fetches (every 10s)
_fv_last_btc_price = None    # Cached current BTC price

# When True, skip the current market and wait for a fresh one.
_skip_first_market = True


# ═══════════════════════════════════════════════════════════════
#  MARKET DISCOVERY (15-minute BTC specific)
# ═══════════════════════════════════════════════════════════════

_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def _build_ticker(close_et: datetime) -> str:
    """Build the Kalshi ticker string from close time in ET."""
    mon = _MONTHS[close_et.month - 1]
    event_ticker = f"KXBTC15M-{close_et.strftime('%y')}{mon}{close_et.strftime('%d%H%M')}"
    return f"{event_ticker}-{close_et.minute:02d}"


def find_current_market(client: KalshiClient) -> dict | None:
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
    return client.fetch_market_safe(ticker)


def find_next_market(client: KalshiClient) -> dict | None:
    """Find the NEXT 15-min BTC market (hasn't started yet or just started)."""
    et = datetime.now(ET)

    current_slot_end_min = ((et.minute // 15) + 1) * 15
    next_close_min = current_slot_end_min + 15
    next_close_hour = et.hour
    day_offset = 0

    while next_close_min >= 60:
        next_close_min -= 60
        next_close_hour += 1
        if next_close_hour >= 24:
            next_close_hour -= 24
            day_offset += 1

    next_close_et = et.replace(
        hour=next_close_hour, minute=next_close_min,
        second=0, microsecond=0
    )
    if day_offset:
        next_close_et += timedelta(days=day_offset)

    ticker = _build_ticker(next_close_et)
    log.info(f"Next market: {ticker} (closes {next_close_et.strftime('%H:%M ET')})")
    return client.fetch_market_safe(ticker)


# ═══════════════════════════════════════════════════════════════
#  BANKROLL & BET SIZING
# ═══════════════════════════════════════════════════════════════

def get_effective_bankroll_cents(client: KalshiClient, cfg: dict) -> int:
    """Cash balance in cents (no locked bankroll in platform version)."""
    return client.get_balance_cents()


def get_r1_bet_dollars(cfg: dict, bankroll: float,
                       edge_pct: float = None, **kwargs) -> float:
    """Compute bet size based on bet mode.

    Modes:
      flat: fixed dollar amount
      percent: fixed % of bankroll
      edge_scaled: base bet scaled by FV model edge using configurable tiers
    """
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


# ═══════════════════════════════════════════════════════════════
#  TRADING MODE
# ═══════════════════════════════════════════════════════════════

def get_trading_mode(cfg: dict) -> str:
    """Get trading mode from config, with fallback derivation from legacy booleans.

    Modes: observe, shadow, hybrid, auto, manual
    """
    mode = cfg.get("trading_mode", "")
    if mode in ("observe", "shadow", "hybrid", "auto", "manual"):
        return mode
    # Derive from legacy booleans
    obs = cfg.get("observe_only", False)
    shd = cfg.get("shadow_trading", False)
    auto = cfg.get("auto_strategy_enabled", False)
    if obs and shd:
        return "shadow"
    elif obs:
        return "observe"
    elif auto:
        return "auto"
    return "manual"


# ═══════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════

def _trade_ctx() -> str:
    """Build context suffix for status messages: trades remaining info."""
    state = _get_state()
    rem = state.get("trades_remaining", 0)
    if rem and rem > 0:
        return f" · {rem} trade{'s' if rem != 1 else ''} left"
    return ""


def _fmt_wait(secs: float) -> str:
    """Format seconds as Xm Xs."""
    s = max(0, int(secs))
    return f"{s // 60}m {s % 60:02d}s"


def build_strategy_key(cfg: dict) -> str:
    """Map bot settings to a Strategy Observatory strategy key."""
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
    """Strip modifiers (thin_, squeeze_, _accel, _decel) to get base label."""
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


def marketStartTime(close_time_str: str) -> str:
    """Convert a market close time to its start time label in Central."""
    from config import CT
    try:
        close_dt = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
        start_dt = close_dt - timedelta(minutes=15)
        ct = start_dt.astimezone(CT)
        return ct.strftime("%-I:%M %p CT")
    except Exception:
        return close_time_str


def _cleanup_logs(retention_days: int = 7):
    """Clean up log file and log_entries table."""
    import os
    from config import LOG_FILE
    from db import get_conn

    try:
        if os.path.isfile(LOG_FILE):
            size_mb = os.path.getsize(LOG_FILE) / (1024 * 1024)
            if size_mb > 5:
                with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                keep = lines[-20_000:]
                with open(LOG_FILE, "w", encoding="utf-8") as f:
                    f.write(f"[log rotated at {now_utc()} — kept last {len(keep)} of {len(lines)} lines]\n")
                    f.writelines(keep)
                blog("INFO", f"Log file rotated: {size_mb:.1f}MB → ~{len(keep)*100/1024/1024:.1f}MB")
    except Exception as e:
        log.warning(f"Log file cleanup error: {e}")

    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with get_conn() as c:
            deleted = c.execute(
                "DELETE FROM log_entries WHERE ts < ?", (cutoff,)
            ).rowcount
        if deleted and deleted > 0:
            blog("DEBUG", f"Cleaned {deleted} log entries older than {retention_days} days")
    except Exception as e:
        log.warning(f"Log table cleanup error: {e}")


def _update_regime_with_notify(regime_label: str):
    """Update regime stats and send notifications."""
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
                total=result["total"],
                win_rate=result["win_rate"],
                old_risk=old_risk,
            )
    except Exception as e:
        blog("WARNING", f"Regime stats/notify error: {e}")


def _resolve_skip_inline(client, trade_id: int, ticker: str,
                          market_id: int = None):
    """Resolve a skipped market's result immediately after close."""
    if not ticker or ticker == "n/a":
        return
    try:
        import time as _t
        for _ in range(8):
            result = client.get_market_result(ticker)
            if result:
                backfill_skipped_result(trade_id, result)
                if market_id:
                    update_market_outcome(market_id, result)
                return
            _t.sleep(3)
    except Exception as e:
        blog("DEBUG", f"Skip inline resolve failed for {ticker}: {e}")


# ═══════════════════════════════════════════════════════════════
#  REGIME GATING
# ═══════════════════════════════════════════════════════════════

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
        risk_info = get_regime_risk(regime_label)
        risk_level = risk_info.get("risk_level", "unknown")
        total_trades = risk_info.get("total_trades", 0)
        win_rate = risk_info.get("win_rate", 0)
        info_str = f"'{regime_label}' ({risk_level}, win={win_rate:.0%}, n={total_trades})"

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
            "should_trade": False,
            "is_data_collection": False,
            "reason": f"Regime {info_str} — skipping",
            "risk_level": risk_level,
            "strategy_risk": strategy_risk,
        }

    return {
        "should_trade": True,
        "is_data_collection": False,
        "reason": f"Regime {info_str}",
        "risk_level": risk_level,
        "strategy_risk": strategy_risk,
    }


# ═══════════════════════════════════════════════════════════════
#  BUILD TRADE CONTEXT
# ═══════════════════════════════════════════════════════════════

def _build_trade_context(client, cfg, state, market, snapshot, gate,
                         coarse_regime, prev_regime, hour_et, day_of_week,
                         vol_level=None, close_str=None):
    """Build common context dict for all trade inserts."""
    btc_price = get_live_price("BTC")
    eff_bankroll_c = get_effective_bankroll_cents(client, cfg)

    spread_c = None
    cheaper_side = None
    cheaper_side_price_c = None
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
        "trade_mode": cfg.get("trade_mode", "continuous"),
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
    from db import get_conn as _gc
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

    with _gc() as c:
        for setup in candidates:
            row = c.execute("""
                SELECT strategy_key, side_rule, exit_rule, entry_time_rule,
                       entry_price_max, ev_per_trade_c, weighted_ev_c,
                       win_rate, sample_size, setup_key
                FROM btc15m_strategy_results
                WHERE setup_key = ? AND sample_size >= 5
                ORDER BY COALESCE(weighted_ev_c, ev_per_trade_c) DESC
                LIMIT 1
            """, (setup,)).fetchone()
            if row:
                return {
                    "setup_key": row["setup_key"],
                    "strategy_key": row["strategy_key"],
                    "side_rule": row["side_rule"],
                    "entry_time_rule": row["entry_time_rule"],
                    "entry_price_max": row["entry_price_max"],
                    "sell_target": row["exit_rule"],
                    "ev_per_trade_c": row["ev_per_trade_c"],
                    "weighted_ev_c": row["weighted_ev_c"],
                    "win_rate": row["win_rate"],
                    "sample_size": row["sample_size"],
                }
    return None


def _place_shadow_trade(client, ticker, side, price_c,
                        market_id=None, regime_label=None,
                        snapshot_id=None, ctx=None,
                        strategy_key=None) -> int | None:
    """Place a 1-contract shadow trade for execution data collection."""
    import time as _t
    if not side or side == "n/a" or not price_c or price_c <= 0 or price_c >= 95:
        return None

    decision_time = _t.time()
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
            deadline = _t.time() + 60
            fill = client.poll_until_filled(order_id, 1, deadline, interval=3)
        else:
            client.cancel_order(order_id)
            return None

        fill_count = fill.get("fill_count", 0) if fill else 0
        fill_time = _t.time()
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
#  LIVE MARKET POLL
# ═══════════════════════════════════════════════════════════════

def poll_live_market(client: KalshiClient, cfg: dict):
    """Poll current market and feed dashboard + Observatory."""
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
            "btc_price": snapshot.get("btc_price") if snapshot else None,
            "vol_regime": snapshot.get("vol_regime") if snapshot else None,
            "trend_regime": snapshot.get("trend_regime") if snapshot else None,
            "volume_regime": snapshot.get("volume_regime") if snapshot else None,
        }

        # Fair Value Model edge for idle display
        global _fv_btc_open, _fv_market_ticker, _fv_last_btc_fetch, _fv_last_btc_price
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

        # Feed Strategy Observatory
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
            _observer.tick(ticker, close_str, market_data, snapshot)

        try:
            insert_live_price(ticker, market.get("yes_ask"), market.get("no_ask"),
                              market.get("yes_bid"), market.get("no_bid"))
        except Exception:
            pass

    except Exception as e:
        log.debug(f"Live market poll error: {e}")


# ═══════════════════════════════════════════════════════════════
#  SKIP WAIT LOOP
# ═══════════════════════════════════════════════════════════════

def _skip_wait_loop(client, cfg, close_dt, skip_trade_id, ticker,
                    regime_label, risk_level, reason,
                    track_side=False, resolve_inline=False,
                    initial_cheaper_side=None, market_id=None) -> bool:
    """Wait for a skipped market to close while processing commands."""
    from db import get_pending_commands, complete_command
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
        _update_state({
            "status_detail": f"Observing {regime_label.replace('_', ' ')} — next in ~{_fmt_wait(remaining)}{ctx}",
        })

        for cmd in get_pending_commands(PLUGIN_ID):
            cmd_type = cmd["command_type"]
            cmd_id = cmd["id"]
            params = json.loads(cmd.get("parameters") or "{}")
            if cmd_type == "stop":
                _update_state({
                    "auto_trading": 0, "trades_remaining": 0,
                    "status": "stopped", "status_detail": "Stopped",
                })
                complete_command(cmd_id)
                stopped_early = True
                break
            elif cmd_type == "update_config":
                for k, v in params.items():
                    set_config(f"btc_15m.{k}", v)
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
        from collections import Counter
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
#  WAIT FOR NEXT MARKET
# ═══════════════════════════════════════════════════════════════

def wait_for_next_market(client: KalshiClient, cfg: dict) -> dict | None:
    """Wait until a fresh market is available that we haven't traded yet."""
    from db import get_pending_commands, complete_command
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
                          f"waiting for next fresh market")

        elif ticker != last_ticker:
            mins_left = client.minutes_until_close(close_str) if close_str else 0
            if mins_left > 12:
                blog("INFO", f"Fresh market available: {ticker} ({mins_left:.1f}m left)")
                _skip_first_market = False
                return current
            else:
                blog("INFO", f"Market {ticker} has only {mins_left:.1f}m left — waiting")

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
                _update_state({"status": "waiting", "status_detail": detail})

                for cmd in get_pending_commands(PLUGIN_ID):
                    cmd_type = cmd["command_type"]
                    cmd_id = cmd["id"]
                    params = json.loads(cmd.get("parameters") or "{}")
                    if cmd_type == "stop":
                        _update_state({
                            "auto_trading": 0, "trades_remaining": 0,
                            "status": "stopped", "status_detail": "Stopped",
                        })
                        complete_command(cmd_id)
                        return None
                    elif cmd_type == "start":
                        mode = params.get("mode", "continuous")
                        count = params.get("count", 1)
                        base = {"auto_trading": 1, "status": "waiting"}
                        if mode == "single":
                            base["trades_remaining"] = 1
                        elif mode == "count":
                            base["trades_remaining"] = count
                        else:
                            base["trades_remaining"] = 0
                        _update_state(base)
                        complete_command(cmd_id, {"mode": mode})
                        return None
                    elif cmd_type == "update_config":
                        for k, v in params.items():
                            set_config(f"btc_15m.{k}", v)
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
    _update_state({"status": "searching", "status_detail": f"Starting — finding market{_trade_ctx()}"})

    for attempt in range(30):
        for cmd in get_pending_commands(PLUGIN_ID):
            cmd_type = cmd["command_type"]
            cmd_id = cmd["id"]
            if cmd_type == "stop":
                _update_state({"auto_trading": 0, "trades_remaining": 0,
                              "status": "stopped", "status_detail": "Stopped"})
                complete_command(cmd_id)
                return None
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
#  COMMAND PROCESSING
# ═══════════════════════════════════════════════════════════════

def process_commands(client: KalshiClient, cfg: dict) -> dict:
    """Process pending commands from the dashboard."""
    import traceback
    from db import get_pending_commands, complete_command, cancel_command, flush_pending_commands

    for cmd in get_pending_commands(PLUGIN_ID):
        cmd_type = cmd["command_type"]
        params = json.loads(cmd.get("parameters") or "{}")
        cmd_id = cmd["id"]

        try:
            if cmd_type == "start":
                global _skip_first_market
                mode = params.get("mode", "continuous")
                count = params.get("count", 1)

                base = {"auto_trading": 1, "auto_trading_since": now_utc(),
                        "last_completed_trade": None,
                        "status": "searching",
                        "loss_streak": 0}

                state_now = _get_state()
                at_now = state_now.get("active_trade")
                if at_now and (at_now.get("is_ignored")):
                    at_now["is_ignored"] = False
                    base["active_trade"] = at_now
                    base["status"] = "trading"
                    base["status_detail"] = "Resumed — monitoring active trade"
                    tid = at_now.get("trade_id")
                    if tid:
                        update_trade(tid, {"is_ignored": 0, "notes": "Restored on resume"})
                    blog("INFO", "Restored ignored trade to active on resume")
                else:
                    _skip_first_market = True
                    if _observer:
                        _observer.discard()
                    blog("INFO", "Will wait for next fresh market before trading")

                if mode == "single":
                    base["trades_remaining"] = 1
                    base["status_detail"] = "Starting — single trade"
                elif mode == "count":
                    base["trades_remaining"] = count
                    base["status_detail"] = f"Starting — {count} trades"
                else:
                    base["trades_remaining"] = 0
                    base["status_detail"] = "Starting — continuous"
                _update_state(base)
                complete_command(cmd_id, {"mode": mode})
                cfg = _get_cfg()
                blog("INFO", f"Started: mode={mode}")

            elif cmd_type == "stop":
                state = _get_state()
                at = state.get("active_trade")
                updates = {
                    "auto_trading": 0, "trades_remaining": 0,
                    "loss_streak": 0,
                    "status": "stopped" if not at else "trading",
                    "status_detail": "Stopped" if not at else "Stopped — trade kept as ignored",
                }
                if at:
                    at["is_ignored"] = True
                    updates["active_trade"] = at
                    tid = at.get("trade_id")
                    if tid:
                        update_trade(tid, {"is_ignored": 1, "notes": "Stopped mid-trade — ignored"})
                if _observer:
                    _observer.discard()
                _update_state(updates)
                complete_command(cmd_id)
                blog("INFO", "Stopped by user" + (" — active trade kept as ignored" if at else ""))

            elif cmd_type == "update_config":
                for k, v in params.items():
                    set_config(f"btc_15m.{k}", v)
                complete_command(cmd_id, {"updated": list(params.keys())})
                cfg = _get_cfg()
                blog("INFO", f"Config updated: {list(params.keys())}")

            elif cmd_type == "set_mode":
                new_mode = params.get("mode", "observe")
                set_config("btc_15m.trading_mode", new_mode)
                cfg = _get_cfg()
                complete_command(cmd_id, {"mode": new_mode})
                blog("INFO", f"Trading mode → {new_mode}")

            elif cmd_type == "run_sim_batch":
                try:
                    from plugins.btc_15m.strategy import run_simulation_batch
                    processed = run_simulation_batch()
                    complete_command(cmd_id, {"processed": processed})
                    if processed:
                        blog("INFO", f"Sim batch: processed {processed} observations")
                except Exception as e:
                    complete_command(cmd_id, {"error": str(e)})
                    blog("WARNING", f"Sim batch error: {e}")

            elif cmd_type == "run_validation_test":
                test_id = params.get("test_id", "")
                blog("INFO", f"Running validation test: {test_id}")
                _vt_result = None
                if test_id == "walkforward":
                    from plugins.btc_15m.strategy import run_walkforward_selection_test
                    _vt_result = run_walkforward_selection_test(
                        n_folds=int(params.get("folds", 5)))
                    verdict = _vt_result.get("verdict", "insufficient_data")
                    if verdict == "selection_works":
                        set_config("_selection_test_result", "passed")
                    elif verdict == "selection_unreliable":
                        set_config("_selection_test_result", "failed")
                elif test_id == "permutation":
                    from plugins.btc_15m.strategy import run_permutation_test
                    _vt_result = run_permutation_test(
                        n_permutations=int(params.get("n", 500)))
                elif test_id == "persistence":
                    from plugins.btc_15m.strategy import test_strategy_persistence
                    _vt_result = test_strategy_persistence()
                else:
                    _vt_result = {"error": f"Unknown test: {test_id}"}
                set_config(f"_validation_result_{test_id}", json.dumps(_vt_result))
                complete_command(cmd_id, {"test_id": test_id, "done": True})
                blog("INFO", f"Validation test {test_id} complete")

            elif cmd_type == "reset_trade_cache":
                try:
                    recompute_all_stats(PLUGIN_ID)
                    complete_command(cmd_id, {"recomputed": True})
                    blog("INFO", "Trade stats recomputed")
                except Exception as e:
                    complete_command(cmd_id, {"error": str(e)})

            elif cmd_type == "dismiss_summary":
                _update_state({"last_completed_trade": None})
                complete_command(cmd_id)

            else:
                cancel_command(cmd_id, f"Unknown command: {cmd_type}")

        except Exception as e:
            tb = traceback.format_exc()
            blog("ERROR", f"Command error ({cmd_type}): {e}")
            blog("ERROR", f"Traceback:\n{tb}")
            try:
                complete_command(cmd_id, {"error": str(e)})
            except Exception:
                pass

    return cfg


# ═══════════════════════════════════════════════════════════════
#  RUN LOOP
# ═══════════════════════════════════════════════════════════════

def run_loop(client: KalshiClient, stop_event):
    """Main trading loop. Called from engine.py via plugin.run()."""
    import traceback
    from db import (
        get_pending_commands, complete_command, flush_pending_commands,
        insert_bankroll_snapshot,
    )

    blog("INFO", "=" * 50)
    blog("INFO", "BTC 15-Minute Trading Bot starting")
    blog("INFO", "=" * 50)

    # Initialize Observatory and FV Model
    global _observer, _fair_value_model
    _observer = MarketObserver()
    _fair_value_model = BtcFairValueModel()
    try:
        _fair_value_model.load(force=True)
        status = _fair_value_model.get_status()
        if status["ready"]:
            blog("INFO", f"Fair value model ready ({status['cells_loaded']} surface cells)")
        else:
            blog("INFO", f"Fair value model: insufficient data ({status['cells_loaded']} cells) — analytical fallback")
    except Exception as e:
        blog("WARNING", f"Fair value model init error: {e}")

    # Connect and get balance
    try:
        balance = client.get_balance_cents()
        blog("INFO", f"Connected to Kalshi. Balance: ${balance / 100:.2f}")
        _update_state({"bankroll_cents": balance})
    except Exception as e:
        blog("ERROR", f"Cannot connect to Kalshi: {e}")
        return

    cfg = _get_cfg()

    # Flush stale commands
    flush_pending_commands(PLUGIN_ID)
    blog("INFO", "Flushed stale command queue")

    # Clear stale transient state
    _update_state({
        "status": "stopped",
        "status_detail": "Restarting...",
        "bankroll_cents": balance,
        "auto_trading": 0,
        "trades_remaining": 0,
        "pending_trade": None,
        "active_shadow": None,
        "active_skip": None,
    })
    insert_bankroll_snapshot(balance, plugin_id=PLUGIN_ID)

    # Resolve stale open trades from previous crash
    try:
        _resolve_stale_open_trades(client)
    except Exception as e:
        blog("ERROR", f"Error resolving stale trades: {e}")

    # Recompute stats from DB
    try:
        recompute_all_stats(PLUGIN_ID)
        blog("INFO", "Stats recomputed from trade history")
    except Exception as e:
        blog("ERROR", f"Error recomputing stats: {e}")

    # Startup backfill
    try:
        _backfill_skipped_results(client, limit=20)
        backfill_observation_results(client, limit=30)
    except Exception as e:
        blog("WARNING", f"Startup backfill error: {e}")

    # Auto-start in data-collecting modes
    cfg = _get_cfg()
    _startup_mode = get_trading_mode(cfg)
    if _startup_mode in ("observe", "shadow", "hybrid"):
        _mode_labels = {"observe": "observe-only", "shadow": "shadow", "hybrid": "hybrid"}
        _update_state({
            "auto_trading": 1,
            "auto_trading_since": now_utc(),
            "status": "searching",
            "status_detail": f"Auto-started — {_mode_labels[_startup_mode]} mode",
            "trades_remaining": 0,
        })
        blog("INFO", f"AUTO-START: {_mode_labels[_startup_mode]} mode — always collecting data")
    else:
        _update_state({
            "status": "stopped",
            "status_detail": "Idle — press play to start",
        })

    sell_c = int(cfg.get('sell_target_c', 0) or 0)
    sell_desc = f"sell@{sell_c}c" if sell_c else "hold"
    blog("INFO", f"Config: bet={cfg.get('bet_mode')} ${cfg.get('bet_size')} | "
                  f"sell_target={sell_desc} | entry≤{cfg.get('entry_price_max_c')}c")

    # ── Main loop ──
    _consecutive_errors = 0
    _was_auto_trading = bool(_get_state().get("auto_trading", 0))
    _last_backfill = 0
    _last_log_cleanup = 0

    while not stop_event.is_set():
        try:
            cfg = process_commands(client, cfg)
            state = _get_state()

            auto_trading = bool(state.get("auto_trading", 0))

            # Detect start/stop transitions
            if auto_trading and not _was_auto_trading:
                _update_state({"auto_trading_since": now_utc()})
            if _was_auto_trading and not auto_trading:
                _update_state({"auto_trading_since": ""})
            _was_auto_trading = auto_trading

            # Periodic backfill
            if time.monotonic() - _last_backfill > 300:
                try:
                    backfill_observation_results(client, limit=30)
                except Exception:
                    pass
                if _observer:
                    try:
                        health = _observer.get_health()
                        _update_state({"observatory_health": health})
                    except Exception:
                        pass
                _last_backfill = time.monotonic()

                if time.monotonic() - _last_log_cleanup > 21600:
                    try:
                        retention_days = int(cfg.get("log_retention_days", 7) or 7)
                        _cleanup_logs(retention_days)
                    except Exception:
                        pass
                    _last_log_cleanup = time.monotonic()

            if not auto_trading:
                # Idle — poll live market for dashboard
                try:
                    poll_live_market(client, cfg)
                except Exception:
                    pass

                bal_update = {}
                try:
                    bal_update["bankroll_cents"] = client.get_balance_cents()
                except Exception:
                    pass

                detail = state.get("status_detail", "")
                _idle_mode = get_trading_mode(cfg)
                is_obs = _idle_mode in ("observe", "shadow", "hybrid")
                if not detail or detail in ("Idle — press play to start", "Bot ready"):
                    new_detail = "Observing — recording market data" if is_obs else "Idle — press play to start"
                else:
                    new_detail = detail

                _update_state({"status": "stopped", "status_detail": new_detail, **bal_update})
                _consecutive_errors = 0
                time.sleep(1)
                continue

            # ── Auto-trading is ON ──
            cfg = _get_cfg()
            traded = _run_one_market(client, cfg)
            _consecutive_errors = 0

            # After trade: check if should stop
            post_state = _get_state()
            if traded and not post_state.get("auto_trading"):
                blog("INFO", "Bot stopped after completing trade")
                time.sleep(1)
                continue

            trades_remaining = post_state.get("trades_remaining", 0)
            if traded and trades_remaining and trades_remaining > 0:
                new_rem = trades_remaining - 1
                _update_state({"trades_remaining": new_rem})
                if new_rem <= 0:
                    _update_state({
                        "auto_trading": 0, "trades_remaining": 0,
                        "status": "stopped",
                        "status_detail": "All trades completed — stopped",
                    })
                    blog("INFO", "All requested trades completed — stopped")

            time.sleep(2)

        except Exception as e:
            _consecutive_errors += 1
            tb = traceback.format_exc()
            blog("ERROR", f"Unexpected error ({_consecutive_errors}x): {e}")
            blog("ERROR", f"Traceback:\n{tb}")
            notify_error(str(e))
            if _consecutive_errors >= 5:
                _update_state({
                    "status": "stopped",
                    "status_detail": f"Stopped: {_consecutive_errors} consecutive errors",
                    "auto_trading": 0,
                })
                blog("ERROR", f"Auto-trading stopped after {_consecutive_errors} consecutive errors")
                _consecutive_errors = 0
            else:
                _update_state({
                    "status_detail": f"Error ({_consecutive_errors}/5): {str(e)[:80]} — retrying",
                })
            time.sleep(15)

    # Shutdown
    if _observer:
        _observer.discard()
    blog("INFO", "Bot stopped")


# ═══════════════════════════════════════════════════════════════
#  RUN ONE MARKET — regime gating + observe/skip/trade decision
# ═══════════════════════════════════════════════════════════════

def _run_one_market(client: KalshiClient, cfg: dict) -> bool:
    """Process one market cycle. Returns True if a real trade was placed."""
    import time as _t
    from db import get_conn as _gc

    state = _get_state()

    # Safety: if there's already an active trade, don't start a new one
    existing = state.get("active_trade")
    if existing:
        blog("WARNING", "Active trade exists — skipping this cycle")
        _t.sleep(5)
        return False

    # Cooldown after loss stop
    cooldown = state.get("cooldown_remaining", 0)
    if cooldown > 0:
        blog("INFO", f"Cooldown active: skipping ({cooldown} remaining)")
        _update_state({"cooldown_remaining": cooldown - 1})
        _t.sleep(30)
        return False

    # ── Find market ──
    market = wait_for_next_market(client, cfg)
    if not market:
        st = _get_state().get("status", "")
        if st != "stopped":
            _update_state({"status": "waiting", "status_detail": f"No market found — retrying{_trade_ctx()}"})
            _t.sleep(15)
        return False

    ticker = market["ticker"]
    close_str = market.get("close_time", "")
    close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
    mins_left = client.minutes_until_close(close_str)

    # Already processed this market?
    if state.get("last_ticker") == ticker:
        secs = (close_dt - datetime.now(timezone.utc)).total_seconds()
        if secs > 0:
            _update_state({"status": "waiting",
                          "status_detail": f"Next market in ~{_fmt_wait(secs)}{_trade_ctx()}"})
            _t.sleep(min(secs + 2, 60))
        return False

    blog("INFO", f"Market: {ticker} | {mins_left:.1f}m to close | mode={get_trading_mode(cfg)}")

    now_et = datetime.now(ET)
    market_id = upsert_market(
        ticker=ticker, close_time_utc=close_dt.isoformat(),
        hour_et=now_et.hour, minute_et=now_et.minute,
        day_of_week=now_et.weekday()
    )
    _update_state({"last_ticker": ticker, "active_skip": None, "active_shadow": None})

    # ── Regime check ──
    snapshot = get_latest_regime_snapshot("BTC")
    regime_label = snapshot.get("composite_label", "unknown") if snapshot else "unknown"
    snapshot_id = snapshot.get("id") if snapshot else None

    # Guard against stale regime data
    if snapshot and regime_label != "unknown":
        try:
            snap_time = datetime.fromisoformat(
                snapshot["captured_at"].replace("Z", "+00:00"))
            snap_age_s = (datetime.now(timezone.utc) - snap_time).total_seconds()
            if snap_age_s > 600:
                blog("WARNING", f"Regime snapshot is {snap_age_s/60:.0f}m old — treating as unknown")
                regime_label = "unknown"
        except Exception:
            pass

    # ── FV Model: capture BTC open for this market ──
    global _fv_btc_open, _fv_market_ticker, _fv_last_btc_fetch, _fv_last_btc_price
    if _fair_value_model and ticker != _fv_market_ticker:
        _fv_market_ticker = ticker
        _fv_btc_open = None
        try:
            btc_now = get_live_price("BTC")
            if btc_now and btc_now > 0:
                _fv_btc_open = btc_now
                _fv_last_btc_price = btc_now
                _fv_last_btc_fetch = _t.time()
        except Exception:
            if snapshot and snapshot.get("btc_price"):
                _fv_btc_open = snapshot["btc_price"]
                _fv_last_btc_price = _fv_btc_open

    # ── Strategy key and risk ──
    _active_strategy_key = None
    _strategy_risk = None
    _trading_mode = get_trading_mode(cfg)

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

    gate = check_regime_gate(cfg, regime_label, strategy_risk=_strategy_risk,
                             coarse_regime=compute_coarse_label(
                                 snapshot.get("vol_regime", 3) if snapshot else 3,
                                 snapshot.get("trend_regime", 0) if snapshot else 0,
                                 snapshot.get("volume_regime") if snapshot else None,
                             ))

    # Observe/shadow: don't place real trades
    if _trading_mode in ("observe", "shadow") and gate["should_trade"]:
        gate = {
            "should_trade": False,
            "is_data_collection": False,
            "reason": "Observe-only mode" if _trading_mode == "observe" else "Shadow mode",
            "risk_level": gate["risk_level"],
        }

    blog("INFO", f"Regime: {gate['reason']}")

    # ── Per-regime condition filters ──
    vol_level = snapshot.get("vol_regime") if snapshot else None
    _regime_filters = cfg.get("regime_filters", {})
    if isinstance(_regime_filters, str):
        _regime_filters = json.loads(_regime_filters)

    if gate["should_trade"]:
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
                day_names = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun']
                skip_reason = f"{day_names[now_et.weekday()]} blocked for {regime_label}"

            if skip_reason:
                gate = {
                    "should_trade": False, "is_data_collection": False,
                    "reason": skip_reason, "risk_level": gate["risk_level"],
                }
                blog("INFO", f"Regime filter: {skip_reason}")

    # ── Enrichment fields ──
    trend_level = snapshot.get("trend_regime", 0) if snapshot else 0
    volume_level = snapshot.get("volume_regime", 3) if snapshot else 3
    coarse_regime = compute_coarse_label(vol_level or 3, trend_level, volume_level)
    prev_regime = get_prev_regime_label()
    trade_hour_et = now_et.hour
    trade_day_of_week = now_et.weekday()

    _ctx = _build_trade_context(
        client, cfg, state, market, snapshot, gate,
        coarse_regime, prev_regime, trade_hour_et, trade_day_of_week,
        vol_level=vol_level, close_str=close_str
    )
    _ctx["auto_strategy_key"] = _active_strategy_key
    _ctx["auto_strategy_setup"] = "manual"

    # ── Auto-strategy lookup ──
    auto_strat = None
    if gate["should_trade"] and _trading_mode in ("hybrid", "auto"):
        _as_rejection = {}
        try:
            _as_rec = get_recommendation(
                regime_label, now_et.hour,
                vol_regime=snapshot.get("vol_regime") if snapshot else None,
                trend_regime=snapshot.get("trend_regime") if snapshot else None,
                rejection_info=_as_rejection,
            )
        except Exception:
            _as_rec = None

        _as_min_n = int(cfg.get("auto_strategy_min_samples", 20))
        _as_min_ev = float(cfg.get("auto_strategy_min_ev_c", 0))

        if _as_rec and _as_rec["ev_per_trade_c"] is not None:
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
            blog("INFO", f"Auto-strategy: {_as_rec['strategy_key']} "
                         f"(EV {_as_ev:+.1f}¢, n={_as_n}, from {_as_rec['setup_key']})")
        else:
            _rej_short = _as_rejection.get("short", "no viable strategy")
            gate = {
                "should_trade": False,
                "is_data_collection": False,
                "reason": f"Auto-strategy: {_rej_short}",
                "risk_level": gate["risk_level"],
                "_auto_skip_short": _rej_short,
            }
            blog("INFO", f"Auto-strategy: {_rej_short}")

    # ── SKIP / OBSERVE PATH ──
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
            "market_id": market_id,
            "regime_snapshot_id": snapshot_id,
            "ticker": ticker,
            "side": initial_skip_side,
            "avg_fill_price_c": initial_skip_price,
            "outcome": "skipped",
            "skip_reason": gate["reason"],
            "cheaper_side": initial_skip_side if initial_skip_side != "n/a" else _ctx.get("cheaper_side"),
            "cheaper_side_price_c": initial_skip_price or _ctx.get("cheaper_side_price_c"),
        })
        notify_observed(regime_label, gate["reason"])
        if _observer:
            _observer.mark_action("observed", skip_trade_id,
                                  market_id=market_id,
                                  strategy_key=_active_strategy_key,
                                  regime_label=regime_label)

        secs_to_close = (close_dt - datetime.now(timezone.utc)).total_seconds()
        risk_display = 'extreme' if gate['risk_level'] == 'terrible' else gate['risk_level']
        _as_short = gate.get("_auto_skip_short", "")
        _update_state({
            "status": "waiting",
            "status_detail": f"Observing {regime_label.replace('_', ' ')} — next in ~{_fmt_wait(secs_to_close)}{_trade_ctx()}",
            "active_skip": {
                "reason": gate["reason"],
                "skip_short": _as_short or f"{risk_display} risk — skip",
                "regime_label": regime_label,
                "risk_level": gate["risk_level"],
                "ticker": ticker,
                "close_time": close_dt.isoformat(),
                "trade_id": skip_trade_id,
                "auto_skip_short": _as_short,
            },
        })

        # Shadow trading in shadow/hybrid mode
        if _trading_mode in ("shadow", "hybrid"):
            try:
                _shadow_market = client.get_market(ticker)
                _sh_ctx = {
                    "spread_at_entry_c": abs((_shadow_market.get("yes_ask") or 0)
                                             - (_shadow_market.get("no_ask") or 0)),
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
                if _sh_rec:
                    _sh_side_rule = _sh_rec["side_rule"]
                    if _sh_side_rule in ("yes", "no"):
                        _sh_side = _sh_side_rule
                        _sh_price = _shadow_market.get(f"{_sh_side}_ask") or 0
                    else:
                        _sh_side, _sh_price = client.get_cheaper_side(_shadow_market)
                else:
                    _sh_side, _sh_price = client.get_cheaper_side(_shadow_market)

                _shadow_id = _place_shadow_trade(
                    client, ticker, _sh_side, _sh_price,
                    market_id=market_id, regime_label=regime_label,
                    snapshot_id=snapshot_id, ctx=_sh_ctx,
                    strategy_key=_sh_rec["strategy_key"] if _sh_rec else None,
                )
                if _shadow_id:
                    from plugins.btc_15m.market_db import delete_trades
                    try:
                        delete_trades([skip_trade_id])
                    except Exception:
                        pass
                    _update_state({"active_shadow": {
                        "trade_id": _shadow_id,
                        "side": _sh_side,
                        "price_c": _sh_price,
                        "strategy_key": _sh_rec["strategy_key"] if _sh_rec else None,
                    }})
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

    # ── TRADE PATH ──────────────────────────────────────────

    # Strategy overrides
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

    # ── Entry delay ──
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
        from db import get_pending_commands, complete_command
        target_mins_left = 15 - entry_delay
        current_mins = client.minutes_until_close(close_str)
        wait_secs = max(0, (current_mins - target_mins_left) * 60)
        if wait_secs > 10:
            blog("INFO", f"Entry delay: waiting {wait_secs:.0f}s ({entry_delay}min into market)")
            delay_deadline = time.monotonic() + wait_secs
            while time.monotonic() < delay_deadline:
                rem = delay_deadline - time.monotonic()
                _update_state({
                    "status": "waiting",
                    "status_detail": f"Delaying entry: {_fmt_wait(rem)}{_trade_ctx()}",
                })
                for cmd in get_pending_commands(PLUGIN_ID):
                    cmd_type = cmd["command_type"]
                    cmd_id = cmd["id"]
                    if cmd_type == "stop":
                        _update_state({"auto_trading": 0, "trades_remaining": 0,
                                      "status": "stopped", "status_detail": "Stopped"})
                        complete_command(cmd_id)
                        return False
                    else:
                        complete_command(cmd_id)
                try:
                    poll_live_market(client, cfg)
                except Exception:
                    pass
                time.sleep(min(2, max(0, rem)))

    # ── Price polling for entry ──
    max_entry_c = cfg.get("entry_price_max_c", 42)
    if auto_strat:
        max_entry_c = auto_strat["entry_price_max"]
    min_entry_c = 1
    poll_interval = cfg.get("price_poll_interval", 2)
    fill_wait = 600
    min_mins = 0.5

    _now_wall = time.time()
    _now_mono = time.monotonic()
    _close_wall = close_dt.timestamp()
    _mono_close = _now_mono + (_close_wall - _now_wall)
    price_deadline = min(_now_mono + fill_wait, _mono_close - (min_mins * 60))

    market_label = marketStartTime(close_str)

    if auto_strat:
        _update_state({"status": "searching",
                      "status_detail": f"Auto: {_auto_strat_label} — watching {market_label}{_trade_ctx()}"})
    else:
        _update_state({"status": "searching",
                      "status_detail": f"Watching {market_label} — price ≤ {max_entry_c}c{_trade_ctx()}"})

    side_info = None
    stopped_early = False
    skip_reason = None
    poll_prices_seen = []
    poll_sides_seen = []
    _entry_model_edge = None
    _entry_model_ev = None
    _entry_model_source = None

    from db import get_pending_commands, complete_command

    while time.monotonic() < price_deadline:
        for cmd in get_pending_commands(PLUGIN_ID):
            cmd_type = cmd["command_type"]
            cmd_id = cmd["id"]
            if cmd_type == "stop":
                _update_state({"auto_trading": 0, "trades_remaining": 0,
                              "loss_streak": 0, "status": "stopped",
                              "status_detail": "Stopped"})
                complete_command(cmd_id)
                stopped_early = True
                break
            elif cmd_type == "update_config":
                params = json.loads(cmd.get("parameters") or "{}")
                for k, v in params.items():
                    set_config(f"btc_15m.{k}", v)
                complete_command(cmd_id)
            else:
                complete_command(cmd_id)

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

        if price_c > 0:
            poll_prices_seen.append(price_c)
            if price_c <= max_entry_c:
                poll_sides_seen.append(side)

        # Feed Observatory during polling
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

        if price_c > 0 and price_c >= min_entry_c and price_c <= max_entry_c:
            mins_now = client.minutes_until_close(close_str)
            if mins_now >= min_mins:
                side_info = (side, price_c)
                break

        if client.minutes_until_close(close_str) < min_mins:
            skip_reason = "Too close to market close"
            break

        time.sleep(poll_interval)

    if stopped_early:
        return False

    if not side_info:
        reason = skip_reason or "Price never reached entry range"
        from collections import Counter
        skip_side = Counter(poll_sides_seen).most_common(1)[0][0] if poll_sides_seen else "n/a"
        skip_avg = round(sum(p for p in poll_prices_seen if p <= max_entry_c) /
                         max(1, len([p for p in poll_prices_seen if p <= max_entry_c]))) if poll_prices_seen else None
        price_skip_id = insert_trade({
            **_ctx, "market_id": market_id, "regime_snapshot_id": snapshot_id,
            "ticker": ticker, "side": skip_side, "avg_fill_price_c": skip_avg,
            "outcome": "skipped", "skip_reason": reason,
            "num_price_samples": len(poll_prices_seen),
        })
        notify_observed(regime_label, reason)
        if _observer:
            _observer.mark_action("observed", price_skip_id, market_id=market_id,
                                  strategy_key=_active_strategy_key, regime_label=regime_label)
        secs = (close_dt - datetime.now(timezone.utc)).total_seconds()
        _update_state({"status": "waiting",
                      "status_detail": f"Observing — next in ~{_fmt_wait(secs)}{_trade_ctx()}"})
        _skip_wait_loop(client, cfg, close_dt, price_skip_id, ticker,
                        regime_label, gate.get("risk_level", "unknown"), reason,
                        resolve_inline=True, market_id=market_id)
        return False

    side, entry_price_c = side_info

    # Capture orderbook at entry
    polled_ya = m.get("yes_ask") or 0
    polled_na = m.get("no_ask") or 0
    polled_yb = m.get("yes_bid") or 0
    polled_nb = m.get("no_bid") or 0

    # Side-specific spread
    if side == "yes":
        spread_at_entry_c = max(0, polled_ya - polled_yb) if polled_ya and polled_yb else None
    else:
        spread_at_entry_c = max(0, polled_na - polled_nb) if polled_na and polled_nb else None
    spread_regime_label = score_spread(spread_at_entry_c)

    # Per-regime side filter
    rf_side = _get_regime_filter(regime_label, _regime_filters)
    blocked_sides = rf_side.get("blocked_sides", [])
    if blocked_sides and side in blocked_sides:
        reason = f"{side.upper()} side blocked for {regime_label}"
        blog("INFO", f"Regime filter: {reason}")
        skip_id = insert_trade({**_ctx, "market_id": market_id, "regime_snapshot_id": snapshot_id,
                               "ticker": ticker, "side": side, "outcome": "skipped", "skip_reason": reason})
        _skip_wait_loop(client, cfg, close_dt, skip_id, ticker,
                        regime_label, gate.get("risk_level", "unknown"), reason,
                        resolve_inline=True, market_id=market_id)
        return False

    # Per-regime spread filter
    max_spread = rf_side.get("max_spread_c", 0)
    if max_spread > 0 and spread_at_entry_c is not None and spread_at_entry_c > max_spread:
        reason = f"Spread {spread_at_entry_c}c > {max_spread}c max for {regime_label}"
        blog("INFO", f"Regime filter: {reason}")
        skip_id = insert_trade({**_ctx, "market_id": market_id, "regime_snapshot_id": snapshot_id,
                               "ticker": ticker, "side": side, "outcome": "skipped", "skip_reason": reason})
        _skip_wait_loop(client, cfg, close_dt, skip_id, ticker,
                        regime_label, gate.get("risk_level", "unknown"), reason,
                        resolve_inline=True, market_id=market_id)
        return False

    # ── Bet sizing ──
    bankroll_c = get_effective_bankroll_cents(client, cfg)
    _update_state({"bankroll_cents": client.get_balance_cents()})

    bet_dollars = get_r1_bet_dollars(cfg, bankroll_c / 100,
                                      edge_pct=_entry_model_edge)
    shares = client.calc_shares_for_dollars(bet_dollars, entry_price_c)
    est_cost = shares * entry_price_c / 100 + client.estimate_fees(shares, entry_price_c)

    # Bankroll safety check
    if est_cost > bankroll_c / 100:
        reason = f"Insufficient bankroll: need ~${est_cost:.2f}"
        blog("WARNING", reason)
        _update_state({"auto_trading": 0, "status": "stopped", "status_detail": reason})
        return False

    stability_c = (max(poll_prices_seen) - min(poll_prices_seen)) if len(poll_prices_seen) >= 2 else None
    blog("INFO", f"Plan: {shares} {side.upper()} @ {entry_price_c}c (~${est_cost:.2f})")

    # ── Place buy order ──
    buy_start_time = time.monotonic()
    _update_state({"status": "trading",
                  "status_detail": f"Buying {shares} {side.upper()} @ {entry_price_c}c{_trade_ctx()}"})

    fill_deadline = min(time.time() + fill_wait, close_dt.timestamp() - (min_mins * 60))
    target_shares = shares
    total_filled = 0
    total_cost_cents = 0
    total_fees_cents = 0
    all_order_ids = []
    buy_price_c = entry_price_c
    _buy_error = None

    # Adaptive entry
    adaptive = bool(cfg.get("adaptive_entry", False))
    if adaptive and spread_at_entry_c and spread_at_entry_c >= 4:
        buy_price_c = max(2, entry_price_c - 2)
        blog("INFO", f"Adaptive entry: starting at {buy_price_c}c (ask={entry_price_c}c)")

    buy_attempt = 0
    while total_filled < target_shares and time.time() < fill_deadline:
        remaining_shares = target_shares - total_filled
        buy_attempt += 1

        try:
            buy_resp = client.place_limit_order(ticker, side, remaining_shares, buy_price_c, action="buy")
            buy_order = buy_resp.get("order", {})
            order_id = buy_order.get("order_id")
        except Exception as e:
            blog("ERROR", f"Buy attempt {buy_attempt} failed: {e}")
            _buy_error = str(e)
            break

        if not order_id:
            _buy_error = "No order_id returned"
            break

        all_order_ids.append(order_id)
        status = buy_order.get("status", "")
        blog("INFO", f"Buy attempt {buy_attempt}: {order_id[:12]}… {remaining_shares}x @ {buy_price_c}c status={status}")

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
                _update_state({"status": "trading",
                              "status_detail": f"Buying {remaining_shares} {side.upper()} @ {buy_price_c}c (filling… {_elapsed}s){_trade_ctx()}"})
                if _observer:
                    try:
                        _fm = client.get_market(ticker)
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
                _fc = _order.get("fill_count", 0)
                if _st in ("executed", "canceled", "expired"):
                    fill = client.parse_fill(_order)
                    break
            if fill is None:
                fill = client.parse_fill(client.get_order(order_id))
        else:
            _buy_error = f"Unexpected status: {status}"
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
                    break
            except Exception:
                break
            time.sleep(1)

    if total_filled == 0:
        _outcome = "error" if _buy_error else "no_fill"
        _reason = f"Order error: {_buy_error[:200]}" if _buy_error else "No fill — order cancelled"
        for oid in all_order_ids:
            try:
                client.cancel_order(oid)
            except Exception:
                pass
        insert_trade({**_ctx, "market_id": market_id, "regime_snapshot_id": snapshot_id,
                     "ticker": ticker, "side": side, "entry_price_c": entry_price_c,
                     "outcome": _outcome, "buy_order_id": all_order_ids[0] if all_order_ids else None,
                     "skip_reason": _reason, "bet_size_dollars": bet_dollars})
        _update_state({"status_detail": _reason})
        if _buy_error:
            notify_error(f"Buy failed: {_buy_error[:100]}")
        secs = (close_dt - datetime.now(timezone.utc)).total_seconds()
        if secs > 0:
            time.sleep(secs + 2)
        return False

    fill_count = total_filled
    actual_cost = (total_cost_cents + total_fees_cents) / 100
    avg_price_c = round(total_cost_cents / total_filled) if total_filled > 0 else buy_price_c
    fees_paid = total_fees_cents / 100
    buy_order_id = all_order_ids[0]

    if total_filled < target_shares:
        blog("INFO", f"Partial fill: {total_filled}/{target_shares} — proceeding")

    blog("INFO", f"Filled: {fill_count} @ ~{avg_price_c}c | cost=${actual_cost:.2f} (fees=${fees_paid:.2f})")
    notify_buy(side, fill_count, avg_price_c, actual_cost, regime_label)

    # ── Sell price calculation ──
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
        expected_profit = fill_count * 100 / 100 - actual_cost
        blog("INFO", f"Strategy: hold to expiry — max profit=${expected_profit:.2f}")
    else:
        expected_gross = fill_count * sell_price_c / 100
        expected_profit = expected_gross - actual_cost
        blog("INFO", f"Sell @ {sell_price_c}c → gross=${expected_gross:.2f} profit=${expected_profit:.2f}")

    # ── Dynamic sell from FV model ──
    _dynamic_sell_active = False
    _dynamic_sell_adjustments = 0
    _dynamic_sell_floor = int(cfg.get("dynamic_sell_floor_c", 3))
    if (bool(cfg.get("dynamic_sell_enabled", False))
            and _fair_value_model and _fv_btc_open and _fv_btc_open > 0
            and not is_hold_to_expiry):
        try:
            _fv_now = _fv_last_btc_price or get_live_price("BTC")
            if _fv_now and _fv_now > 0:
                _ds_dist = (_fv_now - _fv_btc_open) / _fv_btc_open * 100
                _ds_secs = max(0, 900 - client.minutes_until_close(close_str) * 60)
                _ds_model = _fair_value_model.get_yes_probability(
                    _ds_dist, _ds_secs,
                    vol_regime=snapshot.get("vol_regime") if snapshot else None)
                _ds_fv = _ds_model["fair_yes_c"] if side == "yes" else _ds_model["fair_no_c"]
                _ds_target = max(int(_ds_fv) - 1, avg_price_c + 1)
                _ds_target = min(_ds_target, 99)
                if _ds_target != sell_price_c:
                    sell_price_c = _ds_target
                    _dynamic_sell_active = True
                    blog("INFO", f"Dynamic sell: FV={_ds_fv:.1f}c → sell@{sell_price_c}c")
        except Exception:
            pass

    # ── Place sell order ──
    sell_order_id = None
    if is_hold_to_expiry:
        blog("INFO", f"Holding {fill_count} {side.upper()} to expiry")
    else:
        try:
            sell_resp = client.place_limit_order(ticker, side, fill_count, sell_price_c, action="sell")
            sell_order = sell_resp.get("order", {})
            sell_order_id = sell_order.get("order_id")
            blog("INFO", f"Sell placed: {sell_order_id and sell_order_id[:12]}… | {fill_count}x @ {sell_price_c}c")
        except Exception as e:
            blog("ERROR", f"Sell order failed: {e} — holding to close")

    # ── Save trade + state ──
    fill_duration_s = round(time.monotonic() - buy_start_time, 1)

    trade_id = insert_trade({
        **_ctx, "market_id": market_id, "regime_snapshot_id": snapshot_id,
        "ticker": ticker, "side": side,
        "entry_price_c": entry_price_c, "entry_time_utc": now_utc(),
        "minutes_before_close": round(client.minutes_until_close(close_str), 2),
        "shares_ordered": shares, "shares_filled": fill_count,
        "actual_cost": round(actual_cost, 2), "fees_paid": round(fees_paid, 2),
        "avg_fill_price_c": avg_price_c, "buy_order_id": buy_order_id,
        "sell_price_c": sell_price_c, "sell_order_id": sell_order_id,
        "outcome": "open", "is_data_collection": 0,
        "price_stability_c": stability_c, "spread_at_entry_c": spread_at_entry_c,
        "spread_regime": spread_regime_label,
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
        "btc_price": get_live_price("BTC"),
        "expected_profit": round(expected_profit, 2),
        "auto_strategy": _auto_strat_label,
        "auto_strategy_ev": auto_strat["ev_per_trade_c"] if auto_strat else None,
        "strategy_key": _active_strategy_key,
        "is_hold_to_expiry": is_hold_to_expiry,
        "model_edge": _entry_model_edge, "model_ev": _entry_model_ev,
        "dynamic_sell": _dynamic_sell_active,
        "dynamic_adjustments": 0, "dynamic_fv": None,
    }

    _update_state({"status": "trading", "status_detail": f"{fill_count} {side.upper()} ~{avg_price_c}c → {'hold' if is_hold_to_expiry else f'sell@{sell_price_c}c'}",
                   "active_trade": active_trade, "bankroll_cents": client.get_balance_cents()})

    if _observer:
        _observer.mark_action("traded", trade_id, market_id=market_id,
                              strategy_key=_active_strategy_key, regime_label=regime_label)

    # ── Monitor until close ──
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
            # Commands mid-trade
            for cmd in get_pending_commands(PLUGIN_ID):
                cmd_type = cmd["command_type"]
                cmd_id = cmd["id"]
                if cmd_type == "stop":
                    _update_state({"auto_trading": 0, "trades_remaining": 0, "loss_streak": 0})
                    active_trade["is_ignored"] = True
                    _update_state({"active_trade": active_trade})
                    if trade_id:
                        update_trade(trade_id, {"is_ignored": 1, "notes": "Stopped mid-trade"})
                    complete_command(cmd_id)
                elif cmd_type == "update_config":
                    params = json.loads(cmd.get("parameters") or "{}")
                    for k, v in params.items():
                        set_config(f"btc_15m.{k}", v)
                    complete_command(cmd_id)
                else:
                    complete_command(cmd_id)

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

                direction = (1 if cur_bid > avg_price_c
                             else -1 if cur_bid < avg_price_c else 0)
                if last_direction and direction and direction != last_direction:
                    osc_count += 1
                if direction:
                    last_direction = direction

                # Feed Observatory
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

                # Check sell fill
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

                mins_rem = max((end_time - time.monotonic()) / 60, 0)

                # Dynamic sell recalculation
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
                            _ds_prob = _fair_value_model.get_yes_probability(
                                _ds_dist, _ds_secs,
                                vol_regime=snapshot.get("vol_regime") if snapshot else None)
                            _ds_fv_now = _ds_prob["fair_yes_c"] if side == "yes" else _ds_prob["fair_no_c"]
                            _ds_new = max(int(_ds_fv_now) - 1, avg_price_c + 1)
                            _ds_new = min(_ds_new, 99)
                            _ds_diff = _ds_new - sell_price_c

                            if abs(_ds_diff) >= _dynamic_sell_floor and sell_progress == 0:
                                try:
                                    client.cancel_order(sell_order_id)
                                    _ds_resp = client.place_limit_order(
                                        ticker, side, fill_count, _ds_new, action="sell")
                                    _ds_oid = _ds_resp.get("order", {}).get("order_id")
                                    if _ds_oid:
                                        sell_order_id = _ds_oid
                                        sell_price_c = _ds_new
                                        active_trade["sell_order_id"] = _ds_oid
                                        active_trade["sell_price_c"] = _ds_new
                                        _dynamic_sell_adjustments += 1
                                        active_trade["dynamic_adjustments"] = _dynamic_sell_adjustments
                                        active_trade["dynamic_fv"] = round(_ds_fv_now, 1)
                                        blog("INFO", f"Dynamic sell #{_dynamic_sell_adjustments}: → {_ds_new}c")
                                except Exception as _ds_err:
                                    blog("WARNING", f"Dynamic sell adjust failed: {_ds_err}")

                            # Dynamic early exit
                            if (_ds_fv_now < avg_price_c - 5 and mins_rem < 7
                                    and cur_bid > 0 and cur_bid < avg_price_c
                                    and sell_progress == 0):
                                try:
                                    client.cancel_order(sell_order_id)
                                    _ds_exit = client.place_limit_order(
                                        ticker, side, fill_count, cur_bid, action="sell")
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
                            blog("INFO", f"Trailing stop: bid {cur_bid}c <= floor {trail_floor}c")
                            try:
                                client.cancel_order(sell_order_id)
                                exit_resp = client.place_limit_order(
                                    ticker, side, fill_count - sell_progress, cur_bid, action="sell")
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
                if (bool(cfg.get("early_exit_ev", False)) and sell_order_id
                        and mins_rem < 2 and cur_bid > 0 and cur_bid < avg_price_c):
                    time_haircut = max(0.7, mins_rem / 2)
                    adjusted_hold_ev = cur_bid * time_haircut
                    if cur_bid > adjusted_hold_ev + 2:
                        blog("INFO", f"Early exit: sell@{cur_bid}c (EV check)")
                        try:
                            client.cancel_order(sell_order_id)
                            exit_resp = client.place_limit_order(
                                ticker, side, fill_count - sell_progress, cur_bid, action="sell")
                            exit_oid = exit_resp.get("order", {}).get("order_id")
                            if exit_oid:
                                sell_order_id = exit_oid
                                sell_price_c = cur_bid
                                active_trade["sell_order_id"] = exit_oid
                                active_trade["sell_price_c"] = cur_bid
                                update_trade(trade_id, {"is_early_exit": 1, "early_exit_price_c": cur_bid})
                        except Exception:
                            pass

                # Write price point (throttled)
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

                # Update active trade for dashboard
                active_trade["current_bid"] = cur_bid
                active_trade["high_water_c"] = high_water_c
                active_trade["sell_progress"] = sell_progress
                active_trade["minutes_left"] = round(mins_rem, 1)
                _update_state({"status": "trading", "active_trade": active_trade})

                # Trade update notification (silent, every 60s)
                if time.monotonic() - _last_trade_notify >= 60 and mins_rem > 0.5:
                    _last_trade_notify = time.monotonic()
                    try:
                        notify_trade_update(
                            side=side, cur_bid=cur_bid, avg_price_c=avg_price_c,
                            sell_price_c=sell_price_c, mins_left=mins_rem,
                            fill_count=fill_count, actual_cost=actual_cost,
                            regime_label=regime_label)
                    except Exception:
                        pass

            except Exception:
                pass

    # ══════════════════════════════════════════════════════════
    #  TRADE RESOLUTION
    # ══════════════════════════════════════════════════════════

    # Final price update for dashboard
    try:
        m = client.get_market(ticker)
        final_bid = m.get(f"{side}_bid", 0) or 0
        active_trade["current_bid"] = final_bid
        active_trade["minutes_left"] = 0
        _update_state({"status": "trading", "active_trade": active_trade})
    except Exception:
        pass

    # Check sell fill
    sell_filled = 0
    if sell_order_id:
        try:
            sell_filled = client.get_order(sell_order_id).get("fill_count", 0)
        except Exception:
            pass

    is_ignored = active_trade.get("is_ignored", False)

    if sell_filled >= fill_count:
        # ── FAST PATH: Sell filled ──
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

        blog("INFO", f"SELL FILLED — {outcome.upper()} | "
                      f"cost=${actual_cost:.2f} gross=${gross:.2f} pnl={fpnl(pnl)}")

        entry_time_str = active_trade.get("entry_time")
        time_to_target_s = None
        if entry_time_str:
            try:
                ent = datetime.fromisoformat(entry_time_str.replace("Z", "+00:00"))
                time_to_target_s = round((datetime.now(timezone.utc) - ent).total_seconds(), 1)
            except Exception:
                pass

        update_trade(trade_id, {
            "outcome": outcome,
            "gross_proceeds": round(gross, 2),
            "pnl": round(pnl, 2),
            "sell_filled": sell_filled,
            "exit_price_c": sell_price_c,
            "exit_time_utc": now_utc(),
            "price_high_water_c": high_water_c,
            "price_low_water_c": low_water_c,
            "pct_progress_toward_target": 100.0,
            "oscillation_count": osc_count,
            "market_result": market_result,
            "btc_price_at_exit": btc_exit,
            "btc_move_pct": btc_move,
            "exit_method": "sell_fill",
            "time_to_target_seconds": time_to_target_s,
        })

    else:
        # ── SLOW PATH: Market expired — need market result ──
        active_trade["resolving"] = True
        active_trade["minutes_left"] = 0
        _update_state({
            "status": "trading",
            "status_detail": f"Resolving {ticker}...",
            "active_trade": active_trade,
        })

        time.sleep(3)

        market_result = None
        for _ in range(10):
            market_result = client.get_market_result(ticker)
            if market_result:
                break
            try:
                rm = client.get_market(ticker)
                resolve_bid = rm.get(f"{side}_bid", 0) or 0
                if resolve_bid > 0:
                    active_trade["current_bid"] = resolve_bid
                    _update_state({"active_trade": active_trade})
            except Exception:
                pass
            time.sleep(3)

        won = (market_result == side) if market_result else False
        gross = client.calc_gross(fill_count, sell_filled, sell_price_c, won)
        pnl = gross - actual_cost
        trade_won = gross > actual_cost
        outcome = "win" if trade_won else "loss"

        final_price = 99 if won else 1
        active_trade["current_bid"] = final_price
        _update_state({"active_trade": active_trade})

        pct_progress = 0.0
        if sell_price_c > avg_price_c:
            pct_progress = ((high_water_c - avg_price_c) /
                            (sell_price_c - avg_price_c)) * 100
            pct_progress = max(0, min(100, pct_progress))

        blog("INFO", f"Result: {outcome.upper()} | "
                      f"market={market_result} side={side} | "
                      f"cost=${actual_cost:.2f} gross=${gross:.2f} pnl={fpnl(pnl)}")

        if market_result:
            update_market_outcome(market_id, market_result)

        btc_exit = get_live_price("BTC")
        btc_entry = active_trade.get("btc_price")
        btc_move = None
        if btc_exit and btc_entry and btc_entry > 0:
            btc_move = round((btc_exit - btc_entry) / btc_entry * 100, 4)

        update_trade(trade_id, {
            "outcome": outcome,
            "gross_proceeds": round(gross, 2),
            "pnl": round(pnl, 2),
            "sell_filled": sell_filled,
            "exit_price_c": sell_price_c if sell_filled > 0 else (100 if won else 0),
            "exit_time_utc": now_utc(),
            "price_high_water_c": high_water_c,
            "price_low_water_c": low_water_c,
            "pct_progress_toward_target": round(pct_progress, 1),
            "oscillation_count": osc_count,
            "market_result": market_result,
            "btc_price_at_exit": btc_exit,
            "btc_move_pct": btc_move,
            "exit_method": "market_expiry",
        })

    # ── Post-trade updates ──
    from db import insert_bankroll_snapshot
    state = _get_state()
    new_bankroll = client.get_balance_cents()

    lifetime_update = {
        "lifetime_pnl": (state.get("lifetime_pnl") or 0) + pnl,
        ("lifetime_wins" if trade_won else "lifetime_losses"):
            (state.get("lifetime_wins" if trade_won else "lifetime_losses") or 0) + 1,
        "bankroll_cents": new_bankroll,
    }
    _update_state(lifetime_update)
    insert_bankroll_snapshot(new_bankroll, trade_id, plugin_id=PLUGIN_ID)

    # Ignored trades: don't update regime stats or notify
    if is_ignored:
        blog("INFO", f"IGNORED trade ({outcome} {fpnl(pnl)}) — not counted in stats")
        _update_state({
            "active_trade": None,
            "status": "searching",
            "status_detail": f"Last: {outcome} ({fpnl(pnl)}) [IGNORED]",
        })
        return True

    # Normal trade outcome handling
    if trade_won:
        _update_state({"loss_streak": 0})
        blog("INFO", f"WIN +${pnl:.2f}")
        notify_trade_result("win", pnl, regime_label)
        _update_state({"status_detail": f"WIN {fpnl(pnl)} in {regime_label}"})
    else:
        loss_streak = (state.get("loss_streak") or 0) + 1
        max_consec = int(cfg.get("max_consecutive_losses", 0) or 0)
        update_data = {"loss_streak": loss_streak}

        if max_consec > 0 and loss_streak >= max_consec:
            cooldown = int(cfg.get("cooldown_after_loss_stop", 0) or 0)
            update_data.update({
                "cooldown_remaining": cooldown,
                "auto_trading": 0, "trades_remaining": 0,
                "status": "stopped",
                "status_detail": (f"Loss stop: {loss_streak} consecutive losses"
                                  + (f" — {cooldown} market cooldown" if cooldown else "")),
            })
            blog("WARNING", f"LOSS STOP: {loss_streak} consecutive losses")
            notify_max_loss(actual_cost, max_consec, cooldown)
        else:
            streak_info = f" (streak {loss_streak})" if loss_streak > 1 else ""
            update_data["status_detail"] = f"LOSS {fpnl(pnl)}{streak_info}"
            blog("INFO", f"LOSS {fpnl(pnl)} | streak={loss_streak}")

        notify_trade_result("loss", pnl, regime_label)
        _update_state(update_data)

    # Update regime stats
    if regime_label:
        _update_regime_with_notify(regime_label)

    # Post-trade summary for dashboard
    summary = {
        "trade_id": trade_id, "ticker": ticker, "side": side,
        "outcome": outcome, "pnl": round(pnl, 2),
        "actual_cost": round(actual_cost, 2), "gross": round(gross, 2),
        "avg_price_c": avg_price_c, "sell_price_c": sell_price_c,
        "fill_count": fill_count, "sell_filled": sell_filled,
        "high_water_c": high_water_c, "market_result": market_result,
        "regime_label": regime_label, "risk_level": gate["risk_level"],
    }
    _update_state({
        "active_trade": None,
        "last_completed_trade": summary,
    })

    # Backfill skipped trades from this session
    try:
        _backfill_skipped_results(client, limit=10)
    except Exception:
        pass

    return True


# ═══════════════════════════════════════════════════════════════
#  SKIPPED TRADE BACKFILL
# ═══════════════════════════════════════════════════════════════

def _backfill_skipped_results(client: KalshiClient, limit: int = 20):
    """Fetch market results for skipped trades missing outcomes."""
    trades = get_skipped_trades_needing_result(limit=limit)
    if not trades:
        return 0
    filled = 0
    for t in trades:
        try:
            result = client.get_market_result(t["ticker"])
            if result:
                backfill_skipped_result(t["id"], result)
                if t.get("market_id"):
                    update_market_outcome(t["market_id"], result)
                filled += 1
        except Exception:
            pass
    if filled > 0:
        blog("DEBUG", f"Backfilled {filled} skipped trade results")
    return filled


# ═══════════════════════════════════════════════════════════════
#  ORPHAN TRADE RESOLUTION (crash recovery)
# ═══════════════════════════════════════════════════════════════

def _resolve_stale_open_trades(client: KalshiClient):
    """Resolve trades left in 'open' outcome from a previous crash."""
    try:
        stale_trades = [t for t in get_recent_trades(100)
                        if t.get("outcome") == "open"]
        if not stale_trades:
            return

        blog("INFO", f"Found {len(stale_trades)} stale open trade(s) — resolving")
        for st in stale_trades:
            tid = st["id"]
            sticker = st.get("ticker", "")
            sside = st.get("side", "yes")
            sfilled = st.get("shares_filled", 0)
            scost = st.get("actual_cost", 0)
            ssell_price = st.get("sell_price_c") or 0
            ssell_oid = st.get("sell_order_id")

            sell_filled = 0
            if ssell_oid:
                try:
                    sell_filled = client.get_order(ssell_oid).get("fill_count", 0)
                except Exception:
                    pass

            market_result = None
            for _ in range(8):
                market_result = client.get_market_result(sticker)
                if market_result:
                    break
                time.sleep(2)

            if market_result:
                won = (market_result == sside)
                gross = client.calc_gross(sfilled, sell_filled, ssell_price, won)
                pnl = gross - scost
                outcome = "win" if gross > scost else "loss"
                blog("INFO", f"  Resolved trade {tid} ({sticker}): "
                              f"{outcome.upper()} {fpnl(pnl)}")
                update_trade(tid, {
                    "outcome": outcome,
                    "gross_proceeds": round(gross, 2),
                    "pnl": round(pnl, 2),
                    "sell_filled": sell_filled,
                    "exit_time_utc": now_utc(),
                    "market_result": market_result,
                    "exit_method": "market_expiry",
                    "notes": "Resolved on startup",
                })
            else:
                blog("WARNING", f"  No market result for {sticker} — leaving for backfill")
    except Exception as e:
        blog("ERROR", f"Error cleaning stale trades: {e}")
