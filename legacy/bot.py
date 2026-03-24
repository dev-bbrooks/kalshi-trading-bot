"""
bot.py — Main trading engine.
Regime gating, trade execution, sell target management, cash-out.
"""

import json
import math
import time
import logging
import traceback
import threading
from datetime import datetime, timezone, timedelta

from config import (
    KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH,
    ET, CT, DEFAULT_BOT_CONFIG, KALSHI_FEE_RATE,
)
from kalshi import KalshiClient
from regime import regime_worker, compute_snapshot, get_live_btc_price, compute_coarse_label, score_spread
from strategy import MarketObserver, backfill_observation_results, get_recommendation, BtcFairValueModel
from db import (
    init_db, get_bot_state, update_bot_state, clear_active_trade,
    get_pending_commands, complete_command, cancel_command, flush_pending_commands,
    upsert_market, update_market_outcome,
    insert_trade, update_trade, get_trade, get_recent_trades,
    insert_price_point, insert_bankroll_snapshot,
    get_latest_regime_snapshot, get_regime_risk, get_strategy_risk,
    update_regime_stats, recompute_all_stats,
    get_config, set_config, get_all_config,
    insert_log, now_utc,
    insert_live_price,
    get_prev_regime_label,
    get_skipped_trades_needing_result, backfill_skipped_result,
    refresh_all_coarse_regime_stats, refresh_all_hourly_stats,
    get_conn, rows_to_list,
)

# Push notifications (graceful if not set up)
try:
    from push import (notify_trade_result, notify_max_loss,
                      notify_bankroll_limit, notify_error,
                      notify_session_target, notify_cash_out,
                      notify_buy, notify_observed,
                      notify_auto_lock,
                      notify_new_regime, notify_regime_classified,
                      notify_trade_update,
                      notify_profit_goal, notify_early_exit,
                      notify_session_loss_limit, notify_rolling_wr_breaker)
except ImportError:
    def _noop(*a, **k): pass
    notify_trade_result = notify_max_loss = notify_bankroll_limit = _noop
    notify_error = notify_session_target = notify_cash_out = _noop
    notify_buy = notify_observed = notify_auto_lock = _noop
    notify_new_regime = notify_regime_classified = _noop
    notify_trade_update = notify_early_exit = _noop
    notify_profit_goal = _noop
    notify_session_loss_limit = notify_rolling_wr_breaker = _noop

log = logging.getLogger("bot")


# ═══════════════════════════════════════════════════════════════
#  DB LOGGER — writes to both Python logger and log_entries table
# ═══════════════════════════════════════════════════════════════

def blog(level: str, msg: str, category: str = "bot"):
    """Log to both Python logger and database for dashboard display."""
    getattr(log, level.lower(), log.info)(msg)
    try:
        insert_log(level.upper(), msg, category)
    except Exception:
        pass  # Never crash the bot for a log write


def fpnl(val: float) -> str:
    """Format P&L with sign before dollar: +$5.00 or -$5.00"""
    return f"+${val:.2f}" if val >= 0 else f"-${abs(val):.2f}"


def _update_regime_with_notify(regime_label: str):
    """Update regime stats and send notifications for new regimes
    and risk level changes."""
    if not regime_label:
        return
    try:
        result = update_regime_stats(regime_label)
        if not result:
            return

        # New regime discovered
        if result.get("is_new") and result["total"] > 0:
            notify_new_regime(regime_label, result["total"])

        # Risk level changed (including graduating from unknown)
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
    """
    After a skip wait loop finishes (market is closed), immediately
    fetch the market result and update the trade record.
    Called inline — no backfill needed.
    """
    if not ticker or ticker == "n/a":
        return

    try:
        # Wait for Kalshi to settle the market (can take 30-60s)
        time.sleep(5)

        market_result = None
        for attempt in range(12):  # Up to ~65 seconds total
            market_result = client.get_market_result(ticker)
            if market_result:
                break
            time.sleep(5)

        if not market_result:
            blog("INFO", f"Skip {trade_id}: no market result after 60s, backfill will handle")
            return

        update_trade(trade_id, {"market_result": market_result})
        blog("INFO", f"Skip {trade_id}: market result {market_result.upper()}")

        # Update markets table
        if market_id:
            try:
                update_market_outcome(market_id, market_result)
            except Exception:
                pass

    except Exception as e:
        blog("WARNING", f"Inline skip resolve error: {e}")


