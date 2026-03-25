"""
push.py — Web Push notification sender.
Sends real-time alerts to subscribed browsers.
"""

import json
import logging
from pathlib import Path

log = logging.getLogger("push")

def _fpnl(val):
    return f"+${val:.2f}" if val >= 0 else f"-${abs(val):.2f}"

# Try to import pywebpush — gracefully degrade if not installed
try:
    from pywebpush import webpush, WebPushException
    PUSH_AVAILABLE = True
except ImportError:
    PUSH_AVAILABLE = False
    log.warning("pywebpush not installed — push notifications disabled")

VAPID_KEYS_PATH = Path(__file__).parent / "vapid_keys.json"
_vapid_config = None


def _load_vapid():
    global _vapid_config
    if _vapid_config:
        return _vapid_config
    if not VAPID_KEYS_PATH.exists():
        log.warning(f"VAPID keys not found at {VAPID_KEYS_PATH}")
        return None
    with open(VAPID_KEYS_PATH) as f:
        _vapid_config = json.load(f)
    return _vapid_config


def get_public_key() -> str | None:
    """Get the VAPID public key for browser subscription."""
    cfg = _load_vapid()
    return cfg.get("public_key") if cfg else None


def send_push(subscription_info: dict, title: str, body: str,
              tag: str = "trade", url: str = "/", silent: bool = False) -> bool | None:
    """
    Send a push notification to a single subscription.
    silent=True sends with no sound/vibration (low priority popup only).
    Returns True if sent, False if subscription is dead (404/410),
    None on temporary/config failure (don't remove subscription).
    """
    if not PUSH_AVAILABLE:
        return None

    cfg = _load_vapid()
    if not cfg:
        return None

    payload = json.dumps({
        "title": title,
        "body": body,
        "tag": tag,
        "url": url,
        "silent": silent,
        "timestamp": __import__("time").time(),
    })

    try:
        webpush(
            subscription_info=subscription_info,
            data=payload,
            vapid_private_key=cfg["private_key_path"],
            vapid_claims={"sub": cfg.get("admin_email", "mailto:admin@bbrooks.dev")},
            ttl=300,  # 5 min expiry
        )
        return True

    except WebPushException as e:
        status = getattr(e, "response", None)
        if status and status.status_code in (404, 410):
            # Subscription expired/invalid — caller should remove it
            log.info(f"Push subscription expired (HTTP {status.status_code}): {e}")
            return False
        log.warning(f"Push temporary error: {e}")
        return None
    except Exception as e:
        log.error(f"Push send error: {e}")
        return None


def send_to_all(title: str, body: str, tag: str = "trade", url: str = "/",
                silent: bool = False):
    """
    Send push notification to all stored subscriptions.
    Removes dead subscriptions automatically.
    silent=True sends with no sound/vibration.
    """
    if not PUSH_AVAILABLE:
        return

    from db import get_push_subscriptions, remove_push_subscription, insert_push_log

    subs = get_push_subscriptions()
    if not subs:
        return

    sent = False
    for sub in subs:
        try:
            sub_info = json.loads(sub["subscription_json"])
            result = send_push(sub_info, title, body, tag, url, silent=silent)
            if result is True:
                sent = True
            elif result is False:
                # Only remove on confirmed expired (404/410)
                remove_push_subscription(sub["id"])
                log.info(f"Removed expired push subscription {sub['id']}")
            elif result is None:
                log.debug(f"Push to sub {sub['id']} skipped (temp failure)")
        except Exception as e:
            log.error(f"Push to sub {sub['id']} error: {e}")

    if sent:
        try:
            insert_push_log(title, body, tag)
        except Exception:
            pass


