"""
notifications.py — BTC 15-minute plugin push notification formatters.
Uses platform push infrastructure (send_to_all) with plugin-specific
message formatting and notification preferences.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from push import send_to_all
from db import get_config
from config import CT

from datetime import datetime as dt


# ── Helpers ───────────────────────────────────────────────────

def _fpnl(val):
    """Format P&L as +$X.XX or -$X.XX."""
    return f"+${val:.2f}" if val >= 0 else f"-${abs(val):.2f}"


def _should_notify(event_type: str, plugin_id: str = "btc_15m") -> bool:
    """
    Check if we should send this notification based on user preferences.
    Config keys are namespaced: btc_15m.push_notify_wins, btc_15m.push_quiet_start, etc.
    """
    try:
        # Per-event toggles
        toggle_map = {
            "win":                ("push_notify_wins", True),
            "loss":               ("push_notify_losses", True),
            "error":              ("push_notify_errors", True),
            "buy":                ("push_notify_buys", False),
            "observed":           ("push_notify_observed", False),
            "early-exit":         ("push_notify_early_exit", True),
            "new-regime":         ("push_notify_new_regime", True),
            "regime-classified":  ("push_notify_regime_classified", True),
            "trade-update":       ("push_notify_trade_updates", False),
            "strategy-discovery": ("push_notify_strategy_discovery", True),
            "global-best":        ("push_notify_global_best", True),
            "balance-anomaly":    ("push_notify_errors", True),
        }

        if event_type in toggle_map:
            key, default = toggle_map[event_type]
            if not get_config(f"{plugin_id}.{key}", default):
                return False

        # Check quiet hours
        q_start = int(get_config(f"{plugin_id}.push_quiet_start", 0) or 0)
        q_end = int(get_config(f"{plugin_id}.push_quiet_end", 0) or 0)
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


# ── Notification senders ──────────────────────────────────────

def notify_trade_result(outcome: str, pnl: float, side: str = "",
                        ticker: str = "", regime: str = "",
                        is_shadow: bool = False):
    """Notify on trade win/loss result."""
    if not _should_notify(outcome):
        return
    icon = "+" if outcome == "win" else "-"
    pnl_str = _fpnl(pnl)
    title = f"{icon} {outcome.upper()} {pnl_str}"
    body = "Shadow" if is_shadow else ""
    if side:
        body += f" {side.upper()}" if body else side.upper()
    if regime:
        label = regime.replace("_", " ")
        body += f" | {label}" if body else label
    send_to_all(title, body or "Trade result", tag="trade-result")


def notify_buy(side: str, shares: int, price_c: int, cost: float,
               regime: str = ""):
    """Notify on trade entry."""
    if not _should_notify("buy"):
        return
    title = f"Bought {shares} {side.upper()} @ {price_c}c"
    body = f"${cost:.2f}"
    if regime:
        body += f" | {regime.replace('_', ' ')}"
    send_to_all(title, body, tag="buy")


def notify_observed(regime: str, reason: str):
    """Notify when a market is observed but not traded."""
    if not _should_notify("observed"):
        return
    label = regime.replace("_", " ") if regime else "unknown"
    # Shorten common skip reasons
    short_reason = reason
    if "strategy unknown" in reason.lower():
        short_reason = "No strategy data yet"
    elif "observe_only" in reason.lower() or "observe only" in reason.lower():
        short_reason = "Observe-only mode"
    elif "price" in reason.lower() and "range" in reason.lower():
        short_reason = "Price out of range"
    else:
        short_reason = reason[:60]
    send_to_all(f"Observed: {label}", short_reason, tag="observed")


def notify_error(error_msg: str):
    """Notify on bot error."""
    if not _should_notify("error"):
        return
    send_to_all("Bot Error", error_msg[:100], tag="error")


def notify_new_regime(label: str, count: int = 1):
    """Notify when a completely new regime label is seen for the first time."""
    if not _should_notify("new-regime"):
        return
    display = label.replace("_", " ")
    send_to_all("New Regime Discovered",
                f"{display} | first observation",
                tag="new-regime")


def notify_regime_classified(label: str, risk_level: str,
                             total: int = 0, win_rate: float = 0,
                             old_risk: str = "unknown"):
    """Notify when a regime graduates from unknown to a risk classification."""
    if not _should_notify("regime-classified"):
        return
    display = label.replace("_", " ")
    risk_display = "extreme" if risk_level == "terrible" else risk_level
    old_display = "extreme" if old_risk == "terrible" else old_risk
    wr_str = f"{win_rate:.0%}"

    if old_risk == "unknown" or old_risk is None:
        title = f"Regime Classified: {risk_display.upper()}"
        body = f"{display} | {wr_str} win rate | {total} samples"
    else:
        title = f"Regime Risk Changed: {old_display} -> {risk_display}"
        body = f"{display} | {wr_str} win rate | {total} samples"
    send_to_all(title, body, tag="regime-classified")


def notify_trade_update(side: str, cur_bid: int, avg_price_c: int,
                        sell_price_c: int, mins_left: float,
                        fill_count: int, actual_cost: float,
                        regime_label: str = ""):
    """
    Minute-by-minute trade progress notification.
    Sent as silent (no sound/vibration) — just a popup.
    """
    if not _should_notify("trade-update"):
        return

    # Estimate win probability based on current price position
    if cur_bid >= sell_price_c:
        est_win_pct = 95
    elif cur_bid > avg_price_c:
        ratio = (cur_bid - avg_price_c) / max(sell_price_c - avg_price_c, 1)
        est_win_pct = int(50 + ratio * 40)  # 50-90%
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

    # Natural language status
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

    title = f"{mins_str} | ~{est_win_pct}% win"
    body = (f"{status} | entry {avg_price_c}c -> sell {sell_price_c}c "
            f"| est P&L {_fpnl(est_pnl)}")

    send_to_all(title, body, tag="trade-update", silent=True)


def notify_early_exit(sell_price_c: int, pnl: float, regime: str = "",
                      round_num: int = 1, mins_left: float = 0):
    """Notify when an early exit is triggered."""
    if not _should_notify("early-exit"):
        return
    title = f"Early Exit @ {sell_price_c}c"
    body = f"{_fpnl(pnl)} | R{round_num}"
    if regime:
        body += f" | {regime.replace('_', ' ')}"
    if mins_left > 0:
        body += f" | {mins_left:.0f}m left"
    send_to_all(title, body, tag="early-exit")


def notify_balance_anomaly(old_bal: float, new_bal: float):
    """Notify on unexpected balance change."""
    if not _should_notify("balance-anomaly"):
        return
    diff = new_bal - old_bal
    direction = "increased" if diff > 0 else "decreased"
    title = f"Balance Anomaly: {direction}"
    body = f"${old_bal:.2f} -> ${new_bal:.2f} ({_fpnl(diff)})"
    send_to_all(title, body, tag="balance-anomaly")


def notify_strategy_discovery(regime_label: str, strategy_key: str,
                              ev_c: float, win_rate: float,
                              sample_size: int, setup_key: str = ""):
    """Notify when a new +EV strategy is found for a regime."""
    if not _should_notify("strategy-discovery"):
        return
    regime = regime_label.replace("_", " ")
    parts = strategy_key.split(":")
    strat_label = " | ".join(parts) if parts else strategy_key
    wr = f"{win_rate:.0%}" if win_rate else "?"
    title = f"Strategy Found: {regime}"
    body = (f"{strat_label}\n"
            f"EV {ev_c:+.1f}c | WR {wr} | n={sample_size}")
    if setup_key:
        body += f"\nfrom {setup_key}"
    send_to_all(title, body, tag="strategy-discovery")


def notify_global_best_changed(old_key: str, new_key: str,
                               ev_c: float, win_rate: float,
                               sample_size: int):
    """Notify when the global best strategy changes after recompute."""
    if not _should_notify("global-best"):
        return
    old_parts = old_key.split(":")
    new_parts = new_key.split(":")
    old_label = " | ".join(old_parts) if old_parts else old_key
    new_label = " | ".join(new_parts) if new_parts else new_key
    wr = f"{win_rate:.0%}" if win_rate else "?"
    title = "Global Best Changed"
    body = (f"{new_label}\n"
            f"EV {ev_c:+.1f}c | WR {wr} | n={sample_size}\n"
            f"was: {old_label}")
    send_to_all(title, body, tag="global-best")
