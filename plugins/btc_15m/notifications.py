"""
notifications.py — Push notification convenience functions for BTC 15-min plugin.
Uses platform send_to_all() for delivery.
"""

import logging

from push import send_to_all
from db import get_config
from config import CT

log = logging.getLogger("notifications")


def _fpnl(val):
    return f"+${val:.2f}" if val >= 0 else f"-${abs(val):.2f}"


def _should_notify(event_type: str) -> bool:
    """Check if we should send this notification based on user preferences.
    Config keys use btc_15m. namespace prefix."""
    try:
        from datetime import datetime as dt

        prefix = "btc_15m."

        # Check per-event toggles
        if event_type == "win":
            if not get_config(f"{prefix}push_notify_wins", True):
                return False
        elif event_type == "loss":
            if not get_config(f"{prefix}push_notify_losses", True):
                return False
        elif event_type in ("error", "bankroll-limit", "max-loss"):
            if not get_config(f"{prefix}push_notify_errors", True):
                return False
        elif event_type == "buy":
            if not get_config(f"{prefix}push_notify_buys", False):
                return False
        elif event_type == "observed":
            if not get_config(f"{prefix}push_notify_observed", False):
                return False
        elif event_type == "loss-stop":
            if not get_config(f"{prefix}push_notify_losses", True):
                return False
        elif event_type == "early-exit":
            if not get_config(f"{prefix}push_notify_early_exit", True):
                return False
        elif event_type == "health-check":
            if not get_config(f"{prefix}push_notify_health_check", True):
                return False
        elif event_type == "new-regime":
            if not get_config(f"{prefix}push_notify_new_regime", True):
                return False
        elif event_type == "regime-classified":
            if not get_config(f"{prefix}push_notify_regime_classified", True):
                return False
        elif event_type == "trade-update":
            if not get_config(f"{prefix}push_notify_trade_updates", False):
                return False
        elif event_type == "strategy-discovery":
            if not get_config(f"{prefix}push_notify_strategy_discovery", True):
                return False
        elif event_type == "global-best":
            if not get_config(f"{prefix}push_notify_global_best", True):
                return False

        # Check quiet hours
        q_start = int(get_config(f"{prefix}push_quiet_start", 0) or 0)
        q_end = int(get_config(f"{prefix}push_quiet_end", 0) or 0)
        if q_start != 0 or q_end != 0:
            now_ct = dt.now(CT).hour
            if q_start <= q_end:
                if q_start <= now_ct < q_end:
                    return False
            else:  # wraps midnight
                if now_ct >= q_start or now_ct < q_end:
                    return False

        return True
    except Exception:
        return True


# ── Trade notifications ───────────────────────────────────

def notify_trade_result(outcome: str, pnl: float, regime: str = "",
                         is_data: bool = False):
    if not _should_notify(outcome):
        return
    icon = "✅" if outcome == "win" else "❌"
    pnl_str = _fpnl(pnl)
    title = f"{icon} {outcome.upper()} {pnl_str}"
    body = "Data bet" if is_data else ""
    if regime:
        body += f" · {regime.replace('_', ' ')}" if body else regime.replace('_', ' ')
    send_to_all(title, body or "Trade result", tag="trade-result")


def notify_buy(side: str, shares: int, price_c: int, cost: float,
               regime: str = ""):
    if not _should_notify("buy"):
        return
    title = f"Bought {shares} {side.upper()} @ {price_c}¢"
    body = f"${cost:.2f}"
    if regime:
        body += f" · {regime.replace('_', ' ')}"
    send_to_all(title, body, tag="buy")


def notify_observed(regime: str, reason: str):
    if not _should_notify("observed"):
        return
    label = regime.replace('_', ' ') if regime else 'unknown'
    short_reason = reason
    if 'strategy unknown' in reason.lower():
        short_reason = 'No strategy data yet'
    elif 'observe_only' in reason.lower() or 'observe only' in reason.lower():
        short_reason = 'Observe-only mode'
    elif 'price' in reason.lower() and 'range' in reason.lower():
        short_reason = 'Price out of range'
    elif 'blocked' in reason.lower():
        short_reason = reason[:60]
    else:
        short_reason = reason[:60]
    send_to_all(f"Observed: {label}", short_reason, tag="observed")


def notify_trade_update(side: str, cur_bid: int, avg_price_c: int,
                         sell_price_c: int, mins_left: float,
                         fill_count: int,
                         actual_cost: float, regime_label: str = ""):
    """Minute-by-minute trade progress (silent — no sound/vibration)."""
    if not _should_notify("trade-update"):
        return

    if sell_price_c > avg_price_c and cur_bid > 0:
        progress = (cur_bid - avg_price_c) / (sell_price_c - avg_price_c) * 100
        progress = max(0, min(100, progress))
    else:
        progress = 0

    if cur_bid >= sell_price_c:
        est_win_pct = 95
    elif cur_bid > avg_price_c:
        ratio = (cur_bid - avg_price_c) / max(sell_price_c - avg_price_c, 1)
        est_win_pct = int(50 + ratio * 40)
    elif cur_bid == avg_price_c:
        est_win_pct = 50
    else:
        drop_pct = (avg_price_c - cur_bid) / max(avg_price_c, 1) * 100
        if drop_pct > 30:
            est_win_pct = 15
        elif drop_pct > 15:
            est_win_pct = 25
        else:
            est_win_pct = 35

    mins_str = f"{mins_left:.0f}m left"
    est_pnl = fill_count * cur_bid / 100 - actual_cost

    if cur_bid >= sell_price_c:
        status = f"Sell filling! {side.upper()} @ {cur_bid}c"
    elif cur_bid > avg_price_c + 3:
        status = f"Looking good — {side.upper()} climbing to {cur_bid}c"
    elif cur_bid >= avg_price_c - 1:
        status = f"Holding steady at {cur_bid}c"
    elif cur_bid >= avg_price_c - 5:
        status = f"Dipping slightly to {cur_bid}c"
    else:
        status = f"Under pressure — down to {cur_bid}c"

    title = f"{mins_str} · ~{est_win_pct}% win"
    body = (f"{status} · entry {avg_price_c}c → sell {sell_price_c}c "
            f"· est P&L {_fpnl(est_pnl)}")

    send_to_all(title, body, tag="trade-update", silent=True)