def _should_notify(event_type: str) -> bool:
    """Check if we should send this notification based on user preferences."""
    try:
        from db import get_config
        from config import CT
        from datetime import datetime as dt

        # Check per-event toggles
        if event_type == "win":
            if not get_config("push_notify_wins", True):
                return False
        elif event_type == "loss":
            if not get_config("push_notify_losses", True):
                return False
        elif event_type in ("error", "bankroll-limit", "max-loss"):
            if not get_config("push_notify_errors", True):
                return False
        elif event_type == "buy":
            if not get_config("push_notify_buys", False):
                return False
        elif event_type == "observed":
            if not get_config("push_notify_observed", False):
                return False
        elif event_type == "loss-stop":
            if not get_config("push_notify_losses", True):
                return False
        elif event_type == "auto-lock":
            if not get_config("push_notify_auto_lock", True):
                return False
        elif event_type == "early-exit":
            if not get_config("push_notify_early_exit", True):
                return False
        elif event_type == "health-check":
            if not get_config("push_notify_health_check", True):
                return False
        elif event_type == "new-regime":
            if not get_config("push_notify_new_regime", True):
                return False
        elif event_type == "regime-classified":
            if not get_config("push_notify_regime_classified", True):
                return False
        elif event_type == "trade-update":
            if not get_config("push_notify_trade_updates", False):
                return False
        elif event_type == "strategy-discovery":
            if not get_config("push_notify_strategy_discovery", True):
                return False
        elif event_type == "global-best":
            if not get_config("push_notify_global_best", True):
                return False
        elif event_type == "profit-goal":
            return True  # Always notify — this is a milestone

        # Check quiet hours
        q_start = int(get_config("push_quiet_start", 0) or 0)
        q_end = int(get_config("push_quiet_end", 0) or 0)
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


# ── Convenience notification senders ──────────────────────

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


def notify_error(error: str):
    if not _should_notify("error"):
        return
    send_to_all("Bot Error", error[:100], tag="error")


def notify_session_target(pnl: float, target: float):
    send_to_all("Session Target Reached",
                f"${pnl:.2f} ≥ ${target:.2f} — stopped",
                tag="session-target")


def notify_session_loss_limit(pnl: float, limit: float):
    send_to_all("Session Loss Limit Hit",
                f"Session P&L ${pnl:.2f} ≤ -${limit:.2f} — stopped",
                tag="session-loss-limit")


def notify_rolling_wr_breaker(win_rate: float, floor: float, window: int):
    send_to_all("Win Rate Circuit Breaker",
                f"Last {window} trades: {win_rate:.0f}% < {floor:.0f}% floor — stopped",
                tag="rolling-wr-breaker")


def notify_buy(side: str, shares: int, price_c: int, cost: float, regime: str = ""):
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
    # Shorten common skip reasons for notification body
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



def notify_auto_lock(amount: float, total_locked: float, effective: float,
                     action: str = "lock", new_threshold: float = 0):
    """Notify when auto-lock triggers a bankroll lock or keep."""
    if not _should_notify("auto-lock"):
        return
    if action == "keep":
        title = f"📈 Bankroll Increase: ${amount:.2f}"
        body = f"Kept in bankroll · Threshold → ${new_threshold:.2f}"
    else:
        title = f"🔒 Profit Lock: ${amount:.2f}"
        body = f"Total locked: ${total_locked:.2f} · Effective: ${effective:.2f}"
    send_to_all(title, body, tag="auto-lock")


def notify_early_exit(sell_price_c: int, pnl: float, regime: str = "",
                       round_num: int = 1, mins_left: float = 0):
    """Notify when an early exit is triggered."""
    if not _should_notify("early-exit"):
        return
    title = f"⚡ Early Exit @ {sell_price_c}¢"
    body = f"{_fpnl(pnl)} · R{round_num}"
    if regime:
        body += f" · {regime.replace('_', ' ')}"
    if mins_left > 0:
        body += f" · {mins_left:.0f}m left"
    send_to_all(title, body, tag="early-exit")


def notify_health_check_down(silent_mins: float):
    """Notify when the bot appears to have stopped responding."""
    if not _should_notify("health-check"):
        return
    title = "🚨 Bot Unresponsive"
    body = f"No heartbeat for {silent_mins:.0f} min"
    send_to_all(title, body, tag="health-check")


def notify_health_check_recovered(down_mins: float):
    """Notify when the bot comes back after being unresponsive."""
    if not _should_notify("health-check"):
        return
    title = "✅ Bot Recovered"
    body = f"Back online after {down_mins:.0f} min"
    send_to_all(title, body, tag="health-check")


def notify_withdrawal_detected(amount: float, action: str, locked_after: float = 0,
                                effective: float = 0):
    """Notify when a withdrawal is detected and auto-unlock happens (or not)."""
    if not _should_notify("bankroll-limit"):
        return
    if action == "auto_unlock":
        title = f"💸 Withdrawal Detected: ${amount:.2f}"
        body = f"Auto-unlocked ${amount:.2f} · Effective: ${effective:.2f}"
    elif action == "mismatch":
        title = f"💸 Withdrawal Detected: ${amount:.2f}"
        body = f"Locked: ${locked_after:.2f} (no match) · Adjust manually"
    else:
        title = f"💸 Balance Decreased: ${amount:.2f}"
        body = f"No locked funds to unlock"
    send_to_all(title, body, tag="withdrawal")