def _save_prev_session():
    """Snapshot current session stats before resetting.
    Only overwrites if current session has meaningful data (wins+losses > 0),
    so accidental resets followed by empty sessions don't erase the real data."""
    state = get_bot_state()
    sw = state.get("session_wins", 0) or 0
    sl = state.get("session_losses", 0) or 0
    if sw + sl > 0:
        prev = {
            "wins": sw, "losses": sl,
            "pnl": state.get("session_pnl", 0) or 0,
            "skips": state.get("session_skips", 0) or 0,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        update_bot_state({"_prev_session": json.dumps(prev)})
        blog("INFO", f"Saved prev session: {sw}W-{sl}L, {fpnl(prev['pnl'])}")
    else:
        blog("INFO", "No meaningful session stats to save (0 trades)")


def _cleanup_logs(retention_days: int = 7):
    """Clean up log file and log_entries table to prevent unbounded growth.
    Called every 6 hours from the main loop.

    1. bot.log file: if > 5MB, keeps last 20,000 lines (~2MB)
    2. log_entries table: deletes rows older than retention_days
    3. push_log table: already self-cleans (capped at 500 rows)
    """
    import os
    from config import LOG_FILE

    # ── Log file rotation ──
    try:
        if os.path.isfile(LOG_FILE):
            size_mb = os.path.getsize(LOG_FILE) / (1024 * 1024)
            if size_mb > 5:
                # Read last 20,000 lines, rewrite file
                with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                keep = lines[-20_000:]
                with open(LOG_FILE, "w", encoding="utf-8") as f:
                    f.write(f"[log rotated at {now_utc()} — kept last {len(keep)} of {len(lines)} lines]\n")
                    f.writelines(keep)
                blog("INFO", f"Log file rotated: {size_mb:.1f}MB → ~{len(keep)*100/1024/1024:.1f}MB "
                              f"({len(lines) - len(keep)} lines trimmed)")
    except Exception as e:
        log.warning(f"Log file cleanup error: {e}")

    # ── log_entries table cleanup ──
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


# ═══════════════════════════════════════════════════════════════
#  CONFIG HELPERS
# ═══════════════════════════════════════════════════════════════

def load_config() -> dict:
    """Load config from DB, falling back to defaults."""
    stored = get_all_config()
    cfg = {**DEFAULT_BOT_CONFIG}
    for k, v in stored.items():
        if k in cfg:
            cfg[k] = v
    return cfg


def save_config_updates(updates: dict):
    for k, v in updates.items():
        set_config(k, v)


def get_trading_mode(cfg: dict) -> str:
    """Get trading mode from config, with fallback derivation from legacy booleans.

    Modes: observe, shadow, hybrid, auto, manual
    """
    mode = cfg.get("trading_mode", "")
    if mode in ("observe", "shadow", "hybrid", "auto", "manual"):
        return mode
    # Derive from legacy booleans (pre-migration configs)
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
#  EFFECTIVE BANKROLL & BANKROLL GUARDS
# ═══════════════════════════════════════════════════════════════

def get_effective_bankroll_cents(client: KalshiClient, cfg: dict) -> int:
    """Cash balance minus locked bankroll, in cents."""
    raw = client.get_balance_cents()
    locked_c = int(float(cfg.get("locked_bankroll", 0)) * 100)
    return max(raw - locked_c, 0)


def check_auto_lock(client: KalshiClient, cfg: dict):
    """Auto-lock profits when effective bankroll reaches threshold."""
    if not cfg.get("auto_lock_enabled", False):
        return
    threshold = cfg.get("auto_lock_threshold", 0)
    amount = cfg.get("auto_lock_amount", 0)
    if threshold <= 0 or amount <= 0:
        return

    eff = get_effective_bankroll_cents(client, cfg) / 100
    if eff >= threshold:
        random_mode = cfg.get("auto_lock_random", False)

        if random_mode:
            import random
            lock_it = random.choice([True, False])
        else:
            lock_it = True

        if lock_it:
            # LOCK: move profits to locked bankroll
            current_locked = float(cfg.get("locked_bankroll", 0))
            new_locked = current_locked + amount
            set_config("locked_bankroll", new_locked)
            cfg["locked_bankroll"] = new_locked
            new_eff = get_effective_bankroll_cents(client, cfg) / 100
            action = "locked" if not random_mode else "locked (random)"
            blog("INFO", f"AUTO-LOCK: {action} ${amount:.2f} (total locked: ${new_locked:.2f}, "
                          f"effective: ${new_eff:.2f})")
            notify_auto_lock(amount, new_locked, new_eff, action="lock")
            # Check profit goal (only notify once per goal)
            profit_goal = float(cfg.get("profit_goal", 0) or 0)
            if profit_goal > 0 and new_locked >= profit_goal:
                if not cfg.get("profit_goal_reached", False):
                    set_config("profit_goal_reached", True)
                    cfg["profit_goal_reached"] = True
                    blog("INFO", f"🎯 PROFIT GOAL REACHED: ${new_locked:.2f} ≥ ${profit_goal:.2f}")
                    notify_profit_goal(new_locked, profit_goal)
        else:
            # KEEP: raise threshold instead — bankroll grows
            new_threshold = threshold + amount
            set_config("auto_lock_threshold", new_threshold)
            cfg["auto_lock_threshold"] = new_threshold
            blog("INFO", f"AUTO-LOCK RANDOM → KEEP: Bankroll increase! "
                          f"Threshold raised ${threshold:.2f} → ${new_threshold:.2f}")
            notify_auto_lock(amount, float(cfg.get("locked_bankroll", 0)), eff,
                             action="keep", new_threshold=new_threshold)


def check_bankroll_limits(client: KalshiClient, cfg: dict) -> tuple:
    """
    Check if effective bankroll is within min/max bounds.
    Returns (ok, reason). If not ok, stops trading.
    """
    eff = get_effective_bankroll_cents(client, cfg) / 100
    bmin = float(cfg.get("bankroll_min", 0))
    bmax = float(cfg.get("bankroll_max", 0))

    if bmin > 0 and eff < bmin:
        reason = f"Effective bankroll ${eff:.2f} below minimum ${bmin:.2f}"
        blog("WARNING", reason + " — stopping")
        notify_bankroll_limit(reason)
        update_bot_state({
            "auto_trading": 0, "trades_remaining": 0,
            "status": "stopped", "status_detail": reason,
        })
        return False, reason

    if bmax > 0 and eff > bmax:
        reason = f"Effective bankroll ${eff:.2f} above maximum ${bmax:.2f}"
        blog("WARNING", reason + " — stopping")
        notify_bankroll_limit(reason)
        update_bot_state({
            "auto_trading": 0, "trades_remaining": 0,
            "status": "stopped", "status_detail": reason,
        })
        return False, reason

    return True, ""


def check_session_profit_target(cfg: dict, state: dict) -> tuple:
    """Check if session profit target has been reached. Returns (ok, reason)."""
    target = float(cfg.get("session_profit_target", 0))
    if target <= 0:
        return True, ""
    spnl = state.get("session_pnl", 0)
    if spnl >= target:
        reason = f"Session profit target reached: ${spnl:.2f} ≥ ${target:.2f}"
        blog("INFO", reason + " — stopping")
        notify_session_target(spnl, target)
        update_bot_state({
            "auto_trading": 0, "trades_remaining": 0,
            "status": "stopped", "status_detail": reason,
        })
        return False, reason
    return True, ""


def check_session_loss_limit(cfg: dict, state: dict) -> tuple:
    """Check if session loss limit has been exceeded. Returns (ok, reason).
    Catches slow bleeds that consecutive-loss stops miss (e.g. W-L-L-W-L-L pattern)."""
    limit = float(cfg.get("session_loss_limit", 0))
    if limit <= 0:
        return True, ""
    spnl = state.get("session_pnl", 0) or 0
    if spnl <= -limit:
        reason = f"Session loss limit: ${spnl:.2f} ≤ -${limit:.2f}"
        blog("WARNING", reason + " — stopping")
        notify_session_loss_limit(spnl, limit)
        update_bot_state({
            "auto_trading": 0, "trades_remaining": 0,
            "status": "stopped", "status_detail": reason,
        })
        return False, reason
    return True, ""


def check_rolling_win_rate(cfg: dict) -> tuple:
    """Check if recent win rate is above the configured floor. Returns (ok, reason).
    Catches slow bleeds where the bot isn't hitting streak stops but is steadily losing."""
    window = int(cfg.get("rolling_wr_window", 0) or 0)
    floor = float(cfg.get("rolling_wr_floor", 0) or 0)
    if window <= 0 or floor <= 0:
        return True, ""

    recent = get_recent_trades(window)
    # Only count completed, non-ignored trades
    completed = [t for t in recent
                 if t.get("outcome") in ("win", "loss")
                 and not t.get("is_ignored")]

    if len(completed) < window:
        # Not enough trades yet — don't trigger
        return True, ""

    wins = sum(1 for t in completed[:window] if t["outcome"] == "win")
    wr = (wins / window) * 100

    if wr < floor:
        reason = f"Rolling win rate {wr:.0f}% < {floor:.0f}% floor (last {window} trades)"
        blog("WARNING", reason + " — stopping")
        notify_rolling_wr_breaker(wr, floor, window)
        update_bot_state({
            "auto_trading": 0, "trades_remaining": 0,
            "status": "stopped", "status_detail": reason,
        })
        return False, reason
    return True, ""


# ═══════════════════════════════════════════════════════════════
#  REGIME RISK GATING
# ═══════════════════════════════════════════════════════════════

RISK_ORDER = {"low": 0, "moderate": 1, "high": 2, "terrible": 3}


def build_strategy_key(cfg: dict) -> str:
    """
    Map bot settings to a Strategy Observatory strategy key.
    Format: side:timing:entry_max:sell_target
    """
    # Side rule
    side_rule = cfg.get("strategy_side", "cheaper")
    if side_rule not in ("cheaper", "yes", "no", "model"):
        side_rule = "cheaper"

    # Timing: map entry_delay_minutes
    delay = float(cfg.get("entry_delay_minutes", 0))
    if delay >= 8:
        timing = "late"
    elif delay >= 4:
        timing = "mid"
    else:
        timing = "early"

    # Entry max: snap to nearest 5¢, clamp to simulation range (5-95)
    max_price = int(cfg.get("entry_price_max_c", 45))
    entry_max = round(max_price / 5) * 5
    entry_max = max(5, min(entry_max, 95))

    # Sell target: from sell_target_c config (absolute) or hold
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
    """Strip modifiers (thin_, squeeze_, _accel, _decel) to get the base regime label.
    e.g. 'thin_trending_down_strong_accel' → 'trending_down_strong'"""
    base = label
    for prefix in ("thin_", "squeeze_"):
        if base.startswith(prefix):
            base = base[len(prefix):]
    for suffix in ("_accel", "_decel"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
    return base


def _get_regime_filter(regime_label: str, filters: dict) -> dict:
    """Look up per-regime filters with base-label fallback.
    If no filters for exact label, tries the base label (strip modifiers)."""
    rf = filters.get(regime_label, {})
    if not rf:
        base = _base_regime_label(regime_label)
        if base != regime_label:
            rf = filters.get(base, {})
    return rf


def check_regime_gate(cfg: dict, regime_label: str,
                      strategy_risk: dict = None,
                      coarse_regime: str = None) -> dict:
    """
    Determine if we should trade based on strategy risk in this regime.

    Uses strategy_risk (from Strategy Observatory) if provided.
    Falls back to regime-level stats only if strategy_risk is None.

    Override priority: fine-grained override → coarse override → risk level action

    Config keys:
      risk_level_actions: {low:'normal', moderate:'normal', ...}
      regime_overrides: {regime_label: 'normal'|'skip', coarse_label: 'normal'|'skip'}

    Returns dict:
      should_trade: bool
      is_data_collection: bool
      reason: str
      risk_level: str
      strategy_risk: dict (the full strategy risk info)
    """
    if not regime_label:
        regime_label = "unknown"

    # Use strategy-based risk if available, fall back to regime stats
    if strategy_risk:
        risk_level = strategy_risk.get("risk_level", "unknown")
        win_rate = strategy_risk.get("win_rate", 0)
        sample_n = strategy_risk.get("sample_size", 0)
        ev = strategy_risk.get("ev_per_trade_c")
        setup = strategy_risk.get("setup_key", "")
        strat_key = strategy_risk.get("strategy_key", "")
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

    # Trade-all bypass — auto-strategy controls everything, no regime filtering
    if cfg.get("auto_strat_trade_all", False):
        blog("INFO", f"Trade-all active for '{regime_label}' — bypassing risk gate")
        return {
            "should_trade": True,
            "is_data_collection": False,
            "reason": f"Regime {info_str} — trade-all",
            "risk_level": risk_level,
            "strategy_risk": strategy_risk,
        }

    # Quick-trade regime bypass — exclusive whitelist mode
    # When active: selected regimes bypass all filtering, everything else skips
    qt_regimes = cfg.get("quick_trade_regimes", [])
    if isinstance(qt_regimes, str):
        qt_regimes = json.loads(qt_regimes)
    if qt_regimes:
        base = _base_regime_label(regime_label)
        if regime_label in qt_regimes or base in qt_regimes:
            blog("INFO", f"Quick-trade active for '{regime_label}' — bypassing risk gate")
            return {
                "should_trade": True,
                "is_data_collection": False,
                "reason": f"Regime {info_str} — quick-trade",
                "risk_level": risk_level,
                "strategy_risk": strategy_risk,
            }
        else:
            blog("INFO", f"Quick-trade active — '{regime_label}' not in whitelist, skipping")
            return {
                "should_trade": False,
                "is_data_collection": False,
                "reason": f"Regime {info_str} — not in quick-trade whitelist",
                "risk_level": risk_level,
                "strategy_risk": strategy_risk,
            }

    # Determine action: fine override → coarse override → risk level default
    overrides = cfg.get("regime_overrides", {})
    if isinstance(overrides, str):
        overrides = json.loads(overrides)

    risk_actions = cfg.get("risk_level_actions", {})
    if isinstance(risk_actions, str):
        risk_actions = json.loads(risk_actions)

    # Default actions if not configured
    defaults = {"low": "normal", "moderate": "normal", "high": "normal",
                "terrible": "skip", "unknown": "skip"}

    # Override priority: exact fine → base label (strip modifiers) → coarse → risk level
    action = overrides.get(regime_label, "default")
    if action == "_custom":
        action = "default"

    # Strip modifiers to find base label match
    if action == "default":
        base = _base_regime_label(regime_label)
        if base != regime_label:
            action = overrides.get(base, "default")
            if action == "_custom":
                action = "default"

    # Fall back to coarse regime override
    if action == "default" and coarse_regime:
        action = overrides.get(coarse_regime, "default")
        if action == "_custom":
            action = "default"
    # Fall back to risk level action
    if action == "default":
        action = risk_actions.get(risk_level, defaults.get(risk_level, "normal"))

    # "data" is now treated as "skip" — skip tracking handles data collection
    if action == "data":
        action = "skip"
    # "_custom" is a dashboard display state, not a real action — fall through
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
#  BET SIZING & BANKROLL SAFETY
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

def get_r1_bet_dollars(cfg: dict, bankroll: float, edge_pct: float = None,
                       **kwargs) -> float:
    """
    Compute bet size based on bet mode.

    Modes:
      flat: fixed dollar amount
      percent: fixed % of bankroll
      edge_scaled: base bet scaled by FV model edge using configurable tiers

    Returns: bet size in dollars
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
        multiplier = 0.5  # Default for unknown/no edge
        if edge_pct is not None:
            for tier in sorted(tiers, key=lambda t: t["min_edge"], reverse=True):
                if edge_pct >= tier["min_edge"]:
                    multiplier = tier["multiplier"]
                    break
        base = base * multiplier
    else:
        # Default: flat
        base = cfg.get("bet_size", 50.0)

    return base


def check_bankroll_safety(client: KalshiClient, cfg: dict, state: dict,
                          bet_dollars: float, entry_price_c: int) -> tuple:
    """
    Check if we can afford this bet. Returns (safe, reason).
    If unsafe, stops auto-trading.
    """
    bankroll_c = get_effective_bankroll_cents(client, cfg)
    bankroll = bankroll_c / 100
    shares = client.calc_shares_for_dollars(bet_dollars, entry_price_c)
    est_cost = shares * entry_price_c / 100 + client.estimate_fees(shares, entry_price_c)

    if est_cost > bankroll:
        reason = (f"Insufficient bankroll: need ~${est_cost:.2f} but have "
                  f"${bankroll:.2f}. Stopping.")
        blog("WARNING", reason)
        update_bot_state({
            "auto_trading": 0,
            "trades_remaining": 0,
            "status": "stopped",
            "status_detail": f"Bankroll insufficient",
            "bankroll_cents": bankroll_c,
        })
        return False, reason

    return True, ""


# ═══════════════════════════════════════════════════════════════
#  LIVE MARKET MONITOR
# ═══════════════════════════════════════════════════════════════

# Module-level market observer (initialized in main())
_observer = None

# BTC Fair Value Model (initialized in main())
_fair_value_model = None

# BTC open price tracking for current market (reset on new ticker)
_fv_btc_open = None          # BTC price at market open
_fv_market_ticker = None     # Ticker we captured the open for
_fv_last_btc_fetch = 0       # Throttle BTC price fetches (every 10s)
_fv_last_btc_price = None    # Cached current BTC price

# When True, the bot will skip the current market and wait for the next fresh one.
# Set True on startup, on start command, and on deploy/restart.
# Cleared after successfully finding a fresh market to trade/observe.
_skip_first_market = True


def poll_live_market(client: KalshiClient, cfg: dict):
    """
    Poll current market info and write to bot_state for dashboard display.
    Called even when not actively trading.
    Also feeds the Strategy Observatory with price snapshots.
    """
    try:
        market = client.find_current_market()
        if not market:
            update_bot_state({"live_market": None})
            return

        ticker = market["ticker"]
        close_str = market.get("close_time", "")
        mins_left = client.minutes_until_close(close_str) if close_str else 0

        side, price_c = client.get_cheaper_side(market)

        # Get regime info
        snapshot = get_latest_regime_snapshot()
        regime_label = snapshot.get("composite_label", "unknown") if snapshot else "unknown"

        # Guard against stale regime data
        if snapshot and regime_label != "unknown":
            try:
                snap_time = datetime.fromisoformat(
                    snapshot["captured_at"].replace("Z", "+00:00"))
                snap_age_s = (datetime.now(timezone.utc) - snap_time).total_seconds()
                if snap_age_s > 600:
                    regime_label = "unknown"
            except Exception:
                pass

        # Strategy-specific risk from Observatory
        _poll_risk_level = "unknown"
        _poll_win_rate = 0
        _poll_sample_n = 0
        _poll_strat_risk = None
        try:
            _poll_cfg = load_config()
            _poll_strat_key = build_strategy_key(_poll_cfg)
            _poll_strat_risk = get_strategy_risk(regime_label, _poll_strat_key)
            # Model side fallback to cheaper variant
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

        # Fair Value Model: compute edge for idle display
        global _fv_btc_open, _fv_market_ticker, _fv_last_btc_fetch, _fv_last_btc_price
        if _fair_value_model:
            try:
                # Capture BTC open if this is a new market
                if ticker != _fv_market_ticker:
                    _fv_market_ticker = ticker
                    _btc_snap = snapshot.get("btc_price") if snapshot else None
                    if _btc_snap and _btc_snap > 0:
                        _fv_btc_open = _btc_snap
                        _fv_last_btc_price = _btc_snap
                        _fv_last_btc_fetch = time.time()

                # Refresh BTC price (throttled)
                _now_fv = time.time()
                if _fv_btc_open and _now_fv - _fv_last_btc_fetch >= 10:
                    _btc_f = get_live_btc_price()
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

        update_bot_state({"live_market": live_data})

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
            _observer.tick(ticker, close_str, market_data, snapshot, _poll_strat_risk)

        # Record price for dashboard chart backfill
        try:
            insert_live_price(ticker, market.get("yes_ask"), market.get("no_ask"),
                              market.get("yes_bid"), market.get("no_bid"))
        except Exception:
            pass

    except Exception as e:
        log.debug(f"Live market poll error: {e}")




def execute_cash_out(client: KalshiClient, state: dict) -> dict:
    """
    Emergency exit. Cancel existing sell, place aggressive sells.
    Checks for cancel_cash_out flag between attempts.
    """
    active = state.get("active_trade")
    if not active:
        return {"error": "No active trade"}

    ticker = active["ticker"]
    side = active["side"]
    fill_count = active["fill_count"]
    trade_id = active.get("trade_id")
    sell_order_id = active.get("sell_order_id")
    sell_price_c = active.get("sell_price_c") or 0
    actual_cost = active.get("actual_cost", 0)

    blog("WARNING", f"CASH OUT initiated for {ticker}")
    update_bot_state({
        "status": "trading",
        "status_detail": "CASHING OUT — selling aggressively...",
        "cashing_out": 1,
        "cancel_cash_out": 0,
    })

    # Step 1: Cancel existing sell order
    original_sell_price_c = sell_price_c
    if sell_order_id:
        existing = client.get_order(sell_order_id)
        already_sold = existing.get("fill_count", 0)
        client.cancel_order(sell_order_id)
        remaining = fill_count - already_sold
        blog("INFO", f"Cancelled sell order. Already sold: {already_sold}. "
                      f"Remaining: {remaining}")
    else:
        remaining = fill_count
        already_sold = 0

    if remaining <= 0:
        blog("INFO", "All contracts already sold")
        gross = already_sold * sell_price_c / 100
        update_bot_state({"cashing_out": 0, "cancel_cash_out": 0})
        _finalize_cash_out(client, trade_id, active, gross, fill_count, 0)
        return {"sold": fill_count, "remaining": 0}

    # Step 2: Aggressively sell — start at bid, widen spread exponentially
    total_sold = already_sold
    total_gross_cents = already_sold * sell_price_c
    attempts = 0
    max_attempts = 10
    spread_drop = 0
    cancelled = False

    while remaining > 0 and attempts < max_attempts:
        # Check for cancel
        cancel_flag = get_bot_state().get("cancel_cash_out", 0)
        if cancel_flag:
            blog("INFO", "Cash out CANCELLED by user")
            cancelled = True
            break

        try:
            update_bot_state({
                "status": "trading",
                "status_detail": f"CASHING OUT — sold {total_sold}/{fill_count} ({remaining} left, attempt {attempts+1})",
            })

            m = client.get_market(ticker)
            if side == "yes":
                bid = m.get("yes_bid", 0) or 0
            else:
                bid = m.get("no_bid", 0) or 0

            sell_at = max(bid - spread_drop, 2)

            blog("INFO", f"Cash out attempt {attempts + 1}: "
                         f"selling {remaining}x @ {sell_at}c (bid={bid}c, drop={spread_drop})")

            resp = client.place_limit_order(
                ticker, side, remaining, sell_at, action="sell"
            )
            order = resp.get("order", {})
            order_id = order.get("order_id")

            if not order_id:
                attempts += 1
                spread_drop += attempts
                time.sleep(1)
                continue

            time.sleep(2)
            order_status = client.get_order(order_id)
            filled = order_status.get("fill_count", 0)

            if filled > 0:
                total_sold += filled
                remaining -= filled
                total_gross_cents += filled * sell_at
                blog("INFO", f"Cash out filled {filled}. "
                             f"Total sold: {total_sold}/{fill_count}")
                update_bot_state({
                    "status_detail": f"CASHING OUT — sold {total_sold}/{fill_count}",
                })

            if remaining > 0:
                client.cancel_order(order_id)

            if filled == 0:
                attempts += 1
                spread_drop += attempts
                time.sleep(1)

        except Exception as e:
            blog("ERROR", f"Cash out attempt error: {e}")
            attempts += 1
            time.sleep(1)

    update_bot_state({"cashing_out": 0, "cancel_cash_out": 0})

    if cancelled:
        cashout_sold = total_sold - already_sold  # Shares WE sold (not original sell)
        if cashout_sold > 0:
            # Partial cash out — leave remaining as active trade for user
            blog("INFO", f"Cash out cancelled with {cashout_sold} sold, {remaining} remaining")
            # Place new sell for remaining shares
            new_sell_id = None
            try:
                new_sell = client.place_limit_order(
                    ticker, side, remaining, original_sell_price_c, action="sell"
                )
                new_sell_id = new_sell.get("order", {}).get("order_id")
            except Exception as e:
                blog("ERROR", f"Failed to place sell for remaining: {e}")

            active["fill_count"] = remaining
            active["sell_order_id"] = new_sell_id
            active["sell_price_c"] = original_sell_price_c
            active["is_ignored"] = True
            update_bot_state({
                "status": "trading",
                "status_detail": f"Cash out partial: sold {cashout_sold}, {remaining} left — holding to close",
                "active_trade": active,
            })
            # Update trade record
            if trade_id:
                update_trade(trade_id, {
                    "is_ignored": 1,
                    "notes": f"Partial cash out: sold {cashout_sold} at avg ~{total_gross_cents//max(cashout_sold,1)}c, {remaining} remaining",
                })
        else:
            # No shares sold — restore original sell and continue
            blog("INFO", "Cash out cancelled, no shares sold. Restoring original sell.")
            try:
                new_sell = client.place_limit_order(
                    ticker, side, fill_count, original_sell_price_c, action="sell"
                )
                new_sell_id = new_sell.get("order", {}).get("order_id")
                if new_sell_id:
                    active["sell_order_id"] = new_sell_id
                    active["sell_price_c"] = original_sell_price_c
                    update_bot_state({
                        "status": "trading",
                        "status_detail": f"Cash out cancelled — sell restored @ {original_sell_price_c}c",
                        "active_trade": active,
                    })
                    blog("INFO", f"Restored sell: {fill_count}x @ {original_sell_price_c}c")
            except Exception as e:
                blog("ERROR", f"Failed to restore sell: {e}")
                update_bot_state({
                    "status": "trading",
                    "status_detail": "Cash out cancelled — sell restore failed, holding to close",
                })
        return {"cancelled": True, "sold": total_sold, "remaining": remaining}

    if remaining > 0:
        blog("WARNING", f"Cash out incomplete: {remaining} unsold. "
                        f"Will resolve at market close.")

    gross = total_gross_cents / 100
    _finalize_cash_out(client, trade_id, active, gross, total_sold, remaining)
    return {"sold": total_sold, "remaining": remaining}


def _finalize_cash_out(client, trade_id: int, active: dict,
                       gross: float, sold: int, remaining: int):
    """Update trade record and clear active trade after cash out."""
    actual_cost = active.get("actual_cost", 0)
    pnl = gross - actual_cost

    if trade_id:
        update_trade(trade_id, {
            "outcome": "cashed_out",
            "sell_filled": sold,
            "gross_proceeds": round(gross, 2),
            "pnl": round(pnl, 2),
            "exit_time_utc": now_utc(),
            "exit_method": "cash_out",
            "notes": f"Cashed out: sold {sold}, {remaining} remaining",
        })

    summary = {
        "trade_id": trade_id,
        "ticker": active.get("ticker", ""),
        "side": active.get("side", ""),
        "outcome": "cashed_out",
        "pnl": round(pnl, 2),
        "actual_cost": round(actual_cost, 2),
        "gross": round(gross, 2),
        "avg_price_c": active.get("avg_price_c") or 0,
        "sell_price_c": active.get("sell_price_c") or 0,
        "fill_count": active.get("fill_count") or 0,
        "sell_filled": sold,
        "high_water_c": active.get("high_water_c", 0),
        "market_result": None,
        
    }

    clear_active_trade()
    # Update session/lifetime stats
    state = get_bot_state()
    trade_won = pnl > 0
    sess_key = "session_wins" if trade_won else "session_losses"
    lt_key = "lifetime_wins" if trade_won else "lifetime_losses"
    new_bal = client.get_balance_cents()
    update_bot_state({
        "status": "stopped",
        "status_detail": f"Cashed out: {fpnl(pnl)} (sold {sold} contracts)",
        "last_completed_trade": summary,
        "bankroll_cents": new_bal,
        "auto_trading": 0,
        "trades_remaining": 0,
        "loss_streak": 0,
        sess_key: (state.get(sess_key) or 0) + 1,
        "session_pnl": (state.get("session_pnl") or 0) + pnl,
        lt_key: (state.get(lt_key) or 0) + 1,
        "lifetime_pnl": (state.get("lifetime_pnl") or 0) + pnl,
    })

    blog("INFO", f"Cash out complete: gross=${gross:.2f} pnl={fpnl(pnl)}")
    insert_bankroll_snapshot(new_bal, trade_id)
    notify_cash_out(pnl)

    # Refresh live market so dashboard transitions to monitor card
    try:
        from config import DEFAULT_BOT_CONFIG
        poll_live_market(client, DEFAULT_BOT_CONFIG)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
#  ORPHAN TRADE MONITOR
# ═══════════════════════════════════════════════════════════════






def monitor_orphan_trade(client: KalshiClient, cfg: dict):
    """
    Monitor an orphaned trade (from restart or stop). Called from main loop.
    Handles price updates, sell fill checking, market close.
    """
    state = get_bot_state()
    at = state.get("active_trade")
    if not at:
        return

    ticker = at["ticker"]
    side = at["side"]
    close_str = at.get("close_time", "")
    trade_id = at.get("trade_id")
    fill_count = at["fill_count"]
    sell_order_id = at.get("sell_order_id")

    if not close_str:
        return

    close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
    secs_left = (close_dt - datetime.now(timezone.utc)).total_seconds()

    # Check if market has closed
    if secs_left < -5:
        _resolve_orphan_trade(client, at)
        return

    # Poll market
    try:
        m = client.get_market(ticker)
        if side == "yes":
            cur_bid = m.get("yes_bid", 0) or 0
        else:
            cur_bid = m.get("no_bid", 0) or 0

        # Check sell fill
        sell_progress = 0
        if sell_order_id:
            sell_status = client.get_order(sell_order_id)
            sell_progress = sell_status.get("fill_count", 0)
            if sell_progress >= fill_count:
                blog("INFO", f"Sell filled! {sell_progress}/{fill_count}")
                _resolve_orphan_trade(client, at, sell_filled=sell_progress)
                return

        at["current_bid"] = cur_bid
        at["sell_progress"] = sell_progress
        at["minutes_left"] = round(max(secs_left / 60, 0), 1)
        if cur_bid > at.get("high_water_c", 0):
            at["high_water_c"] = cur_bid
        update_bot_state({"status": "trading", "active_trade": at})

    except Exception:
        pass


def _resolve_orphan_trade(client: KalshiClient, at: dict, sell_filled: int = None):
    """Resolve a completed orphan trade and store summary."""
    ticker = at["ticker"]
    side = at["side"]
    trade_id = at.get("trade_id")
    fill_count = at["fill_count"]
    actual_cost = at["actual_cost"]
    sell_price_c = at.get("sell_price_c") or 0

    # Guard against double-resolution: if we crashed after updating stats
    # but before clear_active_trade(), the trade record already has an outcome.
    # Skip stat updates to prevent double-counting session/lifetime numbers.
    if trade_id:
        existing = get_trade(trade_id)
        if existing and existing.get("outcome") in ("win", "loss"):
            blog("INFO", f"Orphan trade {trade_id} already resolved — clearing active trade")
            clear_active_trade()
            return

    if sell_filled is None:
        sell_filled = 0
        if at.get("sell_order_id"):
            sell_filled = client.get_order(at["sell_order_id"]).get("fill_count", 0)

    # Get market result — Kalshi can take 30-60s to settle after close
    market_result = None
    for _ in range(12):
        market_result = client.get_market_result(ticker)
        if market_result:
            break
        time.sleep(3)

    won = (market_result == side) if market_result else False
    gross = client.calc_gross(fill_count, sell_filled, sell_price_c, won)
    pnl = gross - actual_cost
    outcome = "win" if gross > actual_cost else "loss"

    blog("INFO", f"Orphan trade resolved: {outcome.upper()} {fpnl(pnl)} "
                  f"(market={market_result}, sold={sell_filled}/{fill_count})")

    if trade_id:
        update_trade(trade_id, {
            "outcome": outcome,
            "gross_proceeds": round(gross, 2),
            "pnl": round(pnl, 2),
            "sell_price_c": sell_price_c,
            "sell_filled": sell_filled,
            "exit_time_utc": now_utc(),
            "market_result": market_result,
            "exit_method": "market_expiry",
            "notes": "Resolved trade",
        })

    # Update regime stats
    regime_label = at.get("regime_label")
    if regime_label and regime_label != "unknown":
        _update_regime_with_notify(regime_label)

    # Update session/lifetime stats
    state = get_bot_state()
    trade_won = gross > actual_cost
    sess_key = "session_wins" if trade_won else "session_losses"
    lt_key = "lifetime_wins" if trade_won else "lifetime_losses"
    sess_update = {
        sess_key: (state.get(sess_key) or 0) + 1,
        "session_pnl": (state.get("session_pnl") or 0) + pnl,
        lt_key: (state.get(lt_key) or 0) + 1,
        "lifetime_pnl": (state.get("lifetime_pnl") or 0) + pnl,
    }
    update_bot_state(sess_update)

    # Update loss streak for non-ignored trades
    is_ignored = at.get("is_ignored", False)
    if not is_ignored:
        if trade_won:
            update_bot_state({"loss_streak": 0})
            blog("INFO", f"Orphan WIN {fpnl(pnl)}")
            notify_trade_result("win", pnl, regime_label or "")
        else:
            loss_streak = (state.get("loss_streak") or 0) + 1
            update_bot_state({"loss_streak": loss_streak})
            blog("INFO", f"Orphan LOSS {fpnl(pnl)} (streak {loss_streak})")
            notify_trade_result("loss", pnl, regime_label or "")

    # Store summary for dashboard
    summary = {
        "trade_id": trade_id,
        "ticker": ticker,
        "side": side,
        "outcome": outcome,
        "pnl": round(pnl, 2),
        "actual_cost": round(actual_cost, 2),
        "gross": round(gross, 2),
        "avg_price_c": at.get("avg_price_c", 0),
        "sell_price_c": sell_price_c,
        "fill_count": fill_count,
        "sell_filled": sell_filled,
        "high_water_c": at.get("high_water_c", 0),
        "market_result": market_result,
        
    }

    clear_active_trade()
    new_bal = client.get_balance_cents()
    insert_bankroll_snapshot(new_bal, trade_id)
    update_bot_state({
        "status": "stopped",
        "status_detail": f"{outcome.upper()}: {fpnl(pnl)}",
        "last_completed_trade": summary,
        "bankroll_cents": new_bal,
    })


# ═══════════════════════════════════════════════════════════════
#  COMMAND PROCESSOR
# ═══════════════════════════════════════════════════════════════

def process_commands(client: KalshiClient, cfg: dict) -> dict:
    """Process pending commands from the dashboard."""
    for cmd in get_pending_commands():
        cmd_type = cmd["command_type"]
        params = json.loads(cmd.get("parameters") or "{}")
        cmd_id = cmd["id"]

        try:
            if cmd_type == "start":
                global _skip_first_market
                mode = params.get("mode", "continuous")
                count = params.get("count", 1)

                # Decide: new session or resume?
                # If bot stopped recently (within 20 min) AND has existing stats, resume.
                # Otherwise, start fresh session.
                state_now = get_bot_state()
                stopped_at = state_now.get("session_stopped_at", "")
                has_stats = (state_now.get("session_wins", 0) + state_now.get("session_losses", 0)) > 0
                is_resume = False
                if has_stats and stopped_at:
                    try:
                        stopped_dt = datetime.fromisoformat(stopped_at.replace("Z", "+00:00"))
                        mins_since_stop = (datetime.now(timezone.utc) - stopped_dt).total_seconds() / 60
                        if mins_since_stop < 20:
                            is_resume = True
                            blog("INFO", f"Resuming session (stopped {mins_since_stop:.0f}m ago)")
                    except Exception:
                        pass

                base = {"auto_trading": 1, "auto_trading_since": now_utc(),
                        "last_completed_trade": None,
                        "status": "searching",
                        "loss_streak": 0,
                        "session_stopped_at": ""}

                # If there's an active trade marked ignored from the stop, restore it
                # (real money is at stake — let it ride)
                at_now = state_now.get("active_trade")
                if at_now and (at_now.get("is_ignored")):
                    at_now["is_ignored"] = False
                    base["active_trade"] = at_now
                    base["status"] = "trading"
                    base["status_detail"] = f"Resumed — monitoring active trade"
                    tid = at_now.get("trade_id")
                    if tid:
                        update_trade(tid, {"is_ignored": 0, "notes": "Restored on resume"})
                    blog("INFO", f"Restored ignored trade to active on resume")
                else:
                    # No active trade — skip current market, wait for next fresh one
                    _skip_first_market = True
                    if _observer:
                        _observer.discard()
                    blog("INFO", "Will wait for next fresh market before trading")

                if not is_resume:
                    # New session — reset stats
                    _save_prev_session()
                    base.update({
                        "session_wins": 0, "session_losses": 0,
                        "session_pnl": 0, "session_skips": 0,
                    })
                if mode == "single":
                    base["trades_remaining"] = 1
                    base["status_detail"] = "Starting — single trade"
                elif mode == "count":
                    base["trades_remaining"] = count
                    base["status_detail"] = f"Starting — {count} trades"
                else:
                    base["trades_remaining"] = 0
                    base["status_detail"] = "Starting — continuous"
                update_bot_state(base)
                complete_command(cmd_id, {"mode": mode})
                cfg = load_config()
                if is_resume:
                    blog("INFO", f"Session resumed: mode={mode}")
                else:
                    blog("INFO", f"New session started: mode={mode} — session stats reset")

            elif cmd_type == "stop":
                state = get_bot_state()
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
                # Discard incomplete observatory data for this market
                if _observer:
                    _observer.discard()
                update_bot_state(updates)
                complete_command(cmd_id)
                blog("INFO", "Stopped by user" + (" — active trade kept as ignored" if at else ""))

            elif cmd_type == "cash_out":
                state = get_bot_state()
                result = execute_cash_out(client, state)
                complete_command(cmd_id, result)

            elif cmd_type == "reset_streak":
                update_bot_state({
                    "loss_streak": 0,
                    "cooldown_remaining": 0,
                })
                complete_command(cmd_id)
                blog("INFO", "Loss streak and cooldown reset")

            elif cmd_type == "reset_session":
                _save_prev_session()
                update_bot_state({
                    "session_wins": 0,
                    "session_losses": 0,
                    "session_pnl": 0,
                    "session_skips": 0,
                    "session_stopped_at": "",
                    "last_completed_trade": None,
                })
                complete_command(cmd_id)
                blog("INFO", "Session stats reset")

            elif cmd_type == "recover_session":
                state_r = get_bot_state()
                prev_raw = state_r.get("_prev_session", "")
                if prev_raw:
                    prev = json.loads(prev_raw) if isinstance(prev_raw, str) else prev_raw
                    update_bot_state({
                        "session_wins": (state_r.get("session_wins", 0) or 0) + (prev.get("wins", 0) or 0),
                        "session_losses": (state_r.get("session_losses", 0) or 0) + (prev.get("losses", 0) or 0),
                        "session_pnl": (state_r.get("session_pnl", 0) or 0) + (prev.get("pnl", 0) or 0),
                        "session_skips": (state_r.get("session_skips", 0) or 0) + (prev.get("skips", 0) or 0),
                        "_prev_session": "",
                    })
                    blog("INFO", f"Recovered prev session: +{prev.get('wins',0)}W +{prev.get('losses',0)}L {fpnl(prev.get('pnl',0))}")
                    complete_command(cmd_id, {"recovered": True, "prev": prev})
                else:
                    complete_command(cmd_id, {"recovered": False, "reason": "No previous session saved"})
                    blog("INFO", "No previous session to recover")

            elif cmd_type == "lock_bankroll":
                amount = float(params.get("amount", 0))
                current = float(cfg.get("locked_bankroll", 0))
                new_locked = max(0, current + amount)
                set_config("locked_bankroll", new_locked)
                cfg["locked_bankroll"] = new_locked
                complete_command(cmd_id, {"locked": new_locked})
                blog("INFO", f"Locked bankroll: ${new_locked:.2f} "
                              f"({'+'if amount >= 0 else ''}{amount:.2f})")

            elif cmd_type == "update_config":
                for k, v in params.items():
                    set_config(k, v)
                    if k in cfg:
                        cfg[k] = v
                complete_command(cmd_id, {"updated": list(params.keys())})
                blog("INFO", f"Config updated: {list(params.keys())}")

            elif cmd_type == "dismiss_summary":
                update_bot_state({"last_completed_trade": None})
                complete_command(cmd_id)

            elif cmd_type == "run_validation_test":
                # Run heavy validation tests in bot process (no nginx timeout)
                test_id = params.get("test_id", "")
                blog("INFO", f"Running validation test: {test_id}")
                _vt_result = None
                if test_id == "walkforward":
                    from strategy import run_walkforward_selection_test
                    _vt_result = run_walkforward_selection_test(
                        n_folds=int(params.get("folds", 5)))
                    # Store verdict for get_recommendation gate
                    verdict = _vt_result.get("verdict", "insufficient_data")
                    if verdict == "selection_works":
                        set_config("_selection_test_result", "passed")
                    elif verdict == "selection_unreliable":
                        set_config("_selection_test_result", "failed")
                elif test_id == "permutation":
                    from strategy import run_permutation_test
                    _vt_result = run_permutation_test(
                        n_permutations=int(params.get("n", 500)))
                elif test_id == "persistence":
                    from strategy import test_strategy_persistence
                    _vt_result = test_strategy_persistence()
                else:
                    _vt_result = {"error": f"Unknown test: {test_id}"}
                # Store result for dashboard polling
                set_config(f"_validation_result_{test_id}",
                           json.dumps(_vt_result))
                complete_command(cmd_id, {"test_id": test_id, "done": True})
                blog("INFO", f"Validation test {test_id} complete: "
                              f"{_vt_result.get('verdict', _vt_result.get('error', '?'))}")

            else:
                cancel_command(cmd_id, f"Unknown command: {cmd_type}")

        except Exception as e:
            tb = traceback.format_exc()
            blog("ERROR", f"Command error ({cmd_type}): {e}")
            blog("ERROR", f"Traceback:\n{tb}")
            cancel_command(cmd_id, str(e))

    return cfg


# ═══════════════════════════════════════════════════════════════
#  TRADE EXECUTION
# ═══════════════════════════════════════════════════════════════

def wait_for_next_market(client: KalshiClient, cfg: dict) -> dict | None:
    """
    Wait until a fresh market is available that we haven't traded yet.
    Strategy: find current market, wait for it to close, then grab the new one.
    Updates live_market in dashboard while waiting.

    If _skip_first_market is True (after start/restart), always skips the
    current market and waits for the next one to ensure clean data.
    """
    global _skip_first_market
    state = get_bot_state()
    last_ticker = state.get("last_ticker")

    # First, see what's currently live
    current = client.find_current_market()

    if current:
        ticker = current["ticker"]
        close_str = current.get("close_time", "")

        # After start/restart: always skip current market, wait for next fresh one
        if _skip_first_market and ticker != last_ticker:
            mins_left = client.minutes_until_close(close_str) if close_str else 0
            blog("INFO", f"Skipping mid-market {ticker} ({mins_left:.1f}m left) — "
                          f"waiting for next fresh market after start/restart")
            # Fall through to the wait-for-close logic below

        elif ticker != last_ticker:
            # We haven't traded this market — but only enter if it's FRESH
            # (near the start, not a stale market we're joining mid-way)
            mins_left = client.minutes_until_close(close_str) if close_str else 0
            if mins_left > 12:  # Must have 12+ min = within first 3 min of market
                blog("INFO", f"Fresh market available: {ticker} ({mins_left:.1f}m left)")
                _skip_first_market = False
                return current
            else:
                blog("INFO", f"Market {ticker} has only {mins_left:.1f}m left — "
                              f"waiting for next one")

        # Current market already traded or too late — wait for it to close
        secs_left = client.minutes_until_close(close_str) * 60 if close_str else 30
        if secs_left > 0:
            blog("INFO", f"Waiting {secs_left:.0f}s for {ticker} to close")
            deadline = time.monotonic() + secs_left + 5

            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                ctx = _trade_ctx()
                # Preserve skip context if we're waiting for a skipped market
                skip_info = get_bot_state().get("active_skip")
                if skip_info and skip_info.get("ticker") == ticker:
                    skip_reason = skip_info.get("reason", "")
                    short_reason = skip_reason[:50] if skip_reason else "regime filter"
                    detail = f"Observing: {short_reason} — next in ~{_fmt_wait(remaining)}{ctx}"
                else:
                    detail = f"Next market in ~{_fmt_wait(remaining)}{ctx}"
                update_bot_state({
                    "status": "waiting",
                    "status_detail": detail,
                })

                # Process ALL commands during wait (not just stop)
                for cmd in get_pending_commands():
                    cmd_type = cmd["command_type"]
                    cmd_id = cmd["id"]
                    params = json.loads(cmd.get("parameters") or "{}")

                    if cmd_type == "stop":
                        update_bot_state({
                            "auto_trading": 0, "trades_remaining": 0,
                            "status": "stopped",
                            "status_detail": "Stopped",
                                                    })
                        complete_command(cmd_id)
                        blog("INFO", "Stop received while waiting — streak reset")
                        return None

                    if cmd_type == "start":
                        mode = params.get("mode", "continuous")
                        count = params.get("count", 1)
                        # Always a resume when start is received during wait
                        base = {"auto_trading": 1, "status": "waiting",
                                                                                                "session_stopped_at": ""}
                        if mode == "single":
                            base["trades_remaining"] = 1
                            base["status_detail"] = "Restarted — single trade — waiting"
                        elif mode == "count":
                            base["trades_remaining"] = count
                            base["status_detail"] = f"Restarted — {count} trades — waiting"
                        else:
                            base["trades_remaining"] = 0
                            base["status_detail"] = "Restarted — continuous — waiting"
                        update_bot_state(base)
                        complete_command(cmd_id, {"mode": mode})
                        blog("INFO", f"Start received during wait: mode={mode}")
                        return None  # Exit wait, restart requested

                    if cmd_type == "cash_out":
                        state_now = get_bot_state()
                        result = execute_cash_out(client, state_now)
                        complete_command(cmd_id, result)
                        return None

                    if cmd_type == "update_config":
                        for k, v in params.items():
                            set_config(k, v)
                            if k in cfg: cfg[k] = v
                        complete_command(cmd_id)

                    else:
                        complete_command(cmd_id)

                # Update live market for dashboard
                try:
                    poll_live_market(client, cfg)
                except Exception:
                    pass

                # Refresh balance
                try:
                    update_bot_state({"bankroll_cents": client.get_balance_cents()})
                except Exception:
                    pass

                time.sleep(min(2, max(0, remaining)))

    # Current market should be closed now — poll for the new one
    blog("INFO", "Polling for new market...")
    ctx = _trade_ctx()
    update_bot_state({"status": "searching", "status_detail": f"Starting — finding market{ctx}"})

    for attempt in range(30):
        # Process commands while polling
        for cmd in get_pending_commands():
            cmd_type = cmd["command_type"]
            cmd_id = cmd["id"]
            params = json.loads(cmd.get("parameters") or "{}")
            if cmd_type == "cash_out":
                state_now = get_bot_state()
                result = execute_cash_out(client, state_now)
                complete_command(cmd_id, result)
                return None
            elif cmd_type == "stop":
                update_bot_state({"auto_trading": 0, "trades_remaining": 0,
                                  "status": "stopped", "status_detail": "Stopped",
                                  })
                complete_command(cmd_id)
                return None
            elif cmd_type == "update_config":
                for k, v in params.items():
                    set_config(k, v)
                    if k in cfg: cfg[k] = v
                complete_command(cmd_id)
            else:
                complete_command(cmd_id)

        new_market = client.find_current_market()
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


def _trade_ctx() -> str:
    """Build context suffix for status messages: trades remaining info."""
    state = get_bot_state()
    parts = []
    rem = state.get("trades_remaining", 0)
    if rem and rem > 0:
        parts.append(f"{rem} trade{'s' if rem != 1 else ''} left")
    return (" · " + " · ".join(parts)) if parts else ""


def _fmt_wait(secs: float) -> str:
    """Format seconds as Xm Xs."""
    s = max(0, int(secs))
    return f"{s // 60}m {s % 60:02d}s"


def _get_shadow_strategy(regime_label: str, hour_et: int = None,
                         vol_regime: int = None, trend_regime: int = None) -> dict | None:
    """
    Get the best strategy for shadow trading — NO validation gates.
    
    Unlike get_recommendation(), this returns whatever has the highest
    weighted EV regardless of FDR, OOS, fee resilience, or sample minimums.
    Used for shadow trades that validate the Observatory's picks with real fills.
    
    Fallback: regime → coarse_regime → global:all
    """
    from db import get_conn
    candidates = []
    if regime_label and regime_label != "unknown":
        candidates.append(f"regime:{regime_label}")
    if vol_regime is not None and trend_regime is not None:
        try:
            from regime import compute_coarse_label
            coarse = compute_coarse_label(int(vol_regime), int(trend_regime))
            candidates.append(f"coarse_regime:{coarse}")
        except Exception:
            pass
    candidates.append("global:all")

    with get_conn() as c:
        for setup in candidates:
            row = c.execute("""
                SELECT strategy_key, side_rule, exit_rule, entry_time_rule,
                       entry_price_max, ev_per_trade_c, weighted_ev_c,
                       win_rate, sample_size, setup_key
                FROM strategy_results
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


def _place_shadow_trade(client, ticker: str, side: str, price_c: int,
                        market_id: int = None, regime_label: str = None,
                        snapshot_id: int = None, ctx: dict = None,
                        strategy_key: str = None) -> int | None:
    """
    Place a 1-contract shadow trade for execution data collection.

    Shadow trades buy exactly 1 contract using the Observatory's recommended
    side and entry price, hold to expiry, and record actual fill data for
    comparison against simulation assumptions. Strategy key is stored so
    Observatory predictions can be validated against real outcomes.

    Returns trade_id if placed, None if failed.
    """
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

        # Wait for fill (max 60 seconds — shadow trades exist to collect
        # execution data, so we want high fill rates even on thin markets)
        fill = None
        status = order.get("status", "")
        if status == "executed":
            fill = client.parse_fill(order)
        elif status == "resting":
            deadline = time.time() + 60
            fill = client.poll_until_filled(order_id, 1, deadline, interval=3)
        else:
            # Unexpected status — cancel and bail
            client.cancel_order(order_id)
            return None

        fill_count = fill.get("fill_count", 0) if fill else 0
        fill_time = time.time()
        latency_ms = int((fill_time - decision_time) * 1000)

        if fill_count == 0:
            # Didn't fill — cancel and record the attempt
            try:
                client.cancel_order(order_id)
            except Exception:
                pass
            blog("DEBUG", f"Shadow trade: no fill on {ticker} {side}@{price_c}¢")
            return None

        # Successfully filled — record as shadow trade
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
            "outcome": "open",  # Backfill resolves to win/loss after market closes
            "is_shadow": 1,
            "is_ignored": 1,  # Excluded from regular stats; shadow analysis queries separately
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


def _skip_wait_loop(client, cfg, close_dt, skip_trade_id, ticker,
                    regime_label, risk_level, reason,
                    track_side=False, resolve_inline=False,
                    initial_cheaper_side=None, market_id=None) -> bool:
    """
    Wait for a skipped market to close while processing commands and
    updating the dashboard. Consolidates the repeated skip-wait pattern.

    Args:
        track_side: if True, tracks cheaper side during wait for trade record enrichment
        resolve_inline: if True, resolves market result immediately after market close
        initial_cheaper_side: fallback side for inline resolve if tracking not available

    Returns True if stopped early (caller should return False from run_trade).
    """
    secs = (close_dt - datetime.now(timezone.utc)).total_seconds()
    if secs <= 0:
        # Market already closed — still attempt inline resolve
        if resolve_inline:
            _resolve_skip_inline(client, skip_trade_id, ticker,
                                 market_id=market_id)
        return False

    deadline = time.monotonic() + secs + 2
    cheaper_prices = []
    cheaper_sides = []
    stopped_early = False

    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        ctx = _trade_ctx()
        update_bot_state({
            "status_detail": f"Observing {regime_label.replace('_', ' ')} — next in ~{_fmt_wait(remaining)}{ctx}",
        })

        # Process commands
        for cmd in get_pending_commands():
            cmd_type = cmd["command_type"]
            cmd_id = cmd["id"]
            params = json.loads(cmd.get("parameters") or "{}")
            if cmd_type == "stop":
                update_bot_state({
                    "auto_trading": 0, "trades_remaining": 0,
                    "status": "stopped", "status_detail": "Stopped",
                })
                complete_command(cmd_id)
                stopped_early = True
                break
            elif cmd_type == "update_config":
                for k, v in params.items():
                    set_config(k, v)
                    if k in cfg:
                        cfg[k] = v
                complete_command(cmd_id)
            else:
                complete_command(cmd_id)

        if stopped_early:
            break

        # Track cheaper side for trade record enrichment
        if track_side:
            try:
                m_opp = client.get_market(ticker)
                csid, cprice = client.get_cheaper_side(m_opp)
                if cprice > 0:
                    cheaper_prices.append(cprice)
                    cheaper_sides.append(csid)
            except Exception:
                pass

        # Keep live market and balance current
        try:
            poll_live_market(client, cfg)
        except Exception:
            pass
        try:
            update_bot_state({"bankroll_cents": client.get_balance_cents()})
        except Exception:
            pass

        time.sleep(min(2, max(0, remaining)))

    # After loop: update skip trade with cheaper side data
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

    # Resolve market result inline
    if resolve_inline and not stopped_early:
        _resolve_skip_inline(client, skip_trade_id, ticker,
                             market_id=market_id)

    return stopped_early


def _build_trade_context(client, cfg, state, market, snapshot, gate,
                         coarse_regime, prev_regime, hour_et, day_of_week,
                         vol_level=None, close_str=None):
    """Build common context dict for all trade inserts (real and skipped).
    Contains every field we can know at decision time."""
    btc_price = get_live_btc_price()
    eff_bankroll_c = get_effective_bankroll_cents(client, cfg)
    session_total = (state.get("session_wins") or 0) + (state.get("session_losses") or 0)

    # Spread from market
    spread_c = None
    cheaper_side = None
    cheaper_side_price_c = None
    yes_ask = None
    no_ask = None
    yes_bid = None
    no_bid = None
    kalshi_volume = None
    kalshi_oi = None
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

    # Minutes before close
    mins_before_close = None
    if close_str:
        try:
            mins_before_close = round(client.minutes_until_close(close_str), 2)
        except Exception:
            pass

    return {
        # Regime classification
        "regime_label": snapshot.get("composite_label", "unknown") if snapshot else "unknown",
        "regime_risk_level": gate.get("risk_level", "unknown") if gate else "unknown",
        "regime_confidence": snapshot.get("regime_confidence") if snapshot else None,
        "vol_regime": vol_level,
        "trend_regime": snapshot.get("trend_regime") if snapshot else None,
        "volume_regime": snapshot.get("volume_regime") if snapshot else None,
        "coarse_regime": coarse_regime,
        "prev_regime_label": prev_regime,
        # Regime signals (denormalized from snapshot)
        "trend_direction": snapshot.get("trend_direction") if snapshot else None,
        "trend_strength": snapshot.get("trend_strength") if snapshot else None,
        "bollinger_squeeze": snapshot.get("bollinger_squeeze", 0) if snapshot else 0,
        "trend_acceleration": snapshot.get("trend_acceleration") if snapshot else None,
        "volume_spike": snapshot.get("volume_spike", 0) if snapshot else 0,
        # BTC technicals (denormalized from snapshot)
        "bollinger_width": snapshot.get("bollinger_width_15m") if snapshot else None,
        "atr_15m": snapshot.get("atr_15m") if snapshot else None,
        "realized_vol": snapshot.get("realized_vol_15m") if snapshot else None,
        "ema_slope_15m": snapshot.get("ema_slope_15m") if snapshot else None,
        "ema_slope_1h": snapshot.get("ema_slope_1h") if snapshot else None,
        "btc_return_15m": snapshot.get("btc_return_15m") if snapshot else None,
        "btc_return_1h": snapshot.get("btc_return_1h") if snapshot else None,
        "btc_return_4h": snapshot.get("btc_return_4h") if snapshot else None,
        # BTC price
        "btc_price_at_entry": btc_price,
        # Kalshi market orderbook
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
        # Session context
        "session_trade_num": session_total + 1,
        "session_pnl_at_entry": state.get("session_pnl") or 0,
        "session_wins_at_entry": state.get("session_wins") or 0,
        "session_losses_at_entry": state.get("session_losses") or 0,
        # Bankroll
        "bankroll_at_entry_c": eff_bankroll_c,
        # Timing
        "hour_et": hour_et,
        "day_of_week": day_of_week,
        "minute_et": datetime.now(ET).minute,
        "minutes_before_close": mins_before_close,
        "market_close_time_utc": close_str,
        "trade_mode": cfg.get("trade_mode", "continuous"),
        "entry_delay_minutes": cfg.get("entry_delay_minutes", 0),
        # BTC distance from market open — actual distance when FV model has the
        # open price, falls back to btc_return_15m proxy otherwise
        "btc_distance_pct": (
            round((_fv_last_btc_price - _fv_btc_open) / _fv_btc_open * 100, 4)
            if _fv_btc_open and _fv_btc_open > 0 and _fv_last_btc_price
            else (snapshot.get("btc_return_15m") if snapshot else None)
        ),
    }



def run_trade(client: KalshiClient, cfg: dict) -> bool:
    """
    Execute one trade. Returns True if a trade was placed.
    """
    state = get_bot_state()

    # Safety: if there's already an active trade, don't start a new one
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
                # Still open — wait for it
                while secs > -5:
                    monitor_orphan_trade(client, cfg)
                    time.sleep(2)
                    secs = (close_dt - datetime.now(timezone.utc)).total_seconds()
                    if not get_bot_state().get("active_trade"):
                        break
                if get_bot_state().get("active_trade"):
                    _resolve_orphan_trade(client, get_bot_state()["active_trade"])
        else:
            clear_active_trade()
        return False

    # ── 0. Pre-trade checks ───────────────────────────────────
    # Cooldown after loss stop
    cooldown = state.get("cooldown_remaining", 0)
    if cooldown > 0:
        blog("INFO", f"Cooldown active: skipping this market ({cooldown} remaining)")
        market = client.find_current_market()
        if market:
            ticker = market["ticker"]
            close_str = market.get("close_time", "")
            update_bot_state({
                "cooldown_remaining": cooldown - 1,
                "last_ticker": ticker,
            })
            secs = client.minutes_until_close(close_str) * 60 if close_str else 30
            if secs > 0:
                cd_deadline = time.monotonic() + min(secs + 2, 300)
                while time.monotonic() < cd_deadline:
                    remaining = cd_deadline - time.monotonic()
                    update_bot_state({
                        "status": "waiting",
                        "status_detail": f"Cooling down — {cooldown} market{'s' if cooldown != 1 else ''} left — {_fmt_wait(remaining)}{_trade_ctx()}",
                    })
                    try:
                        poll_live_market(client, cfg)
                    except Exception:
                        pass
                    time.sleep(min(2, max(0, remaining)))
        else:
            update_bot_state({
                "status": "waiting",
                "status_detail": f"Cooling down — {cooldown} market{'s' if cooldown != 1 else ''} left{_trade_ctx()}",
            })
            time.sleep(30)
        return False

    # Bankroll limits
    ok, reason = check_bankroll_limits(client, cfg)
    if not ok:
        return False

    # Session profit target
    ok, reason = check_session_profit_target(cfg, state)
    if not ok:
        return False

    # Session loss floor
    ok, reason = check_session_loss_limit(cfg, state)
    if not ok:
        return False

    # Rolling win-rate circuit breaker
    ok, reason = check_rolling_win_rate(cfg)
    if not ok:
        return False

    # Auto-lock check
    check_auto_lock(client, cfg)

    # ── 1. Find market ────────────────────────────────────────
    market = wait_for_next_market(client, cfg)

    if not market:
        st = get_bot_state().get("status", "")
        if st != "stopped":
            update_bot_state({"status": "waiting", "status_detail": f"No market found — retrying{_trade_ctx()}"})
            time.sleep(15)
        return False

    ticker = market["ticker"]
    close_str = market.get("close_time", "")
    close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
    mins_left = client.minutes_until_close(close_str)

    # Already traded this market?
    if state.get("last_ticker") == ticker:
        secs = (close_dt - datetime.now(timezone.utc)).total_seconds()
        if secs > 0:
            ctx = _trade_ctx()
            # Preserve skip context if this was a skipped market
            skip_info = state.get("active_skip")
            if skip_info and skip_info.get("ticker") == ticker:
                short = (skip_info.get("reason") or "")[:50]
                detail = f"Observing: {short} — next in ~{_fmt_wait(secs)}{ctx}"
            else:
                detail = f"Next market in ~{_fmt_wait(secs)}{ctx}"
            update_bot_state({"status": "waiting", "status_detail": detail})
            time.sleep(min(secs + 2, 60))
        return False

    blog("INFO", f"Market: {ticker} | {mins_left:.1f}m to close | mode={get_trading_mode(cfg)}")

    now_et = datetime.now(ET)
    market_id = upsert_market(
        ticker=ticker, close_time_utc=close_dt.isoformat(),
        hour_et=now_et.hour, minute_et=now_et.minute,
        day_of_week=now_et.weekday()
    )
    update_bot_state({"last_ticker": ticker, "active_skip": None, "active_shadow": None})

    # ── 2. Regime check ───────────────────────────────────────
    snapshot = get_latest_regime_snapshot()
    regime_label = snapshot.get("composite_label", "unknown") if snapshot else "unknown"
    snapshot_id = snapshot.get("id") if snapshot else None

    # Regime observation count for dashboard display
    _regime_obs_n = 0
    try:
        from db import get_conn as _gc
        with _gc() as _c:
            _ron = _c.execute(
                "SELECT COUNT(*) as n FROM market_observations WHERE regime_label = ? AND market_result IS NOT NULL",
                (regime_label,)).fetchone()
            _regime_obs_n = _ron["n"] if _ron else 0
    except Exception:
        pass

    # Guard against stale regime data (regime worker crash, Binance outage)
    if snapshot and regime_label != "unknown":
        try:
            snap_time = datetime.fromisoformat(
                snapshot["captured_at"].replace("Z", "+00:00"))
            snap_age_s = (datetime.now(timezone.utc) - snap_time).total_seconds()
            if snap_age_s > 600:  # 10 minutes
                blog("WARNING", f"Regime snapshot is {snap_age_s/60:.0f}m old — "
                                 f"treating as unknown (was {regime_label})")
                regime_label = "unknown"
        except Exception:
            pass

    # ── 2-fv. Fair Value Model: capture BTC open for this market ──
    global _fv_btc_open, _fv_market_ticker, _fv_last_btc_fetch, _fv_last_btc_price
    if _fair_value_model and ticker != _fv_market_ticker:
        _fv_market_ticker = ticker
        _fv_btc_open = None
        try:
            btc_now = get_live_btc_price()
            if btc_now and btc_now > 0:
                _fv_btc_open = btc_now
                _fv_last_btc_price = btc_now
                _fv_last_btc_fetch = time.time()
                blog("DEBUG", f"FV model: BTC open for {ticker} = ${btc_now:,.0f}")
        except Exception as e:
            blog("DEBUG", f"FV model: failed to capture BTC open: {e}")
            # Fallback to regime snapshot BTC price
            if snapshot and snapshot.get("btc_price"):
                _fv_btc_open = snapshot["btc_price"]
                _fv_last_btc_price = _fv_btc_open
                blog("DEBUG", f"FV model: using snapshot BTC = ${_fv_btc_open:,.0f}")

    # ── 2a. Determine strategy key and compute strategy-based risk ──
    _active_strategy_key = None
    _strategy_risk = None
    _trading_mode = get_trading_mode(cfg)
    if _trading_mode in ("hybrid", "auto"):
        # Auto-strategy: peek at recommendation to get strategy key
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
        # Manual strategy: derive key from settings
        _active_strategy_key = build_strategy_key(cfg)

    try:
        _strategy_risk = get_strategy_risk(regime_label, _active_strategy_key)
        # Model side has no Observatory data — fall back to "cheaper" variant
        # for risk assessment since it's the closest proxy
        if (_strategy_risk and _strategy_risk.get("risk_level") == "unknown"
                and cfg.get("strategy_side") == "model"):
            _fallback_key = _active_strategy_key.replace("model:", "cheaper:", 1)
            _fb_risk = get_strategy_risk(regime_label, _fallback_key)
            if _fb_risk and _fb_risk.get("risk_level") != "unknown":
                _strategy_risk = _fb_risk
                _strategy_risk["_model_fallback"] = True
                blog("DEBUG", f"Model side: using cheaper variant risk ({_fb_risk.get('risk_level')})")
    except Exception as e:
        blog("DEBUG", f"Strategy risk lookup error: {e}")

    gate = check_regime_gate(cfg, regime_label, strategy_risk=_strategy_risk,
                             coarse_regime=compute_coarse_label(
                                 snapshot.get("vol_regime", 3) if snapshot else 3,
                                 snapshot.get("trend_regime", 0) if snapshot else 0,
                                 snapshot.get("volume_regime") if snapshot else None,
                             ))

    # Observe/shadow mode: record everything but don't place full trades
    if _trading_mode in ("observe", "shadow") and gate["should_trade"]:
        gate = {
            "should_trade": False,
            "is_data_collection": False,
            "reason": "Observe-only mode" if _trading_mode == "observe" else "Shadow mode",
            "risk_level": gate["risk_level"],
        }

    blog("INFO", f"Regime: {gate['reason']}")

    # ── 2b. Per-regime condition filters ─────────────────────
    vol_level = snapshot.get("vol_regime") if snapshot else None

    # Parse regime filters once for all downstream filter checks
    # (used here and again in side/spread/stability filters after price polling)
    _regime_filters = cfg.get("regime_filters", {})
    if isinstance(_regime_filters, str):
        _regime_filters = json.loads(_regime_filters)

    # Quick-trade and trade-all regimes bypass ALL per-regime filters (vol, hour, day, side, spread, stability)
    _is_quick_trade = "quick-trade" in gate.get("reason", "") or "trade-all" in gate.get("reason", "")

    # Check per-regime granular filters (vol, hour, day)
    # Note: side, spread, and stability filters run later after price data is available
    if gate["should_trade"] and not _is_quick_trade:
        rf = _get_regime_filter(regime_label, _regime_filters)
        if rf:
            skip_reason = None
            # Vol level filter
            vol_min = rf.get("vol_min", 1)
            vol_max = rf.get("vol_max", 5)
            if vol_level is not None and (vol_level < vol_min or vol_level > vol_max):
                skip_reason = f"Vol {vol_level}/5 outside {vol_min}-{vol_max} for {regime_label}"
            # Hour filter
            blocked_hours = rf.get("blocked_hours", [])
            if blocked_hours and now_et.hour in blocked_hours:
                skip_reason = f"Hour {now_et.hour} ET blocked for {regime_label}"
            # Day filter
            blocked_days = rf.get("blocked_days", [])
            if blocked_days and now_et.weekday() in blocked_days:
                day_names = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun']
                skip_reason = f"{day_names[now_et.weekday()]} blocked for {regime_label}"

            if skip_reason:
                gate = {
                    "should_trade": False,
                    "is_data_collection": False,
                    "reason": skip_reason,
                    "risk_level": gate["risk_level"],
                }
                blog("INFO", f"Regime filter: {skip_reason}")

    # ── 2c. Compute enrichment fields for data collection ──────
    trend_level = snapshot.get("trend_regime", 0) if snapshot else 0
    volume_level = snapshot.get("volume_regime", 3) if snapshot else 3
    coarse_regime = compute_coarse_label(
        vol_level or 3, trend_level, volume_level
    )
    prev_regime = get_prev_regime_label()
    trade_hour_et = now_et.hour
    trade_day_of_week = now_et.weekday()

    # Build common context for all trade inserts
    _ctx = _build_trade_context(
        client, cfg, state, market, snapshot, gate,
        coarse_regime, prev_regime, trade_hour_et, trade_day_of_week,
        vol_level=vol_level, close_str=close_str
    )

    # Always store the active strategy key — whether auto or picker-configured
    _ctx["auto_strategy_key"] = _active_strategy_key
    _ctx["auto_strategy_setup"] = "manual"  # Overridden below if auto-strategy active

    # ── 2d. Auto-strategy lookup ──────────────────────────────
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
            _strat_label = (f"{_as_rec['side_rule'].upper()} "
                           f"{'hold' if _as_rec['sell_target'] == 'hold' else 'sell@'+str(_as_rec['sell_target'])+'¢'} "
                           f"{_as_rec['entry_time_rule']} ≤{_as_rec['entry_price_max']}¢")
            blog("INFO", f"Auto-strategy: {_strat_label} "
                         f"(EV {_as_ev:+.1f}¢, n={_as_n}, "
                         f"WR={_as_rec['win_rate']:.0%}, "
                         f"from {_as_rec['setup_key']})")
        else:
            # No valid strategy — build detailed skip reason
            if _as_rec and _as_ev is not None:
                if _as_ev < _as_min_ev:
                    _skip_r = (f"Auto-strategy: best EV {_as_ev:+.1f}¢ "
                              f"below {_as_min_ev}¢ min ({_as_rec['setup_key']})")
                    _as_short = f"EV too low ({_as_ev:+.1f}¢)"
                else:
                    _skip_r = (f"Auto-strategy: n={_as_n} < {_as_min_n} min "
                              f"({_as_rec['setup_key']})")
                    _as_short = f"n={_as_n} < {_as_min_n} min"
            else:
                # get_recommendation returned None — use rejection_info for specific reason
                _rej_short = _as_rejection.get("short", "")
                _rej_detail = _as_rejection.get("detail", "")
                _rej_setup = _as_rejection.get("setup_key", "")
                if _rej_short:
                    _skip_r = (f"Auto-strategy: {_rej_detail}"
                              if _rej_detail else f"Auto-strategy: {_rej_short}")
                    # Prepend n if we have obs and it's not already in the message
                    if _regime_obs_n > 0 and "n=" not in _rej_short:
                        _as_short = f"n={_regime_obs_n}, {_rej_short}"
                    else:
                        _as_short = _rej_short
                elif _regime_obs_n > 0:
                    _skip_r = (f"Auto-strategy: {regime_label.replace('_',' ')} has {_regime_obs_n} obs "
                              f"but no strategy passes validation gates (need OOS+, fee resilient, n>={_as_min_n})")
                    _as_short = f"n={_regime_obs_n}, fails validation"
                else:
                    _skip_r = f"Auto-strategy: no observations for {regime_label.replace('_',' ')} yet"
                    _as_short = "no observations yet"
            gate = {
                "should_trade": False,
                "is_data_collection": False,
                "reason": _skip_r,
                "risk_level": gate["risk_level"],
                "_auto_skip_short": _as_short,
            }
            blog("INFO", _skip_r)

    if not gate["should_trade"]:
        # Get initial cheaper side from market for data collection
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
                                  strategy_key=_active_strategy_key, regime_label=regime_label)
        s = get_bot_state()
        secs_to_close = (close_dt - datetime.now(timezone.utc)).total_seconds()
        risk_display = 'extreme' if gate['risk_level'] == 'terrible' else gate['risk_level']
        is_obs_mode = _trading_mode in ("observe", "shadow", "hybrid")
        regime_display = regime_label.replace('_', ' ')
        _as_short = gate.get("_auto_skip_short", "")
        # Build short skip label for status bar
        if _as_short:
            _skip_short = _as_short
        elif "Observe-only" in gate["reason"]:
            _skip_short = ""
        elif "Vol " in gate["reason"] and " outside " in gate["reason"]:
            _skip_short = gate["reason"].split(" for ")[0]
        elif "blocked" in gate["reason"]:
            _skip_short = gate["reason"].split(" for ")[0]
        elif "— skipping" in gate["reason"]:
            _skip_short = f"{risk_display} risk — skip"
        elif "whitelist" in gate["reason"]:
            _skip_short = "not in quick-trade list"
        else:
            _skip_short = gate["reason"][:40]
        update_bot_state({
            "status": "waiting",
            "status_detail": f"Observing {regime_display} — next in ~{_fmt_wait(secs_to_close)}{_trade_ctx()}",
            "session_skips": (s.get("session_skips") or 0) + 1,
            "active_skip": {
                "reason": gate["reason"],
                "skip_short": _skip_short,
                "regime_label": regime_label,
                "risk_level": gate["risk_level"],
                "ticker": ticker,
                "close_time": close_dt.isoformat(),
                "trade_id": skip_trade_id,
                "auto_skip_short": _as_short,
                "regime_obs_n": _regime_obs_n,
            },
        })

        # ── Shadow trading: buy 1 contract using Observatory recommendation ──
        # Fires in shadow mode (every market) and hybrid mode (auto-strategy fallback)
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
                # Get Observatory's best strategy for this regime (no validation gates)
                _sh_rec = _get_shadow_strategy(
                    regime_label, hour_et=now_et.hour,
                    vol_regime=snapshot.get("vol_regime") if snapshot else None,
                    trend_regime=snapshot.get("trend_regime") if snapshot else None,
                )
                _sh_strat_key = None
                if _sh_rec:
                    _sh_side_rule = _sh_rec["side_rule"]
                    _sh_strat_key = _sh_rec["strategy_key"]
                    _sh_entry_max = _sh_rec["entry_price_max"]
                    # Determine side from recommendation
                    if _sh_side_rule in ("yes", "no"):
                        _sh_side = _sh_side_rule
                        _sh_price = _shadow_market.get(f"{_sh_side}_ask") or 0
                    elif _sh_side_rule == "model":
                        # Use fair value model if available, else cheaper
                        _sh_side, _sh_price = client.get_cheaper_side(_shadow_market)
                        if _fair_value_model and _fv_btc_open and _fv_btc_open > 0:
                            try:
                                _sh_btc = _fv_last_btc_price or (_fv_btc_open if _fv_btc_open else None)
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
                        # cheaper (default)
                        _sh_side, _sh_price = client.get_cheaper_side(_shadow_market)
                    # Apply entry price max from strategy (skip if too expensive)
                    if _sh_price > _sh_entry_max:
                        blog("DEBUG", f"Shadow: {_sh_side}@{_sh_price}c > {_sh_entry_max}c max "
                                      f"({_sh_strat_key}) — buying at ask anyway")
                        # Still trade — just collecting data, not optimizing
                else:
                    # No strategy data at all — fall back to cheaper side
                    _sh_side, _sh_price = client.get_cheaper_side(_shadow_market)

                _shadow_label = "Hybrid fallback" if _trading_mode == "hybrid" else "Shadow"
                blog("DEBUG", f"{_shadow_label} strategy: {_sh_strat_key or 'cheaper (fallback)'} "
                              f"→ {_sh_side}@{_sh_price}c for {regime_label}")
                _shadow_id = _place_shadow_trade(
                    client, ticker, _sh_side, _sh_price,
                    market_id=market_id, regime_label=regime_label,
                    snapshot_id=snapshot_id, ctx=_sh_ctx,
                    strategy_key=_sh_strat_key,
                )
                if _shadow_id:
                    # Shadow trade filled — remove the skip record so there's
                    # only one trade card per market
                    try:
                        from db import delete_trades
                        delete_trades([skip_trade_id])
                    except Exception:
                        pass
                    update_bot_state({
                        "active_shadow": {
                            "trade_id": _shadow_id,
                            "side": _sh_side,
                            "price_c": _sh_price,
                            "strategy_key": _sh_strat_key,
                        },
                    })
            except Exception as _she:
                blog("DEBUG", f"Shadow trade error: {_she}")

        stopped = _skip_wait_loop(
            client, cfg, close_dt, skip_trade_id, ticker,
            regime_label, gate["risk_level"], gate["reason"],
            track_side=True, resolve_inline=True,
            initial_cheaper_side=initial_skip_side, market_id=market_id,
        )

        # Clear shadow state so it doesn't persist into next market
        update_bot_state({"active_shadow": None, "active_skip": None})

        return False

    # ── 2e. Strategy overrides ─────────────────────────────────
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
        # Manual mode: apply strategy_side from settings picker
        manual_side = cfg.get("strategy_side", "cheaper")
        if manual_side and manual_side != "cheaper":
            _auto_side_rule = manual_side

    # ── 3. Entry delay + Poll for entry price ───────────────────
    entry_delay = cfg.get("entry_delay_minutes", 0)

    # Auto-strategy timing override
    if auto_strat:
        _time_rule = auto_strat["entry_time_rule"]
        if _time_rule == "late":
            entry_delay = 10
        elif _time_rule == "mid":
            entry_delay = 5
        else:  # "early"
            entry_delay = 0
    if entry_delay > 0:
        # Wait N minutes from market open (market is ~15 min long)
        target_mins_left = 15 - entry_delay
        current_mins = client.minutes_until_close(close_str)
        wait_secs = max(0, (current_mins - target_mins_left) * 60)
        if wait_secs > 10:
            blog("INFO", f"Entry delay: waiting {wait_secs:.0f}s "
                          f"({entry_delay}min into market)")
            delay_end = datetime.now(timezone.utc) + timedelta(seconds=wait_secs)
            update_bot_state({
                "status": "waiting",
                "status_detail": f"Entry delay: {_fmt_wait(wait_secs)}{_trade_ctx()}",
                "_delay_end_iso": delay_end.isoformat(),
            })
            delay_deadline = time.monotonic() + wait_secs
            while time.monotonic() < delay_deadline:
                rem = delay_deadline - time.monotonic()
                update_bot_state({
                    "status": "waiting",
                    "status_detail": f"Delaying entry: {_fmt_wait(rem)}{_trade_ctx()}",
                })
                # Process commands during delay
                for cmd in get_pending_commands():
                    cmd_type = cmd["command_type"]
                    cmd_id = cmd["id"]
                    params = json.loads(cmd.get("parameters") or "{}")
                    if cmd_type == "cash_out":
                        state_now = get_bot_state()
                        result = execute_cash_out(client, state_now)
                        complete_command(cmd_id, result)
                        update_bot_state({"_delay_end_iso": None})
                        return False
                    elif cmd_type == "stop":
                        update_bot_state({
                            "auto_trading": 0, "trades_remaining": 0,
                            "status": "stopped",
                            "status_detail": "Stopped",
                            "_delay_end_iso": None,
                                                    })
                        complete_command(cmd_id)
                        return False
                    elif cmd_type == "update_config":
                        for k, v in params.items():
                            set_config(k, v)
                            if k in cfg: cfg[k] = v
                        complete_command(cmd_id)
                    else:
                        complete_command(cmd_id)
                try:
                    poll_live_market(client, cfg)
                except Exception:
                    pass
                time.sleep(min(2, max(0, rem)))
            update_bot_state({"_delay_end_iso": None})

    max_entry_c = cfg.get("entry_price_max_c", 42)
    min_entry_c = 1    # No min filter — simulation doesn't model this
    poll_interval = cfg.get("price_poll_interval", 2)
    fill_wait = 600   # Use full time window — matches simulation (checks all snapshots)
    min_mins = 0.5    # 30s buffer — matches simulation's duration-30 cutoff

    # Auto-strategy price override
    if auto_strat:
        max_entry_c = auto_strat["entry_price_max"]
        # For forced-side strategies, relax min_entry to avoid skipping valid entries
        if _auto_side_rule in ("yes", "no", "model"):
            min_entry_c = max(min_entry_c, 1)

    # Convert close time to monotonic clock for safe deadline computation
    _now_wall = time.time()
    _now_mono = time.monotonic()
    _close_wall = close_dt.timestamp()
    _mono_close = _now_mono + (_close_wall - _now_wall)

    price_deadline = min(
        _now_mono + fill_wait,
        _mono_close - (min_mins * 60),
    )

    # Convert market time to display label
    market_label = marketStartTime(close_str)

    if auto_strat:
        update_bot_state({
            "status": "searching",
            "status_detail": f"Auto: {_auto_strat_label} — watching {market_label}{_trade_ctx()}"
        })
    elif _auto_side_rule == "model":
        _min_edge_display = float(cfg.get("min_model_edge_pct", 3.0))
        update_bot_state({
            "status": "searching",
            "status_detail": f"Model: scanning {market_label} — edge ≥{_min_edge_display:.0f}% (≤{max_entry_c}c){_trade_ctx()}"
        })
    else:
        update_bot_state({
            "status": "searching",
            "status_detail": f"Watching {market_label} — price ≤ {max_entry_c}c{_trade_ctx()}"
        })

    side_info = None
    stopped_early = False
    skip_reason = None
    poll_prices_seen = []  # Track all prices seen during polling for stability calc
    poll_sides_seen = []   # Track cheaper side for skip trade enrichment
    _entry_model_edge = None  # Capture model edge at entry for trade record
    _entry_model_ev = None
    _entry_model_source = None
    while time.monotonic() < price_deadline:
        # Process stop commands during price polling
        for cmd in get_pending_commands():
            cmd_type = cmd["command_type"]
            cmd_id = cmd["id"]
            params = json.loads(cmd.get("parameters") or "{}")
            if cmd_type == "stop":
                update_bot_state({
                    "auto_trading": 0, "trades_remaining": 0,
                    "loss_streak": 0,
                    "status": "stopped",
                    "status_detail": "Stopped",
                })
                complete_command(cmd_id)
                blog("INFO", "Stop received during price polling")
                stopped_early = True
                break
            elif cmd_type == "update_config":
                for k, v in params.items():
                    set_config(k, v)
                    if k in cfg:
                        cfg[k] = v
                complete_command(cmd_id)
            else:
                pass  # Other commands wait

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
                # Use fair value model to pick side with highest edge
                _model_side = None
                _model_edge_val = 0
                _model_ev_val = 0
                if _fair_value_model and _fv_btc_open and _fv_btc_open > 0:
                    try:
                        # Refresh BTC price (throttled to every 10s)
                        _now_m = time.time()
                        if _now_m - _fv_last_btc_fetch >= 10:
                            _btc_m = get_live_btc_price()
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
                    except Exception as _me:
                        log.debug(f"Model side error: {_me}")
                if _model_side:
                    side = _model_side
                    price_c = m.get(f"{side}_ask") or 0
                    # Capture model data for trade record
                    _entry_model_edge = _model_edge_val
                    _entry_model_ev = _model_ev_val
                    try:
                        _entry_model_source = _m_edge["model"]["source"]
                    except Exception:
                        _entry_model_source = "unknown"
                else:
                    # No edge — suppress entry by zeroing price
                    price_c = 0
                    _entry_model_edge = None
                    _entry_model_ev = None
                    _entry_model_source = None

        if price_c > 0:
            poll_prices_seen.append(price_c)
            if price_c <= max_entry_c:
                poll_sides_seen.append(side)

        # Update live market info so dashboard shows what we're watching
        cur_stab = (max(poll_prices_seen) - min(poll_prices_seen)) if len(poll_prices_seen) >= 2 else None
        _live_market_data = {
                "ticker": ticker,
                "close_time": close_str,
                "minutes_left": round(client.minutes_until_close(close_str), 1),
                "cheaper_side": side,
                "cheaper_price_c": price_c,
                "yes_ask": m.get("yes_ask"),
                "no_ask": m.get("no_ask"),
                "yes_bid": m.get("yes_bid"),
                "no_bid": m.get("no_bid"),
                "regime_label": regime_label,
                "risk_level": gate["risk_level"],
                "regime_win_rate": _strategy_risk.get("win_rate", 0) if _strategy_risk else 0,
                "regime_trades": _strategy_risk.get("sample_size", 0) if _strategy_risk else 0,
                "btc_price": snapshot.get("btc_price") if snapshot else None,
                "vol_regime": snapshot.get("vol_regime") if snapshot else None,
                "trend_regime": snapshot.get("trend_regime") if snapshot else None,
                "volume_regime": snapshot.get("volume_regime") if snapshot else None,
                "stability_c": cur_stab,
                "regime_obs_n": _regime_obs_n,
        }
        if auto_strat:
            _live_market_data["auto_strategy"] = _auto_strat_label
            _live_market_data["auto_strategy_ev"] = auto_strat["ev_per_trade_c"]
        _live_market_data["strategy_key"] = _active_strategy_key

        # ── Fair Value Model: compute edge every poll cycle ──
        if _fair_value_model and _fv_btc_open and _fv_btc_open > 0:
            try:
                # Refresh BTC price (throttled to every 10s)
                _now_fv = time.time()
                if _now_fv - _fv_last_btc_fetch >= 10:
                    _btc_fresh = get_live_btc_price()
                    if _btc_fresh and _btc_fresh > 0:
                        _fv_last_btc_price = _btc_fresh
                        _fv_last_btc_fetch = _now_fv

                if _fv_last_btc_price and _fv_last_btc_price > 0:
                    _fv_dist = (_fv_last_btc_price - _fv_btc_open) / _fv_btc_open * 100
                    _fv_secs = max(0, 900 - client.minutes_until_close(close_str) * 60)
                    _fv_rvol = snapshot.get("realized_vol_15m") if snapshot else None

                    _fv_edge = _fair_value_model.compute_edge(
                        yes_ask_c=m.get("yes_ask") or 0,
                        no_ask_c=m.get("no_ask") or 0,
                        btc_distance_pct=_fv_dist,
                        seconds_into_market=_fv_secs,
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
            except Exception as _fv_err:
                log.debug(f"FV model poll error: {_fv_err}")

        if auto_strat:
            _poll_status = (f"Auto: {_auto_strat_label} — "
                           f"{side.upper()} {price_c}c{_trade_ctx()}")
        elif _auto_side_rule == "model":
            if price_c > 0:
                _poll_status = (f"Model: {side.upper()} {price_c}c "
                               f"edge +{_model_edge_val:.1f}% "
                               f"(need ≤{max_entry_c}c){_trade_ctx()}")
            else:
                _poll_status = (f"Model: scanning — no edge ≥{float(cfg.get('min_model_edge_pct', 3.0)):.0f}%{_trade_ctx()}")
        else:
            _poll_status = (f"Watching {market_label} — "
                           f"{side.upper()} {price_c}c "
                           f"(need {min_entry_c}–{max_entry_c}c)")
        update_bot_state({
            "live_market": _live_market_data,
            "status_detail": _poll_status,
        })

        # Feed Strategy Observatory with price data during polling
        if _observer:
            _obs_data = {
                "yes_ask": m.get("yes_ask"),
                "no_ask": m.get("no_ask"),
                "yes_bid": m.get("yes_bid"),
                "no_bid": m.get("no_bid"),
                "btc_price": snapshot.get("btc_price") if snapshot else None,
                "volume": m.get("volume"),
                "open_interest": m.get("open_interest"),
            }
            _observer.tick(ticker, close_str, _obs_data, snapshot,
                          _strategy_risk)

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
            _min_edge_skip = float(cfg.get("min_model_edge_pct", 3.0))
            reason = f"Model found no edge ≥{_min_edge_skip:.0f}%"
        else:
            reason = "Price never reached entry range"

        # Determine most common cheaper side from polling (only in-range prices)
        from collections import Counter
        skip_side = "n/a"
        skip_avg_price = None
        if poll_sides_seen:
            skip_side = Counter(poll_sides_seen).most_common(1)[0][0]
            in_range = [p for p in poll_prices_seen if p <= max_entry_c]
            skip_avg_price = round(sum(in_range) / len(in_range)) if in_range else None

        price_skip_id = insert_trade({
            **_ctx,
            "market_id": market_id,
            "regime_snapshot_id": snapshot_id,
            "ticker": ticker,
            "side": skip_side,
            "avg_fill_price_c": skip_avg_price,
            "outcome": "skipped",
            "skip_reason": reason,
            "price_stability_c": (max(poll_prices_seen) - min(poll_prices_seen)) if len(poll_prices_seen) >= 2 else None,
            "num_price_samples": len(poll_prices_seen),
            "cheaper_side": skip_side if skip_side != "n/a" else _ctx.get("cheaper_side"),
            "cheaper_side_price_c": skip_avg_price or _ctx.get("cheaper_side_price_c"),
        })
        notify_observed(regime_label, reason)
        if _observer:
            _observer.mark_action("observed", price_skip_id,
                                  market_id=market_id,
                                  strategy_key=_active_strategy_key, regime_label=regime_label)
        s = get_bot_state()
        secs_to_close = (close_dt - datetime.now(timezone.utc)).total_seconds()
        update_bot_state({
            "status": "waiting",
            "status_detail": f"Observing {regime_label.replace('_', ' ')} — next in ~{_fmt_wait(secs_to_close)}{_trade_ctx()}",
            "session_skips": (s.get("session_skips") or 0) + 1,
            "active_skip": {
                "reason": reason,
                "skip_short": reason,
                "regime_label": regime_label,
                "risk_level": gate.get("risk_level", "unknown"),
                "ticker": ticker,
                "close_time": close_dt.isoformat(),
                "trade_id": price_skip_id, "regime_obs_n": _regime_obs_n,
            },
        })

        _skip_wait_loop(
            client, cfg, close_dt, price_skip_id, ticker,
            regime_label, gate.get("risk_level", "unknown"), reason,
            resolve_inline=True, initial_cheaper_side=skip_side,
            market_id=market_id,
        )

        return False

    side, entry_price_c = side_info

    # Capture full orderbook from last polled market (m) for all subsequent paths
    polled_ya = polled_na = polled_yb = polled_nb = None
    try:
        polled_ya = m.get("yes_ask") or 0
        polled_yb = m.get("yes_bid") or 0
        polled_na = m.get("no_ask") or 0
        polled_nb = m.get("no_bid") or 0
    except Exception:
        pass

    # ── Side filter — per-regime blocked sides ──
    rf_side = _get_regime_filter(regime_label, _regime_filters)
    blocked_sides = rf_side.get("blocked_sides", [])
    if not _is_quick_trade and blocked_sides and side in blocked_sides:
        reason = f"{side.upper()} side blocked for {regime_label}"
        blog("INFO", f"Regime filter: {reason}")
        side_skip_id = insert_trade({
            **_ctx,
            "market_id": market_id, "regime_snapshot_id": snapshot_id,
            "ticker": ticker, "side": side,
            "avg_fill_price_c": entry_price_c,
            "outcome": "skipped", "skip_reason": reason,
            "price_stability_c": (max(poll_prices_seen) - min(poll_prices_seen)) if len(poll_prices_seen) >= 2 else None,
            "yes_ask_at_entry": polled_ya,
            "no_ask_at_entry": polled_na,
            "yes_bid_at_entry": polled_yb,
            "no_bid_at_entry": polled_nb,
            "num_price_samples": len(poll_prices_seen),
        })
        notify_observed(regime_label, reason)
        if _observer:
            _observer.mark_action("observed", side_skip_id,
                                  market_id=market_id,
                                  strategy_key=_active_strategy_key, regime_label=regime_label)
        s_side = get_bot_state()
        update_bot_state({
            "status": "waiting",
            "status_detail": f"Observing {regime_label.replace('_', ' ')} — next in ~{_fmt_wait((close_dt - datetime.now(timezone.utc)).total_seconds())}{_trade_ctx()}",
            "session_skips": (s_side.get("session_skips") or 0) + 1,
            "active_skip": {
                "reason": reason, "skip_short": reason.split(" for ")[0],
                "regime_label": regime_label,
                "risk_level": gate.get("risk_level", "unknown"),
                "ticker": ticker, "close_time": close_dt.isoformat(),
                "trade_id": side_skip_id, "regime_obs_n": _regime_obs_n,
            },
        })
        _skip_wait_loop(client, cfg, close_dt, side_skip_id, ticker,
                        regime_label, gate.get("risk_level", "unknown"), reason,
                        resolve_inline=True, market_id=market_id)
        return False

    # Capture spread at entry from polled orderbook
    try:
        if side == "yes":
            spread_at_entry_c = max(0, polled_ya - polled_yb) if polled_ya and polled_yb else None
        else:
            spread_at_entry_c = max(0, polled_na - polled_nb) if polled_na and polled_nb else None
    except Exception:
        spread_at_entry_c = None

    # ── Spread filter — per-regime max spread ──
    spread_regime_label = score_spread(spread_at_entry_c)
    rf_spread = _get_regime_filter(regime_label, _regime_filters)
    max_spread = rf_spread.get("max_spread_c", 0)
    if not _is_quick_trade and max_spread > 0 and spread_at_entry_c is not None and spread_at_entry_c > max_spread:
        reason = f"Spread {spread_at_entry_c}c > {max_spread}c max for {regime_label}"
        blog("INFO", f"Regime filter: {reason}")
        spread_skip_id = insert_trade({
            **_ctx,
            "market_id": market_id, "regime_snapshot_id": snapshot_id,
            "ticker": ticker, "side": side,
            "avg_fill_price_c": entry_price_c,
            "spread_at_entry_c": spread_at_entry_c,
            "spread_regime": spread_regime_label,
            "outcome": "skipped", "skip_reason": reason,
            "price_stability_c": (max(poll_prices_seen) - min(poll_prices_seen)) if len(poll_prices_seen) >= 2 else None,
            "yes_ask_at_entry": polled_ya,
            "no_ask_at_entry": polled_na,
            "yes_bid_at_entry": polled_yb,
            "no_bid_at_entry": polled_nb,
            "num_price_samples": len(poll_prices_seen),
        })
        notify_observed(regime_label, reason)
        if _observer:
            _observer.mark_action("observed", spread_skip_id,
                                  market_id=market_id,
                                  strategy_key=_active_strategy_key, regime_label=regime_label)
        s_spread = get_bot_state()
        update_bot_state({
            "status": "waiting",
            "status_detail": f"Observing {regime_label.replace('_', ' ')} — next in ~{_fmt_wait((close_dt - datetime.now(timezone.utc)).total_seconds())}{_trade_ctx()}",
            "session_skips": (s_spread.get("session_skips") or 0) + 1,
            "active_skip": {
                "reason": reason, "skip_short": reason.split(" for ")[0],
                "regime_label": regime_label,
                "risk_level": gate.get("risk_level", "unknown"),
                "ticker": ticker, "close_time": close_dt.isoformat(),
                "trade_id": spread_skip_id, "regime_obs_n": _regime_obs_n,
            },
        })
        _skip_wait_loop(client, cfg, close_dt, spread_skip_id, ticker,
                        regime_label, gate.get("risk_level", "unknown"), reason,
                        resolve_inline=True, market_id=market_id)
        return False

    # ── 4. Calculate bet size ─────────────────────────────────
    is_ignored = bool(cfg.get("ignore_mode", False))

    bankroll_c = get_effective_bankroll_cents(client, cfg)
    update_bot_state({"bankroll_cents": client.get_balance_cents()})

    bet_dollars = get_r1_bet_dollars(cfg, bankroll_c / 100)
    blog("INFO", f"Bet: ${bet_dollars:.2f} ({cfg.get('bet_mode', 'flat')})")
    shares = client.calc_shares_for_dollars(bet_dollars, entry_price_c)

    est_cost = shares * entry_price_c / 100
    est_fees = client.estimate_fees(shares, entry_price_c)

    stability_c = (max(poll_prices_seen) - min(poll_prices_seen)) if len(poll_prices_seen) >= 2 else None
    stability_str = f" stability={stability_c}c" if stability_c is not None else ""

    # Check per-regime stability filter
    rf = _get_regime_filter(regime_label, _regime_filters)
    stab_max = rf.get("stability_max", 0) if rf else 0
    if not _is_quick_trade and stab_max > 0 and stability_c is not None and stability_c > stab_max:
        reason = f"Stability {stability_c}c > {stab_max}c max for {regime_label}"
        blog("INFO", f"Regime filter: {reason}")
        stab_skip_id = insert_trade({
            **_ctx,
            "market_id": market_id, "regime_snapshot_id": snapshot_id,
            "ticker": ticker, "side": side,
            "outcome": "skipped", "skip_reason": reason,
            "price_stability_c": stability_c,
            "spread_at_entry_c": spread_at_entry_c,
            "spread_regime": spread_regime_label,
            "yes_ask_at_entry": polled_ya,
            "no_ask_at_entry": polled_na,
            "yes_bid_at_entry": polled_yb,
            "no_bid_at_entry": polled_nb,
            "num_price_samples": len(poll_prices_seen),
        })
        notify_observed(regime_label, reason)
        if _observer:
            _observer.mark_action("observed", stab_skip_id,
                                  market_id=market_id,
                                  strategy_key=_active_strategy_key, regime_label=regime_label)
        s_stab = get_bot_state()
        update_bot_state({
            "status": "waiting",
            "status_detail": f"Observing {regime_label.replace('_', ' ')} — next in ~{_fmt_wait((close_dt - datetime.now(timezone.utc)).total_seconds())}{_trade_ctx()}",
            "session_skips": (s_stab.get("session_skips") or 0) + 1,
            "active_skip": {
                "reason": reason, "skip_short": reason.split(" for ")[0],
                "regime_label": regime_label,
                "risk_level": gate.get("risk_level", "unknown"),
                "ticker": ticker, "close_time": close_dt.isoformat(),
                "trade_id": stab_skip_id, "regime_obs_n": _regime_obs_n,
            },
        })
        _skip_wait_loop(client, cfg, close_dt, stab_skip_id, ticker,
                        regime_label, gate.get("risk_level", "unknown"), reason,
                        resolve_inline=True, market_id=market_id)
        return False

    blog("INFO", f"Plan: {shares} {side.upper()} @ {entry_price_c}c "
                  f"(~${est_cost:.2f} + ~${est_fees:.2f} fees){stability_str}")

    # ── 4b. Edge-scaled sizing ─────────────────────────────
    #    Scale bet size by FV model edge for the selected side.
    #    Uses _entry_model_edge if already captured (model side rule),
    #    otherwise computes edge for the chosen side on the fly.
    if cfg.get("bet_mode") == "edge_scaled":
        _sizing_edge = _entry_model_edge  # Already set if model side rule
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
                    # Use edge for the side we're actually buying
                    _sizing_edge = _s_edge.get(f"{side}_edge_pct")
                    if _sizing_edge is not None and _sizing_edge < 0:
                        _sizing_edge = 0  # No negative scaling
            except Exception:
                _sizing_edge = None

        if _sizing_edge is not None and _sizing_edge > 0:
            bet_dollars = get_r1_bet_dollars(cfg, bankroll_c / 100,
                                             edge_pct=_sizing_edge)
            shares = client.calc_shares_for_dollars(bet_dollars, entry_price_c)
            est_cost = shares * entry_price_c / 100
            est_fees = client.estimate_fees(shares, entry_price_c)
            blog("INFO", f"Edge-scaled: ${bet_dollars:.2f} ({shares} shares) "
                          f"for FV edge +{_sizing_edge:.1f}%")

    # ── 4b2. Drawdown-responsive sizing ───────────────────
    #    Gradually reduce size as session drawdown approaches limit,
    #    instead of binary on/off at the circuit breaker.
    dd_max = float(cfg.get("session_loss_limit", 0))
    if dd_max > 0:
        session_pnl = float(state.get("session_pnl") or 0)
        if session_pnl < 0:
            dd_pct = abs(session_pnl) / dd_max  # 0.0 to 1.0+
            if dd_pct >= 0.5:
                # Scale: at 50% of limit → 75% size, at 75% → 50%, at 100% → stop
                if dd_pct >= 1.0:
                    dd_scale = 0.0  # Circuit breaker handles this
                else:
                    dd_scale = max(0.25, 1.0 - dd_pct)
                shares = max(1, round(shares * dd_scale))
                est_cost = shares * entry_price_c / 100
                est_fees = client.estimate_fees(shares, entry_price_c)
                bet_dollars = est_cost + est_fees
                blog("INFO", f"Drawdown scaling: {dd_scale:.0%} size "
                              f"(session {session_pnl:+.2f} / {dd_max:.2f} limit)")

    # ── 4c. Bankroll safety check ─────────────────────────────
    safe, reason = check_bankroll_safety(client, cfg, state, bet_dollars, entry_price_c)
    if not safe:
        safety_skip_id = insert_trade({
            **_ctx,
            "market_id": market_id, "regime_snapshot_id": snapshot_id,
            "ticker": ticker, "side": side,
            "outcome": "skipped",
            "skip_reason": reason,
            "price_stability_c": stability_c,
            "spread_at_entry_c": spread_at_entry_c,
            "spread_regime": spread_regime_label,
            "yes_ask_at_entry": polled_ya,
            "no_ask_at_entry": polled_na,
            "yes_bid_at_entry": polled_yb,
            "no_bid_at_entry": polled_nb,
            "num_price_samples": len(poll_prices_seen),
            "bet_size_dollars": bet_dollars,
        })
        if _observer:
            _observer.mark_action("observed", safety_skip_id,
                                  market_id=market_id,
                                  strategy_key=_active_strategy_key, regime_label=regime_label)
        s2 = get_bot_state()
        update_bot_state({
            "status": "waiting",
            "status_detail": f"Observing {regime_label.replace('_', ' ')} — next in ~{_fmt_wait((close_dt - datetime.now(timezone.utc)).total_seconds())}{_trade_ctx()}",
            "session_skips": (s2.get("session_skips") or 0) + 1,
            "active_skip": {
                "reason": reason,
                "skip_short": reason,
                "regime_label": regime_label,
                "risk_level": gate.get("risk_level", "unknown"),
                "ticker": ticker,
                "close_time": close_dt.isoformat(),
                "trade_id": safety_skip_id, "regime_obs_n": _regime_obs_n,
            },
        })
        _skip_wait_loop(client, cfg, close_dt, safety_skip_id, ticker,
                        regime_label, gate.get("risk_level", "unknown"), reason,
                        resolve_inline=True, market_id=market_id)
        return False

    # ── 5. Place buy order(s) — adaptive entry execution ────
    buy_start_time = time.monotonic()
    update_bot_state({
        "status": "trading",
        "status_detail": f"Buying {shares} {side.upper()} @ {entry_price_c}c{_trade_ctx()}"
    })

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
    _buy_error = None  # Track API/order errors vs genuine no-fills

    # Adaptive entry: start below ask to save on spread when possible
    # First attempt at ask-2 with short timeout, then walk up
    adaptive = bool(cfg.get("adaptive_entry", False))
    if adaptive and spread_at_entry_c and spread_at_entry_c >= 4:
        # Only worth it when spread is wide enough to save meaningfully
        buy_price_c = max(2, entry_price_c - 2)
        blog("INFO", f"Adaptive entry: starting at {buy_price_c}c "
                      f"(ask={entry_price_c}c, spread={spread_at_entry_c}c)")

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
            blog("ERROR", f"No order_id on attempt {buy_attempt}: {buy_resp}")
            _buy_error = f"No order_id returned: {str(buy_resp)[:200]}"
            break

        all_order_ids.append(order_id)
        status = buy_order.get("status", "")
        blog("INFO", f"Buy attempt {buy_attempt}: {order_id[:12]}… "
                      f"{remaining_shares}x @ {buy_price_c}c status={status}")

        if status == "executed":
            fill = client.parse_fill(buy_order)
        elif status == "resting":
            # Adaptive entry: use shorter deadline for underpriced first attempt
            attempt_deadline = fill_deadline
            if adaptive and buy_attempt == 1 and buy_price_c < entry_price_c:
                attempt_deadline = min(fill_deadline, time.time() + 20)
            # Inline polling — keeps dashboard heartbeat alive and feeds Observatory
            _poll_int = cfg.get("order_poll_interval", 3)
            fill = None
            while time.time() < attempt_deadline:
                time.sleep(_poll_int)
                # Heartbeat — prevents dashboard "Bot Offline" during fill wait
                _elapsed = int(time.monotonic() - buy_start_time)
                update_bot_state({
                    "status": "trading",
                    "status_detail": (f"Buying {remaining_shares} {side.upper()} "
                                      f"@ {buy_price_c}c (filling… {_elapsed}s){_trade_ctx()}")
                })
                # Feed Observatory with live prices during fill wait
                try:
                    _fm = client.get_market(ticker)
                    if _observer:
                        _observer.tick(ticker, close_str, {
                            "yes_ask": _fm.get("yes_ask"),
                            "no_ask": _fm.get("no_ask"),
                            "yes_bid": _fm.get("yes_bid"),
                            "no_bid": _fm.get("no_bid"),
                            "btc_price": snapshot.get("btc_price") if snapshot else None,
                            "volume": _fm.get("volume"),
                            "open_interest": _fm.get("open_interest"),
                        }, snapshot, _strategy_risk)
                except Exception:
                    pass
                # Check order fill status
                _order = client.get_order(order_id)
                _st = _order.get("status", "")
                _fc = _order.get("fill_count", 0)
                blog("INFO", f"  Poll: {_st} — {_fc}/{remaining_shares} filled")
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
            blog("INFO", f"Attempt {buy_attempt}: filled {filled_now}, "
                          f"total {total_filled}/{target_shares}")

        # If we still need more, cancel unfilled remainder and retry at current price
        if total_filled < target_shares and time.time() < fill_deadline:
            try:
                client.cancel_order(order_id)
            except Exception:
                pass

            # Check current market price for retry
            try:
                m = client.get_market(ticker)
                if side == "yes":
                    new_price = m.get("yes_ask", 0) or 0
                else:
                    new_price = m.get("no_ask", 0) or 0

                if new_price >= min_entry_c and new_price <= max_entry_c:
                    # Same side still in range — retry at current price
                    buy_price_c = new_price
                    blog("INFO", f"Retrying at {buy_price_c}c for "
                                  f"{target_shares - total_filled} more shares")
                else:
                    # Current side out of range — check the other side
                    _other = "no" if side == "yes" else "yes"
                    _other_price = m.get(f"{_other}_ask", 0) or 0
                    if _other_price >= min_entry_c and _other_price <= max_entry_c:
                        side = _other
                        buy_price_c = _other_price
                        blog("INFO", f"Switched to {side.upper()} @ {buy_price_c}c")
                    else:
                        # Neither side in range — wait and re-poll until
                        # something comes back or fill deadline expires
                        blog("INFO", f"Both sides outside {min_entry_c}-{max_entry_c}c — "
                                      f"waiting for price to return")
                        update_bot_state({"status_detail": f"Waiting for price — "
                                          f"both sides out of range{_trade_ctx()}"})
                        _found_reentry = False
                        while time.time() < fill_deadline:
                            time.sleep(poll_interval)
                            try:
                                _rm = client.get_market(ticker)
                                # Feed observatory during wait
                                if _observer:
                                    _obs_d = {
                                        "yes_ask": _rm.get("yes_ask"),
                                        "no_ask": _rm.get("no_ask"),
                                        "yes_bid": _rm.get("yes_bid"),
                                        "no_bid": _rm.get("no_bid"),
                                        "btc_price": snapshot.get("btc_price") if snapshot else None,
                                        "volume": _rm.get("volume"),
                                        "open_interest": _rm.get("open_interest"),
                                    }
                                    _observer.tick(ticker, close_str, _obs_d, snapshot,
                                                   _strategy_risk)
                                for _cs in ("yes", "no"):
                                    _cp = _rm.get(f"{_cs}_ask", 0) or 0
                                    if _cp >= min_entry_c and _cp <= max_entry_c:
                                        side = _cs
                                        buy_price_c = _cp
                                        blog("INFO", f"Price returned — "
                                                      f"{side.upper()} @ {buy_price_c}c")
                                        _found_reentry = True
                                        break
                                if _found_reentry:
                                    break
                            except Exception:
                                continue
                        if not _found_reentry:
                            blog("INFO", "Price never returned to range — "
                                          "giving up on fill")
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
            **_ctx,
            "market_id": market_id, "regime_snapshot_id": snapshot_id,
            "ticker": ticker, "side": side,
            "entry_price_c": entry_price_c,
            "outcome": _outcome, "buy_order_id": all_order_ids[0] if all_order_ids else None,
            "skip_reason": _reason,
            "price_stability_c": stability_c,
            "spread_at_entry_c": spread_at_entry_c,
            "spread_regime": spread_regime_label,
            "yes_ask_at_entry": polled_ya,
            "no_ask_at_entry": polled_na,
            "yes_bid_at_entry": polled_yb,
            "no_bid_at_entry": polled_nb,
            "num_price_samples": len(poll_prices_seen),
            "bet_size_dollars": bet_dollars,
        })
        update_bot_state({"status_detail": _reason})
        secs = (close_dt - datetime.now(timezone.utc)).total_seconds()
        if secs > 0:
            time.sleep(secs + 2)
        return False

    fill_count = total_filled
    actual_cost = (total_cost_cents + total_fees_cents) / 100
    avg_price_c = round(total_cost_cents / total_filled) if total_filled > 0 else buy_price_c
    fees_paid = total_fees_cents / 100
    buy_order_id = all_order_ids[0]  # Primary order ID for records

    if total_filled < target_shares:
        blog("INFO", f"Partial fill: {total_filled}/{target_shares} shares — "
                      f"proceeding with what we got")

    blog("INFO", f"Filled: {fill_count} @ ~{avg_price_c}c | "
                  f"cost=${actual_cost:.2f} (fees=${fees_paid:.2f})"
                  f"{' [partial]' if total_filled < target_shares else ''}")
    notify_buy(side, fill_count, avg_price_c, actual_cost, regime_label)

    # ── 7. Calculate sell price ───────────────────────────────
    is_hold_to_expiry = False

    # Auto-strategy exit override
    if _auto_sell_target is not None:
        if _auto_sell_target == "hold":
            is_hold_to_expiry = True
            blog("INFO", f"Auto-strategy exit: HOLD to expiry")
        else:
            # Absolute sell target in cents
            sell_price_c = min(int(_auto_sell_target), 99)
            blog("INFO", f"Auto-strategy exit: sell at {sell_price_c}¢")

    if is_hold_to_expiry:
        sell_price_c = 99  # Used for display/records only
        expected_gross = fill_count * 100 / 100  # full payout if win
        expected_profit = expected_gross - actual_cost
        blog("INFO", f"Hold to expiry: cost=${actual_cost:.2f}, "
                      f"win payout=${expected_gross:.2f}, "
                      f"max profit=${expected_profit:.2f}")
    elif _auto_sell_target is None:
        # Manual strategy: use absolute sell target from picker
        manual_sell = int(cfg.get("sell_target_c", 0) or 0)
        if manual_sell > 0:
            sell_price_c = min(manual_sell, 99)
        else:
            # Fallback: hold to expiry if no sell target set
            is_hold_to_expiry = True
            sell_price_c = 99

    if not is_hold_to_expiry:
        expected_gross = fill_count * sell_price_c / 100
        expected_profit = expected_gross - actual_cost

    if sell_price_c >= 99 and not is_hold_to_expiry:
        blog("WARNING", f"Sell price capped at 99c — target may be too aggressive")

    if is_hold_to_expiry:
        blog("INFO", f"Strategy: hold to expiry — no sell order")
    else:
        blog("INFO", f"Sell @ {sell_price_c}c → gross=${expected_gross:.2f} "
                      f"profit=${expected_profit:.2f}")

    # ── 7b. Dynamic sell: override initial sell target from fair value model ──
    _dynamic_sell_active = False
    _dynamic_sell_adjustments = 0
    _dynamic_sell_initial = None
    _dynamic_sell_floor = int(cfg.get("dynamic_sell_floor_c", 3))
    if (bool(cfg.get("dynamic_sell_enabled", False))
            and _fair_value_model and _fv_btc_open and _fv_btc_open > 0
            and not is_hold_to_expiry):
        try:
            _fv_now = _fv_last_btc_price or get_live_btc_price()
            if _fv_now and _fv_now > 0:
                _ds_dist = (_fv_now - _fv_btc_open) / _fv_btc_open * 100
                _ds_secs = max(0, 900 - client.minutes_until_close(close_str) * 60)
                _ds_rvol = snapshot.get("realized_vol_15m") if snapshot else None
                _ds_model = _fair_value_model.get_yes_probability(
                    _ds_dist, _ds_secs, _ds_rvol,
                    vol_regime=snapshot.get("vol_regime") if snapshot else None)
                # Fair value of our side
                if side == "yes":
                    _ds_fv = _ds_model["fair_yes_c"]
                else:
                    _ds_fv = _ds_model["fair_no_c"]
                # Sell target = fair value minus 1¢ (sell just below fair value)
                # But never below our entry price + 1¢
                _ds_target = max(int(_ds_fv) - 1, avg_price_c + 1)
                _ds_target = min(_ds_target, 99)
                if _ds_target > sell_price_c:
                    blog("INFO", f"Dynamic sell: FV={_ds_fv:.1f}c → "
                                  f"raising sell from {sell_price_c}c to {_ds_target}c")
                    sell_price_c = _ds_target
                elif _ds_target < sell_price_c and _ds_target > avg_price_c:
                    blog("INFO", f"Dynamic sell: FV={_ds_fv:.1f}c → "
                                  f"lowering sell from {sell_price_c}c to {_ds_target}c")
                    sell_price_c = _ds_target
                _dynamic_sell_active = True
                _dynamic_sell_initial = sell_price_c
                expected_gross = fill_count * sell_price_c / 100
                expected_profit = expected_gross - actual_cost
                blog("INFO", f"Dynamic sell enabled: initial target {sell_price_c}c "
                              f"(FV {_ds_fv:.1f}c, floor {_dynamic_sell_floor}c moves)")
        except Exception as _dse:
            blog("DEBUG", f"Dynamic sell init error: {_dse}")

    # ── 8. Place sell order ───────────────────────────────────
    sell_order_id = None
    if is_hold_to_expiry:
        blog("INFO", f"Holding {fill_count} {side.upper()} to expiry (auto-strategy)")
    else:
        try:
            sell_resp = client.place_limit_order(
                ticker, side, fill_count, sell_price_c, action="sell"
            )
            sell_order = sell_resp.get("order", {})
            sell_order_id = sell_order.get("order_id")
            blog("INFO", f"Sell placed: {sell_order_id and sell_order_id[:12]}… "
                          f"| {fill_count}x {side} @ {sell_price_c}c")
        except Exception as e:
            blog("ERROR", f"Sell order failed: {e} — holding to close")

    # ── 9. Save trade + state ─────────────────────────────────
    btc_price = get_live_btc_price()
    mins_at_entry = client.minutes_until_close(close_str)

    fill_duration_s = round(time.monotonic() - buy_start_time, 1)

    trade_id = insert_trade({
        **_ctx,
        "market_id": market_id,
        "regime_snapshot_id": snapshot_id,
        "ticker": ticker,
        "side": side,
        "entry_price_c": entry_price_c,
        "entry_time_utc": now_utc(),
        "minutes_before_close": round(mins_at_entry, 2),
        "shares_ordered": shares,
        "shares_filled": fill_count,
        "actual_cost": round(actual_cost, 2),
        "fees_paid": round(fees_paid, 2),
        "avg_fill_price_c": avg_price_c,
        "buy_order_id": buy_order_id,
        "sell_price_c": sell_price_c,
        "sell_order_id": sell_order_id,
        "outcome": "open",
        "is_data_collection": 0,
        "is_ignored": int(is_ignored),
        "price_stability_c": (max(poll_prices_seen) - min(poll_prices_seen)) if len(poll_prices_seen) >= 2 else None,
        "spread_at_entry_c": spread_at_entry_c,
        "spread_regime": spread_regime_label,
        # Override orderbook with polled values (more accurate than _ctx discovery-time values)
        "yes_ask_at_entry": polled_ya,
        "no_ask_at_entry": polled_na,
        "yes_bid_at_entry": polled_yb,
        "no_bid_at_entry": polled_nb,
        "bet_size_dollars": round(bet_dollars, 2),
        "fill_duration_seconds": fill_duration_s,
        "num_price_samples": len(poll_prices_seen),
        # Fair value model edge at entry (if model side was used)
        "model_edge_at_entry": _entry_model_edge,
        "model_ev_at_entry": _entry_model_ev,
        "model_source_at_entry": _entry_model_source,
    })

    active_trade = {
        "trade_id": trade_id,
        "ticker": ticker,
        "side": side,
        "fill_count": fill_count,
        "actual_cost": round(actual_cost, 2),
        "avg_price_c": avg_price_c,
        "sell_price_c": sell_price_c,
        "sell_order_id": sell_order_id,
        "buy_order_id": buy_order_id,
        "close_time": close_str,
        "entry_time": now_utc(),
        # Regime details for dashboard
        "regime_label": regime_label,
        "risk_level": gate["risk_level"],
        "regime_win_rate": _strategy_risk.get("win_rate", 0) if _strategy_risk else 0,
        "regime_trades": _strategy_risk.get("sample_size", 0) if _strategy_risk else 0,
        "regime_ci_lower": _strategy_risk.get("ci_lower", 0) if _strategy_risk else 0,
        "regime_obs_n": _regime_obs_n,
        "vol_regime": snapshot.get("vol_regime") if snapshot else None,
        "trend_regime": snapshot.get("trend_regime") if snapshot else None,
        "volume_regime": snapshot.get("volume_regime") if snapshot else None,
        "btc_price": btc_price,
        "expected_profit": round(expected_profit, 2),
        "predicted_win_pct": _ctx.get("predicted_win_pct"),
        "confidence_level": _ctx.get("confidence_level"),
        "predicted_edge_pct": _ctx.get("predicted_edge_pct"),
        "ev_per_contract_c": _ctx.get("ev_per_contract_c"),
        # Auto-strategy details for dashboard
        "auto_strategy": _auto_strat_label,
        "auto_strategy_ev": auto_strat["ev_per_trade_c"] if auto_strat else None,
        "auto_strategy_setup": auto_strat["setup_key"] if auto_strat else None,
        "strategy_key": _active_strategy_key,
        "is_hold_to_expiry": is_hold_to_expiry,
        # Fair value model
        "model_edge": _entry_model_edge,
        "model_ev": _entry_model_ev,
        # Dynamic sell
        "dynamic_sell": _dynamic_sell_active,
        "dynamic_initial": _dynamic_sell_initial,
        "dynamic_adjustments": 0,
        "dynamic_fv": None,
    }

    if auto_strat:
        if is_hold_to_expiry:
            _trade_status = f"Auto: {fill_count} {side.upper()} ~{avg_price_c}c → hold to expiry"
        else:
            _trade_status = f"Auto: {fill_count} {side.upper()} ~{avg_price_c}c → sell@{sell_price_c}c"
    elif _auto_side_rule == "model" and _entry_model_edge:
        _sell_label = "hold" if is_hold_to_expiry else f"sell@{sell_price_c}c"
        _trade_status = f"Model: {fill_count} {side.upper()} ~{avg_price_c}c edge +{_entry_model_edge:.1f}% → {_sell_label}"
    else:
        _trade_status = f"{fill_count} {side.upper()} ~{avg_price_c}c → sell@{sell_price_c}c"

    update_bot_state({
        "status": "trading",
        "status_detail": _trade_status,
        "active_trade": active_trade,
        "bankroll_cents": client.get_balance_cents(),
    })

    # Mark this market as traded in Observatory
    if _observer:
        _observer.mark_action("traded", trade_id,
                              market_id=market_id,
                              strategy_key=_active_strategy_key, regime_label=regime_label)

    # ── 10. Monitor until close ───────────────────────────────
    high_water_c = avg_price_c
    low_water_c = avg_price_c
    osc_count = 0
    last_direction = None
    secs_to_close = (close_dt - datetime.now(timezone.utc)).total_seconds()
    cashed_out = False

    if secs_to_close > 0:
        blog("INFO", f"Monitoring for {secs_to_close:.0f}s...")
        end_time = time.monotonic() + secs_to_close + 2
        poll_s = cfg.get("price_poll_interval", 2)
        last_db_write = 0  # monotonic timestamp of last price_path insert
        _last_trade_notify = 0  # monotonic timestamp of last trade update notification

        while time.monotonic() < end_time:
            # ── Check for commands mid-trade ──────────────────
            for cmd in get_pending_commands():
                cmd_type = cmd["command_type"]
                cmd_id = cmd["id"]
                params = json.loads(cmd.get("parameters") or "{}")

                if cmd_type == "cash_out":
                    blog("WARNING", "Cash out command received mid-trade")
                    # Save state so we can restore on clean cancel
                    pre_cashout = {
                        "auto_trading": get_bot_state().get("auto_trading", 0),
                        "trades_remaining": get_bot_state().get("trades_remaining", 0),
                    }
                    # Stop auto-trading
                    update_bot_state({
                        "auto_trading": 0, "trades_remaining": 0,
                    })
                    state_now = get_bot_state()
                    result = execute_cash_out(client, state_now)
                    complete_command(cmd_id, result)

                    if result.get("cancelled") and result.get("sold", 0) == 0:
                        # Cancel with no shares sold — restore everything
                        update_bot_state(pre_cashout)
                        refreshed = get_bot_state().get("active_trade", {})
                        sell_order_id = refreshed.get("sell_order_id")
                        sell_price_c = refreshed.get("sell_price_c", sell_price_c)
                        blog("INFO", "Cash out cancelled cleanly — fully restored")
                    else:
                        # Fully cashed out or partial cancel — trade is done
                        cashed_out = True
                    break

                if cmd_type == "stop":
                    # Stop — mark trade as ignored, keep sell running
                    update_bot_state({
                        "auto_trading": 0, "trades_remaining": 0,
                        "loss_streak": 0,
                    })
                    active_trade["is_ignored"] = True
                    update_bot_state({"active_trade": active_trade})
                    if trade_id:
                        update_trade(trade_id, {"is_ignored": 1, "notes": "Stopped mid-trade — ignored"})
                    complete_command(cmd_id)
                    blog("INFO", "Stop received — trade kept as ignored")

                if cmd_type == "update_config":
                    for k, v in params.items():
                        set_config(k, v)
                        if k in cfg:
                            cfg[k] = v
                    complete_command(cmd_id)

                else:
                    # Queue other commands for later
                    pass

            if cashed_out:
                break

            sleep_secs = min(poll_s, end_time - time.monotonic())
            if sleep_secs > 0:
                time.sleep(sleep_secs)

            try:
                m = client.get_market(ticker)
                if side == "yes":
                    cur_bid = m.get("yes_bid", 0) or 0
                    cur_ask = m.get("yes_ask", 0) or 0
                else:
                    cur_bid = m.get("no_bid", 0) or 0
                    cur_ask = m.get("no_ask", 0) or 0

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

                # Feed Observatory — never stop observing
                if _observer:
                    try:
                        _obs_mon = {
                            "yes_ask": m.get("yes_ask"),
                            "no_ask": m.get("no_ask"),
                            "yes_bid": m.get("yes_bid"),
                            "no_bid": m.get("no_bid"),
                            "btc_price": snapshot.get("btc_price") if snapshot else None,
                            "volume": m.get("volume"),
                            "open_interest": m.get("open_interest"),
                        }
                        _observer.tick(ticker, close_str, _obs_mon, snapshot,
                                       _strategy_risk)
                    except Exception:
                        pass

                # Check sell fill progress
                sell_progress = 0
                if sell_order_id:
                    sell_status = client.get_order(sell_order_id)
                    sell_progress = sell_status.get("fill_count", 0)
                    if sell_progress >= fill_count:
                        blog("INFO", f"Sell fully filled! {sell_progress}/{fill_count}")
                        # Update dashboard immediately before breaking
                        active_trade["current_bid"] = cur_bid
                        active_trade["high_water_c"] = high_water_c
                        active_trade["sell_progress"] = sell_progress
                        active_trade["minutes_left"] = 0
                        update_bot_state({"status": "trading", "active_trade": active_trade})
                        break

                secs_left = end_time - time.monotonic()
                mins_rem = max(secs_left / 60, 0)

                # ── Dynamic sell: recalculate sell target from fair value model ──
                if (_dynamic_sell_active and sell_order_id
                        and _fair_value_model and _fv_btc_open and _fv_btc_open > 0
                        and mins_rem > 0.5):  # Don't adjust in final 30s
                    try:
                        # Refresh BTC price (reuse throttled global)
                        _ds_now = time.time()
                        if _ds_now - _fv_last_btc_fetch >= 10:
                            _ds_btc = get_live_btc_price()
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

                            if side == "yes":
                                _ds_fv_now = _ds_prob["fair_yes_c"]
                            else:
                                _ds_fv_now = _ds_prob["fair_no_c"]

                            # New target: sell just below fair value, but never below entry
                            _ds_new_target = max(int(_ds_fv_now) - 1, avg_price_c + 1)
                            _ds_new_target = min(_ds_new_target, 99)

                            _ds_diff = _ds_new_target - sell_price_c

                            # Only adjust if change exceeds floor and sell not partially filled
                            if abs(_ds_diff) >= _dynamic_sell_floor and sell_progress == 0:
                                try:
                                    client.cancel_order(sell_order_id)
                                    _ds_resp = client.place_limit_order(
                                        ticker, side,
                                        fill_count - sell_progress,
                                        _ds_new_target, action="sell"
                                    )
                                    _ds_order = _ds_resp.get("order", {})
                                    _ds_oid = _ds_order.get("order_id")
                                    if _ds_oid:
                                        sell_order_id = _ds_oid
                                        sell_price_c = _ds_new_target
                                        active_trade["sell_order_id"] = _ds_oid
                                        active_trade["sell_price_c"] = _ds_new_target
                                        _dynamic_sell_adjustments += 1
                                        active_trade["dynamic_adjustments"] = _dynamic_sell_adjustments
                                        active_trade["dynamic_fv"] = round(_ds_fv_now, 1)
                                        blog("INFO", f"Dynamic sell #{_dynamic_sell_adjustments}: "
                                                      f"FV={_ds_fv_now:.1f}c → "
                                                      f"sell@{_ds_new_target}c "
                                                      f"({'↑' if _ds_diff > 0 else '↓'}{abs(_ds_diff)}c)")
                                except Exception as _ds_err:
                                    blog("WARNING", f"Dynamic sell adjust failed: {_ds_err}")
                            else:
                                # Update FV display even without adjustment
                                active_trade["dynamic_fv"] = round(_ds_fv_now, 1)
                                active_trade["dynamic_adjustments"] = _dynamic_sell_adjustments

                            # Early exit via model: if FV drops well below entry
                            # and we're past the midpoint of the market
                            if (_ds_fv_now < avg_price_c - 5 and mins_rem < 7
                                    and cur_bid > 0 and cur_bid < avg_price_c
                                    and sell_progress == 0):
                                blog("INFO", f"Dynamic sell: FV {_ds_fv_now:.1f}c << "
                                              f"entry {avg_price_c}c — cutting losses at {cur_bid}c")
                                try:
                                    client.cancel_order(sell_order_id)
                                    _ds_exit = client.place_limit_order(
                                        ticker, side,
                                        fill_count, cur_bid, action="sell"
                                    )
                                    _ds_exit_o = _ds_exit.get("order", {})
                                    _ds_exit_oid = _ds_exit_o.get("order_id")
                                    if _ds_exit_oid:
                                        sell_order_id = _ds_exit_oid
                                        sell_price_c = cur_bid
                                        active_trade["sell_order_id"] = _ds_exit_oid
                                        active_trade["sell_price_c"] = cur_bid
                                        _dynamic_sell_active = False  # Stop adjusting
                                        blog("INFO", f"Dynamic early exit at {cur_bid}c")
                                        update_trade(trade_id, {"is_early_exit": 1, "early_exit_price_c": cur_bid})
                                        est_pnl = fill_count * cur_bid / 100 - actual_cost
                                        notify_early_exit(cur_bid, est_pnl, regime_label, mins_left=mins_rem)
                                except Exception as _ds_exit_err:
                                    blog("WARNING", f"Dynamic early exit failed: {_ds_exit_err}")
                    except Exception as _ds_loop_err:
                        log.debug(f"Dynamic sell loop error: {_ds_loop_err}")

                # ── Trailing stop: lock in gains once price reaches threshold ──
                trailing_pct = float(cfg.get("trailing_stop_pct", 0))
                if trailing_pct > 0 and sell_order_id and sell_price_c > avg_price_c:
                    target_range = sell_price_c - avg_price_c
                    progress = (high_water_c - avg_price_c) / target_range if target_range > 0 else 0
                    if progress >= (trailing_pct / 100):
                        # Trailing stop is active — floor is HWM minus buffer
                        trail_buffer = max(2, int(target_range * 0.15))
                        trail_floor = high_water_c - trail_buffer
                        if cur_bid > 0 and cur_bid <= trail_floor and trail_floor > avg_price_c:
                            blog("INFO", f"Trailing stop triggered: bid {cur_bid}c "
                                          f"<= floor {trail_floor}c (HWM {high_water_c}c)")
                            try:
                                client.cancel_order(sell_order_id)
                                # Place sell at current bid to exit immediately
                                exit_resp = client.place_limit_order(
                                    ticker, side, fill_count - sell_progress,
                                    cur_bid, action="sell"
                                )
                                exit_order = exit_resp.get("order", {})
                                exit_oid = exit_order.get("order_id")
                                if exit_oid:
                                    sell_order_id = exit_oid
                                    sell_price_c = cur_bid
                                    active_trade["sell_order_id"] = exit_oid
                                    active_trade["sell_price_c"] = cur_bid
                                    blog("INFO", f"Trailing exit: selling at {cur_bid}c")
                                    update_trade(trade_id, {"is_early_exit": 1, "early_exit_price_c": cur_bid})
                            except Exception as e:
                                blog("WARNING", f"Trailing stop exit failed: {e}")

                # ── Early exit EV: cut losses when holding is negative EV ──
                early_exit_enabled = bool(cfg.get("early_exit_ev", False))
                if (early_exit_enabled and sell_order_id and mins_rem < 2
                        and cur_bid > 0 and cur_bid < avg_price_c):
                    # With <2 min left and bid below entry, compare:
                    #   Sell now: lose (cost - bid*shares) — certain
                    #   Hold: market-implied EV ≈ bid (for our side)
                    # Early exit if we'd save >2c/contract vs expected hold outcome
                    hold_ev_c = cur_bid  # Market-implied value per contract
                    # But adjust for time: with <2 min, market might not be pricing
                    # in the reduced recovery probability. Apply a haircut.
                    time_haircut = max(0.7, mins_rem / 2)  # 1 min left → 0.7x
                    adjusted_hold_ev = hold_ev_c * time_haircut
                    sell_now_value = cur_bid

                    if sell_now_value > adjusted_hold_ev + 2:
                        blog("INFO", f"Early exit: sell@{cur_bid}c > "
                                      f"hold EV {adjusted_hold_ev:.0f}c "
                                      f"(~{mins_rem:.1f} min left)")
                        try:
                            client.cancel_order(sell_order_id)
                            exit_resp = client.place_limit_order(
                                ticker, side, fill_count - sell_progress,
                                cur_bid, action="sell"
                            )
                            exit_order = exit_resp.get("order", {})
                            exit_oid = exit_order.get("order_id")
                            if exit_oid:
                                sell_order_id = exit_oid
                                sell_price_c = cur_bid
                                active_trade["sell_order_id"] = exit_oid
                                active_trade["sell_price_c"] = cur_bid
                                update_trade(trade_id, {"is_early_exit": 1, "early_exit_price_c": cur_bid})
                        except Exception as e:
                            blog("WARNING", f"Early exit failed: {e}")

                # Write price point to DB (throttled to every ~5s)
                now_mono = time.monotonic()
                if now_mono - last_db_write >= 5:
                    last_db_write = now_mono
                    insert_price_point(trade_id, {
                        "minutes_left": round(mins_rem, 2),
                        "yes_bid": m.get("yes_bid"), "yes_ask": m.get("yes_ask"),
                        "no_bid": m.get("no_bid"), "no_ask": m.get("no_ask"),
                        "our_side_bid": cur_bid, "our_side_ask": cur_ask,
                        "btc_price": get_live_btc_price(),
                    })
                    # Also refresh live market data for dashboard
                    try:
                        poll_live_market(client, cfg)
                    except Exception:
                        pass

                # Update active trade for dashboard
                active_trade["current_bid"] = cur_bid
                active_trade["high_water_c"] = high_water_c
                active_trade["sell_progress"] = sell_progress
                active_trade["minutes_left"] = round(mins_rem, 1)
                update_bot_state({"status": "trading", "active_trade": active_trade})

                # Minute-by-minute trade update notification (silent)
                if time.monotonic() - _last_trade_notify >= 60 and mins_rem > 0.5:
                    _last_trade_notify = time.monotonic()
                    try:
                        notify_trade_update(
                            side=side, cur_bid=cur_bid,
                            avg_price_c=avg_price_c,
                            sell_price_c=sell_price_c,
                            mins_left=mins_rem,
                            fill_count=fill_count,
                            actual_cost=actual_cost,
                            regime_label=regime_label,
                        )
                    except Exception:
                        pass

            except Exception:
                pass

    # ── 11. Resolve outcome ───────────────────────────────────
    # If cashed out, trade was already finalized — skip everything
    if cashed_out:
        blog("INFO", "Trade was cashed out — skipping outcome resolution")
        return True

    # Final price update to avoid stale data on dashboard
    try:
        m = client.get_market(ticker)
        final_bid = m.get(f"{side}_bid", 0) or 0
        active_trade["current_bid"] = final_bid
        active_trade["minutes_left"] = 0
        update_bot_state({"status": "trading", "active_trade": active_trade})
    except Exception:
        pass

    # Check if sell fully filled during monitoring
    sell_filled = 0
    if sell_order_id:
        sell_filled = client.get_order(sell_order_id).get("fill_count", 0)

    if sell_filled >= fill_count:
        # ── FAST PATH: Sell filled — we know exact PnL immediately ──
        gross = fill_count * sell_price_c / 100
        pnl = gross - actual_cost
        trade_won = pnl > 0
        outcome = "win" if trade_won else "loss"

        # Fetch market result for records (may not be available yet — that's OK)
        market_result = None
        try:
            market_result = client.get_market_result(ticker)
        except Exception:
            pass

        # BTC price at exit for movement analysis
        btc_exit = get_live_btc_price()
        btc_entry = active_trade.get("btc_price")
        btc_move = None
        if btc_exit and btc_entry and btc_entry > 0:
            btc_move = round((btc_exit - btc_entry) / btc_entry * 100, 4)

        blog("INFO", f"SELL FILLED — instant resolve: {outcome.upper()} | "
                      f"cost=${actual_cost:.2f} gross=${gross:.2f} pnl={fpnl(pnl)}")
        blog("INFO", f"  HWM={high_water_c}c LWM={low_water_c}c | "
                      f"sell_filled={sell_filled}/{fill_count}")

        # Calculate time to target from entry
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
        # Get final market price to update dashboard
        try:
            final_m = client.get_market(ticker)
            if side == "yes":
                final_bid = final_m.get("yes_bid", 0) or 0
            else:
                final_bid = final_m.get("no_bid", 0) or 0
            active_trade["current_bid"] = final_bid
        except Exception:
            pass
        active_trade["resolving"] = True
        active_trade["minutes_left"] = 0
        update_bot_state({
            "status": "trading",
            "status_detail": f"Resolving {ticker}...",
            "active_trade": active_trade,
        })

        time.sleep(3)  # Buffer for Kalshi to settle

        market_result = None
        for _ in range(10):
            market_result = client.get_market_result(ticker)
            if market_result:
                break
            # Update price during resolve wait
            try:
                rm = client.get_market(ticker)
                resolve_bid = rm.get(f"{side}_bid", 0) or 0
                if resolve_bid > 0:
                    active_trade["current_bid"] = resolve_bid
                    update_bot_state({"active_trade": active_trade})
            except Exception:
                pass
            time.sleep(3)

        won = (market_result == side) if market_result else False
        gross = client.calc_gross(fill_count, sell_filled, sell_price_c, won)
        pnl = gross - actual_cost
        trade_won = gross > actual_cost
        outcome = "win" if trade_won else "loss"

        # Update price to true final value (100¢ if won, 1¢ if lost)
        final_price = 99 if won else 1
        active_trade["current_bid"] = final_price
        active_trade["resolving"] = True
        update_bot_state({"active_trade": active_trade})

        pct_progress = 0.0
        if sell_price_c > avg_price_c:
            pct_progress = ((high_water_c - avg_price_c) /
                            (sell_price_c - avg_price_c)) * 100
            pct_progress = max(0, min(100, pct_progress))

        blog("INFO", f"Result: {outcome.upper()} | "
                      f"market={market_result} side={side} | "
                      f"cost=${actual_cost:.2f} gross=${gross:.2f} pnl={fpnl(pnl)}")
        blog("INFO", f"  HWM={high_water_c}c LWM={low_water_c}c | "
                      f"sell_filled={sell_filled}/{fill_count} | "
                      f"progress={pct_progress:.0f}%")

        if market_result:
            update_market_outcome(market_id, market_result)

        # BTC price at exit for movement analysis
        btc_exit = get_live_btc_price()
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


    # ── 12. Update outcome ────────────────────────────────
    state = get_bot_state()  # Fresh read
    new_bankroll = client.get_balance_cents()

    # Session stats — update in ALL paths (ignored, data, normal)
    sess_win_key = "session_wins" if trade_won else "session_losses"
    sess_update = {
        sess_win_key: (state.get(sess_win_key) or 0) + 1,
        "session_pnl": (state.get("session_pnl") or 0) + pnl,
        "lifetime_pnl": (state.get("lifetime_pnl") or 0) + pnl,
        ("lifetime_wins" if trade_won else "lifetime_losses"):
            (state.get("lifetime_wins" if trade_won else "lifetime_losses") or 0) + 1,
        "bankroll_cents": new_bankroll,
    }
    update_bot_state(sess_update)

    # Record bankroll snapshot for charts
    insert_bankroll_snapshot(new_bankroll, trade_id)

    # Ignored trades: don't update regime stats
    if is_ignored:
        blog("INFO", f"IGNORED trade ({outcome} {fpnl(pnl)}) — not counted in stats")
        clear_active_trade()
        update_bot_state({
            "status": "searching",
            "status_detail": f"Last: {outcome} ({fpnl(pnl)}) [IGNORED]",
        })
        return True


    # Normal trade outcome handling
    if trade_won:
        # WIN — reset loss streak
        update_bot_state({"loss_streak": 0})
        blog("INFO", f"WIN +${pnl:.2f}")
        notify_trade_result("win", pnl, regime_label)
        update_bot_state({
            "status_detail": f"WIN {fpnl(pnl)} in {regime_label}",
        })
    else:
        # LOSS — increment loss streak, check safety stop
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
            blog("WARNING", f"LOSS STOP: {loss_streak} consecutive losses — stopping"
                            + (f" + {cooldown} market cooldown" if cooldown else ""))
            notify_max_loss(actual_cost, max_consec, cooldown)
        else:
            streak_info = f" (streak {loss_streak})" if loss_streak > 1 else ""
            update_data["status_detail"] = f"LOSS {fpnl(pnl)}{streak_info}"
            blog("INFO", f"LOSS {fpnl(pnl)} | streak={loss_streak}")

        notify_trade_result("loss", pnl, regime_label)
        update_bot_state(update_data)

    # Update regime stats (for non-ignored, non-data trades)
    if regime_label:
        _update_regime_with_notify(regime_label)

    # Store post-trade summary for dashboard
    summary = {
        "trade_id": trade_id,
        "ticker": ticker,
        "side": side,
        "outcome": outcome,
        "pnl": round(pnl, 2),
        "actual_cost": round(actual_cost, 2),
        "gross": round(gross, 2),
        "avg_price_c": avg_price_c,
        "sell_price_c": sell_price_c,
        "fill_count": fill_count,
        "sell_filled": sell_filled,
        "high_water_c": high_water_c,
        "market_result": market_result,
        "regime_label": regime_label,
        "risk_level": gate["risk_level"],
        "regime_win_rate": _strategy_risk.get("win_rate", 0) if _strategy_risk else 0,
        "regime_trades": _strategy_risk.get("sample_size", 0) if _strategy_risk else 0,
    }

    clear_active_trade()
    update_bot_state({
        "last_completed_trade": summary,
    })

    return True