def notify_early_exit(sell_price_c: int, pnl: float, regime: str = "",
                       round_num: int = 1, mins_left: float = 0):
    if not _should_notify("early-exit"):
        return
    title = f"Early Exit @ {sell_price_c}¢"
    body = f"{_fpnl(pnl)} · R{round_num}"
    if regime:
        body += f" · {regime.replace('_', ' ')}"
    if mins_left > 0:
        body += f" · {mins_left:.0f}m left"
    send_to_all(title, body, tag="early-exit")


# ── Error notifications ───────────────────────────────────

def notify_error(error: str):
    if not _should_notify("error"):
        return
    send_to_all("Bot Error", error[:100], tag="error")


def notify_max_loss(loss_amount: float, max_losses: int, cooldown: int = 0):
    if not _should_notify("max-loss"):
        return
    title = f"LOSS STOP ({max_losses} in a row)"
    body = f"Last loss: ${loss_amount:.2f}"
    if cooldown:
        body += f" · {cooldown} market cooldown"
    send_to_all(title, body, tag="max-loss")


def notify_bankroll_limit(reason: str):
    if not _should_notify("bankroll-limit"):
        return
    send_to_all("Trading Stopped", reason, tag="bankroll-limit")


def notify_health_check_down(silent_mins: float):
    if not _should_notify("health-check"):
        return
    send_to_all("Bot Unresponsive",
                f"No heartbeat for {silent_mins:.0f} min",
                tag="health-check")


def notify_health_check_recovered(down_mins: float):
    if not _should_notify("health-check"):
        return
    send_to_all("Bot Recovered",
                f"Back online after {down_mins:.0f} min",
                tag="health-check")


# ── Regime notifications ──────────────────────────────────

def notify_new_regime(regime_label: str, total: int = 1):
    if not _should_notify("new-regime"):
        return
    label = regime_label.replace("_", " ")
    send_to_all("New Regime Discovered",
                f"{label} · first observation",
                tag="new-regime")


def notify_regime_classified(regime_label: str, risk_level: str,
                              total: int = 0, win_rate: float = 0,
                              old_risk: str = "unknown"):
    if not _should_notify("regime-classified"):
        return
    label = regime_label.replace("_", " ")
    risk_display = "extreme" if risk_level == "terrible" else risk_level
    old_display = "extreme" if old_risk == "terrible" else old_risk
    wr_str = f"{win_rate:.0%}"

    if old_risk == "unknown" or old_risk is None:
        title = f"Regime Classified: {risk_display.upper()}"
        body = f"{label} · {wr_str} win rate · {total} samples"
    else:
        title = f"Regime Risk Changed: {old_display} → {risk_display}"
        body = f"{label} · {wr_str} win rate · {total} samples"
    send_to_all(title, body, tag="regime-classified")


# ── Strategy notifications ────────────────────────────────

def notify_strategy_discovery(regime_label: str, strategy_key: str,
                              ev_c: float, win_rate: float, sample_size: int,
                              setup_key: str = ""):
    if not _should_notify("strategy-discovery"):
        return
    regime = regime_label.replace("_", " ")
    parts = strategy_key.split(":")
    strat_label = " · ".join(parts) if parts else strategy_key
    wr = f"{win_rate:.0%}" if win_rate else "?"
    title = f"Strategy Found: {regime}"
    body = (f"{strat_label}\n"
            f"EV {ev_c:+.1f}¢ · WR {wr} · n={sample_size}")
    if setup_key:
        body += f"\nfrom {setup_key}"
    send_to_all(title, body, tag="strategy-discovery")


def notify_global_best_changed(old_key: str, new_key: str,
                                ev_c: float, win_rate: float,
                                sample_size: int):
    if not _should_notify("global-best"):
        return
    old_parts = old_key.split(":")
    new_parts = new_key.split(":")
    old_label = " · ".join(old_parts) if old_parts else old_key
    new_label = " · ".join(new_parts) if new_parts else new_key
    wr = f"{win_rate:.0%}" if win_rate else "?"
    title = "Global Best Changed"
    body = (f"{new_label}\n"
            f"EV {ev_c:+.1f}¢ · WR {wr} · n={sample_size}\n"
            f"was: {old_label}")
    send_to_all(title, body, tag="global-best")