# ── New regime & classification notifications ─────────────

def notify_new_regime(regime_label: str, total: int = 1):
    """Notify when a completely new regime label is seen for the first time."""
    if not _should_notify("new-regime"):
        return
    label = regime_label.replace("_", " ")
    send_to_all("🆕 New Regime Discovered",
                f"{label} · first observation",
                tag="new-regime")


def notify_regime_classified(regime_label: str, risk_level: str,
                              total: int = 0, win_rate: float = 0,
                              old_risk: str = "unknown"):
    """Notify when a regime graduates from unknown to a risk classification."""
    if not _should_notify("regime-classified"):
        return
    label = regime_label.replace("_", " ")
    risk_display = "extreme" if risk_level == "terrible" else risk_level
    old_display = "extreme" if old_risk == "terrible" else old_risk
    wr_str = f"{win_rate:.0%}"

    if old_risk == "unknown" or old_risk is None:
        title = f"📊 Regime Classified: {risk_display.upper()}"
        body = f"{label} · {wr_str} win rate · {total} samples"
    else:
        title = f"📊 Regime Risk Changed: {old_display} → {risk_display}"
        body = f"{label} · {wr_str} win rate · {total} samples"
    send_to_all(title, body, tag="regime-classified")


def notify_trade_update(side: str, cur_bid: int, avg_price_c: int,
                         sell_price_c: int, mins_left: float,
                         fill_count: int,
                         actual_cost: float, regime_label: str = ""):
    """
    Minute-by-minute trade progress notification.
    Sent as silent (no sound/vibration) — just a popup.
    Includes natural language status and estimated win probability.
    """
    if not _should_notify("trade-update"):
        return

    # Calculate progress and estimated win probability
    if sell_price_c > avg_price_c and cur_bid > 0:
        progress = (cur_bid - avg_price_c) / (sell_price_c - avg_price_c) * 100
        progress = max(0, min(100, progress))
    else:
        progress = 0

    # Estimate win probability based on current price position
    # Higher bid relative to entry = more likely to sell or win at close
    if cur_bid >= sell_price_c:
        est_win_pct = 95
    elif cur_bid > avg_price_c:
        # Above entry, between entry and sell target
        ratio = (cur_bid - avg_price_c) / max(sell_price_c - avg_price_c, 1)
        est_win_pct = int(50 + ratio * 40)  # 50-90%
    elif cur_bid == avg_price_c:
        est_win_pct = 50
    else:
        # Below entry — how far
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

    title = f"{mins_str} · ~{est_win_pct}% win"
    body = (f"{status} · entry {avg_price_c}c → sell {sell_price_c}c "
            f"· est P&L {_fpnl(est_pnl)}")

    send_to_all(title, body, tag="trade-update", silent=True)


def notify_profit_goal(locked_total: float, goal: float):
    """Notify when locked profits reach the profit goal. Always sends."""
    if not _should_notify("profit-goal"):
        return
    over = locked_total - goal
    title = "🎯🎉 PROFIT GOAL REACHED!"
    body = f"${locked_total:.2f} locked"
    if over > 0.50:
        body += f" — ${over:.2f} over target"
    body += f" (goal was ${goal:.2f})"
    send_to_all(title, body, tag="profit-goal")


def notify_strategy_discovery(regime_label: str, strategy_key: str,
                              ev_c: float, win_rate: float, sample_size: int,
                              setup_key: str = ""):
    """Notify when a new +EV strategy is found for a regime."""
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
    """Notify when the global best strategy changes after recompute."""
    if not _should_notify("global-best"):
        return
    old_parts = old_key.split(":")
    new_parts = new_key.split(":")
    old_label = " \u00b7 ".join(old_parts) if old_parts else old_key
    new_label = " \u00b7 ".join(new_parts) if new_parts else new_key
    wr = f"{win_rate:.0%}" if win_rate else "?"
    title = "Global Best Changed"
    body = (f"{new_label}\n"
            f"EV {ev_c:+.1f}\u00a2 \u00b7 WR {wr} \u00b7 n={sample_size}\n"
            f"was: {old_label}")
    send_to_all(title, body, tag="global-best")