# ═══════════════════════════════════════════════════════════════
#  SKIPPED MARKET RESULT BACKFILL
# ═══════════════════════════════════════════════════════════════

def backfill_skipped_results(client: KalshiClient, limit: int = 20):
    """
    Fetch market results for skipped trades.
    Runs periodically — fetches a batch each time.
    """
    trades = get_skipped_trades_needing_result(limit=limit)
    if not trades:
        return 0

    filled = 0
    for t in trades:
        ticker = t.get("ticker")
        if not ticker or ticker == "n/a":
            continue
        try:
            result = client.get_market_result(ticker)
            if result:
                backfill_skipped_result(t["id"], result)
                filled += 1

                # Update the markets table
                mid = t.get("market_id")
                if mid:
                    try:
                        update_market_outcome(mid, result)
                    except Exception:
                        pass
        except Exception:
            pass  # Market might not be settled yet

    if filled > 0:
        blog("INFO", f"Backfilled {filled}/{len(trades)} skipped market results")
    return filled


def backfill_trade_market_results(client, limit: int = 20):
    """
    Backfill market_result for real trades (wins/losses) where it's NULL.
    Also recalculates outcome/PnL if the trade was resolved before the result
    was available (prevents wins from being permanently recorded as losses).
    """
    with get_conn() as c:
        rows = c.execute("""
            SELECT id, ticker, market_id, side, shares_filled,
                   sell_filled, sell_price_c, actual_cost
            FROM trades
            WHERE outcome IN ('win', 'loss', 'open')
              AND market_result IS NULL
              AND ticker IS NOT NULL AND ticker != 'n/a'
              AND datetime(created_at) < datetime('now', '-3 minutes')
            ORDER BY created_at DESC LIMIT ?
        """, (limit,)).fetchall()
        trades = rows_to_list(rows)

    if not trades:
        return 0

    filled = 0
    corrected = 0
    for t in trades:
        try:
            result = client.get_market_result(t["ticker"])
            if result:
                updates = {"market_result": result}

                # Recalculate outcome/PnL if we have the data
                side = t.get("side")
                sfilled = t.get("shares_filled", 0) or 0
                sell_filled = t.get("sell_filled", 0) or 0
                sell_price_c = t.get("sell_price_c", 0) or 0
                actual_cost = t.get("actual_cost", 0) or 0

                if side and sfilled > 0 and actual_cost > 0:
                    won = (result == side)
                    gross = client.calc_gross(sfilled, sell_filled, sell_price_c, won)
                    pnl = gross - actual_cost
                    new_outcome = "win" if gross > actual_cost else "loss"
                    updates["outcome"] = new_outcome
                    updates["gross_proceeds"] = round(gross, 2)
                    updates["pnl"] = round(pnl, 2)
                    if not t.get("exit_time_utc"):
                        updates["exit_time_utc"] = now_utc()
                        updates["exit_method"] = "market_expiry"
                    corrected += 1

                update_trade(t["id"], updates)
                if t.get("market_id"):
                    try:
                        update_market_outcome(t["market_id"], result)
                    except Exception:
                        pass
                filled += 1
        except Exception:
            pass

    if filled > 0:
        blog("INFO", f"Backfilled {filled}/{len(trades)} trade market results"
                      f"{f' ({corrected} outcomes recalculated)' if corrected else ''}")
        if corrected > 0:
            try:
                recompute_all_stats()
                blog("INFO", f"Stats recomputed after {corrected} outcome correction(s)")
            except Exception as e:
                blog("WARNING", f"Stats recompute after backfill failed: {e}")
    return filled




