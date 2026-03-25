"""
push.py — Web Push notification infrastructure.
Sends real-time alerts to subscribed browsers.
"""

import json
import logging
from pathlib import Path

log = logging.getLogger("push")

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