# ═══════════════════════════════════════════════════════════════
#  MAIN LOOP
# ═══════════════════════════════════════════════════════════════

def main():
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(
                __import__("config").LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(),
        ]
    )

    blog("INFO", "=" * 50)
    blog("INFO", "Kalshi BTC Trading Bot starting")
    blog("INFO", "=" * 50)

    # ── Crash diagnostic: check if previous run was OOM-killed ──
    try:
        import subprocess as _sp
        # Check dmesg for recent OOM kills (try with iso format, fallback to raw)
        try:
            _dmesg = _sp.run(["dmesg", "--time-format=iso", "-l", "err,crit,alert,emerg"],
                             capture_output=True, text=True, timeout=5)
        except Exception:
            _dmesg = _sp.run(["dmesg"], capture_output=True, text=True, timeout=5)
        _oom_lines = [l for l in (_dmesg.stdout or "").splitlines()
                      if "oom" in l.lower() or "killed process" in l.lower()
                      or "out of memory" in l.lower()]
        if _oom_lines:
            # Show last 5 OOM-related lines
            blog("WARNING", f"OOM KILLER DETECTED — last {min(5, len(_oom_lines))} entries:")
            for _ol in _oom_lines[-5:]:
                blog("WARNING", f"  {_ol.strip()}")
            # Send push notification with crash info
            _oom_summary = _oom_lines[-1].strip()[:150] if _oom_lines else "Unknown"
            try:
                from push import send_to_all
                send_to_all("Bot Crashed — OOM Kill",
                           f"Previous run killed by OOM killer. Last entry: {_oom_summary}",
                           tag="crash", url="/")
            except Exception:
                pass
        # Log system memory
        _mem = _sp.run(["free", "-m"], capture_output=True, text=True, timeout=5)
        if _mem.stdout:
            for _ml in _mem.stdout.strip().splitlines():
                blog("INFO", f"  MEM: {_ml}")
        # Check supervisor config
        _sv = _sp.run(["supervisorctl", "status"],
                      capture_output=True, text=True, timeout=5)
        if _sv.stdout:
            for _sl in _sv.stdout.strip().splitlines():
                blog("INFO", f"  SUPERVISOR: {_sl}")
        _svconf = _sp.run(["grep", "-r", "autorestart", "/etc/supervisor/conf.d/"],
                          capture_output=True, text=True, timeout=5)
        if _svconf.stdout:
            for _cl in _svconf.stdout.strip().splitlines():
                blog("INFO", f"  SVCONF: {_cl}")
        else:
            blog("WARNING", "  SVCONF: no autorestart setting found in supervisor configs")
    except Exception as _diag_e:
        blog("DEBUG", f"Startup diagnostic skipped: {_diag_e}")

    init_db()

    # One-time data migration: clean slate for v2 (preserves observations)
    from db import run_migration_v2_clean_slate
    if run_migration_v2_clean_slate():
        blog("INFO", "Migration v2 applied: cleared trades/stats, preserved observations")

    # Initialize Strategy Observatory
    global _observer
    _observer = MarketObserver()
    blog("INFO", "Strategy Observatory initialized")

    # Initialize BTC Fair Value Model
    global _fair_value_model
    _fair_value_model = BtcFairValueModel()
    try:
        _fair_value_model.load(force=True)
        status = _fair_value_model.get_status()
        if status["ready"]:
            blog("INFO", f"Fair value model ready ({status['cells_loaded']} surface cells)")
        else:
            blog("INFO", f"Fair value model: insufficient data ({status['cells_loaded']}/{status['min_cells_needed']} cells) — will use analytical fallback")
    except Exception as e:
        blog("WARNING", f"Fair value model init error: {e}")

    if not KALSHI_API_KEY_ID or not KALSHI_PRIVATE_KEY_PATH:
        blog("ERROR", "Missing Kalshi credentials. Set KALSHI_API_KEY_ID "
                       "and KALSHI_PRIVATE_KEY_PATH.")
        return

    client = KalshiClient(KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH)

    try:
        balance = client.get_balance_cents()
        blog("INFO", f"Connected to Kalshi. Balance: ${balance / 100:.2f}")
        update_bot_state({"bankroll_cents": balance})
    except Exception as e:
        blog("ERROR", f"Cannot connect to Kalshi: {e}")
        return

    cfg = load_config()
    state = get_bot_state()

    # Flush stale commands from before restart FIRST
    flush_pending_commands()
    blog("INFO", "Flushed stale command queue")

    # ── Check for orphaned active trade from previous run ──
    orphan = state.get("active_trade")
    if orphan:
        ticker = orphan.get("ticker", "")
        blog("WARNING", f"Found orphaned active trade: {ticker}")
        try:
            close_str = orphan.get("close_time", "")
            if close_str:
                close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
                secs_left = (close_dt - datetime.now(timezone.utc)).total_seconds()
                if secs_left < -10:
                    # Market already closed — resolve it
                    blog("INFO", "Market already closed — resolving orphaned trade")
                    _resolve_orphan_trade(client, orphan)
                else:
                    # Market still open — check if buy order actually filled
                    buy_oid = orphan.get("buy_order_id")
                    stored_fills = orphan.get("fill_count", 0)
                    live_fills = stored_fills

                    if buy_oid:
                        try:
                            live_order = client.get_order(buy_oid)
                            live_fills = live_order.get("fill_count", 0)
                        except Exception:
                            pass

                    if live_fills == 0:
                        # No contracts filled — cancel any pending orders and skip
                        blog("INFO", f"Orphaned trade has 0 fills — canceling and skipping market")
                        if buy_oid:
                            try:
                                client.cancel_order(buy_oid)
                                blog("INFO", f"Canceled unfilled buy order {buy_oid}")
                            except Exception as ce:
                                blog("WARNING", f"Failed to cancel buy order: {ce}")
                        sell_oid = orphan.get("sell_order_id")
                        if sell_oid:
                            try:
                                client.cancel_order(sell_oid)
                            except Exception:
                                pass
                        # Mark the DB trade as skipped
                        tid = orphan.get("trade_id")
                        if tid:
                            update_trade(tid, {
                                "outcome": "skipped",
                                "skip_reason": "Restart — no fills, canceled",
                                "is_ignored": 1,
                                "notes": "Orphaned on restart with 0 fills — canceled",
                            })
                        clear_active_trade()
                        blog("INFO", "Orphaned trade cleared — will wait for next market")
                    else:
                        # Real money at stake — monitor to completion
                        blog("INFO", f"Market still open ({secs_left:.0f}s left), "
                                      f"{live_fills} contracts filled — monitoring to completion")
                        update_bot_state({"status": "trading"})
                        # Block until market closes, monitoring the trade
                        while True:
                            # Process commands during orphan monitoring
                            for cmd in get_pending_commands():
                                cmd_type = cmd["command_type"]
                                cmd_id = cmd["id"]
                                params = json.loads(cmd.get("parameters") or "{}")
                                if cmd_type == "cash_out":
                                    state_now = get_bot_state()
                                    result = execute_cash_out(client, state_now)
                                    complete_command(cmd_id, result)
                                    blog("INFO", "Cash out during orphan monitoring")
                                    break
                                elif cmd_type == "stop":
                                    state_now = get_bot_state()
                                    at_now = state_now.get("active_trade")
                                    if at_now:
                                        at_now["is_ignored"] = True
                                        update_bot_state({"auto_trading": 0, "active_trade": at_now})
                                    complete_command(cmd_id)
                                elif cmd_type == "update_config":
                                    for k, v in params.items():
                                        set_config(k, v)
                                        if k in cfg: cfg[k] = v
                                    complete_command(cmd_id)
                                else:
                                    complete_command(cmd_id)

                            try:
                                monitor_orphan_trade(client, cfg)
                            except Exception:
                                pass
                            # Check if trade resolved
                            check_state = get_bot_state()
                            if not check_state.get("active_trade"):
                                blog("INFO", "Orphaned trade resolved")
                                break
                            secs_now = (close_dt - datetime.now(timezone.utc)).total_seconds()
                            if secs_now < -30:
                                blog("WARNING", "Orphan monitor timed out — force resolving")
                                _resolve_orphan_trade(client, check_state.get("active_trade"))
                                break
                            time.sleep(2)
            else:
                # No close time — just clear it
                blog("WARNING", "Orphaned trade has no close_time — clearing")
                clear_active_trade()
        except Exception as e:
            blog("ERROR", f"Error resolving orphan: {e}")
            clear_active_trade()

    # ── Resolve ALL stale open trades in DB ────────────────
    try:
        # Skip the currently active trade (if any survived orphan check)
        active_tid = None
        active_now = get_bot_state().get("active_trade")
        if active_now:
            active_tid = active_now.get("trade_id")

        stale_trades = [t for t in get_recent_trades(100)
                        if t.get("outcome") == "open" and t["id"] != active_tid]
        if stale_trades:
            blog("INFO", f"Found {len(stale_trades)} stale open trade(s) — resolving")
        for st_trade in stale_trades:
            tid = st_trade["id"]
            sticker = st_trade.get("ticker", "")
            sside = st_trade.get("side", "yes")
            sfilled = st_trade.get("shares_filled", 0)
            scost = st_trade.get("actual_cost", 0)
            ssell_price = st_trade.get("sell_price_c") or 0
            ssell_oid = st_trade.get("sell_order_id")

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
            else:
                # Market result unavailable — defer to periodic backfill
                # rather than wrongly marking as loss
                blog("WARNING", f"  No market result for {sticker} — "
                                 f"leaving as open for backfill to resolve")
                continue

            update_trade(tid, {
                "outcome": outcome,
                "gross_proceeds": round(gross if market_result else 0, 2),
                "pnl": round(pnl, 2),
                "sell_filled": sell_filled,
                "exit_time_utc": now_utc(),
                "market_result": market_result,
                "exit_method": "market_expiry",
                "notes": "Resolved on startup",
            })
    except Exception as e:
        blog("ERROR", f"Error cleaning stale trades: {e}")

    # Recompute all stats from DB to ensure consistency after orphan resolution
    try:
        recompute_all_stats()
        blog("INFO", "Stats recomputed from trade history")
    except Exception as e:
        blog("ERROR", f"Error recomputing stats: {e}")

    # ── Save pre-restart trading state before resetting ──────
    pre_state = get_bot_state()
    was_auto_trading = bool(pre_state.get("auto_trading", 0))
    was_trades_remaining = pre_state.get("trades_remaining", 0)

    blog("INFO", f"Pre-restart state: auto_trading={was_auto_trading}, "
                  f"trades_remaining={was_trades_remaining}")

    # Reset transient state
    update_bot_state({
        "status": "stopped",
        "status_detail": "Restarting...",
        "bankroll_cents": balance,
        "auto_trading": 0,
        "trades_remaining": 0,
        "pending_trade": None,
        "active_shadow": None,
        "active_skip": None,
        "cashing_out": 0,
        "cancel_cash_out": 0,
    })
    insert_bankroll_snapshot(balance)

    sell_c = int(cfg.get('sell_target_c', 0) or 0)
    sell_desc = f"sell@{sell_c}c" if sell_c else "hold"
    blog("INFO", f"Config: bet={cfg.get('bet_mode')} ${cfg.get('bet_size')} | "
                  f"sell_target={sell_desc} | "
                  f"entry≤{cfg.get('entry_price_max_c')}c")

    # Start regime analysis background thread
    stop_event = threading.Event()
    regime_thread = threading.Thread(
        target=regime_worker, args=(stop_event,), daemon=True
    )
    regime_thread.start()

    # Flush again — catches any commands sent DURING orphan monitoring
    flush_pending_commands()

    # Backfill market results for any previously skipped trades
    try:
        backfill_skipped_results(client, limit=20)
        backfill_trade_market_results(client, limit=20)
        backfill_observation_results(client, limit=30)
    except Exception as e:
        blog("WARNING", f"Startup backfill error: {e}")

    # ── Restore auto-trading if it was active before restart ──
    if was_auto_trading:
        trade_mode = cfg.get("trade_mode", "continuous")

        # Check if we should actually resume
        should_resume = True
        resume_reason = f"Resuming: mode={trade_mode}"

        # Don't resume single-trade mode if no trades were remaining
        if trade_mode == "single" and was_trades_remaining <= 0:
            should_resume = False
            resume_reason = "Single trade mode completed"

        # Don't resume count mode if no trades remaining
        if trade_mode == "count" and was_trades_remaining <= 0:
            should_resume = False
            resume_reason = "All counted trades completed"

        # Check bankroll safety
        try:
            eff_bankroll = get_effective_bankroll_cents(client, cfg)
            bmin = float(cfg.get("bankroll_min", 0) or 0) * 100
            if bmin > 0 and eff_bankroll < bmin:
                should_resume = False
                resume_reason = f"Bankroll ${eff_bankroll/100:.2f} below minimum ${bmin/100:.2f}"
        except Exception:
            pass

        if should_resume:
            # Deploy cooldown: delay auto-resume after restart
            cooldown_min = float(cfg.get("deploy_cooldown_minutes", 0))
            if cooldown_min > 0:
                should_resume = False
                resume_reason = (f"Deploy cooldown: waiting {cooldown_min:.0f}m "
                                 f"before auto-resume (set deploy_cooldown_minutes=0 to skip)")
                blog("INFO", f"DEPLOY COOLDOWN: {cooldown_min:.0f}m delay active. "
                              f"Send 'start' command to override, or wait.")
                update_bot_state({
                    "status": "stopped",
                    "status_detail": f"Deploy cooldown — {cooldown_min:.0f}m pause. "
                                     f"Press Start to override.",
                })
                # Notify operator
                try:
                    from push import send_push
                    send_push(f"Deploy cooldown: {cooldown_min:.0f}m pause before auto-resume. "
                              f"Press Start to override.", tag="deploy")
                except Exception:
                    pass

        if should_resume:
            # Check if the previous status was a skip — preserve that context
            prev_detail = pre_state.get("status_detail", "")
            was_skipping = "kipped" in prev_detail or "skip" in prev_detail.lower()

            resume_state = {
                "auto_trading": 1,
                "auto_trading_since": now_utc(),
                "status": "searching",
            }
            if trade_mode == "single":
                resume_state["trades_remaining"] = max(was_trades_remaining, 1)
                resume_state["status_detail"] = "Auto-resumed — single trade"
            elif trade_mode == "count":
                resume_state["trades_remaining"] = max(was_trades_remaining, 1)
                n = resume_state["trades_remaining"]
                resume_state["status_detail"] = f"Auto-resumed — {n} trade{'s' if n != 1 else ''} left"
            else:
                resume_state["trades_remaining"] = 0
                resume_state["status_detail"] = "Auto-resumed — continuous"

            # If the bot was mid-skip, show that instead of generic resume message
            if was_skipping and prev_detail:
                resume_state["status"] = "waiting"
                resume_state["status_detail"] = prev_detail

            # Restore any ignored active trade back to normal
            at_pre = pre_state.get("active_trade")
            if at_pre and (at_pre.get("is_ignored")):
                at_pre["is_ignored"] = False
                resume_state["active_trade"] = at_pre
                resume_state["status"] = "trading"
                resume_state["status_detail"] = "Auto-resumed — monitoring active trade"
                tid = at_pre.get("trade_id")
                if tid:
                    update_trade(tid, {"is_ignored": 0, "notes": "Restored on auto-resume"})
                blog("INFO", f"Restored ignored trade to active on auto-resume")

            update_bot_state(resume_state)
            blog("INFO", f"AUTO-RESUMED: {resume_reason}, "
                          f"trades_remaining={resume_state.get('trades_remaining', 0)}")
            # Only notify if bot was actually down for a while (not a brief process recycle)
            try:
                last_update = pre_state.get("last_updated", "")
                if last_update:
                    last_dt = datetime.fromisoformat(last_update.replace("Z", "+00:00"))
                    down_secs = (datetime.now(timezone.utc) - last_dt).total_seconds()
                    if down_secs > 60:
                        from push import send_to_all
                        send_to_all("Bot Auto-Resumed",
                                   f"Trading resumed after {int(down_secs)}s downtime ({trade_mode})",
                                   tag="deploy", url="/")
                        blog("INFO", f"Resume notification sent (down {down_secs:.0f}s)")
                    else:
                        blog("INFO", f"Skipped resume notification (only down {down_secs:.0f}s)")
            except Exception:
                pass
        else:
            update_bot_state({
                "status": "stopped",
                "status_detail": resume_reason,
            })
            blog("INFO", f"NOT resuming: {resume_reason}")
    else:
        update_bot_state({
            "status": "stopped",
            "status_detail": "Idle — press play to start",
        })
        blog("INFO", "Bot was not auto-trading before restart — staying stopped")

    # ── Auto-start in data-collecting modes ──────────────────
    # In observe/shadow/hybrid, the bot should ALWAYS be collecting data.
    # If we reach here without auto_trading=1 (manual stop, circuit
    # breaker, crash after stop), force-start observation.
    cfg = load_config()
    _startup_mode = get_trading_mode(cfg)
    if _startup_mode in ("observe", "shadow", "hybrid") and not get_bot_state().get("auto_trading"):
        _mode_labels = {"observe": "observe-only", "shadow": "shadow", "hybrid": "hybrid"}
        update_bot_state({
            "auto_trading": 1,
            "auto_trading_since": now_utc(),
            "status": "searching",
            "status_detail": f"Auto-started — {_mode_labels[_startup_mode]} mode",
            "trades_remaining": 0,
        })
        blog("INFO", f"AUTO-START: {_mode_labels[_startup_mode]} mode — always collecting data")
        try:
            from push import send_to_all
            send_to_all("Bot Auto-Started",
                       f"{_mode_labels[_startup_mode].title()} mode: auto-started data collection after restart.",
                       tag="deploy", url="/")
        except Exception:
            pass

    try:
        _consecutive_errors = 0
        _was_auto_trading = bool(get_bot_state().get("auto_trading", 0))
        _last_backfill = 0  # monotonic timestamp for skipped-market backfill
        _last_log_cleanup = 0  # monotonic timestamp for log file/table cleanup
        _prev_balance_c = None  # for balance sanity check
        _idle_last_ticker = None  # for idle-mode observed notifications
        while True:
            try:
                cfg = process_commands(client, cfg)
                state = get_bot_state()

                auto_trading = bool(state.get("auto_trading", 0))
                should_run = auto_trading

                # Detect start transition — record timestamp
                if auto_trading and not _was_auto_trading:
                    update_bot_state({"auto_trading_since": now_utc()})

                # Detect stop transition — record timestamp for session resume logic
                if _was_auto_trading and not auto_trading:
                    if not state.get("session_stopped_at"):
                        update_bot_state({"session_stopped_at": now_utc()})
                    update_bot_state({"auto_trading_since": ""})
                _was_auto_trading = auto_trading

                # Periodic backfill — runs regardless of trading state
                if time.monotonic() - _last_backfill > 300:
                    try:
                        backfill_skipped_results(client, limit=20)
                        backfill_trade_market_results(client, limit=20)
                        backfill_observation_results(client, limit=30)
                    except Exception:
                        pass
                    # Update Observatory health metrics
                    if _observer:
                        try:
                            health = _observer.get_health()
                            update_bot_state({"observatory_health": json.dumps(health)})
                            if health["total_attempted"] > 0:
                                blog("DEBUG", f"Observatory health: {health['written']} written, "
                                              f"{health['dropped_partial']} partial, "
                                              f"{health['dropped_short']} short, "
                                              f"drop rate {health['drop_rate_pct']}%")
                        except Exception:
                            pass
                    _last_backfill = time.monotonic()

                    # Periodic log cleanup — every 6 hours
                    if time.monotonic() - _last_log_cleanup > 21600:
                        try:
                            retention_days = int(cfg.get("log_retention_days", 7) or 7)
                            _cleanup_logs(retention_days)
                        except Exception:
                            pass
                        _last_log_cleanup = time.monotonic()

                    # Balance sanity check — detect unexpected large drops
                    try:
                        cur_balance = client.get_balance_cents()
                        if _prev_balance_c is not None and _prev_balance_c > 0:
                            drop_pct = (_prev_balance_c - cur_balance) / _prev_balance_c
                            if drop_pct > 0.5 and cur_balance < _prev_balance_c - 5000:
                                # Balance dropped >50% and >$50 since last check
                                blog("WARNING", f"BALANCE ANOMALY: ${_prev_balance_c/100:.2f} "
                                                 f"→ ${cur_balance/100:.2f} "
                                                 f"({drop_pct:.0%} drop)")
                                try:
                                    from push import send_push
                                    send_push(f"Balance anomaly: dropped {drop_pct:.0%} "
                                              f"(${_prev_balance_c/100:.2f} → "
                                              f"${cur_balance/100:.2f})", tag="anomaly")
                                except Exception:
                                    pass
                        _prev_balance_c = cur_balance
                    except Exception:
                        pass

                if not should_run:

                    # Check for ANY active trade (orphaned from restart or stop)
                    active = state.get("active_trade")
                    if active:
                        update_bot_state({"status": "trading"})
                        monitor_orphan_trade(client, cfg)
                        time.sleep(1)
                        continue

                    # Idle — poll live market and compute projections
                    try:
                        poll_live_market(client, cfg)
                    except Exception:
                        pass

                    # In data-collecting modes, send observed notification on market transition
                    # (normally this only fires from run_trade which requires auto_trading)
                    cfg = load_config()
                    _idle_mode = get_trading_mode(cfg)
                    is_obs = _idle_mode in ("observe", "shadow", "hybrid")
                    if is_obs:
                        try:
                            _fresh = get_bot_state()
                            _live = _fresh.get("live_market")
                            if isinstance(_live, str):
                                _live = json.loads(_live) if _live else {}
                            cur_ticker = _live.get("ticker") if isinstance(_live, dict) else None
                            if (cur_ticker and _idle_last_ticker
                                    and cur_ticker != _idle_last_ticker):
                                _regime = _live.get("regime_label", "unknown")
                                notify_observed(_regime, f"{_idle_mode.title()} mode")
                            _idle_last_ticker = cur_ticker
                        except Exception:
                            pass

                    detail = state.get("status_detail", "")
                    if not detail or detail == "Idle — press play to start" or detail == "Bot ready":
                        new_detail = "Observing — recording market data" if is_obs else "Idle — press play to start"
                    else:
                        new_detail = detail

                    bal_update = {}
                    try:
                        bal_update["bankroll_cents"] = client.get_balance_cents()
                    except Exception:
                        pass

                    update_bot_state({
                        "status": "stopped",
                        "status_detail": new_detail,
                        **bal_update,
                    })
                    _consecutive_errors = 0  # Reset on successful iteration
                    time.sleep(1)
                    continue

                # ── Auto-trading is ON ──
                # If there's an ignored/orphaned trade, monitor it and wait
                active = state.get("active_trade")
                if active and (active.get("is_ignored")):
                    monitor_orphan_trade(client, cfg)
                    time.sleep(1)
                    continue

                cfg = load_config()
                traded = run_trade(client, cfg)
                _consecutive_errors = 0  # Reset on successful iteration

                # After trade, check if we should stop
                post_state = get_bot_state()
                if traded and not post_state.get("auto_trading"):
                    # Was running, now stopped (stop clicked, count completed, etc.)
                    # Don't overwrite if mode already set a detailed status
                    detail = post_state.get("status_detail", "")
                    if not detail.startswith("Stopped"):
                        update_bot_state({
                            "status": "stopped",
                            "status_detail": "Stopped — trade completed",
                        })
                    blog("INFO", "Bot stopped after completing trade")
                    time.sleep(1)
                    continue

                trades_remaining = post_state.get("trades_remaining", 0)
                if traded and trades_remaining and trades_remaining > 0:
                    new_rem = trades_remaining - 1
                    update_bot_state({"trades_remaining": new_rem})
                    if new_rem <= 0:
                        update_bot_state({
                            "auto_trading": 0,
                            "trades_remaining": 0,
                            "status": "stopped",
                            "status_detail": "All trades completed — stopped",
                        })
                        blog("INFO", "All requested trades completed — stopped")

                time.sleep(2)

            except (KeyboardInterrupt, SystemExit):
                raise  # Let outer handler deal with it
            except Exception as e:
                _consecutive_errors += 1
                tb = traceback.format_exc()
                blog("ERROR", f"Unexpected error ({_consecutive_errors}x): {e}")
                blog("ERROR", f"Traceback:\n{tb}")
                notify_error(str(e))
                if _consecutive_errors >= 5:
                    # Too many consecutive errors — stop to prevent runaway
                    update_bot_state({
                        "status": "stopped",
                        "status_detail": f"Stopped: {_consecutive_errors} consecutive errors",
                        "auto_trading": 0,
                    })
                    blog("ERROR", f"Auto-trading stopped after {_consecutive_errors} consecutive errors")
                    _consecutive_errors = 0
                else:
                    update_bot_state({
                        "status_detail": f"Error ({_consecutive_errors}/5): {str(e)[:80]} — retrying",
                    })
                time.sleep(15)

    except KeyboardInterrupt:
        blog("INFO", "Shutting down (KeyboardInterrupt — stop)")
        update_bot_state({
            "status": "stopped",
            "status_detail": "Shutdown",
            "auto_trading": 0,
        })
    except SystemExit:
        # SIGTERM from supervisorctl — preserve auto_trading for resume
        blog("INFO", "Shutting down (SIGTERM — service restart)")
        update_bot_state({
            "status": "stopped",
            "status_detail": "Service restarting...",
        })
    finally:
        # Discard incomplete observatory data — partial price paths
        # would distort simulation results (missing end-of-market behavior)
        if _observer:
            _observer.discard()
        stop_event.set()
        regime_thread.join(timeout=5)
        blog("INFO", "Bot stopped")


if __name__ == "__main__":
    main()