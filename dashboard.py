"""
dashboard.py — Flask web dashboard for the Kalshi BTC trading bot.
Two pages: main dashboard (live trade, controls) and logs (infinite scroll).
All displayed times are Central Time.
"""

import json
import os
import requests as http_requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, render_template_string, request, jsonify, Response, make_response
from functools import wraps

from config import DASHBOARD_HOST, DASHBOARD_PORT, DASHBOARD_USER, DASHBOARD_PASS, CT, ET
from db import (
    init_db, get_conn, get_all_config, set_config, get_config, now_utc,
    row_to_dict, rows_to_list, insert_log, get_logs, insert_audit_log,
    get_push_log, save_push_subscription, remove_push_subscription_by_endpoint,
    get_bankroll_chart_data, get_pnl_chart_data, get_candles,
    get_latest_regime_snapshot, get_regime_heartbeat, is_regime_worker_running,
    backup_database, insert_bankroll_snapshot, get_plugin_state,
    update_plugin_state, enqueue_command, get_pending_commands,
    get_logs_after,
)
from plugins.btc_15m.market_db import (
    get_trade, get_recent_trades, delete_trades, get_price_path,
    get_live_prices, get_trade_summary, get_lifetime_stats,
    get_all_regime_stats, get_regime_risk, recompute_all_stats,
    get_skip_analysis, get_all_hourly_stats, get_regime_stability_summary,
    get_shadow_trade_analysis, reconcile_shadow_trades, get_pnl_attribution,
    get_observation_count, get_observatory_summary, get_net_edge_summary,
    get_realized_edge, get_btc_surface_data, get_feature_importance,
    get_metric_snapshot_near, get_latest_metric_snapshot,
    get_open_trade, update_trade, get_top_strategies,
    compute_strategy_risk_score,
)

def get_bot_state() -> dict:
    """Compatibility wrapper: reads plugin_state and returns flat dict matching legacy shape."""
    ps = get_plugin_state("btc_15m")
    if not ps:
        return {"status": "stopped", "status_detail": "", "last_updated": ""}
    return dict(ps)

def get_regime_worker_status() -> dict:
    """Compatibility wrapper: rebuilds legacy regime status dict from platform db."""
    with get_conn() as c:
        candle_count = c.execute("SELECT COUNT(*) as n FROM candles").fetchone()["n"]
        latest_candle = c.execute(
            "SELECT ts FROM candles ORDER BY ts DESC LIMIT 1").fetchone()
        earliest_candle = c.execute(
            "SELECT ts FROM candles ORDER BY ts ASC LIMIT 1").fetchone()
        snapshot_count = c.execute(
            "SELECT COUNT(*) as n FROM regime_snapshots").fetchone()["n"]
        latest_snap = get_latest_regime_snapshot("BTC")
        baseline_count = c.execute(
            "SELECT COUNT(*) as n FROM baselines").fetchone()["n"]
        regime_label_count = c.execute(
            "SELECT COUNT(DISTINCT regime_label) as n FROM btc15m_regime_stats").fetchone()["n"]
        recent_snaps = c.execute("""
            SELECT captured_at FROM regime_snapshots
            ORDER BY captured_at DESC LIMIT 10
        """).fetchall()
        avg_interval = None
        if len(recent_snaps) >= 2:
            from datetime import datetime as dt
            times = [dt.fromisoformat(r["captured_at"].replace("Z", "+00:00"))
                     for r in recent_snaps]
            diffs = [(times[i] - times[i+1]).total_seconds()
                     for i in range(len(times)-1)]
            avg_interval = round(sum(diffs) / len(diffs))
        state = get_bot_state()
        phase = state.get("regime_engine_phase")
        return {
            "candle_count": candle_count,
            "candles_expected": 525_600,
            "candle_pct": round(candle_count / 525_600 * 100, 1),
            "latest_candle_ts": latest_candle["ts"] if latest_candle else None,
            "earliest_candle_ts": earliest_candle["ts"] if earliest_candle else None,
            "snapshot_count": snapshot_count,
            "latest_snapshot": latest_snap,
            "baseline_count": baseline_count,
            "regime_labels_tracked": regime_label_count,
            "avg_snapshot_interval_s": avg_interval,
            "engine_phase": phase,
        }

app = Flask(__name__)

# ═══════════════════════════════════════════════════════════════
#  SECURITY: Rate limiting, CSRF, Destruction PIN
# ═══════════════════════════════════════════════════════════════

import time as _time
import secrets as _secrets

# ── Login rate limiting (in-memory, resets on restart) ──
_login_attempts = {}  # ip -> [(timestamp, success)]
_LOGIN_WINDOW = 600   # 10 minute window
_LOGIN_MAX = 5        # max failed attempts per window

def _check_login_rate(ip: str) -> str | None:
    """Returns error message if rate limited, None if OK."""
    now = _time.time()
    attempts = _login_attempts.get(ip, [])
    attempts = [(t, s) for t, s in attempts if now - t < _LOGIN_WINDOW]
    _login_attempts[ip] = attempts
    recent_fails = sum(1 for t, s in attempts if not s)
    if recent_fails >= _LOGIN_MAX:
        return "Too many failed attempts. Try again in 10 minutes."
    return None

def _record_login_attempt(ip: str, success: bool):
    now = _time.time()
    if ip not in _login_attempts:
        _login_attempts[ip] = []
    _login_attempts[ip].append((now, success))
    _login_attempts[ip] = _login_attempts[ip][-20:]

# ── CSRF protection (Origin check on state-changing requests) ──
_CSRF_EXEMPT = {"/api/login", "/api/change_password"}

@app.before_request
def _csrf_check():
    """Block cross-origin POST/PUT/DELETE requests."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if request.path in _CSRF_EXEMPT:
        return
    origin = request.headers.get("Origin", "")
    if not origin:
        return  # Same-origin or curl — allow
    allowed = request.host_url.rstrip("/")
    if origin == allowed or origin == allowed.replace("http://", "https://"):
        return
    if "btcbotapp.com" in origin or "bbrooks.dev" in origin:
        return
    insert_audit_log("csrf_blocked", f"origin={origin} path={request.path}",
                     ip=request.remote_addr or "", success=False)
    return jsonify({"error": "Request blocked (cross-origin)"}), 403

# ── Destruction PIN (protects data-destroying operations) ──
_DESTRUCTIVE_SCOPES = {"trades", "regime_engine", "full"}

def _check_destruction_pin(pin: str) -> bool:
    """Verify the destruction PIN. Returns True if valid or no PIN set."""
    import hashlib
    stored_hash = get_config("destruction_pin_hash")
    if not stored_hash:
        return True  # No PIN set = not enforced
    return hashlib.sha256(pin.encode()).hexdigest() == stored_hash

def _set_destruction_pin(pin: str):
    """Store a new destruction PIN (hashed)."""
    import hashlib
    set_config("destruction_pin_hash", hashlib.sha256(pin.encode()).hexdigest())

def fpnl(val):
    """Format P&L: +$5.00 or -$5.00"""
    v = float(val or 0)
    return f"+${v:.2f}" if v >= 0 else f"-${abs(v):.2f}"

# ═══════════════════════════════════════════════════════════════
#  AUTH
# ═══════════════════════════════════════════════════════════════

def check_auth(u, p):
    import hashlib, hmac
    if u != DASHBOARD_USER:
        return False
    try:
        stored_hash = get_config("dashboard_pass_hash")
        if stored_hash:
            return hmac.compare_digest(hashlib.sha256(p.encode()).hexdigest(), stored_hash)
    except Exception:
        pass
    return hmac.compare_digest(p, DASHBOARD_PASS)

def _get_session_salt():
    try:
        salt = get_config("_session_salt")
        if salt: return salt
    except Exception: pass
    salt = _secrets.token_hex(16)
    set_config("_session_salt", salt)
    return salt

def _auth_token():
    """Signed auth token including session salt for invalidation."""
    import hashlib
    salt = _get_session_salt()
    try:
        stored_hash = get_config("dashboard_pass_hash")
        if stored_hash:
            return hashlib.sha256(f"{DASHBOARD_USER}:{stored_hash}:{salt}".encode()).hexdigest()
    except Exception:
        pass
    return hashlib.sha256(f"{DASHBOARD_USER}:{DASHBOARD_PASS}:{salt}".encode()).hexdigest()

LOGIN_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Login — BTC Bot</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, sans-serif; background: #0d1117; color: #c9d1d9;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; padding: 20px; }
  .login-box { background: #161b22; border: 1px solid #30363d; border-radius: 12px;
               padding: 32px 24px; width: 100%; max-width: 340px; }
  h2 { color: #58a6ff; font-size: 18px; text-align: center; margin-bottom: 20px; }
  input { width: 100%; padding: 10px 12px; border-radius: 6px; border: 1px solid #30363d;
          background: #0d1117; color: #c9d1d9; font-size: 14px; margin-bottom: 12px; }
  input:focus { outline: none; border-color: #58a6ff; }
  .btn { width: 100%; padding: 10px; border-radius: 6px; border: none;
         color: white; font-size: 14px; font-weight: 600; cursor: pointer; margin-bottom: 8px; }
  .btn:active { filter: brightness(1.1); }
  .btn-green { background: #238636; }
  .btn-dim { background: #21262d; border: 1px solid #30363d; color: #8b949e; }
  .msg { font-size: 12px; text-align: center; margin-top: 8px; display: none; }
  .err { color: #f85149; }
  .ok { color: #3fb950; }
  .hidden { display: none; }
</style>
</head><body>
<div class="login-box">
  <h2 id="formTitle">Kalshi BTC Bot</h2>

  <!-- Login form -->
  <div id="loginForm">
    <input type="text" id="user" placeholder="Username" autocapitalize="off" autocomplete="username">
    <input type="password" id="pass" placeholder="Password" autocomplete="current-password">
    <button class="btn btn-green" onclick="doLogin()">Log In</button>
    <button class="btn btn-dim" onclick="showChangePass()">Change Password</button>
    <div class="msg err" id="loginErr">Invalid credentials</div>
  </div>

  <!-- Change password form -->
  <div id="changeForm" class="hidden">
    <input type="text" id="cpUser" placeholder="Username" autocapitalize="off">
    <input type="password" id="cpOld" placeholder="Current password">
    <input type="password" id="cpNew" placeholder="New password">
    <input type="password" id="cpConfirm" placeholder="Confirm new password">
    <button class="btn btn-green" onclick="doChangePass()">Update Password</button>
    <button class="btn btn-dim" onclick="showLogin()">← Back to Login</button>
    <div class="msg err" id="cpErr"></div>
    <div class="msg ok" id="cpOk"></div>
  </div>
</div>
<script>
document.getElementById('pass').addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });
document.getElementById('cpConfirm').addEventListener('keydown', e => { if (e.key === 'Enter') doChangePass(); });

function showChangePass() {
  document.getElementById('loginForm').classList.add('hidden');
  document.getElementById('changeForm').classList.remove('hidden');
  document.getElementById('formTitle').textContent = 'Change Password';
  document.getElementById('cpErr').style.display = 'none';
  document.getElementById('cpOk').style.display = 'none';
}
function showLogin() {
  document.getElementById('changeForm').classList.add('hidden');
  document.getElementById('loginForm').classList.remove('hidden');
  document.getElementById('formTitle').textContent = 'Kalshi BTC Bot';
}

async function doLogin() {
  const u = document.getElementById('user').value;
  const p = document.getElementById('pass').value;
  const r = await fetch('/api/login', {
    method: 'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({username: u, password: p})
  });
  if (r.ok) {
    window.location.href = '/';
  } else {
    document.getElementById('loginErr').style.display = '';
  }
}

async function doChangePass() {
  const errEl = document.getElementById('cpErr');
  const okEl = document.getElementById('cpOk');
  errEl.style.display = 'none';
  okEl.style.display = 'none';

  const user = document.getElementById('cpUser').value;
  const old = document.getElementById('cpOld').value;
  const nw = document.getElementById('cpNew').value;
  const confirm = document.getElementById('cpConfirm').value;

  if (!user || !old || !nw) { errEl.textContent = 'All fields required'; errEl.style.display = ''; return; }
  if (nw !== confirm) { errEl.textContent = 'Passwords do not match'; errEl.style.display = ''; return; }
  if (nw.length < 6) { errEl.textContent = 'Password must be at least 6 characters'; errEl.style.display = ''; return; }

  const r = await fetch('/api/change_password', {
    method: 'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({username: user, old_password: old, new_password: nw})
  });
  const d = await r.json();
  if (r.ok) {
    okEl.textContent = 'Password changed! You can now log in.';
    okEl.style.display = '';
    setTimeout(showLogin, 2000);
  } else {
    errEl.textContent = d.error || 'Failed';
    errEl.style.display = '';
  }
}
</script>
</body></html>"""

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # Check cookie first (persistent login)
        token = request.cookies.get("platform_auth")
        if token and _secrets.compare_digest(token, _auth_token()):
            return f(*args, **kwargs)
        # Fall back to Basic Auth (for API clients)
        auth = request.authorization
        if auth and check_auth(auth.username, auth.password):
            resp = make_response(f(*args, **kwargs))
            resp.set_cookie("platform_auth", _auth_token(), max_age=30*86400,
                            httponly=True, samesite="Lax", secure=request.is_secure)
            return resp
        # Redirect to login page for browser page requests
        if not request.path.startswith('/api/'):
            return render_template_string(LOGIN_HTML), 401
        return Response(json.dumps({"error": "Unauthorized"}), 401,
                        {"Content-Type": "application/json"})
    return decorated


# ═══════════════════════════════════════════════════════════════
#  TIME HELPER
# ═══════════════════════════════════════════════════════════════

def to_central(iso_str: str) -> str:
    """Convert ISO UTC string to Central Time display string."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        ct = dt.astimezone(CT)
        return ct.strftime("%m/%d %I:%M:%S %p CT")
    except Exception:
        return iso_str


def _ticker_to_market_time(ticker: str) -> str:
    """Parse ticker like KXBTC15M-26MAR051630-30 to market start time in CT."""
    try:
        # Ticker format: KXBTC15M-YYMMMDDHHM-MM
        parts = ticker.split("-")
        if len(parts) < 3:
            return ""
        date_part = parts[1]  # e.g. 26MAR051630
        min_part = parts[2]   # e.g. 30
        # Close time = HHMM where HH = date_part[-4:-2], MM = min_part
        year = int("20" + date_part[:2])
        month_str = date_part[2:5]
        day = int(date_part[5:7])
        hour = int(date_part[7:9])
        minute = int(min_part)
        months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                  "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
        month = months.get(month_str, 1)
        close_et = datetime(year, month, day, hour, minute, tzinfo=ET)
        start_et = close_et - timedelta(minutes=15)
        start_ct = start_et.astimezone(CT)
        return start_ct.strftime("%-I:%M %p")
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════
#  API ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.route("/api/login", methods=["POST"])
def api_login():
    ip = request.remote_addr or "unknown"
    rate_err = _check_login_rate(ip)
    if rate_err:
        insert_audit_log("login_rate_limited", f"ip={ip}", ip=ip, success=False)
        return jsonify({"error": rate_err}), 429
    data = request.get_json() or {}
    if check_auth(data.get("username", ""), data.get("password", "")):
        _record_login_attempt(ip, True)
        insert_audit_log("login_success", "", ip=ip)
        resp = jsonify({"ok": True})
        resp.set_cookie("platform_auth", _auth_token(), max_age=30*86400,
                        httponly=True, samesite="Lax", secure=request.is_secure)
        return resp
    _record_login_attempt(ip, False)
    insert_audit_log("login_failed", "", ip=ip, success=False)
    return jsonify({"error": "Invalid credentials"}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    resp = jsonify({"ok": True})
    resp.set_cookie("platform_auth", "", max_age=0)
    return resp

@app.route("/api/invalidate_sessions", methods=["POST"])
@requires_auth
def api_invalidate_sessions():
    """Rotate session salt — invalidates ALL sessions everywhere."""
    new_salt = _secrets.token_hex(16)
    set_config("_session_salt", new_salt)
    resp = jsonify({"ok": True, "msg": "All sessions invalidated"})
    resp.set_cookie("platform_auth", _auth_token(), max_age=30*86400,
                    httponly=True, samesite="Lax", secure=request.is_secure)
    return resp


@app.route("/api/change_password", methods=["POST"])
def api_change_password():
    import hashlib
    data = request.get_json() or {}
    username = data.get("username", "")
    old_pass = data.get("old_password", "")
    new_pass = data.get("new_password", "")

    if not username or not old_pass or not new_pass:
        return jsonify({"error": "All fields required"}), 400
    if len(new_pass) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    if not check_auth(username, old_pass):
        return jsonify({"error": "Current credentials are incorrect"}), 401

    # Store hashed password in DB
    new_hash = hashlib.sha256(new_pass.encode()).hexdigest()
    set_config("dashboard_pass_hash", new_hash)
    insert_audit_log("password_changed", "", ip=request.remote_addr or "")
    return jsonify({"ok": True})


@app.route("/api/destruction_pin", methods=["GET"])
@requires_auth
def api_destruction_pin_status():
    return jsonify({"has_pin": bool(get_config("destruction_pin_hash"))})

@app.route("/api/destruction_pin", methods=["POST"])
@requires_auth
def api_destruction_pin_set():
    data = request.get_json() or {}
    new_pin = data.get("pin", "")
    current_pin = data.get("current_pin", "")
    ip = request.remote_addr or ""
    if get_config("destruction_pin_hash"):
        if not _check_destruction_pin(current_pin):
            insert_audit_log("pin_change_failed", "wrong current", ip=ip, success=False)
            return jsonify({"error": "Current PIN is incorrect"}), 401
    if not new_pin or len(new_pin) < 4 or len(new_pin) > 8 or not new_pin.isdigit():
        return jsonify({"error": "PIN must be 4-8 digits"}), 400
    _set_destruction_pin(new_pin)
    insert_audit_log("pin_changed", "", ip=ip)
    return jsonify({"ok": True})

@app.route("/api/audit_log")
@requires_auth
def api_audit_log():
    limit = min(int(request.args.get("limit", 50)), 200)
    try:
        with get_conn() as c:
            rows = c.execute("SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        from db import rows_to_list
        return jsonify({"entries": rows_to_list(rows)})
    except Exception as e:
        return jsonify({"entries": [], "error": str(e)})


@app.route("/api/state")
@requires_auth
def api_state():
    state = get_bot_state()
    state["last_updated_ct"] = to_central(state.get("last_updated", ""))
    # Include trading_mode for mode selector (migrate from legacy booleans if needed)
    try:
        tm = get_config("btc_15m.trading_mode", "")
        if not tm:
            # Migration: derive from legacy booleans
            obs = get_config("btc_15m.observe_only", False)
            shd = get_config("btc_15m.shadow_trading", False)
            auto = get_config("btc_15m.auto_strategy_enabled", False)
            if obs and shd:
                tm = "shadow"
            elif obs:
                tm = "observe"
            elif auto:
                tm = "auto"
            else:
                tm = "manual"
            set_config("btc_15m.trading_mode", tm)
        state["trading_mode"] = tm
    except Exception:
        state["trading_mode"] = "observe"
    # Legacy observe_only for backward compat
    try:
        state["observe_only"] = get_config("btc_15m.observe_only", False)
    except Exception:
        state["observe_only"] = False

    # Include active shadow trade for current market (if any)
    try:
        ticker = state.get("last_ticker")
        if ticker:
            with get_conn() as c:
                shadow = c.execute("""
                    SELECT id, ticker, side, avg_fill_price_c, entry_price_c,
                           actual_cost, outcome, shadow_decision_price_c,
                           shadow_fill_latency_ms, spread_at_entry_c,
                           created_at
                    FROM btc15m_trades
                    WHERE COALESCE(is_shadow, 0) = 1
                      AND ticker = ?
                    ORDER BY created_at DESC LIMIT 1
                """, (ticker,)).fetchone()
                if shadow:
                    state["shadow_trade"] = dict(shadow)
    except Exception:
        pass

    return jsonify(state)


@app.route("/api/summary")
@requires_auth
def api_summary():
    return jsonify(get_trade_summary())


@app.route("/api/config")
@requires_auth
def api_config():
    from config import DEFAULT_BOT_CONFIG
    cfg = {**DEFAULT_BOT_CONFIG}
    cfg.update(get_all_config())
    cfg.pop("anthropic_api_key", None)  # Never expose via config API
    return jsonify(cfg)


@app.route("/api/config", methods=["POST"])
@requires_auth
def api_set_config():
    data = request.get_json() or {}
    # When trading_mode is set, derive legacy booleans for bot compatibility
    if "trading_mode" in data:
        mode = data["trading_mode"]
        legacy = _trading_mode_to_legacy(mode)
        for k, v in legacy.items():
            data[k] = v
    for k, v in data.items():
        set_config(k, v)
    enqueue_command("btc_15m", "update_config", data)
    return jsonify({"ok": True})


def _trading_mode_to_legacy(mode: str) -> dict:
    """Derive observe_only, shadow_trading, auto_strategy_enabled from trading_mode."""
    return {
        "observe": {"observe_only": True, "shadow_trading": False, "auto_strategy_enabled": False},
        "shadow":  {"observe_only": True, "shadow_trading": True,  "auto_strategy_enabled": False},
        "hybrid":  {"observe_only": False, "shadow_trading": True,  "auto_strategy_enabled": True},
        "auto":    {"observe_only": False, "shadow_trading": False, "auto_strategy_enabled": True},
        "manual":  {"observe_only": False, "shadow_trading": False, "auto_strategy_enabled": False},
    }.get(mode, {"observe_only": True, "shadow_trading": False, "auto_strategy_enabled": False})



@app.route("/api/command", methods=["POST"])
@requires_auth
def api_command():
    data = request.get_json() or {}
    cmd = data.get("command", "")
    params = data.get("params", {})
    if cmd not in ("start", "stop", "reset_streak",
                   "update_config",
                   "dismiss_summary"):
        return jsonify({"error": "Invalid command"}), 400

    # Dismiss summary: clear immediately, no need to queue
    if cmd == "dismiss_summary":
        update_plugin_state("btc_15m", {"last_completed_trade": None})
        return jsonify({"ok": True})

    # Reset streak: handle immediately
    if cmd == "reset_streak":
        update_plugin_state("btc_15m", {"loss_streak": 0, "cooldown_remaining": 0})
        return jsonify({"ok": True})

    # Start: set state for instant UI feedback
    if cmd == "start":
        state = get_bot_state()
        at = state.get("active_trade")
        has_ignored = at and at.get("is_ignored")
        update_plugin_state("btc_15m", {
            "auto_trading": 1,
            "status": "trading" if has_ignored else "searching",
            "status_detail": "Waiting for ignored trade to resolve..." if has_ignored else "Starting...",
        })

    # Stop: set state for instant UI feedback
    if cmd == "stop":
        update_plugin_state("btc_15m", {
            "auto_trading": 0,
            "trades_remaining": 0,
            "loss_streak": 0,
            "status_detail": "Stopping...",
        })

    cmd_id = enqueue_command("btc_15m", cmd, params)
    return jsonify({"ok": True, "command_id": cmd_id})


@app.route("/api/trades")
@requires_auth
def api_trades():
    limit = request.args.get("limit", 50, type=int)
    trades = get_recent_trades(limit)
    for t in trades:
        t["created_ct"] = to_central(t.get("created_at", ""))
        # Compute market start time from ticker
        ticker = t.get("ticker", "")
        t["market_ct"] = _ticker_to_market_time(ticker)
    return jsonify(trades)


@app.route("/api/trades_v2")
@requires_auth
def api_trades_v2():
    """Server-side filtered, paginated trades with aggregate stats."""
    from db import get_conn, rows_to_list
    regime = request.args.get("regime", "")
    offset = request.args.get("offset", 0, type=int)
    limit = request.args.get("limit", 30, type=int)

    # Filter name → SQL condition mapping
    _FILTER_SQL = {
        "win": "outcome = 'win'",
        "loss": "outcome = 'loss'",
        "skipped": "outcome IN ('skipped', 'no_fill')",
        "error": "outcome = 'error'",
        "incomplete": "outcome = 'skipped' AND market_result IS NULL",
        "ignored": "COALESCE(is_ignored, 0) = 1",
        "shadow": "COALESCE(is_shadow, 0) = 1",
        "yes": "side = 'yes'",
        "no": "side = 'no'",
        "early": "auto_strategy_key LIKE '%:early:%'",
        "mid": "auto_strategy_key LIKE '%:mid:%'",
        "late": "auto_strategy_key LIKE '%:late:%'",
        "cheaper": "auto_strategy_key LIKE 'cheaper:%'",
        "model": "auto_strategy_key LIKE 'model:%'",
        "sold": "exit_method = 'sell_fill'",
        "hold": "exit_method = 'market_expiry'",
    }

    # Build WHERE clauses — include/exclude system
    where_parts = []
    params = []

    # Support both new include/exclude params and legacy filters param
    include_raw = request.args.get("include", "")
    exclude_raw = request.args.get("exclude", "")
    legacy_filters = request.args.get("filters", "")

    if include_raw or exclude_raw:
        include_list = [f.strip() for f in include_raw.split(",") if f.strip()]
        exclude_list = [f.strip() for f in exclude_raw.split(",") if f.strip()]
    elif legacy_filters and legacy_filters != "all":
        include_list = [f.strip() for f in legacy_filters.split(",") if f.strip()]
        exclude_list = []
    else:
        include_list = []
        exclude_list = []

    # Includes are OR'd together
    inc_conditions = [_FILTER_SQL[f] for f in include_list if f in _FILTER_SQL]
    if inc_conditions:
        where_parts.append("(" + " OR ".join(inc_conditions) + ")")

    # Excludes are each AND NOT'd
    for f in exclude_list:
        if f in _FILTER_SQL:
            where_parts.append(f"NOT ({_FILTER_SQL[f]})")

    if regime:
        where_parts.append("regime_label = ?")
        params.append(regime)

    # Exclude open trades — they get their own card
    where_parts.append("outcome != 'open'")

    where_sql = " AND ".join(where_parts) if where_parts else "1=1"

    with get_conn() as c:
        # Aggregate stats for ALL matching trades
        stats_row = c.execute(f"""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) as losses,
                SUM(CASE WHEN outcome IN ('skipped','no_fill') THEN 1 ELSE 0 END) as skips,
                SUM(CASE WHEN outcome = 'error' THEN 1 ELSE 0 END) as errors,
                SUM(CASE WHEN outcome IN ('win','loss') THEN COALESCE(pnl, 0) ELSE 0 END) as pnl,
                MAX(CASE WHEN outcome IN ('win','loss') THEN pnl ELSE NULL END) as best,
                MIN(CASE WHEN outcome IN ('win','loss') THEN pnl ELSE NULL END) as worst,
                SUM(CASE WHEN outcome IN ('win','loss') THEN COALESCE(actual_cost, 0) ELSE 0 END) as wagered,
                AVG(CASE WHEN outcome IN ('win','loss') THEN pnl ELSE NULL END) as avg_pnl,
                AVG(CASE WHEN outcome IN ('win','loss') THEN avg_fill_price_c ELSE NULL END) as avg_entry,
                SUM(CASE WHEN COALESCE(is_shadow, 0) = 1 THEN 1 ELSE 0 END) as shadows
            FROM btc15m_trades WHERE {where_sql}
        """, params).fetchone()

        stats = {
            "total": stats_row["total"] or 0,
            "wins": stats_row["wins"] or 0,
            "losses": stats_row["losses"] or 0,
            "skips": stats_row["skips"] or 0,
            "errors": stats_row["errors"] or 0,
            "pnl": round(stats_row["pnl"] or 0, 2),
            "best": round(stats_row["best"] or 0, 2),
            "worst": round(stats_row["worst"] or 0, 2),
            "wagered": round(stats_row["wagered"] or 0, 2),
            "avg_pnl": round(stats_row["avg_pnl"] or 0, 2),
            "avg_entry": round(stats_row["avg_entry"] or 0),
            "shadows": stats_row["shadows"] or 0,
        }
        real = stats["wins"] + stats["losses"]
        stats["win_rate"] = round(stats["wins"] / real * 100, 1) if real > 0 else 0
        stats["roi"] = round(stats["pnl"] / stats["wagered"] * 100, 1) if stats["wagered"] > 0 else 0

        # Paginated trades
        rows = c.execute(f"""
            SELECT * FROM btc15m_trades WHERE {where_sql}
            ORDER BY created_at DESC LIMIT ? OFFSET ?
        """, params + [limit, offset]).fetchall()
        trades = rows_to_list(rows)

        # Distinct regime labels for filter dropdown
        regimes = c.execute("""
            SELECT DISTINCT regime_label FROM btc15m_trades
            WHERE regime_label IS NOT NULL AND outcome != 'open'
            ORDER BY regime_label
        """).fetchall()
        regime_list = [r["regime_label"] for r in regimes]

    for t in trades:
        try:
            t["created_ct"] = to_central(t.get("created_at", ""))
            t["market_ct"] = _ticker_to_market_time(t.get("ticker", ""))
            # Entry/exit time formatting and time-in-market computation
            _entry_utc = t.get("entry_time_utc") or ""
            _exit_utc = t.get("exit_time_utc") or ""
            t["entry_ct"] = to_central(_entry_utc) if _entry_utc else ""
            t["exit_ct"] = to_central(_exit_utc) if _exit_utc else ""
            if _entry_utc and _exit_utc:
                try:
                    from datetime import datetime as _dt, timezone as _tz
                    _e = _dt.fromisoformat(_entry_utc.replace("Z", "+00:00"))
                    _x = _dt.fromisoformat(_exit_utc.replace("Z", "+00:00"))
                    t["time_in_market_s"] = max(0, int((_x - _e).total_seconds()))
                except Exception:
                    t["time_in_market_s"] = None
            else:
                t["time_in_market_s"] = None
            # Strategy key (auto or manual)
            t["strategy_display"] = t.get("auto_strategy_key") or ""
        except Exception:
            t.setdefault("created_ct", "")
            t.setdefault("market_ct", "")
            t.setdefault("entry_ct", "")
            t.setdefault("exit_ct", "")
            t.setdefault("time_in_market_s", None)
            t.setdefault("strategy_display", "")

    return jsonify({
        "trades": trades,
        "stats": stats,
        "has_more": len(trades) >= limit,
        "regimes": regime_list,
    })



@app.route("/api/trade/<int:trade_id>/delete", methods=["POST"])
@requires_auth
def api_delete_trade(trade_id):
    """Delete a single trade and recompute stats."""
    trade = get_trade(trade_id)
    if not trade:
        return jsonify({"error": "Trade not found"}), 404
    delete_trades([trade_id])
    insert_audit_log("delete_trade", f"id={trade_id}", ip=request.remote_addr or "")
    recompute_all_stats()
    return jsonify({"ok": True, "deleted": 1})


@app.route("/api/trades/delete_incomplete", methods=["POST"])
@requires_auth
def api_delete_incomplete():
    """Delete all incomplete observed trades (no market result)."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT id FROM btc15m_trades
            WHERE outcome = 'skipped' AND market_result IS NULL
        """).fetchall()
        ids = [r["id"] for r in rows]
    if ids:
        delete_trades(ids)
        recompute_all_stats()
    return jsonify({"ok": True, "deleted": len(ids)})


@app.route("/api/trade/<int:trade_id>/price_path")
@requires_auth
def api_price_path(trade_id):
    path = get_price_path(trade_id)
    return jsonify(path)


@app.route("/api/trade/<int:trade_id>/detail")
@requires_auth
def api_trade_detail(trade_id):
    t = get_trade(trade_id)
    if not t:
        return jsonify({"error": "Not found"}), 404
    t["created_ct"] = to_central(t.get("created_at", ""))
    t["market_ct"] = _ticker_to_market_time(t.get("ticker", ""))
    _entry_utc = t.get("entry_time_utc") or ""
    _exit_utc = t.get("exit_time_utc") or ""
    t["entry_ct"] = to_central(_entry_utc) if _entry_utc else ""
    t["exit_ct"] = to_central(_exit_utc) if _exit_utc else ""
    if _entry_utc and _exit_utc:
        try:
            from datetime import datetime as _dt
            _e = _dt.fromisoformat(_entry_utc.replace("Z", "+00:00"))
            _x = _dt.fromisoformat(_exit_utc.replace("Z", "+00:00"))
            t["time_in_market_s"] = max(0, int((_x - _e).total_seconds()))
        except Exception:
            t["time_in_market_s"] = None
    else:
        t["time_in_market_s"] = None
    t["strategy_display"] = t.get("auto_strategy_key") or ""
    path = get_price_path(trade_id)
    return jsonify({"trade": t, "price_path": path})


@app.route("/api/trades/csv")
@requires_auth
def api_trades_csv():
    """Export all trades as CSV."""
    import csv, io
    trades = get_recent_trades(limit=10000)
    if not trades:
        return Response("No trades", mimetype="text/plain")

    cols = [
        "id", "ticker", "side", "outcome", "pnl", "actual_cost", "gross_proceeds",
        "fees_paid", "avg_fill_price_c", "sell_price_c", "shares_filled", "sell_filled",
        "price_high_water_c", "price_low_water_c", "pct_progress_toward_target",
        "oscillation_count", "regime_label", "regime_risk_level", "vol_regime",
        "trend_regime", "volume_regime", "is_data_collection",
        "is_ignored", "is_early_exit", "entry_delay_minutes", "trade_mode",
        "price_stability_c", "btc_price_at_entry", "market_result",
        "skip_reason", "notes", "created_at",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    for t in trades:
        writer.writerow({c: t.get(c, "") for c in cols})

    resp = Response(buf.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = f"attachment; filename=trades_{now_utc()[:10]}.csv"
    return resp



@app.route("/api/reset", methods=["POST"])
@requires_auth
def api_reset():
    """Granular reset endpoint. Destructive scopes require PIN + auto-backup."""
    data = request.json or {}
    scope = data.get("scope", "")
    ip = request.remote_addr or ""
    from db import get_conn

    # Destructive scopes need PIN + auto-backup
    if scope in _DESTRUCTIVE_SCOPES:
        pin = data.get("pin", "")
        if not _check_destruction_pin(pin):
            insert_audit_log("reset_pin_failed", f"scope={scope}", ip=ip, success=False)
            return jsonify({"error": "Invalid destruction PIN", "pin_required": True}), 403
        bp = backup_database(reason=f"pre_{scope}")
        if bp:
            insert_audit_log("auto_backup", f"scope={scope}", ip=ip)

    insert_audit_log("reset", f"scope={scope}", ip=ip)

    try:
        if scope == "settings":
            with get_conn() as c:
                keep = ('push_subscription', 'push_vapid_public',
                        'push_vapid_private', 'dashboard_password', 'dashboard_user')
                rows = c.execute("SELECT key FROM bot_config").fetchall()
                for r in rows:
                    if r["key"] not in keep:
                        c.execute("DELETE FROM bot_config WHERE key = ?", (r["key"],))
            return jsonify({"ok": True, "scope": "settings", "msg": "Settings reset to defaults"})

        elif scope == "trades":
            with get_conn() as c:
                c.execute("DELETE FROM btc15m_trades")
                c.execute("DELETE FROM btc15m_price_path")
                c.execute("DELETE FROM btc15m_exit_simulations")
                c.execute("DELETE FROM btc15m_regime_opportunities")
                c.execute("DELETE FROM btc15m_observations")
                c.execute("DELETE FROM btc15m_strategy_results")
                # Clear confidence data derived from trades
                try:
                    c.execute("DELETE FROM confidence_factors")
                    c.execute("DELETE FROM confidence_calibration")
                    c.execute("DELETE FROM edge_calibration")
                except Exception:
                    pass
                # Clear analysis data derived from observations
                try:
                    c.execute("DELETE FROM btc15m_probability_surface")
                    c.execute("DELETE FROM btc15m_feature_importance")
                    c.execute("DELETE FROM regime_stability_log")
                except Exception:
                    pass
            recompute_all_stats()
            update_plugin_state("btc_15m", {"active_trade": None, "active_skip": None, "active_shadow": None,
                              "last_completed_trade": None, "last_ticker": None})
            return jsonify({"ok": True, "scope": "trades", "msg": "All trades and observations cleared"})

        elif scope == "regime_filters":
            set_config("btc_15m.regime_filters", {})
            set_config("btc_15m.regime_overrides", {})
            return jsonify({"ok": True, "scope": "regime_filters", "msg": "Regime filters and overrides cleared"})

        elif scope == "regime_engine":
            with get_conn() as c:
                c.execute("DELETE FROM btc15m_regime_stats")
                c.execute("DELETE FROM btc15m_hourly_stats")
                c.execute("DELETE FROM regime_snapshots")
                c.execute("DELETE FROM baselines")
                c.execute("DELETE FROM candles")
                try:
                    c.execute("DELETE FROM regime_stability_log")
                except Exception:
                    pass
            update_plugin_state("btc_15m", {"regime_engine_phase": None})
            return jsonify({"ok": True, "scope": "regime_engine", "msg": "Regime engine data wiped"})

        elif scope == "full":
            with get_conn() as c:
                tables = ['btc15m_trades', 'btc15m_price_path', 'btc15m_exit_simulations',
                          'btc15m_regime_opportunities', 'btc15m_regime_stats',
                          'btc15m_hourly_stats', 'btc15m_live_prices', 'btc15m_markets',
                          'btc15m_observations', 'btc15m_strategy_results',
                          'btc15m_probability_surface', 'btc15m_feature_importance',
                          'regime_snapshots', 'baselines', 'candles',
                          'bankroll_snapshots', 'regime_stability_log',
                          'push_log', 'log_entries', 'bot_commands', 'audit_log']
                for t in tables:
                    try:
                        c.execute(f"DELETE FROM {t}")
                    except Exception:
                        pass
                keep = ('push_subscription', 'push_vapid_public', 'push_vapid_private',
                        'dashboard_password', 'dashboard_user')
                rows = c.execute("SELECT key FROM bot_config").fetchall()
                for r in rows:
                    if r["key"] not in keep:
                        c.execute("DELETE FROM bot_config WHERE key = ?", (r["key"],))
            update_plugin_state("btc_15m", {
                "status": "stopped", "status_detail": "Full reset",
                "auto_trading": 0, "trades_remaining": 0,
                "lifetime_pnl": 0, "lifetime_wins": 0, "lifetime_losses": 0,
                "loss_streak": 0,
                "cooldown_remaining": 0, "active_trade": None,
                "pending_trade": None, "live_market": None,
                "last_ticker": None,
                "active_skip": None,
                "active_shadow": None,
                "regime_engine_phase": None,
                "last_completed_trade": None,
                "auto_trading_since": "",
                "_delay_end_iso": None,
                "bankroll_cents": 0,
                "observatory_health": None,
            })
            return jsonify({"ok": True, "scope": "full", "msg": "Complete wipe — all data cleared"})

        else:
            return jsonify({"error": f"Unknown scope: {scope}"}), 400

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e), "msg": f"Reset failed: {e}"}), 500


@app.route("/api/skip_conditions")
@requires_auth
def api_skip_conditions():
    """Return all active rules that determine when to observe instead of trade."""
    from config import DEFAULT_BOT_CONFIG
    cfg = {**DEFAULT_BOT_CONFIG}
    cfg.update(get_all_config())
    state = get_bot_state()
    conditions = []

    # Risk level actions
    risk_acts = cfg.get("risk_level_actions", {})
    if isinstance(risk_acts, str):
        risk_acts = json.loads(risk_acts)
    defaults = {"low": "normal", "moderate": "normal", "high": "normal",
                "terrible": "skip", "unknown": "skip"}
    for level in ["low", "moderate", "high", "terrible", "unknown"]:
        action = risk_acts.get(level, defaults.get(level, "normal"))
        if action == "skip":
            conditions.append({"type": "risk_level", "label": f"{level.title()} risk → Observe",
                               "color": {"low":"green","moderate":"yellow","high":"orange",
                                         "terrible":"red","unknown":"dim"}.get(level, "dim")})


    # Per-regime overrides
    overrides = cfg.get("regime_overrides", {})
    if isinstance(overrides, str):
        overrides = json.loads(overrides)
    skip_overrides = [k for k, v in overrides.items() if v == "skip"]
    if skip_overrides:
        conditions.append({"type": "regime_override",
                           "label": f"{len(skip_overrides)} regime(s) forced to Observe",
                           "color": "orange", "detail": ", ".join(r.replace("_"," ") for r in skip_overrides[:5])})

    # Per-regime filters
    regime_filters = cfg.get("regime_filters", {})
    if isinstance(regime_filters, str):
        regime_filters = json.loads(regime_filters)
    for label, rf in regime_filters.items():
        parts = []
        if rf.get("blocked_hours"):
            parts.append(f"{len(rf['blocked_hours'])} hours blocked")
        if rf.get("blocked_days"):
            parts.append(f"{len(rf['blocked_days'])} days blocked")
        if rf.get("vol_min", 1) > 1 or rf.get("vol_max", 5) < 5:
            parts.append(f"vol {rf.get('vol_min',1)}-{rf.get('vol_max',5)}")
        if rf.get("stability_max", 0) > 0:
            parts.append(f"stability ≤{rf['stability_max']}¢")
        if rf.get("blocked_sides"):
            parts.append(', '.join(s.upper() for s in rf['blocked_sides']) + ' side blocked')

        if rf.get("max_spread_c", 0) > 0:
            parts.append(f"spread ≤{rf['max_spread_c']}¢")
        if parts:
            conditions.append({"type": "regime_filter",
                               "label": label.replace("_", " "),
                               "color": "blue", "detail": " · ".join(parts)})

    # Entry price range
    pmax = cfg.get("entry_price_max_c", 45)
    conditions.append({"type": "entry_price", "label": f"Entry price ≤{pmax}¢",
                       "color": "dim"})

    # Cooldown
    cd = state.get("cooldown_remaining", 0)
    if cd > 0:
        conditions.append({"type": "cooldown", "label": f"Cooldown: {cd} market(s) left",
                           "color": "orange"})
    cd_cfg = cfg.get("cooldown_after_loss_stop", 0)
    if cd_cfg > 0:
        conditions.append({"type": "cooldown_cfg", "label": f"Cooldown after max loss: {cd_cfg} markets",
                           "color": "dim"})

    # Bankroll guards
    bmin = float(cfg.get("bankroll_min", 0))
    bmax = float(cfg.get("bankroll_max", 0))
    if bmin > 0:
        conditions.append({"type": "bankroll_min", "label": f"Min bankroll: ${bmin:.0f}",
                           "color": "dim"})
    if bmax > 0:
        conditions.append({"type": "bankroll_max", "label": f"Max bankroll: ${bmax:.0f}",
                           "color": "dim"})


    # Rolling win-rate circuit breaker
    rw_window = int(cfg.get("rolling_wr_window", 0) or 0)
    rw_floor = float(cfg.get("rolling_wr_floor", 0) or 0)
    if rw_window > 0 and rw_floor > 0:
        conditions.append({"type": "rolling_wr", "label": f"Win rate floor: {rw_floor:.0f}% over last {rw_window}",
                           "color": "orange"})

    return jsonify(conditions)


@app.route("/api/lifetime")
@requires_auth
def api_lifetime():
    return jsonify(get_lifetime_stats())


@app.route("/api/observatory")
@requires_auth
def api_observatory():
    """Strategy Observatory summary — observations, top strategies, avoid list."""
    try:
        # get_observatory_summary already imported from market_db
        return jsonify(get_observatory_summary())
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/fee-sensitivity")
@requires_auth
def api_fee_sensitivity():
    """Fee sensitivity analysis — stress-test top strategies at higher fee rates."""
    try:
        from strategy import fee_sensitivity_analysis
        setup = request.args.get("setup", "global:all")
        top_n = int(request.args.get("top", 10))
        results = fee_sensitivity_analysis(setup_key=setup, top_n=top_n)
        return jsonify({"setup": setup, "strategies": results})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/strategies")
@requires_auth
def api_strategies():
    """Strategy Lab API — two modes: best per regime or strategy lookup."""
    mode = request.args.get("mode", "best")
    min_n = int(request.args.get("min", 5))
    try:
        from db import get_conn, rows_to_list
        with get_conn() as c:
            if mode == "best":
                # Best strategy per regime — top 1 per regime setup
                rows = c.execute("""
                    SELECT sr.setup_key, sr.strategy_key, sr.side_rule,
                           sr.exit_rule as sell_target, sr.entry_time_rule,
                           sr.entry_price_max, sr.sample_size, sr.wins, sr.losses,
                           sr.win_rate, sr.total_pnl_c, sr.ev_per_trade_c,
                           sr.profit_factor, sr.ci_lower, sr.ci_upper,
                           sr.max_consecutive_losses, sr.max_drawdown_c
                    FROM btc15m_strategy_results sr
                    INNER JOIN (
                        SELECT setup_key, MAX(ev_per_trade_c) as max_ev
                        FROM btc15m_strategy_results
                        WHERE setup_type = 'coarse_regime' AND sample_size >= ?
                          AND ev_per_trade_c > 0
                        GROUP BY setup_key
                    ) best ON sr.setup_key = best.setup_key
                        AND sr.ev_per_trade_c = best.max_ev
                        AND sr.setup_type = 'coarse_regime'
                        AND sr.sample_size >= ?
                    ORDER BY sr.ev_per_trade_c DESC
                """, (min_n, min_n)).fetchall()
                return jsonify({"mode": "best", "strategies": rows_to_list(rows)})

            else:
                # Lookup mode — filter by strategy components, show across regimes
                side = request.args.get("side", "")
                timing = request.args.get("timing", "")
                entry = request.args.get("entry", "")
                sell = request.args.get("sell", "")

                # Build strategy_key LIKE filter
                parts = []
                if side: parts.append(side)
                else: parts.append("%")
                if timing: parts.append(timing)
                else: parts.append("%")
                if entry: parts.append(entry)
                else: parts.append("%")
                if sell: parts.append(sell)
                else: parts.append("%")
                like_pattern = ":".join(parts)

                rows = c.execute("""
                    SELECT setup_key, strategy_key, side_rule,
                           exit_rule as sell_target, entry_time_rule,
                           entry_price_max, sample_size, wins, losses,
                           win_rate, total_pnl_c, ev_per_trade_c,
                           profit_factor, ci_lower, ci_upper,
                           max_consecutive_losses, max_drawdown_c
                    FROM btc15m_strategy_results
                    WHERE strategy_key LIKE ? AND sample_size >= ?
                    ORDER BY ev_per_trade_c DESC
                    LIMIT 100
                """, (like_pattern, min_n)).fetchall()
                return jsonify({"mode": "lookup", "strategies": rows_to_list(rows)})
    except Exception as e:
        return jsonify({"mode": mode, "strategies": [], "error": str(e)})


@app.route("/api/strategy_regime_preview")
@requires_auth
def api_strategy_regime_preview():
    """Per-regime breakdown for a given strategy key — unified sim + real trade data."""
    strategy_key = request.args.get("key", "")
    if not strategy_key:
        return jsonify({"regimes": [], "error": "No strategy key"})
    try:
        from plugins.btc_15m.market_db import _classify_risk, _wilson_ci
        from config import REGIME_THRESHOLDS
        min_n = REGIME_THRESHOLDS.get("min_sim_known", 10)
        regime_data = {}  # regime_label -> {sim: dict, real_*: ...}

        with get_conn() as c:
            # 1. Observatory simulation data
            sim_rows = c.execute("""
                SELECT setup_key, strategy_key, sample_size, wins, losses,
                       win_rate, total_pnl_c, ev_per_trade_c, profit_factor,
                       ci_lower, ci_upper, max_consecutive_losses, max_drawdown_c,
                       weighted_ev_c, weighted_win_rate,
                       oos_ev_c, oos_win_rate, oos_sample_size,
                       fdr_significant, fdr_q_value, pnl_std_c,
                       slippage_1c_ev, slippage_2c_ev, breakeven_fee_rate
                FROM btc15m_strategy_results
                WHERE strategy_key = ? AND setup_type = 'coarse_regime'
            """, (strategy_key,)).fetchall()

            for r in rows_to_list(sim_rows):
                sk = r.get("setup_key", "")
                regime = sk.replace("coarse_regime:", "", 1) if sk.startswith("coarse_regime:") else sk
                regime_data[regime] = {"sim": r, "rt": 0, "rw": 0, "rl": 0, "rpnl": 0}

            # 2. Real trade data for this strategy key per regime
            trade_rows = c.execute("""
                SELECT regime_label,
                       COUNT(*) as total,
                       SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) as losses,
                       SUM(COALESCE(pnl, 0)) as total_pnl
                FROM btc15m_trades
                WHERE auto_strategy_key = ?
                  AND outcome IN ('win', 'loss')
                  AND COALESCE(is_ignored, 0) = 0
                  AND regime_label IS NOT NULL
                GROUP BY regime_label
            """, (strategy_key,)).fetchall()

            for tr in trade_rows:
                regime = tr["regime_label"]
                if regime not in regime_data:
                    regime_data[regime] = {"sim": None, "rt": 0, "rw": 0, "rl": 0, "rpnl": 0}
                regime_data[regime]["rt"] = tr["total"] or 0
                regime_data[regime]["rw"] = tr["wins"] or 0
                regime_data[regime]["rl"] = tr["losses"] or 0
                regime_data[regime]["rpnl"] = tr["total_pnl"] or 0

        # 3. Merge into unified metrics per regime
        result = []
        for regime, rd in regime_data.items():
            sim = rd["sim"]
            rt, rw, rl, rpnl_dollars = rd["rt"], rd["rw"], rd["rl"], rd["rpnl"]
            rpnl_c = round(rpnl_dollars * 100)  # convert dollars to cents

            sim_n = (sim.get("sample_size") or 0) if sim else 0
            sim_w = (sim.get("wins") or 0) if sim else 0
            sim_l = (sim.get("losses") or 0) if sim else 0
            sim_pnl_c = (sim.get("total_pnl_c") or 0) if sim else 0

            # Combined totals — real trades are just more data points
            total_n = sim_n + rt
            total_w = sim_w + rw
            total_l = sim_l + rl
            total_pnl_c = sim_pnl_c + rpnl_c
            combined_wr = total_w / total_n if total_n > 0 else 0
            combined_ev = total_pnl_c / total_n if total_n > 0 else 0
            ci_lo, ci_hi = _wilson_ci(total_w, total_n)

            # Build a synthetic row for the composite risk scorer
            # Start with sim fields (for OOS, FDR, slippage, etc.), override merged fields
            merged = dict(sim) if sim else {}
            merged["sample_size"] = total_n
            merged["wins"] = total_w
            merged["losses"] = total_l
            merged["win_rate"] = combined_wr
            merged["total_pnl_c"] = total_pnl_c
            merged["ev_per_trade_c"] = combined_ev
            merged["ci_lower"] = ci_lo
            merged["ci_upper"] = ci_hi
            # Recalculate profit factor from combined data
            gross_w = sum(1 for _ in range(total_w))  # simplified
            if sim and sim.get("profit_factor") and rt == 0:
                merged["profit_factor"] = sim["profit_factor"]
            elif total_l > 0 and total_w > 0 and total_pnl_c != 0:
                # Approximate: can't get exact gross win/loss from aggregates
                # Keep sim profit_factor if available, it's the best we have
                pass

            score = compute_strategy_risk_score(merged)
            risk = _classify_risk(score, total_n, min_n)

            entry = {
                "regime_label": regime,
                "risk_level": risk,
                "risk_score": score,
                "sample_size": total_n,
                "sim_n": sim_n,
                "live_n": rt,
                "wins": total_w,
                "losses": total_l,
                "win_rate": combined_wr,
                "ev_per_trade_c": combined_ev,
                "total_pnl_c": total_pnl_c,
                "ci_lower": ci_lo,
                "ci_upper": ci_hi,
                "profit_factor": merged.get("profit_factor"),
                "weighted_ev_c": merged.get("weighted_ev_c"),
                "fdr_significant": merged.get("fdr_significant"),
                "oos_ev_c": merged.get("oos_ev_c"),
            }
            result.append(entry)

        result.sort(key=lambda r: (0 if r["risk_level"] == "unknown" else 1,
                                   r.get("risk_score", 0)), reverse=True)
        return jsonify({"regimes": result, "strategy_key": strategy_key})
    except Exception as e:
        return jsonify({"regimes": [], "error": str(e)})


@app.route("/api/skip_analysis")
@requires_auth
def api_skip_analysis():
    return jsonify(get_skip_analysis())


@app.route("/api/recompute_strategies", methods=["POST"])
@requires_auth
def api_recompute_strategies():
    """Trigger full recompute of strategy results. No gap — upserts over existing, then cleans stale."""
    try:
        from db import get_conn, set_config, now_utc
        with get_conn() as c:
            count = c.execute("SELECT COUNT(*) as n FROM btc15m_observations WHERE market_result IS NOT NULL").fetchone()["n"]
        # Clear discovery cache so new ones fire
        set_config("btc_15m._strategy_discoveries_notified", "[]")
        # Record timestamp before recompute
        _before = now_utc()
        # Recompute — upserts overwrite existing rows, adds new ones
        from strategy import run_simulation_batch
        processed = run_simulation_batch()
        # Clean up stale rows that weren't touched by this recompute
        # (strategies from deleted observations or changed setups)
        if processed > 0:
            with get_conn() as c:
                stale = c.execute("DELETE FROM btc15m_strategy_results WHERE updated_at < ?", (_before,))
                stale_count = stale.rowcount
            if stale_count:
                import logging
                logging.getLogger("bot").info(f"Cleaned {stale_count} stale strategy results")
        return jsonify({"ok": True, "observations": count, "processed": processed})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/net_edge")
@requires_auth
def api_net_edge():
    """Net edge per contract — the single most important metric."""
    try:
        # get_net_edge_summary already imported from market_db
        return jsonify(get_net_edge_summary())
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/realized_edge")
@requires_auth
def api_realized_edge():
    """Realized edge: actual P&L per contract vs simulated EV.
    The gap between these is the execution tax."""
    try:
        # get_realized_edge already imported from market_db
        return jsonify(get_realized_edge())
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/hold_vs_sell")
@requires_auth
def api_hold_vs_sell():
    """Hold-to-expiry vs sell-target comparison.
    Answers: do sell targets actually beat hold after fees?"""
    try:
        from strategy import compare_hold_vs_sell
        setup = request.args.get("setup", "global:all")
        return jsonify(compare_hold_vs_sell(setup_key=setup))
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/pnl_attribution")
@requires_auth
def api_pnl_attribution():
    """P&L attribution: model edge, execution cost, exit method, side selection."""
    try:
        # get_pnl_attribution already imported from market_db
        days = int(request.args.get("days", 30))
        return jsonify(get_pnl_attribution(days=days))
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/shadow_analysis")
@requires_auth
def api_shadow_analysis():
    """Shadow trade analysis: sim-to-reality gap measurement.
    Shows actual fill slippage, latency, and outcome accuracy."""
    try:
        # get_shadow_trade_analysis already imported from market_db
        return jsonify(get_shadow_trade_analysis())
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/shadow_reconciliation")
@requires_auth
def api_shadow_reconciliation():
    """Shadow trade reconciliation: compares actual shadow trade outcomes
    to what the simulation engine predicted for the same markets.
    The systematic_gap_c is the execution integrity metric."""
    try:
        # reconcile_shadow_trades already imported from market_db
        return jsonify(reconcile_shadow_trades())
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/active_shadow")
@requires_auth
def api_active_shadow():
    """Return the most recent open shadow trade for a given ticker."""
    try:
        ticker = request.args.get("ticker", "")
        if not ticker:
            return jsonify({"shadow": None})
        with get_conn() as c:
            row = c.execute("""
                SELECT id, ticker, side, avg_fill_price_c, entry_price_c,
                       actual_cost, outcome, shadow_decision_price_c,
                       shadow_fill_latency_ms, spread_at_entry_c,
                       created_at
                FROM btc15m_trades
                WHERE COALESCE(is_shadow, 0) = 1
                  AND ticker = ?
                  AND outcome = 'open'
                ORDER BY created_at DESC LIMIT 1
            """, (ticker,)).fetchone()
            return jsonify({"shadow": dict(row) if row else None})
    except Exception as e:
        return jsonify({"shadow": None, "error": str(e)})


@app.route("/api/strategy_persistence")
@requires_auth
def api_strategy_persistence():
    """Strategy persistence test: do first-half winners persist in second half?
    Optional ?setup=global:all&top_n=10"""
    try:
        from strategy import test_strategy_persistence
        setup = request.args.get("setup", "global:all")
        top_n = int(request.args.get("top_n", 10))
        return jsonify(test_strategy_persistence(setup_key=setup, top_n=top_n))
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/permutation_test")
@requires_auth
def api_permutation_test():
    """Permutation test: does the best strategy beat randomized outcomes?
    Optional ?setup=global:all&n=500. Computationally expensive — run on demand."""
    try:
        from strategy import run_permutation_test
        setup = request.args.get("setup", "global:all")
        n = int(request.args.get("n", 500))
        n = min(n, 1000)  # Cap at 1000 to prevent abuse
        return jsonify(run_permutation_test(setup_key=setup, n_permutations=n))
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/walkforward_selection")
@requires_auth
def api_walkforward_selection():
    """Walk-forward selection test: does the advisor's pick produce OOS returns?
    Optional ?folds=5. Stores result for use as a gate in get_recommendation."""
    try:
        from strategy import run_walkforward_selection_test
        from db import set_config
        folds = int(request.args.get("folds", 5))
        folds = max(3, min(folds, 10))
        result = run_walkforward_selection_test(n_folds=folds)
        # Store the verdict so get_recommendation can gate on it
        verdict = result.get("verdict", "insufficient_data")
        if verdict == "selection_works":
            set_config("btc_15m._selection_test_result", "passed")
        elif verdict == "selection_unreliable":
            set_config("btc_15m._selection_test_result", "failed")
        # insufficient_data → don't update (leave previous state)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/walkforward_selection_reset", methods=["POST"])
@requires_auth
def api_walkforward_selection_reset():
    """Clear the walk-forward selection test result, allowing recommendations
    during data collection until the test is re-run."""
    try:
        from db import set_config
        set_config("btc_15m._selection_test_result", "")
        return jsonify({"ok": True, "message": "Selection test result cleared. "
                        "Recommendations now allowed during data collection."})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/validation_result/<test_id>")
@requires_auth
def api_validation_result(test_id):
    """Poll for stored validation test result (set by bot command processor)."""
    try:
        from db import get_config
        raw = get_config(f"_validation_result_{test_id}")
        if not raw:
            return jsonify({"pending": True})
        import json as _json
        return jsonify(_json.loads(raw))
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/validation_result/<test_id>/clear", methods=["POST"])
@requires_auth
def api_validation_result_clear(test_id):
    """Clear stored validation result before starting a new run."""
    try:
        from db import set_config
        set_config(f"_validation_result_{test_id}", "")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/validation_summary")
@requires_auth
def api_validation_summary():
    """Lightweight summary for the Validation & Execution readiness overview.
    Gathers observation counts, gate status, and data thresholds in one call."""
    try:
        from db import get_conn, rows_to_list, get_config
        from datetime import datetime, timezone, timedelta
        from config import KALSHI_FEE_RATE

        with get_conn() as c:
            # Total usable observations
            total_obs = c.execute("""
                SELECT COUNT(*) as n FROM btc15m_observations
                WHERE market_result IS NOT NULL
                  AND COALESCE(obs_quality, 'full') IN ('full', 'short')
            """).fetchone()["n"]

            # Daily rate (last 7 days)
            week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            rate_row = c.execute("""
                SELECT COUNT(*) as n,
                       MIN(close_time_utc) as first_t,
                       MAX(close_time_utc) as last_t
                FROM btc15m_observations
                WHERE close_time_utc > ?
                  AND COALESCE(obs_quality, 'full') IN ('full', 'short')
            """, (week_ago,)).fetchone()

            recent_n = rate_row["n"] if rate_row else 0
            daily_rate = 0
            if recent_n > 0 and rate_row["first_t"] and rate_row["last_t"]:
                try:
                    first = datetime.fromisoformat(
                        rate_row["first_t"].replace("Z", "+00:00"))
                    last = datetime.fromisoformat(
                        rate_row["last_t"].replace("Z", "+00:00"))
                    days_span = max(1, (last - first).total_seconds() / 86400)
                    daily_rate = round(recent_n / days_span, 1)
                except Exception:
                    daily_rate = round(recent_n / 7, 1)

            # Real trade count
            trade_count = c.execute("""
                SELECT COUNT(*) as n FROM btc15m_trades
                WHERE outcome IN ('win', 'loss')
                  AND COALESCE(is_ignored, 0) = 0
            """).fetchone()["n"]

            # Shadow trade count
            shadow_count = c.execute("""
                SELECT COUNT(*) as n FROM btc15m_trades
                WHERE COALESCE(is_shadow, 0) = 1
                  AND outcome IN ('win', 'loss')
            """).fetchone()["n"]

            # Capture rate (last 24h)
            day_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            cap_row = c.execute("""
                SELECT COUNT(*) as n FROM btc15m_observations
                WHERE close_time_utc > ?
            """, (day_ago,)).fetchone()
            capture_24h = cap_row["n"] if cap_row else 0

            # Best strategy + gate status
            fee_buffer = float(get_config("btc_15m.min_breakeven_fee_buffer", 0.03) or 0.03)
            min_breakeven = KALSHI_FEE_RATE + fee_buffer

            best_row = c.execute("""
                SELECT strategy_key, ev_per_trade_c, weighted_ev_c,
                       oos_ev_c, oos_sample_size, breakeven_fee_rate,
                       fdr_significant
                FROM btc15m_strategy_results
                WHERE setup_key = 'global:all'
                  AND ev_per_trade_c > 0
                ORDER BY COALESCE(weighted_ev_c, ev_per_trade_c) DESC
                LIMIT 1
            """).fetchone()

        gates = {}
        best_strategy = None
        if best_row:
            best_strategy = best_row["strategy_key"]
            wev = best_row["weighted_ev_c"] or best_row["ev_per_trade_c"] or 0
            oos_ev = best_row["oos_ev_c"] or 0
            oos_n = best_row["oos_sample_size"] or 0
            bfe = best_row["breakeven_fee_rate"] or 0
            fdr = bool(best_row["fdr_significant"])

            gates["positive_ev"] = {
                "passed": wev > 0,
                "detail": f"Weighted EV: {wev:+.1f}¢" if wev else "No positive EV yet",
            }
            gates["oos_positive"] = {
                "passed": oos_ev > 0,
                "detail": f"OOS EV: {oos_ev:+.1f}¢" if oos_n > 0 else "No OOS data yet",
            }
            gates["oos_sufficient"] = {
                "passed": oos_n >= 30,
                "detail": f"{oos_n}/30 OOS samples",
            }
            gates["fee_resilient"] = {
                "passed": bfe >= min_breakeven,
                "detail": f"Breakeven fee: {bfe*100:.1f}% (need {min_breakeven*100:.1f}%)" if bfe else "Not computed yet",
            }
            gates["fdr_significant"] = {
                "passed": fdr,
                "detail": "Passed FDR correction" if fdr else "Not yet FDR-significant",
            }

        # Compute readiness score (0-100)
        # Components: data volume (40%), gate progress (40%), capture health (20%)
        data_score = min(40, round(total_obs / 200 * 40))
        gate_passed = sum(1 for g in gates.values() if g["passed"]) if gates else 0
        gate_total = len(gates) if gates else 5
        gate_score = round(gate_passed / gate_total * 40) if gate_total > 0 else 0
        cap_pct = capture_24h / 96 if capture_24h else 0
        cap_score = min(20, round(cap_pct * 20))
        readiness_score = data_score + gate_score + cap_score

        # Thresholds for each test (have vs need)
        thresholds = {
            "persistence": {"need": 100, "have": total_obs, "label": "observations"},
            "permutation": {"need": 50, "have": total_obs, "label": "observations"},
            "walkforward": {"need": 100, "have": total_obs, "label": "observations"},
            "realized_edge": {"need": 1, "have": trade_count, "label": "real trades"},
            "pnl_attribution": {"need": 5, "have": trade_count, "label": "real trades"},
            "shadow": {"need": 1, "have": shadow_count, "label": "shadow trades"},
        }

        # Selection test status
        selection_test_result = get_config("btc_15m._selection_test_result") or ""

        return jsonify({
            "readiness_score": readiness_score,
            "total_observations": total_obs,
            "daily_rate": daily_rate,
            "trade_count": trade_count,
            "shadow_count": shadow_count,
            "capture_24h": capture_24h,
            "expected_24h": 96,
            "gates": gates,
            "gate_status": {
                "selection_test": selection_test_result or None,
            },
            "best_strategy": best_strategy,
            "thresholds": thresholds,
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/time_to_actionable")
@requires_auth
def api_time_to_actionable():
    """Estimate days until the first strategy passes all validation gates."""
    try:
        from strategy import estimate_time_to_actionable
        return jsonify(estimate_time_to_actionable())
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/capture_rate")
@requires_auth
def api_capture_rate():
    """Observation capture rate over a time window. Optional ?hours=24"""
    try:
        from strategy import get_observation_capture_rate
        hours = int(request.args.get("hours", 24))
        return jsonify(get_observation_capture_rate(hours=hours))
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/data_convergence")
@requires_auth
def api_data_convergence():
    """Compare current metric snapshot to one from N hours ago.
    Returns per-category stability scores and an overall convergence %.
    ?hours=24 (default). Higher score = more stable = less is changing."""
    try:
        # get_metric_snapshot_near, get_latest_metric_snapshot already imported from market_db
        from datetime import datetime, timezone, timedelta
        import json as _json

        hours = int(request.args.get("hours", 24))
        hours = max(1, min(hours, 168))  # 1h to 7d

        now_ts, current = get_latest_metric_snapshot()
        if not current:
            return jsonify({"error": "No snapshots yet — data will appear after the next simulation batch (~30 min).",
                            "has_data": False})

        target_time = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        # Allow up to 50% drift — asking for 24h ago, accept anything 12-36h ago
        max_drift = max(hours * 0.5, 1.0)
        prev_ts, previous = get_metric_snapshot_near(target_time, max_drift_hours=max_drift)

        if not previous:
            # Calculate how long we've been collecting snapshots
            min_wait = hours
            return jsonify({"error": f"No snapshot found near {hours}h ago. "
                            f"Need at least {hours}h of snapshot history to measure change over this window. "
                            f"Try a shorter timeframe or check back later.",
                            "has_data": False})

        # Make sure the previous snapshot is meaningfully older than current
        # (at least 25% of the requested window or 30 min, whichever is larger)
        if now_ts and prev_ts:
            try:
                now_dt = datetime.fromisoformat(now_ts.replace("Z", "+00:00"))
                prev_dt = datetime.fromisoformat(prev_ts.replace("Z", "+00:00"))
                age_hours = (now_dt - prev_dt).total_seconds() / 3600
                min_age = max(hours * 0.25, 0.5)  # At least 25% of window or 30 min
                if age_hours < min_age:
                    return jsonify({
                        "error": f"Oldest snapshot is only {age_hours:.1f}h old — need at least "
                                 f"{min_age:.0f}h of history for a {hours}h window. "
                                 f"Try a shorter timeframe or check back later.",
                        "has_data": False,
                    })
            except Exception:
                pass

        def _ev_stability(cur, prev, scale=5.0):
            """Stability of an EV metric. 5¢ shift = 0% stable."""
            if cur is None or prev is None:
                return None
            delta = abs((cur or 0) - (prev or 0))
            return max(0.0, 1.0 - delta / scale)

        def _pct_stability(cur, prev, scale=0.10):
            """Stability of a rate/percentage. 10pp shift = 0% stable."""
            if cur is None or prev is None:
                return None
            delta = abs((cur or 0) - (prev or 0))
            return max(0.0, 1.0 - delta / scale)

        def _count_stability(cur, prev):
            """Stability of a count metric."""
            c, p = cur or 0, prev or 0
            mx = max(abs(c), abs(p), 1)
            delta = abs(c - p)
            return max(0.0, 1.0 - delta / mx)

        # ── Strategy Rankings (35%) ──
        strat_scores = []
        strat_details = []

        ev_s = _ev_stability(current.get("best_ev_c"), previous.get("best_ev_c"))
        if ev_s is not None:
            strat_scores.append(ev_s)
            delta_ev = (current.get("best_ev_c", 0) or 0) - (previous.get("best_ev_c", 0) or 0)
            strat_details.append(f"Best EV: {delta_ev:+.1f}¢")

        top5_s = _ev_stability(current.get("top5_avg_ev_c"), previous.get("top5_avg_ev_c"), 3.0)
        if top5_s is not None:
            strat_scores.append(top5_s)
            d = (current.get("top5_avg_ev_c", 0) or 0) - (previous.get("top5_avg_ev_c", 0) or 0)
            strat_details.append(f"Top-5 avg: {d:+.1f}¢")

        same_leader = current.get("best_strategy") == previous.get("best_strategy")
        strat_scores.append(1.0 if same_leader else 0.0)
        if not same_leader:
            strat_details.append(f"#1 changed")
        else:
            strat_details.append("#1 unchanged")

        strat_score = sum(strat_scores) / len(strat_scores) if strat_scores else 0

        # ── Edge Quality (30%) ──
        edge_scores = []
        edge_details = []

        pev_s = _count_stability(current.get("pos_ev_count"), previous.get("pos_ev_count"))
        edge_scores.append(pev_s)
        d_pev = (current.get("pos_ev_count", 0) or 0) - (previous.get("pos_ev_count", 0) or 0)
        edge_details.append(f"+EV strategies: {d_pev:+d}")

        fdr_s = _count_stability(current.get("fdr_sig_count"), previous.get("fdr_sig_count"))
        edge_scores.append(fdr_s)
        d_fdr = (current.get("fdr_sig_count", 0) or 0) - (previous.get("fdr_sig_count", 0) or 0)
        edge_details.append(f"FDR-sig: {d_fdr:+d}")

        oos_s = _ev_stability(current.get("best_oos_ev_c"), previous.get("best_oos_ev_c"), 4.0)
        if oos_s is not None:
            edge_scores.append(oos_s)

        edge_score = sum(edge_scores) / len(edge_scores) if edge_scores else 0

        # ── Side & Timing (20%) ──
        dim_scores = []
        dim_details = []

        cur_sides = current.get("side_evs", {})
        prev_sides = previous.get("side_evs", {})
        for side in ('yes', 'no', 'cheaper', 'model'):
            s = _ev_stability(cur_sides.get(side), prev_sides.get(side), 4.0)
            if s is not None:
                dim_scores.append(s)

        cur_timings = current.get("timing_evs", {})
        prev_timings = previous.get("timing_evs", {})
        for t in ('early', 'mid', 'late'):
            s = _ev_stability(cur_timings.get(t), prev_timings.get(t), 4.0)
            if s is not None:
                dim_scores.append(s)

        # Summarize biggest side shift
        biggest_side_shift = 0
        for side in ('yes', 'no', 'cheaper', 'model'):
            c_v = cur_sides.get(side) or 0
            p_v = prev_sides.get(side) or 0
            if abs(c_v - p_v) > abs(biggest_side_shift):
                biggest_side_shift = c_v - p_v
        if abs(biggest_side_shift) > 0.1:
            dim_details.append(f"Biggest side shift: {biggest_side_shift:+.1f}¢")
        else:
            dim_details.append("Side rankings stable")

        dim_score = sum(dim_scores) / len(dim_scores) if dim_scores else 0

        # ── Execution (15%) ──
        exec_scores = []
        exec_details = []

        wr_s = _pct_stability(current.get("shadow_wr"), previous.get("shadow_wr"), 0.08)
        if wr_s is not None:
            exec_scores.append(wr_s)
            c_wr = (current.get("shadow_wr") or 0) * 100
            p_wr = (previous.get("shadow_wr") or 0) * 100
            exec_details.append(f"Shadow WR: {p_wr:.0f}→{c_wr:.0f}%")

        slip_s = _ev_stability(current.get("shadow_avg_slip_c"), previous.get("shadow_avg_slip_c"), 2.0)
        if slip_s is not None:
            exec_scores.append(slip_s)

        exec_score = sum(exec_scores) / len(exec_scores) if exec_scores else 1.0

        # ── Overall ──
        weights = {"strategy": 0.35, "edge": 0.30, "dimensions": 0.20, "execution": 0.15}
        overall = (strat_score * weights["strategy"]
                   + edge_score * weights["edge"]
                   + dim_score * weights["dimensions"]
                   + exec_score * weights["execution"])

        # Observation growth for context
        obs_delta = (current.get("total_obs", 0) or 0) - (previous.get("total_obs", 0) or 0)
        obs_pct = round(obs_delta / max(previous.get("total_obs", 1) or 1, 1) * 100, 1)

        return jsonify({
            "has_data": True,
            "hours": hours,
            "overall_pct": round(overall * 100),
            "obs_current": current.get("total_obs", 0),
            "obs_previous": previous.get("total_obs", 0),
            "obs_delta": obs_delta,
            "obs_growth_pct": obs_pct,
            "categories": {
                "strategy": {
                    "score": round(strat_score * 100),
                    "label": "Strategy Rankings",
                    "details": strat_details,
                    "weight": "35%",
                },
                "edge": {
                    "score": round(edge_score * 100),
                    "label": "Edge Quality",
                    "details": edge_details,
                    "weight": "30%",
                },
                "dimensions": {
                    "score": round(dim_score * 100),
                    "label": "Side & Timing",
                    "details": dim_details,
                    "weight": "20%",
                },
                "execution": {
                    "score": round(exec_score * 100),
                    "label": "Execution Reality",
                    "details": exec_details,
                    "weight": "15%",
                },
            },
            "snapshot_current": current,
            "snapshot_previous": previous,
        })
    except Exception as e:
        return jsonify({"error": str(e), "has_data": False})


@app.route("/api/shadow_stats")
@requires_auth
def api_shadow_stats():
    """Comprehensive shadow trade statistics with time filtering.
    ?hours=0 (0=all time, or specific window like 24, 48, 168)"""
    try:
        from db import get_conn, rows_to_list
        from datetime import datetime, timezone, timedelta
        import math

        hours = int(request.args.get("hours", 0))
        hours = max(0, min(hours, 8760))  # 0=all, up to 1 year

        with get_conn() as c:
            time_filter = ""
            params = []
            if hours > 0:
                since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
                time_filter = " AND datetime(created_at) > datetime(?)"
                params = [since]

            rows = c.execute(f"""
                SELECT outcome, pnl, side, shares_filled,
                       entry_price_c, avg_fill_price_c,
                       shadow_decision_price_c, shadow_fill_latency_ms,
                       regime_label, spread_at_entry_c, auto_strategy_key,
                       yes_ask_at_entry, no_ask_at_entry,
                       hour_et, day_of_week, market_result,
                       created_at
                FROM btc15m_trades
                WHERE COALESCE(is_shadow, 0) = 1
                  AND outcome IN ('win', 'loss')
                  {time_filter}
                ORDER BY created_at ASC
            """, params).fetchall()
            trades = rows_to_list(rows)

            # Also count no-fills for fill rate
            no_fill_q = f"""
                SELECT COUNT(*) as n FROM btc15m_trades
                WHERE COALESCE(is_shadow, 0) = 1
                  AND outcome = 'no_fill'
                  {time_filter}
            """
            nf_row = c.execute(no_fill_q, params).fetchone()
            no_fills = nf_row["n"] if nf_row else 0

        if not trades:
            return jsonify({"has_data": False, "n": 0,
                            "message": "No completed shadow trades in this window."})

        n = len(trades)
        wins = sum(1 for t in trades if t["outcome"] == "win")
        losses = n - wins
        total_pnl = sum(t.get("pnl") or 0 for t in trades)
        pnls = [(t.get("pnl") or 0) for t in trades]

        # ── Streak analysis ──
        max_win_streak = max_loss_streak = cur_win = cur_loss = 0
        for t in trades:
            if t["outcome"] == "win":
                cur_win += 1; cur_loss = 0
                max_win_streak = max(max_win_streak, cur_win)
            else:
                cur_loss += 1; cur_win = 0
                max_loss_streak = max(max_loss_streak, cur_loss)

        # ── Cumulative PnL curve (sampled to max 50 points) ──
        cum_pnl = []
        running = 0
        for t in trades:
            running += (t.get("pnl") or 0)
            cum_pnl.append(round(running, 2))
        step = max(1, len(cum_pnl) // 50)
        pnl_curve = [cum_pnl[i] for i in range(0, len(cum_pnl), step)]
        if cum_pnl and pnl_curve[-1] != cum_pnl[-1]:
            pnl_curve.append(cum_pnl[-1])

        # ── Slippage & latency ──
        slippages = []
        latencies = []
        for t in trades:
            dp = t.get("shadow_decision_price_c")
            fp = t.get("avg_fill_price_c")
            if dp and fp and dp > 0 and fp > 0:
                slippages.append(fp - dp)
            lat = t.get("shadow_fill_latency_ms")
            if lat is not None:
                latencies.append(lat)

        # ── By side ──
        by_side = {}
        for t in trades:
            s = t.get("side", "unknown")
            if s not in by_side:
                by_side[s] = {"n": 0, "wins": 0, "pnl": 0}
            by_side[s]["n"] += 1
            by_side[s]["pnl"] += (t.get("pnl") or 0)
            if t["outcome"] == "win":
                by_side[s]["wins"] += 1
        for s, d in by_side.items():
            d["wr"] = round(d["wins"] / d["n"], 4) if d["n"] else 0
            d["avg_pnl"] = round(d["pnl"] / d["n"], 2) if d["n"] else 0
            d["pnl"] = round(d["pnl"], 2)

        # ── By regime (top 10) ──
        by_regime = {}
        for t in trades:
            r = t.get("regime_label") or "unknown"
            if r not in by_regime:
                by_regime[r] = {"n": 0, "wins": 0, "pnl": 0}
            by_regime[r]["n"] += 1
            by_regime[r]["pnl"] += (t.get("pnl") or 0)
            if t["outcome"] == "win":
                by_regime[r]["wins"] += 1
        for r, d in by_regime.items():
            d["wr"] = round(d["wins"] / d["n"], 4) if d["n"] else 0
            d["avg_pnl"] = round(d["pnl"] / d["n"], 2) if d["n"] else 0
            d["pnl"] = round(d["pnl"], 2)
        by_regime_sorted = sorted(by_regime.items(), key=lambda x: -x[1]["n"])[:12]

        # ── By spread bucket ──
        by_spread = {"tight (1-3¢)": {"n":0,"wins":0,"pnl":0,"slips":[]},
                     "normal (4-6¢)": {"n":0,"wins":0,"pnl":0,"slips":[]},
                     "wide (7+¢)": {"n":0,"wins":0,"pnl":0,"slips":[]}}
        for t in trades:
            sp = t.get("spread_at_entry_c")
            dp = t.get("shadow_decision_price_c")
            fp = t.get("avg_fill_price_c")
            if sp is None:
                continue
            bucket = "tight (1-3¢)" if sp <= 3 else "normal (4-6¢)" if sp <= 6 else "wide (7+¢)"
            by_spread[bucket]["n"] += 1
            by_spread[bucket]["pnl"] += (t.get("pnl") or 0)
            if t["outcome"] == "win":
                by_spread[bucket]["wins"] += 1
            if dp and fp and dp > 0 and fp > 0:
                by_spread[bucket]["slips"].append(fp - dp)
        for b, d in by_spread.items():
            d["wr"] = round(d["wins"] / d["n"], 4) if d["n"] else 0
            d["avg_slip"] = round(sum(d["slips"]) / len(d["slips"]), 2) if d["slips"] else None
            d["pnl"] = round(d["pnl"], 2)
            del d["slips"]

        # ── By hour (ET) ──
        by_hour = {}
        for t in trades:
            h = t.get("hour_et")
            if h is None:
                continue
            if h not in by_hour:
                by_hour[h] = {"n": 0, "wins": 0, "pnl": 0}
            by_hour[h]["n"] += 1
            by_hour[h]["pnl"] += (t.get("pnl") or 0)
            if t["outcome"] == "win":
                by_hour[h]["wins"] += 1
        for h, d in by_hour.items():
            d["wr"] = round(d["wins"] / d["n"], 4) if d["n"] else 0
            d["avg_pnl"] = round(d["pnl"] / d["n"], 2) if d["n"] else 0

        # ── By strategy key (top 10) ──
        by_strat = {}
        for t in trades:
            sk = t.get("auto_strategy_key") or "none"
            if sk not in by_strat:
                by_strat[sk] = {"n": 0, "wins": 0, "pnl": 0}
            by_strat[sk]["n"] += 1
            by_strat[sk]["pnl"] += (t.get("pnl") or 0)
            if t["outcome"] == "win":
                by_strat[sk]["wins"] += 1
        for sk, d in by_strat.items():
            d["wr"] = round(d["wins"] / d["n"], 4) if d["n"] else 0
            d["avg_pnl"] = round(d["pnl"] / d["n"], 2) if d["n"] else 0
            d["pnl"] = round(d["pnl"], 2)
        by_strat_sorted = sorted(by_strat.items(), key=lambda x: -x[1]["n"])[:10]

        # ── Entry price distribution ──
        entry_prices = [t.get("avg_fill_price_c") or 0 for t in trades if t.get("avg_fill_price_c")]
        avg_entry = round(sum(entry_prices) / len(entry_prices), 1) if entry_prices else 0
        # Bucket by 10¢ ranges
        entry_buckets = {}
        for ep in entry_prices:
            bk = f"{(ep // 10) * 10}-{(ep // 10) * 10 + 9}¢"
            entry_buckets[bk] = entry_buckets.get(bk, 0) + 1

        # ── Recent trades (last 10) ──
        recent = []
        for t in reversed(trades[-10:]):
            recent.append({
                "side": t.get("side"),
                "fill": t.get("avg_fill_price_c"),
                "ask": t.get("shadow_decision_price_c"),
                "slip": (t.get("avg_fill_price_c") or 0) - (t.get("shadow_decision_price_c") or 0)
                        if t.get("avg_fill_price_c") and t.get("shadow_decision_price_c") else None,
                "outcome": t.get("outcome"),
                "pnl": round(t.get("pnl") or 0, 2),
                "regime": t.get("regime_label"),
                "strategy": t.get("auto_strategy_key"),
                "time": t.get("created_at", "")[:16],
            })

        return jsonify({
            "has_data": True,
            "hours": hours,
            "overview": {
                "n": n,
                "wins": wins,
                "losses": losses,
                "wr": round(wins / n, 4),
                "total_pnl": round(total_pnl, 2),
                "avg_pnl": round(total_pnl / n, 2),
                "no_fills": no_fills,
                "fill_rate": round(n / (n + no_fills), 4) if (n + no_fills) > 0 else 0,
                "max_win_streak": max_win_streak,
                "max_loss_streak": max_loss_streak,
                "avg_entry_c": avg_entry,
            },
            "slippage": {
                "n": len(slippages),
                "avg_c": round(sum(slippages) / len(slippages), 2) if slippages else 0,
                "min_c": min(slippages) if slippages else 0,
                "max_c": max(slippages) if slippages else 0,
                "pct_better": round(sum(1 for s in slippages if s < 0) / len(slippages) * 100, 1) if slippages else 0,
                "pct_equal": round(sum(1 for s in slippages if s == 0) / len(slippages) * 100, 1) if slippages else 0,
                "pct_worse": round(sum(1 for s in slippages if s > 0) / len(slippages) * 100, 1) if slippages else 0,
            },
            "latency": {
                "n": len(latencies),
                "avg_ms": round(sum(latencies) / len(latencies)) if latencies else 0,
                "min_ms": min(latencies) if latencies else 0,
                "max_ms": max(latencies) if latencies else 0,
                "p50_ms": sorted(latencies)[len(latencies)//2] if latencies else 0,
                "p95_ms": sorted(latencies)[int(len(latencies)*0.95)] if len(latencies) >= 20 else (max(latencies) if latencies else 0),
            },
            "pnl_curve": pnl_curve,
            "by_side": by_side,
            "by_regime": dict(by_regime_sorted),
            "by_spread": {k: v for k, v in by_spread.items() if v["n"] > 0},
            "by_hour": by_hour,
            "by_strategy": dict(by_strat_sorted),
            "entry_buckets": entry_buckets,
            "recent": recent,
        })
    except Exception as e:
        return jsonify({"error": str(e), "has_data": False})


@app.route("/api/btc_surface")
@requires_auth
def api_btc_surface():
    """BTC probability surface — distance from open × time → win rate.
    Optional ?vol=calm|normal|volatile|all (default: all)."""
    try:
        # get_btc_surface_data already imported from market_db
        vol = request.args.get("vol", "all")
        if vol not in ("calm", "normal", "volatile", "all"):
            vol = "all"
        return jsonify({"cells": get_btc_surface_data(vol_bucket=vol),
                        "vol_bucket": vol})
    except Exception as e:
        return jsonify({"error": str(e), "cells": []})


@app.route("/api/regime_stability")
@requires_auth
def api_regime_stability():
    """Regime label stability metrics."""
    hours = int(request.args.get("hours", 24))
    try:
        # get_regime_stability_summary already imported from market_db
        return jsonify(get_regime_stability_summary(hours))
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/feature_importance")
@requires_auth
def api_feature_importance():
    """Feature importance rankings."""
    try:
        # get_feature_importance already imported from market_db
        return jsonify({"features": get_feature_importance()})
    except Exception as e:
        return jsonify({"error": str(e), "features": []})


@app.route("/api/regime_effectiveness")
@requires_auth
def api_regime_effectiveness():
    """Compare fine-grained vs coarse regime effectiveness."""
    try:
        from strategy import compute_regime_effectiveness
        return jsonify(compute_regime_effectiveness())
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/fv_model_status")
@requires_auth
def api_fv_model_status():
    """Get BTC Fair Value Model status and current edge (if live market)."""
    try:
        from strategy import BtcFairValueModel
        model = BtcFairValueModel()
        status = model.get_status()
        # Include current live edge if available
        state = get_bot_state()
        lm = state.get("live_market") or {}
        fv = lm.get("fv_model")
        if fv:
            status["live_edge"] = fv
        return jsonify(status)
    except Exception as e:
        return jsonify({"error": str(e)})




@app.route("/api/regimes")
@requires_auth
def api_regimes():
    from db import get_conn, rows_to_list
    from regime import compute_coarse_label
    regimes = get_all_regime_stats()

    # Build lookup by label
    regime_map = {r["regime_label"]: r for r in regimes}

    try:
        with get_conn() as c:
            # Get all observed regimes from market_observations
            # Also get vol/trend to compute coarse labels
            obs_rows = c.execute("""
                SELECT regime_label,
                       COUNT(*) as obs_count,
                       SUM(CASE WHEN market_result IS NOT NULL THEN 1 ELSE 0 END) as resolved_count,
                       CAST(ROUND(AVG(vol_regime)) AS INTEGER) as avg_vol,
                       CAST(ROUND(AVG(trend_regime)) AS INTEGER) as avg_trend,
                       CAST(ROUND(AVG(volume_regime)) AS INTEGER) as avg_volume
                FROM btc15m_observations
                WHERE regime_label IS NOT NULL
                GROUP BY regime_label
            """).fetchall()

            # Also count trade encounters per regime (including skips)
            # This uses the DECISION-TIME label, which may differ from
            # the observatory's first-sight label
            trade_encounter_rows = c.execute("""
                SELECT regime_label,
                       COUNT(*) as encounter_count,
                       CAST(ROUND(AVG(vol_regime)) AS INTEGER) as avg_vol,
                       CAST(ROUND(AVG(trend_regime)) AS INTEGER) as avg_trend,
                       CAST(ROUND(AVG(volume_regime)) AS INTEGER) as avg_volume
                FROM btc15m_trades
                WHERE regime_label IS NOT NULL
                GROUP BY regime_label
            """).fetchall()
            trade_encounters = {}
            for tr in trade_encounter_rows:
                trade_encounters[tr["regime_label"]] = {
                    "count": tr["encounter_count"],
                    "avg_vol": tr["avg_vol"],
                    "avg_trend": tr["avg_trend"],
                    "avg_volume": tr["avg_volume"],
                }

            # Build coarse label map from observations
            coarse_map = {}  # fine_label -> coarse_label
            for obs in obs_rows:
                label = obs["regime_label"]
                if not label:
                    continue
                if label in regime_map:
                    regime_map[label]["obs_count"] = obs["obs_count"]
                    regime_map[label]["resolved_count"] = obs["resolved_count"]
                else:
                    # Observation-only regime — create a stub entry
                    regime_map[label] = {
                        "regime_label": label,
                        "total_trades": 0, "wins": 0, "losses": 0,
                        "total_pnl": 0, "avg_pnl": 0, "win_rate": 0,
                        "ci_lower": 0, "ci_upper": 1,
                        "risk_level": "unknown",
                        "obs_count": obs["obs_count"],
                        "resolved_count": obs["resolved_count"],
                    }
                # Compute coarse label from observation averages
                try:
                    vol = obs["avg_vol"] or 3
                    trend = obs["avg_trend"] or 0
                    volume = obs["avg_volume"]
                    coarse_map[label] = compute_coarse_label(vol, trend, volume)
                except Exception:
                    coarse_map[label] = "unknown"

            # Ensure trade-only regimes (not in observations) get entries
            for label, te in trade_encounters.items():
                if label not in regime_map and not label.startswith("coarse:"):
                    regime_map[label] = {
                        "regime_label": label,
                        "total_trades": 0, "wins": 0, "losses": 0,
                        "total_pnl": 0, "avg_pnl": 0, "win_rate": 0,
                        "ci_lower": 0, "ci_upper": 1,
                        "risk_level": "unknown",
                        "obs_count": 0, "resolved_count": 0,
                    }
                if label not in coarse_map and not label.startswith("coarse:"):
                    try:
                        coarse_map[label] = compute_coarse_label(
                            te["avg_vol"] or 3, te["avg_trend"] or 0, te["avg_volume"])
                    except Exception:
                        coarse_map[label] = "unknown"

            # Add encounter counts to all regimes
            for label, r in regime_map.items():
                te = trade_encounters.get(label)
                r["encounter_count"] = te["count"] if te else 0

            # For regimes still missing coarse, try regime_snapshots
            for label in list(regime_map.keys()):
                if label not in coarse_map and not label.startswith("coarse:"):
                    snap = c.execute("""
                        SELECT vol_regime, trend_regime, volume_regime
                        FROM regime_snapshots
                        WHERE composite_label = ?
                        ORDER BY captured_at DESC LIMIT 1
                    """, (label,)).fetchone()
                    if snap:
                        try:
                            coarse_map[label] = compute_coarse_label(
                                snap["vol_regime"] or 3,
                                snap["trend_regime"] or 0,
                                snap["volume_regime"]
                            )
                        except Exception:
                            coarse_map[label] = "unknown"
                    else:
                        coarse_map[label] = "unknown"

            # Enrich all with best strategy EV and coarse label
            for label, r in regime_map.items():
                if "obs_count" not in r:
                    r["obs_count"] = 0
                if "resolved_count" not in r:
                    r["resolved_count"] = 0

                # Skip coarse: prefixed entries (old coarse stats)
                if label.startswith("coarse:"):
                    continue

                r["coarse_label"] = coarse_map.get(label, "unknown")

                ev_row = c.execute("""
                    SELECT ev_per_trade_c, sample_size, strategy_key, win_rate
                    FROM btc15m_strategy_results
                    WHERE setup_key = ? AND sample_size >= 5
                    ORDER BY ev_per_trade_c DESC
                    LIMIT 1
                """, (f"regime:{label}",)).fetchone()
                if ev_row:
                    r["best_ev_c"] = ev_row["ev_per_trade_c"]
                    r["best_ev_n"] = ev_row["sample_size"]
                    r["best_ev_wr"] = ev_row["win_rate"]
                else:
                    r["best_ev_c"] = None
                    r["best_ev_n"] = 0
                    r["best_ev_wr"] = None
    except Exception:
        pass

    # Filter out coarse: prefixed entries (legacy) and sort
    result = [r for r in regime_map.values() if not r.get("regime_label", "").startswith("coarse:")]
    result = sorted(result,
                    key=lambda r: max(r.get("obs_count", 0), r.get("encounter_count", 0)),
                    reverse=True)
    return jsonify(result)


@app.route("/api/regimes/csv")
@requires_auth
def api_regimes_csv():
    """Export regime hierarchy data as CSV."""
    import io, csv
    from db import get_conn
    from regime import compute_coarse_label

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "base_regime", "variant", "modifier", "encounters", "observations",
        "resolved_obs", "real_trades", "wins", "losses", "win_rate",
        "total_pnl", "risk_level", "best_ev_c", "best_ev_n",
        "effective_override"
    ])

    # Reuse the /api/regimes logic to get enriched data
    try:
        data = api_regimes().get_json()
    except Exception:
        data = []

    cfg = get_all_config()
    overrides = cfg.get("regime_overrides", {})
    if isinstance(overrides, str):
        overrides = json.loads(overrides)

    def _base(label):
        b = label
        for pfx in ("thin_", "squeeze_"):
            if b.startswith(pfx): b = b[len(pfx):]
        for sfx in ("_accel", "_decel"):
            if b.endswith(sfx): b = b[:-len(sfx)]
        return b

    for r in data:
        label = r.get("regime_label", "")
        if label.startswith("coarse:"):
            continue
        base = _base(label)
        modifier = label.replace(base, "").strip("_") if label != base else ""
        # Compute effective override mirroring bot.py logic
        override = overrides.get(label, "default")
        if override == "default" and base != label:
            override = overrides.get(base, "default")
        wr = r.get("win_rate", 0)
        writer.writerow([
            base, label, modifier,
            r.get("encounter_count", 0), r.get("obs_count", 0),
            r.get("resolved_count", 0), r.get("total_trades", 0),
            r.get("wins", 0), r.get("losses", 0),
            f"{wr*100:.1f}%" if wr else "0%",
            f"{r.get('total_pnl', 0):.2f}",
            r.get("risk_level", "unknown"),
            f"{r['best_ev_c']:+.1f}" if r.get("best_ev_c") is not None else "",
            r.get("best_ev_n", 0),
            override,
        ])

    resp = make_response(output.getvalue())
    resp.headers["Content-Type"] = "text/csv"
    resp.headers["Content-Disposition"] = f"attachment; filename=regimes_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    return resp


@app.route("/api/live_prices")
@requires_auth
def api_live_prices():
    ticker = request.args.get("ticker")
    prices = get_live_prices(ticker=ticker)
    return jsonify(prices)


@app.route("/api/regime/<path:label>/detail")
@requires_auth
def api_regime_detail(label):
    from db import get_conn, row_to_dict, rows_to_list
    with get_conn() as c:
        # Base stats
        stats = get_regime_risk(label)

        # Per-entry-price breakdown
        rounds = c.execute("""
            SELECT
                CASE
                    WHEN avg_fill_price_c <= 30 THEN '≤30c'
                    WHEN avg_fill_price_c <= 35 THEN '31-35c'
                    WHEN avg_fill_price_c <= 40 THEN '36-40c'
                    ELSE '41c+'
                END as price_bucket,
                COUNT(*) as n,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                SUM(COALESCE(pnl,0)) as pnl
            FROM btc15m_trades WHERE regime_label = ? AND outcome IN ('win','loss')
              AND COALESCE(is_ignored,0) = 0 AND avg_fill_price_c > 0
            GROUP BY price_bucket ORDER BY price_bucket
        """, (label,)).fetchall()

        # Side breakdown
        sides = c.execute("""
            SELECT side, COUNT(*) as n,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                SUM(COALESCE(pnl,0)) as pnl
            FROM btc15m_trades WHERE regime_label = ? AND outcome IN ('win','loss')
              AND COALESCE(is_ignored,0) = 0
            GROUP BY side
        """, (label,)).fetchall()

        # Recent trades
        recent = c.execute("""
            SELECT id, outcome, side, pnl, avg_fill_price_c,
                   avg_fill_price_c as entry_price_c, sell_price_c,
                   price_high_water_c, created_at
            FROM btc15m_trades WHERE regime_label = ? AND outcome IN ('win','loss')
              AND COALESCE(is_ignored,0) = 0
            ORDER BY id DESC LIMIT 10
        """, (label,)).fetchall()

        # Avg entry price, avg HWM
        avgs = c.execute("""
            SELECT AVG(avg_fill_price_c) as avg_entry,
                   AVG(price_high_water_c) as avg_hwm,
                   AVG(sell_price_c) as avg_sell,
                   MAX(pnl) as best_pnl, MIN(pnl) as worst_pnl
            FROM btc15m_trades WHERE regime_label = ? AND outcome IN ('win','loss')
              AND COALESCE(is_ignored,0) = 0
        """, (label,)).fetchone()

        recent_list = rows_to_list(recent)
        for r in recent_list:
            r["created_ct"] = to_central(r.get("created_at", ""))

        # Hourly breakdown
        by_hour = c.execute("""
            SELECT hour_et, COUNT(*) as n,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                SUM(COALESCE(pnl,0)) as pnl
            FROM btc15m_trades WHERE regime_label = ? AND outcome IN ('win','loss')
              AND COALESCE(is_ignored,0) = 0 AND hour_et IS NOT NULL
            GROUP BY hour_et ORDER BY hour_et
        """, (label,)).fetchall()

        # Day of week breakdown
        by_day = c.execute("""
            SELECT day_of_week, COUNT(*) as n,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                SUM(COALESCE(pnl,0)) as pnl
            FROM btc15m_trades WHERE regime_label = ? AND outcome IN ('win','loss')
              AND COALESCE(is_ignored,0) = 0 AND day_of_week IS NOT NULL
            GROUP BY day_of_week ORDER BY day_of_week
        """, (label,)).fetchall()

        # Volatility level breakdown
        by_vol = c.execute("""
            SELECT vol_regime, COUNT(*) as n,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                SUM(COALESCE(pnl,0)) as pnl
            FROM btc15m_trades WHERE regime_label = ? AND outcome IN ('win','loss')
              AND COALESCE(is_ignored,0) = 0 AND vol_regime IS NOT NULL
            GROUP BY vol_regime ORDER BY vol_regime
        """, (label,)).fetchall()

        # Stability breakdown (buckets: 0-3, 4-6, 7-10, 11-15, 16+)
        by_stab = c.execute("""
            SELECT
                CASE
                    WHEN price_stability_c <= 3 THEN '0-3'
                    WHEN price_stability_c <= 6 THEN '4-6'
                    WHEN price_stability_c <= 10 THEN '7-10'
                    WHEN price_stability_c <= 15 THEN '11-15'
                    ELSE '16+'
                END as bucket,
                COUNT(*) as n,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                SUM(COALESCE(pnl,0)) as pnl
            FROM btc15m_trades WHERE regime_label = ? AND outcome IN ('win','loss')
              AND COALESCE(is_ignored,0) = 0 AND price_stability_c IS NOT NULL
            GROUP BY bucket ORDER BY MIN(price_stability_c)
        """, (label,)).fetchall()

        # Spread breakdown (buckets: 1-3 tight, 4-6 normal, 7-10 wide, 11+ very wide)
        by_spread = c.execute("""
            SELECT
                CASE
                    WHEN spread_at_entry_c <= 3 THEN 'Tight (1-3c)'
                    WHEN spread_at_entry_c <= 6 THEN 'Normal (4-6c)'
                    WHEN spread_at_entry_c <= 10 THEN 'Wide (7-10c)'
                    ELSE 'Very Wide (11c+)'
                END as bucket,
                COUNT(*) as n,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                SUM(COALESCE(pnl,0)) as pnl,
                AVG(spread_at_entry_c) as avg_spread
            FROM btc15m_trades WHERE regime_label = ? AND outcome IN ('win','loss')
              AND COALESCE(is_ignored,0) = 0 AND spread_at_entry_c IS NOT NULL
            GROUP BY bucket ORDER BY MIN(spread_at_entry_c)
        """, (label,)).fetchall()

        # Current regime filters
        from db import get_config
        regime_filters = get_config("btc_15m.regime_filters", {})
        if isinstance(regime_filters, str):
            import json as _json
            regime_filters = _json.loads(regime_filters)
        current_filter = regime_filters.get(label, {})

        # Top strategies for this regime from Strategy Observatory
        strategies = []
        if not label.startswith("coarse:"):
            try:
                strat_rows = c.execute("""
                    SELECT strategy_key, side_rule, exit_rule, entry_time_rule,
                           entry_price_max, sample_size, wins, losses, win_rate,
                           total_pnl_c, ev_per_trade_c, profit_factor,
                           ci_lower, ci_upper, max_consecutive_losses
                    FROM btc15m_strategy_results
                    WHERE setup_key = ? AND sample_size >= 5
                    ORDER BY ev_per_trade_c DESC
                    LIMIT 5
                """, (f"regime:{label}",)).fetchall()
                strategies = rows_to_list(strat_rows)
            except Exception:
                pass

        # Observation counts from market_observations
        obs_info = c.execute("""
            SELECT COUNT(*) as obs_count,
                   SUM(CASE WHEN market_result IS NOT NULL THEN 1 ELSE 0 END) as resolved_count
            FROM btc15m_observations
            WHERE regime_label = ?
        """, (label,)).fetchone()
        obs_count = obs_info["obs_count"] if obs_info else 0
        resolved_count = obs_info["resolved_count"] if obs_info else 0

        return jsonify({
            "stats": stats,
            "rounds": rows_to_list(rounds),
            "sides": rows_to_list(sides),
            "recent": recent_list,
            "averages": row_to_dict(avgs) if avgs else {},
            "by_hour": rows_to_list(by_hour),
            "by_day": rows_to_list(by_day),
            "by_vol": rows_to_list(by_vol),
            "by_stability": rows_to_list(by_stab),
            "by_spread": rows_to_list(by_spread),
            "filters": current_filter,
            "strategies": strategies,
            "obs_count": obs_count,
            "resolved_count": resolved_count,
        })


@app.route("/api/regime_status")
@requires_auth
def api_regime_status():
    s = get_regime_worker_status()
    if s.get("latest_snapshot"):
        s["latest_snapshot"]["captured_ct"] = to_central(
            s["latest_snapshot"].get("captured_at", ""))
        label = s["latest_snapshot"].get("composite_label", "")
        if label:
            risk_info = get_regime_risk(label)
            s["latest_snapshot"]["risk_level"] = risk_info.get("risk_level", "unknown")
    if s.get("latest_candle_ts"):
        s["latest_candle_ct"] = to_central(s["latest_candle_ts"])
    return jsonify(s)


@app.route("/api/confidence_status")
@requires_auth
def api_confidence_status():
    """Stub — confidence model removed. Returns empty status."""
    return jsonify({"total_factors": 0, "total_calibration_outcomes": 0,
                    "calibration": [], "edge_calibration": []})


@app.route("/api/chart/bankroll")
@requires_auth
def api_chart_bankroll():
    hours = request.args.get("hours", type=int)
    data = get_bankroll_chart_data(hours)
    return jsonify(data)


@app.route("/api/chart/pnl")
@requires_auth
def api_chart_pnl():
    hours = request.args.get("hours", type=int)
    data = get_pnl_chart_data(hours)
    return jsonify(data)


@app.route("/api/btc_chart")
@requires_auth
def api_btc_chart():
    """Return recent BTC candles for chart. Default 60 min."""
    minutes = request.args.get("minutes", 60, type=int)
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    candles = get_candles(since=since, limit=minutes + 5)
    # Thin to just what the chart needs
    return jsonify([{"ts": c["ts"], "close": c["close"], "high": c["high"],
                     "low": c["low"], "volume": c["volume"]} for c in candles])


@app.route("/api/llm_summary")
@requires_auth
def api_llm_summary():
    """Generate a text summary suitable for pasting to an LLM."""
    detailed = request.args.get("detailed", "0") == "1"
    stats = get_lifetime_stats()
    cfg = get_all_config()
    state = get_bot_state()
    regimes = get_all_regime_stats()
    trades = get_recent_trades(200 if detailed else 30)

    lines = []
    lines.append("# Kalshi BTC 15-min Trading Bot — Status Report")
    lines.append(f"Generated: {to_central(now_utc())}")
    lines.append("")

    # Config
    lines.append("## Configuration")
    bet = f"${cfg.get('bet_size', 50)}" if cfg.get('bet_mode') == 'flat' else f"{cfg.get('bet_size', 5)}%"
    lines.append(f"- Bet: {bet} ({cfg.get('bet_mode', 'flat')} mode)")
    sell_c = int(cfg.get('sell_target_c', 0) or 0)
    sell_desc = f"sell@{sell_c}c" if sell_c else "hold to expiry"
    lines.append(f"- Sell target: {sell_desc}")
    lines.append(f"- Entry max: {cfg.get('entry_price_max_c', 45)}c")
    lines.append(f"- Bankroll min/max: ${cfg.get('bankroll_min', 0)} / ${cfg.get('bankroll_max', 0)}")
    regime_filters = cfg.get('regime_filters', {})
    if regime_filters:
        if isinstance(regime_filters, str):
            import json as _j
            regime_filters = _j.loads(regime_filters)
        filtered = [k for k, v in regime_filters.items() if v]
        if filtered:
            lines.append(f"- Regime filters active: {len(filtered)} regimes have custom filters")
    lines.append("")

    # State
    lines.append("## Current State")
    lines.append(f"- Balance: ${(state.get('bankroll_cents', 0) or 0) / 100:.2f}")
    lines.append(f"- Loss streak: {state.get('loss_streak', 0)}")
    lines.append("")

    # Lifetime stats
    lines.append("## Lifetime Stats")
    w = stats.get('wins') or 0
    l = stats.get('losses') or 0
    lines.append(f"- Record: {w}W-{l}L ({stats.get('win_rate_pct', 0)}% win rate)")
    lines.append(f"- Total P&L: {fpnl(stats.get('total_pnl', 0))}")
    lines.append(f"- Total wagered: ${stats.get('total_wagered', 0):.2f}")
    lines.append(f"- Total fees: ${stats.get('total_fees', 0):.2f}")
    lines.append(f"- ROI: {stats.get('roi_pct', 0)}%")
    lines.append(f"- Profit factor: {stats.get('profit_factor', 0)}")
    lines.append(f"- Best win streak: {stats.get('best_win_streak', 0)}")
    lines.append(f"- Worst loss streak: {stats.get('worst_loss_streak', 0)}")
    lines.append(f"- Max drawdown: ${stats.get('max_drawdown', 0):.2f}")
    lines.append(f"- Peak P&L: {fpnl(stats.get('peak_pnl', 0))}")
    
    lines.append("")

    # Round breakdown


    # Regime stats
    if regimes:
        lines.append("## Regime Stats")
        for r in sorted(regimes, key=lambda x: -(x.get('total_trades', 0) or 0)):
            n = r.get('total_trades', 0) or 0
            if n == 0:
                continue
            wr = round((r.get('win_rate', 0) or 0) * 100)
            lines.append(f"- {r.get('regime_label', '?')} [{r.get('risk_level', '?')}]: "
                        f"{wr}% win (n={n}), P&L {fpnl(r.get('total_pnl', 0))}")
        lines.append("")

    # Daily P&L
    dp = stats.get('daily_pnl', [])
    if dp:
        lines.append("## Daily P&L (last 14 days)")
        for d in dp:
            lines.append(f"- {d['day']}: {d.get('wins',0)}W/{d.get('losses',0)}L, {fpnl(d.get('pnl',0))}")
        lines.append("")

    # Entry delay breakdown
    db = stats.get('delay_breakdown', [])
    if db:
        lines.append("## Entry Delay Breakdown")
        for d in db:
            dt = (d.get('wins', 0) or 0) + (d.get('losses', 0) or 0)
            dwr = round((d.get('wins', 0) or 0) / dt * 100) if dt > 0 else 0
            lines.append(f"- {d['delay_min']}min delay: {d.get('wins',0)}W/{d.get('losses',0)}L ({dwr}%), net {fpnl(d.get('net_pnl',0))}")
        lines.append("")

    # Price stability breakdown
    sb = stats.get('stability_breakdown', [])
    if sb:
        lines.append("## Price Stability Breakdown (price range during polling)")
        for s_row in sb:
            st2 = (s_row.get('wins', 0) or 0) + (s_row.get('losses', 0) or 0)
            swr = round((s_row.get('wins', 0) or 0) / st2 * 100) if st2 > 0 else 0
            lines.append(f"- {s_row.get('stability_bucket','?')}: {s_row.get('wins',0)}W/{s_row.get('losses',0)}L ({swr}%), net {fpnl(s_row.get('net_pnl',0))}")
        lines.append("")

    # Volatility level breakdown
    vb = stats.get('vol_breakdown', [])
    if vb:
        lines.append("## Volatility Level Breakdown (BTC vol 1-5)")
        for v_row in vb:
            vt = (v_row.get('wins', 0) or 0) + (v_row.get('losses', 0) or 0)
            vwr = round((v_row.get('wins', 0) or 0) / vt * 100) if vt > 0 else 0
            lines.append(f"- Vol {v_row.get('vol_level',0)}/5: {v_row.get('wins',0)}W/{v_row.get('losses',0)}L ({vwr}%), net {fpnl(v_row.get('net_pnl',0))}")
        lines.append("")

    # Trade log (detailed only)
    if detailed and trades:
        lines.append("## Recent Trade Log")
        for t in trades:
            side = (t.get('side') or '').upper()
            entry = t.get('avg_fill_price_c') or t.get('entry_price_c') or 0
            sell = t.get('sell_price_c') or 0
            pnl = t.get('pnl') or 0
            outcome = t.get('outcome', '?')
            regime = t.get('regime_label', '?')
            risk = t.get('regime_risk_level', '?')
            hwm = t.get('price_high_water_c') or 0
            shares = t.get('shares_filled') or 0
            cost = t.get('actual_cost') or 0
            flags = []
            if t.get('is_data_collection'):
                flags.append('DATA')
            if t.get('is_ignored'):
                flags.append('IGNORED')
            flag_str = f" [{','.join(flags)}]" if flags else ""
            ts = to_central(t.get('created_at', ''))

            stab = t.get('price_stability_c')
            delay = t.get('entry_delay_minutes', 0)
            stab_str = f" stab={stab}c" if stab is not None else ""
            delay_str = f" delay={delay}m" if delay else ""

            lines.append(f"- {ts} | {outcome.upper()} {fpnl(pnl)} | "
                        f"{side}@{entry}c→{sell}c | "
                        f"{shares}sh ${cost:.2f} | HWM={hwm}c | "
                        f"{regime} [{risk}]{stab_str}{delay_str}{flag_str}")
        lines.append("")

    return jsonify({"text": "\n".join(lines)})


# ── Push Notifications ──────────────────────────────────────

@app.route("/manifest.json")
def manifest_json():
    return jsonify({
        "name": "Kalshi BTC Bot",
        "short_name": "BTC Bot",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0d1117",
        "theme_color": "#0d1117",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ]
    })


@app.route("/sw.js")
def service_worker():
    sw_code = """
self.addEventListener('push', event => {
  let data = {title: 'BTC Bot', body: 'Notification', tag: 'default', url: '/'};
  try { data = event.data.json(); } catch(e) {}

  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      tag: data.tag,
      icon: '/icon-192.png',
      badge: '/icon-192.png',
      data: {url: data.url},
      renotify: !data.silent,
      silent: !!data.silent,
    })
  );
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = event.notification.data?.url || '/';
  event.waitUntil(
    clients.matchAll({type: 'window'}).then(list => {
      for (const c of list) {
        if (c.url.includes(url) && 'focus' in c) return c.focus();
      }
      return clients.openWindow(url);
    })
  );
});
"""
    return app.response_class(sw_code, mimetype='application/javascript',
                               headers={'Service-Worker-Allowed': '/'})


@app.route("/icon-192.png")
@app.route("/icon-512.png")
def app_icon():
    """Serve custom icon if exists, else generate placeholder."""
    from pathlib import Path
    import struct, zlib

    # Try custom icon file
    icon_path = Path(__file__).parent / "icon.png"
    if icon_path.exists():
        return app.response_class(icon_path.read_bytes(), mimetype='image/png')

    size = 192 if '192' in request.path else 512
    def create_png(w, h, r, g, b):
        def chunk(ctype, data):
            c = ctype + data
            return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
        sig = b'\x89PNG\r\n\x1a\n'
        ihdr = chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
        raw = b''
        for y in range(h):
            raw += b'\x00' + bytes([r, g, b]) * w
        idat = chunk(b'IDAT', zlib.compress(raw))
        iend = chunk(b'IEND', b'')
        return sig + ihdr + idat + iend

    png = create_png(size, size, 63, 185, 80)  # Green placeholder
    return app.response_class(png, mimetype='image/png')


@app.route("/api/push/vapid-key")
@requires_auth
def api_vapid_key():
    try:
        from push import get_public_key
        key = get_public_key()
        return jsonify({"key": key})
    except Exception as e:
        return jsonify({"key": None, "error": str(e)})


@app.route("/api/push/subscribe", methods=["POST"])
@requires_auth
def api_push_subscribe():
    data = request.get_json() or {}
    sub = data.get("subscription")
    if not sub or "endpoint" not in sub:
        return jsonify({"error": "Invalid subscription"}), 400
    save_push_subscription(sub["endpoint"], json.dumps(sub))
    return jsonify({"ok": True})


@app.route("/api/push/unsubscribe", methods=["POST"])
@requires_auth
def api_push_unsubscribe():
    data = request.get_json() or {}
    endpoint = data.get("endpoint", "")
    if endpoint:
        remove_push_subscription_by_endpoint(endpoint)
    return jsonify({"ok": True})


@app.route("/api/push/log")
@requires_auth
def api_push_log():
    limit = request.args.get("limit", 200, type=int)
    tag = request.args.get("tag", type=str)
    logs = get_push_log(limit=limit, tag=tag)
    for l in logs:
        l["sent_ct"] = to_central(l.get("sent_at", ""))
    return jsonify(logs)


@app.route("/api/logs")
@requires_auth
def api_logs():
    before_id = request.args.get("before", type=int)
    limit = request.args.get("limit", 100, type=int)
    level = request.args.get("level", type=str)
    logs = get_logs(before_id=before_id, limit=limit, level=level)
    for l in logs:
        l["ts_ct"] = to_central(l.get("ts", ""))
    return jsonify(logs)


@app.route("/api/logs/new")
@requires_auth
def api_logs_new():
    after_id = request.args.get("after", 0, type=int)
    logs = get_logs_after(after_id)
    for l in logs:
        l["ts_ct"] = to_central(l.get("ts", ""))
    return jsonify(logs)


@app.route("/api/server/exec", methods=["POST"])
@requires_auth
def api_server_exec():
    """Execute a server command. Limited to safe operations."""
    import subprocess as _sp
    data = request.get_json() or {}
    cmd = data.get("command", "").strip()
    if not cmd:
        return jsonify({"error": "No command"}), 400

    # Log it
    insert_log("INFO", f"[ServerCmd] Running: {cmd}", "deploy")

    # Allow: pip install, supervisorctl, python version checks
    allowed_prefixes = ["pip install ", "pip3 install ", "pip list", "pip show ",
                        "supervisorctl ", "python3 --version", "python3 -c ",
                        "pip --version", "pip3 --version", "which pip", "which python",
                        "df -h", "free -m", "uptime", "dmesg",
                        "cat /etc/supervisor"]
    if not any(cmd.startswith(p) for p in allowed_prefixes):
        insert_log("WARN", f"[ServerCmd] Blocked: {cmd}", "deploy")
        return jsonify({"error": f"Command not allowed. Allowed: pip install, supervisorctl, python3 --version, pip list, pip show, df -h, free -m, uptime, dmesg, cat /etc/supervisor*"}), 403

    # Reject shell metacharacters to prevent command injection
    import re as _re
    _dangerous = _re.compile(r'[;&|`$\\\n]|&&|\|\|')
    if _dangerous.search(cmd):
        insert_log("WARN", f"[ServerCmd] Rejected (shell metacharacters): {cmd}", "deploy")
        return jsonify({"error": "Command contains unsafe characters"}), 403

    # Auto-add --break-system-packages for pip install
    # Route through sys.executable to ensure same Python as dashboard
    import sys as _sys
    _use_shell = True  # Default: use shell for simple commands
    if cmd.startswith("pip install ") or cmd.startswith("pip3 install "):
        pkg_part = cmd.split("install ", 1)[1]
        # Validate package spec: only allow safe characters
        import re as _re2
        if not _re2.match(r'^[a-zA-Z0-9_.>=<!,\-\[\]\s]+$', pkg_part):
            insert_log("WARN", f"[ServerCmd] Rejected unsafe pip spec: {pkg_part}", "deploy")
            return jsonify({"error": "Package specification contains unsafe characters"}), 403
        parts = [_sys.executable, "-m", "pip", "install"] + pkg_part.split()
        if "--break-system-packages" not in parts:
            parts.append("--break-system-packages")
        cmd = parts  # list form — no shell
        _use_shell = False
        insert_log("INFO", f"[ServerCmd] Pip install: {' '.join(parts)}", "deploy")
    elif cmd.startswith("pip list") or cmd.startswith("pip3 list"):
        cmd = [_sys.executable, "-m", "pip", "list"]
        _use_shell = False
    elif cmd.startswith("pip show ") or cmd.startswith("pip3 show "):
        pkg = cmd.split("show ", 1)[1].strip()
        cmd = [_sys.executable, "-m", "pip", "show", pkg]
        _use_shell = False
    elif cmd.startswith("pip --version") or cmd.startswith("pip3 --version"):
        cmd = [_sys.executable, "-m", "pip", "--version"]
        _use_shell = False

    try:
        r = _sp.run(cmd, shell=_use_shell, capture_output=True, text=True, timeout=120)
        output = (r.stdout + r.stderr).strip()
        # Log result
        for line in output.splitlines()[-5:]:
            if line.strip():
                insert_log("INFO", f"[ServerCmd] {line.strip()}", "deploy")
        if r.returncode != 0:
            insert_log("WARN", f"[ServerCmd] Exit code {r.returncode}", "deploy")

        needs_restart = not _use_shell and isinstance(cmd, list) and "install" in cmd
        return jsonify({
            "output": output,
            "exit_code": r.returncode,
            "needs_restart": needs_restart,
        })
    except Exception as e:
        insert_log("ERROR", f"[ServerCmd] Exception: {e}", "deploy")
        return jsonify({"error": str(e)}), 500


@app.route("/api/server/restart", methods=["POST"])
@requires_auth
def api_server_restart():
    """Restart bot and dashboard services."""
    import subprocess as _sp
    insert_log("INFO", "[ServerCmd] Restarting services...", "deploy")
    results = {}
    for svc in ["plugin-btc-15m", "platform-dashboard"]:
        try:
            r = _sp.run(["supervisorctl", "restart", svc],
                       capture_output=True, text=True, timeout=10)
            results[svc] = r.stdout.strip() or r.stderr.strip()
            insert_log("INFO", f"[ServerCmd] {svc}: {results[svc]}", "deploy")
        except Exception as e:
            results[svc] = str(e)
            insert_log("ERROR", f"[ServerCmd] {svc}: {e}", "deploy")
    return jsonify(results)


@app.route("/api/server/control", methods=["POST"])
@requires_auth
def api_server_control():
    """Control individual supervisor services: start, stop, restart."""
    import subprocess as _sp
    data = request.get_json() or {}
    action = data.get("action", "restart")
    service = data.get("service", "all")

    if action not in ("start", "stop", "restart", "status"):
        return jsonify({"error": "Invalid action"}), 400

    services = ["plugin-btc-15m", "platform-dashboard"] if service == "all" else [service]
    valid = {"plugin-btc-15m", "platform-dashboard"}
    services = [s for s in services if s in valid]

    if not services:
        return jsonify({"error": "Invalid service"}), 400

    results = {}
    for svc in services:
        try:
            r = _sp.run(["supervisorctl", action, svc],
                       capture_output=True, text=True, timeout=10)
            results[svc] = r.stdout.strip() or r.stderr.strip()
            insert_log("INFO", f"[ServerCmd] {action} {svc}: {results[svc]}", "deploy")
        except Exception as e:
            results[svc] = str(e)
            insert_log("ERROR", f"[ServerCmd] {action} {svc}: {e}", "deploy")
    return jsonify(results)


@app.route("/api/server/status")
@requires_auth
def api_server_status():
    """Get supervisor status for all services."""
    import subprocess as _sp
    try:
        r = _sp.run(["supervisorctl", "status"],
                    capture_output=True, text=True, timeout=5)
        lines = r.stdout.strip().split("\n")
        services = {}
        for line in lines:
            parts = line.split()
            if len(parts) >= 2:
                name = parts[0]
                status = parts[1]
                services[name] = {"status": status, "detail": " ".join(parts[2:])}
        return jsonify(services)
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/system/stats")
@requires_auth
def api_system_stats():
    """Get server resource usage: disk, CPU, memory, network bandwidth."""
    import shutil
    stats = {}

    # ── Disk usage ──
    try:
        usage = shutil.disk_usage("/")
        stats["disk"] = {
            "total_gb": round(usage.total / (1024**3), 1),
            "used_gb": round(usage.used / (1024**3), 1),
            "free_gb": round(usage.free / (1024**3), 1),
            "pct": round(usage.used / usage.total * 100, 1),
        }
        # Database file size
        db_path = os.path.join(os.environ.get("BOT_DIR", "/opt/trading-platform"), "platform.db")
        if os.path.isfile(db_path):
            db_mb = os.path.getsize(db_path) / (1024**2)
            # Include WAL file if present
            wal_path = db_path + "-wal"
            if os.path.isfile(wal_path):
                db_mb += os.path.getsize(wal_path) / (1024**2)
            stats["disk"]["db_mb"] = round(db_mb, 1)
        # Log file size
        log_path = os.path.join(os.environ.get("BOT_DIR", "/opt/trading-platform"), "bot.log")
        if os.path.isfile(log_path):
            stats["disk"]["log_mb"] = round(os.path.getsize(log_path) / (1024**2), 1)
    except Exception:
        pass

    # ── CPU + Network (combined 0.5s sample window) ──
    try:
        import time as _t

        def _read_cpu():
            with open("/proc/stat") as f:
                line = f.readline()
            parts = line.split()
            vals = [int(x) for x in parts[1:9]]
            total = sum(vals)
            idle = vals[3] + vals[4]
            return total, idle

        def _read_net():
            rx_total = tx_total = 0
            with open("/proc/net/dev") as f:
                for line in f:
                    line = line.strip()
                    if ":" not in line:
                        continue
                    parts = line.split()
                    iface = parts[0].rstrip(":")
                    if iface == "lo":
                        continue
                    rx_total += int(parts[1])
                    tx_total += int(parts[9])
            return rx_total, tx_total

        # Sample both at once, single sleep
        cpu1_total, cpu1_idle = _read_cpu()
        net1_rx, net1_tx = _read_net()
        _t.sleep(0.5)
        cpu2_total, cpu2_idle = _read_cpu()
        net2_rx, net2_tx = _read_net()

        # CPU
        dt = cpu2_total - cpu1_total
        di = cpu2_idle - cpu1_idle
        cpu_pct = round((1 - di / dt) * 100, 1) if dt > 0 else 0
        stats["cpu"] = {"pct": cpu_pct}
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        stats["cpu"]["load_1m"] = float(parts[0])
        stats["cpu"]["load_5m"] = float(parts[1])
        stats["cpu"]["load_15m"] = float(parts[2])

        # Network (scale to per-second from 0.5s sample)
        rx_bps = (net2_rx - net1_rx) * 2
        tx_bps = (net2_tx - net1_tx) * 2
        stats["network"] = {
            "rx_kbps": round(rx_bps / 1024, 1),
            "tx_kbps": round(tx_bps / 1024, 1),
            "rx_total_gb": round(net2_rx / (1024**3), 2),
            "tx_total_gb": round(net2_tx / (1024**3), 2),
        }
    except Exception:
        pass

    # ── Memory ──
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                key = parts[0].rstrip(":")
                val_kb = int(parts[1])
                mem[key] = val_kb
        total = mem.get("MemTotal", 1)
        avail = mem.get("MemAvailable", mem.get("MemFree", 0))
        used = total - avail
        stats["memory"] = {
            "total_mb": round(total / 1024, 0),
            "used_mb": round(used / 1024, 0),
            "free_mb": round(avail / 1024, 0),
            "pct": round(used / total * 100, 1) if total > 0 else 0,
        }
    except Exception:
        pass

    # ── Uptime ──
    try:
        with open("/proc/uptime") as f:
            uptime_s = float(f.read().split()[0])
        days = int(uptime_s // 86400)
        hours = int((uptime_s % 86400) // 3600)
        mins = int((uptime_s % 3600) // 60)
        stats["uptime"] = {
            "seconds": round(uptime_s),
            "display": f"{days}d {hours}h {mins}m" if days > 0 else f"{hours}h {mins}m",
        }
    except Exception:
        pass

    return jsonify(stats)


@app.route("/api/export/ai-analysis", methods=["POST"])
@requires_auth
def api_export_ai_analysis():
    """Generate comprehensive data export for AI-assisted analysis.
    Returns a markdown file containing everything needed to evaluate
    and improve the bot's models, simulations, and strategy selection."""
    import time as _time
    # all functions already imported at module level from db / market_db

    try:
        ts = datetime.now(timezone.utc)
        parts = []

        parts.append(f"# AI Analysis Export — Kalshi BTC 15-Minute Bot")
        parts.append(f"Generated: {ts.strftime('%Y-%m-%d %H:%M:%S UTC')}\n")
        parts.append("This file contains all data needed to evaluate and improve the bot's "
                      "models, simulations, and trading logic. Share this with an AI assistant "
                      "who has access to the PROJECT_SUMMARY.md for architecture context.\n")

        # ── 1. Current Config ──
        cfg = get_all_config()
        _secrets = {"anthropic_api_key", "push_vapid_private", "push_vapid_public",
                     "push_subscription", "dashboard_password"}
        parts.append("## 1. Current Configuration\n")
        for k, v in sorted(cfg.items()):
            if k in _secrets:
                continue
            parts.append(f"- {k}: {v}")

        # ── 2. Data Collection Status ──
        obs = get_observation_count()
        parts.append(f"\n## 2. Data Collection Status\n")
        parts.append(f"- Total observations: {obs.get('total', 0)}")
        parts.append(f"- Resolved (with outcome): {obs.get('resolved', 0)}")
        parts.append(f"- Traded: {obs.get('traded', 0)}")
        parts.append(f"- Observed (skipped): {obs.get('observed', 0)}")
        parts.append(f"- Idle: {obs.get('idle', 0)}")
        parts.append(f"- First observation: {obs.get('first_obs', 'none')}")
        parts.append(f"- Last observation: {obs.get('last_obs', 'none')}")
        parts.append(f"- Quality: full={obs.get('quality_full', 0)}, "
                      f"partial={obs.get('quality_partial', 0)}, "
                      f"short={obs.get('quality_short', 0)}, "
                      f"few={obs.get('quality_few', 0)}")

        with get_conn() as c:
            # Regime distribution
            regime_dist = c.execute("""
                SELECT regime_label, COUNT(*) as n,
                       SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) as yes_wins,
                       SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) as no_wins
                FROM btc15m_observations
                WHERE market_result IS NOT NULL AND regime_label IS NOT NULL
                GROUP BY regime_label ORDER BY n DESC
            """).fetchall()

            parts.append(f"\n### Regime Distribution (resolved observations)")
            parts.append(f"| Regime | Count | YES wins | NO wins | YES rate |")
            parts.append(f"|--------|-------|----------|---------|----------|")
            for r in regime_dist:
                total = r["n"]
                yw = r["yes_wins"] or 0
                yr = f"{yw/total*100:.1f}%" if total > 0 else "—"
                parts.append(f"| {r['regime_label']} | {total} | {yw} | {r['no_wins'] or 0} | {yr} |")

            # Hour distribution
            hour_dist = c.execute("""
                SELECT hour_et, COUNT(*) as n,
                       SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) as yes_wins,
                       SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) as no_wins
                FROM btc15m_observations
                WHERE market_result IS NOT NULL AND hour_et IS NOT NULL
                GROUP BY hour_et ORDER BY hour_et
            """).fetchall()

            parts.append(f"\n### Hour Distribution (ET, resolved observations)")
            parts.append(f"| Hour ET | Count | YES wins | NO wins | YES rate |")
            parts.append(f"|---------|-------|----------|---------|----------|")
            for r in hour_dist:
                total = r["n"]
                yw = r["yes_wins"] or 0
                yr = f"{yw/total*100:.1f}%" if total > 0 else "—"
                parts.append(f"| {r['hour_et']}:00 | {total} | {yw} | {r['no_wins'] or 0} | {yr} |")

            # Day of week distribution
            dow_dist = c.execute("""
                SELECT day_of_week, COUNT(*) as n,
                       SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) as yes_wins
                FROM btc15m_observations
                WHERE market_result IS NOT NULL AND day_of_week IS NOT NULL
                GROUP BY day_of_week ORDER BY day_of_week
            """).fetchall()
            day_names = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun']

            parts.append(f"\n### Day of Week Distribution")
            parts.append(f"| Day | Count | YES rate |")
            parts.append(f"|-----|-------|----------|")
            for r in dow_dist:
                total = r["n"]
                yw = r["yes_wins"] or 0
                yr = f"{yw/total*100:.1f}%" if total > 0 else "—"
                dn = day_names[r["day_of_week"]] if 0 <= r["day_of_week"] <= 6 else str(r["day_of_week"])
                parts.append(f"| {dn} | {total} | {yr} |")

            # Snapshot count distribution
            snap_dist = c.execute("""
                SELECT
                    CASE
                        WHEN snapshot_count < 10 THEN '<10'
                        WHEN snapshot_count < 50 THEN '10-49'
                        WHEN snapshot_count < 80 THEN '50-79'
                        WHEN snapshot_count < 120 THEN '80-119'
                        WHEN snapshot_count < 160 THEN '120-159'
                        ELSE '160+'
                    END as bucket,
                    COUNT(*) as n
                FROM btc15m_observations
                GROUP BY bucket ORDER BY MIN(snapshot_count)
            """).fetchall()
            parts.append(f"\n### Snapshot Count Distribution")
            for r in snap_dist:
                parts.append(f"- {r['bucket']} snapshots: {r['n']} observations")

            # ── 3. Strategy Observatory Results ──
            parts.append(f"\n## 3. Strategy Observatory Results\n")

            # Overall stats
            strat_meta = c.execute("""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN ev_per_trade_c > 0 THEN 1 ELSE 0 END) as positive_ev,
                       SUM(CASE WHEN fdr_significant = 1 THEN 1 ELSE 0 END) as fdr_sig,
                       COUNT(DISTINCT setup_key) as setup_count
                FROM btc15m_strategy_results WHERE sample_size >= 10
            """).fetchone()
            parts.append(f"- Total strategy-setup combinations (n≥10): {strat_meta['total']}")
            parts.append(f"- Positive EV: {strat_meta['positive_ev']}")
            parts.append(f"- FDR-significant: {strat_meta['fdr_sig']}")
            parts.append(f"- Distinct setups: {strat_meta['setup_count']}")

            # Top 50 global strategies
            top_global = c.execute("""
                SELECT strategy_key, sample_size, wins, losses, win_rate,
                       ev_per_trade_c, COALESCE(weighted_ev_c, ev_per_trade_c) as w_ev,
                       ci_lower, ci_upper, profit_factor,
                       max_consecutive_losses, max_drawdown_c, total_pnl_c,
                       oos_ev_c, oos_win_rate, oos_sample_size,
                       fdr_significant, fdr_q_value,
                       slippage_1c_ev, slippage_2c_ev,
                       quality_full_ev_c, quality_degraded_ev_c,
                       breakeven_fee_rate, pnl_std_c
                FROM btc15m_strategy_results
                WHERE setup_key = 'global:all' AND sample_size >= 10
                ORDER BY COALESCE(weighted_ev_c, ev_per_trade_c) DESC
                LIMIT 50
            """).fetchall()
            top_global = rows_to_list(top_global)

            if top_global:
                parts.append(f"\n### Top 50 Global Strategies (by weighted EV, n≥10)")
                parts.append(f"| Strategy Key | n | WR | EV¢ | wEV¢ | CI | PF | maxL | DD¢ | OOS_EV¢ | OOS_n | FDR | q | slip1¢ | slip2¢ | fullEV | bkevFee | std¢ |")
                parts.append(f"|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
                for s in top_global:
                    wr = f"{(s['win_rate'] or 0)*100:.0f}%"
                    ci = f"{(s['ci_lower'] or 0)*100:.0f}-{(s['ci_upper'] or 1)*100:.0f}"
                    pf = f"{s['profit_factor']:.2f}" if s.get('profit_factor') else "—"
                    oos_ev = f"{s['oos_ev_c']:+.1f}" if s.get('oos_ev_c') is not None else "—"
                    oos_n = s.get('oos_sample_size') or "—"
                    fdr = "✓" if s.get('fdr_significant') else ""
                    q = f"{s['fdr_q_value']:.3f}" if s.get('fdr_q_value') is not None else "—"
                    s1 = f"{s['slippage_1c_ev']:+.1f}" if s.get('slippage_1c_ev') is not None else "—"
                    s2 = f"{s['slippage_2c_ev']:+.1f}" if s.get('slippage_2c_ev') is not None else "—"
                    fev = f"{s['quality_full_ev_c']:+.1f}" if s.get('quality_full_ev_c') is not None else "—"
                    bfe = f"{s['breakeven_fee_rate']:.2%}" if s.get('breakeven_fee_rate') is not None else "—"
                    std = f"{s['pnl_std_c']:.1f}" if s.get('pnl_std_c') is not None else "—"
                    parts.append(f"| {s['strategy_key']} | {s['sample_size']} | {wr} | "
                                 f"{(s['ev_per_trade_c'] or 0):+.1f} | {(s.get('w_ev') or 0):+.1f} | "
                                 f"{ci} | {pf} | {s.get('max_consecutive_losses',0)} | "
                                 f"{s.get('max_drawdown_c',0)} | {oos_ev} | {oos_n} | "
                                 f"{fdr} | {q} | {s1} | {s2} | {fev} | {bfe} | {std} |")

            # Bottom 20 global
            bottom_global = c.execute("""
                SELECT strategy_key, sample_size, win_rate, ev_per_trade_c,
                       COALESCE(weighted_ev_c, ev_per_trade_c) as w_ev,
                       ci_lower, total_pnl_c, oos_ev_c
                FROM btc15m_strategy_results
                WHERE setup_key = 'global:all' AND sample_size >= 10
                ORDER BY COALESCE(weighted_ev_c, ev_per_trade_c) ASC
                LIMIT 20
            """).fetchall()
            bottom_global = rows_to_list(bottom_global)

            if bottom_global:
                parts.append(f"\n### Bottom 20 Global Strategies (worst EV)")
                parts.append(f"| Strategy Key | n | WR | EV¢ | wEV¢ | CI lower | OOS_EV¢ | Total PnL |")
                parts.append(f"|---|---|---|---|---|---|---|---|")
                for s in bottom_global:
                    oos_ev = f"{s['oos_ev_c']:+.1f}" if s.get('oos_ev_c') is not None else "—"
                    parts.append(f"| {s['strategy_key']} | {s['sample_size']} | "
                                 f"{(s['win_rate'] or 0)*100:.0f}% | {(s['ev_per_trade_c'] or 0):+.1f} | "
                                 f"{(s.get('w_ev') or 0):+.1f} | {(s['ci_lower'] or 0)*100:.0f}% | "
                                 f"{oos_ev} | {(s['total_pnl_c'] or 0)/100:+.2f} |")

            # All FDR-significant strategies (across all setups)
            fdr_strats = c.execute("""
                SELECT setup_key, strategy_key, sample_size, win_rate,
                       ev_per_trade_c, COALESCE(weighted_ev_c, ev_per_trade_c) as w_ev,
                       ci_lower, profit_factor, fdr_q_value,
                       oos_ev_c, oos_sample_size, breakeven_fee_rate
                FROM btc15m_strategy_results
                WHERE fdr_significant = 1 AND sample_size >= 10
                ORDER BY COALESCE(weighted_ev_c, ev_per_trade_c) DESC
            """).fetchall()
            fdr_strats = rows_to_list(fdr_strats)

            parts.append(f"\n### All FDR-Significant Strategies ({len(fdr_strats)} total)")
            if fdr_strats:
                parts.append(f"| Setup | Strategy | n | WR | EV¢ | wEV¢ | CI low | PF | q-value | OOS_EV¢ | bkev fee |")
                parts.append(f"|---|---|---|---|---|---|---|---|---|---|---|")
                for s in fdr_strats:
                    pf = f"{s['profit_factor']:.2f}" if s.get('profit_factor') else "—"
                    oos = f"{s['oos_ev_c']:+.1f}" if s.get('oos_ev_c') is not None else "—"
                    bfe = f"{s['breakeven_fee_rate']:.2%}" if s.get('breakeven_fee_rate') is not None else "—"
                    parts.append(f"| {s['setup_key']} | {s['strategy_key']} | {s['sample_size']} | "
                                 f"{(s['win_rate'] or 0)*100:.0f}% | {(s['ev_per_trade_c'] or 0):+.1f} | "
                                 f"{(s.get('w_ev') or 0):+.1f} | {(s['ci_lower'] or 0)*100:.0f}% | "
                                 f"{pf} | {s['fdr_q_value']:.4f} | {oos} | {bfe} |")

            # ── 4. Strategy Dimension Analysis ──
            parts.append(f"\n## 4. Strategy Dimension Analysis (global:all, n≥10)\n")
            parts.append("Aggregated performance by each strategy dimension to identify "
                          "which parameters systematically help or hurt.\n")

            # By side rule
            side_agg = c.execute("""
                SELECT side_rule,
                       COUNT(*) as n_strats,
                       SUM(sample_size) as total_trades,
                       AVG(ev_per_trade_c) as avg_ev,
                       AVG(win_rate) as avg_wr,
                       SUM(CASE WHEN ev_per_trade_c > 0 THEN 1 ELSE 0 END) as pos_ev_count,
                       SUM(CASE WHEN fdr_significant = 1 THEN 1 ELSE 0 END) as fdr_count,
                       MAX(ev_per_trade_c) as best_ev,
                       MIN(ev_per_trade_c) as worst_ev
                FROM btc15m_strategy_results
                WHERE setup_key = 'global:all' AND sample_size >= 10
                GROUP BY side_rule ORDER BY avg_ev DESC
            """).fetchall()
            parts.append(f"### By Side Rule")
            parts.append(f"| Side Rule | Strategies | Avg EV¢ | Avg WR | +EV count | FDR sig | Best EV¢ | Worst EV¢ |")
            parts.append(f"|-----------|-----------|---------|--------|-----------|---------|----------|-----------|")
            for r in side_agg:
                parts.append(f"| {r['side_rule']} | {r['n_strats']} | {(r['avg_ev'] or 0):+.1f} | "
                             f"{(r['avg_wr'] or 0)*100:.0f}% | {r['pos_ev_count']} | {r['fdr_count']} | "
                             f"{(r['best_ev'] or 0):+.1f} | {(r['worst_ev'] or 0):+.1f} |")

            # By timing rule
            timing_agg = c.execute("""
                SELECT entry_time_rule,
                       COUNT(*) as n_strats,
                       AVG(ev_per_trade_c) as avg_ev,
                       AVG(win_rate) as avg_wr,
                       SUM(CASE WHEN ev_per_trade_c > 0 THEN 1 ELSE 0 END) as pos_ev_count,
                       SUM(CASE WHEN fdr_significant = 1 THEN 1 ELSE 0 END) as fdr_count
                FROM btc15m_strategy_results
                WHERE setup_key = 'global:all' AND sample_size >= 10
                GROUP BY entry_time_rule ORDER BY avg_ev DESC
            """).fetchall()
            parts.append(f"\n### By Timing Rule")
            parts.append(f"| Timing | Strategies | Avg EV¢ | Avg WR | +EV count | FDR sig |")
            parts.append(f"|--------|-----------|---------|--------|-----------|---------|")
            for r in timing_agg:
                parts.append(f"| {r['entry_time_rule']} | {r['n_strats']} | {(r['avg_ev'] or 0):+.1f} | "
                             f"{(r['avg_wr'] or 0)*100:.0f}% | {r['pos_ev_count']} | {r['fdr_count']} |")

            # By entry price bucket
            price_agg = c.execute("""
                SELECT
                    CASE
                        WHEN entry_price_max <= 20 THEN '5-20c'
                        WHEN entry_price_max <= 35 THEN '25-35c'
                        WHEN entry_price_max <= 50 THEN '40-50c'
                        WHEN entry_price_max <= 65 THEN '55-65c'
                        ELSE '70-95c'
                    END as price_bucket,
                    COUNT(*) as n_strats,
                    AVG(ev_per_trade_c) as avg_ev,
                    AVG(win_rate) as avg_wr,
                    SUM(CASE WHEN ev_per_trade_c > 0 THEN 1 ELSE 0 END) as pos_ev_count,
                    SUM(CASE WHEN fdr_significant = 1 THEN 1 ELSE 0 END) as fdr_count
                FROM btc15m_strategy_results
                WHERE setup_key = 'global:all' AND sample_size >= 10
                GROUP BY price_bucket ORDER BY MIN(entry_price_max)
            """).fetchall()
            parts.append(f"\n### By Entry Price Range")
            parts.append(f"| Price Range | Strategies | Avg EV¢ | Avg WR | +EV count | FDR sig |")
            parts.append(f"|-------------|-----------|---------|--------|-----------|---------|")
            for r in price_agg:
                parts.append(f"| {r['price_bucket']} | {r['n_strats']} | {(r['avg_ev'] or 0):+.1f} | "
                             f"{(r['avg_wr'] or 0)*100:.0f}% | {r['pos_ev_count']} | {r['fdr_count']} |")

            # By sell target type (hold vs sell)
            sell_agg = c.execute("""
                SELECT
                    CASE
                        WHEN sell_target = 'hold' THEN 'hold'
                        WHEN CAST(sell_target AS INTEGER) >= 90 THEN 'sell 90-99c'
                        WHEN CAST(sell_target AS INTEGER) >= 70 THEN 'sell 70-85c'
                        ELSE 'sell <70c'
                    END as sell_bucket,
                    COUNT(*) as n_strats,
                    AVG(ev_per_trade_c) as avg_ev,
                    AVG(win_rate) as avg_wr,
                    SUM(CASE WHEN ev_per_trade_c > 0 THEN 1 ELSE 0 END) as pos_ev_count,
                    SUM(CASE WHEN fdr_significant = 1 THEN 1 ELSE 0 END) as fdr_count
                FROM btc15m_strategy_results
                WHERE setup_key = 'global:all' AND sample_size >= 10
                GROUP BY sell_bucket ORDER BY avg_ev DESC
            """).fetchall()
            parts.append(f"\n### By Sell Target")
            parts.append(f"| Sell Target | Strategies | Avg EV¢ | Avg WR | +EV count | FDR sig |")
            parts.append(f"|-------------|-----------|---------|--------|-----------|---------|")
            for r in sell_agg:
                parts.append(f"| {r['sell_bucket']} | {r['n_strats']} | {(r['avg_ev'] or 0):+.1f} | "
                             f"{(r['avg_wr'] or 0)*100:.0f}% | {r['pos_ev_count']} | {r['fdr_count']} |")

            # Walk-forward IS vs OOS gap analysis
            wf_gap = c.execute("""
                SELECT strategy_key, sample_size, ev_per_trade_c,
                       oos_ev_c, oos_sample_size,
                       ev_per_trade_c - COALESCE(oos_ev_c, ev_per_trade_c) as gap
                FROM btc15m_strategy_results
                WHERE setup_key = 'global:all' AND sample_size >= 20
                  AND oos_ev_c IS NOT NULL AND oos_sample_size >= 10
                ORDER BY gap DESC
                LIMIT 20
            """).fetchall()
            wf_gap = rows_to_list(wf_gap)

            if wf_gap:
                parts.append(f"\n### Walk-Forward Overfitting Check (biggest IS→OOS gaps)")
                parts.append("Positive gap = in-sample EV is higher than out-of-sample (potential overfitting).\n")
                parts.append(f"| Strategy | n | IS EV¢ | OOS EV¢ | OOS n | Gap¢ |")
                parts.append(f"|----------|---|--------|---------|-------|------|")
                for s in wf_gap:
                    parts.append(f"| {s['strategy_key']} | {s['sample_size']} | "
                                 f"{(s['ev_per_trade_c'] or 0):+.1f} | {(s['oos_ev_c'] or 0):+.1f} | "
                                 f"{s['oos_sample_size']} | {(s['gap'] or 0):+.1f} |")

            # ── 5. Per-Regime Best Strategies ──
            parts.append(f"\n## 5. Per-Regime Best Strategies (top 5 each, n≥5)\n")

            regime_strats = c.execute("""
                SELECT setup_key, strategy_key, sample_size, win_rate,
                       ev_per_trade_c, COALESCE(weighted_ev_c, ev_per_trade_c) as w_ev,
                       ci_lower, profit_factor, oos_ev_c, fdr_significant
                FROM btc15m_strategy_results
                WHERE setup_type = 'coarse_regime' AND sample_size >= 5
                ORDER BY setup_key, COALESCE(weighted_ev_c, ev_per_trade_c) DESC
            """).fetchall()
            regime_strats = rows_to_list(regime_strats)

            current_setup = None
            count = 0
            for s in regime_strats:
                if s['setup_key'] != current_setup:
                    current_setup = s['setup_key']
                    count = 0
                    regime_name = current_setup.replace('coarse_regime:', '')
                    parts.append(f"\n### {regime_name}")
                    parts.append(f"| # | Strategy | n | WR | EV¢ | wEV¢ | CI low | PF | OOS | FDR |")
                    parts.append(f"|---|----------|---|----|----|------|--------|----|----|-----|")
                count += 1
                if count > 5:
                    continue
                pf = f"{s['profit_factor']:.2f}" if s.get('profit_factor') else "—"
                oos = f"{s['oos_ev_c']:+.1f}" if s.get('oos_ev_c') is not None else "—"
                fdr = "✓" if s.get('fdr_significant') else ""
                parts.append(f"| {count} | {s['strategy_key']} | {s['sample_size']} | "
                             f"{(s['win_rate'] or 0)*100:.0f}% | {(s['ev_per_trade_c'] or 0):+.1f} | "
                             f"{(s.get('w_ev') or 0):+.1f} | {(s['ci_lower'] or 0)*100:.0f}% | "
                             f"{pf} | {oos} | {fdr} |")

            # ── 6. Per-Hour Best Strategies ──
            parts.append(f"\n## 6. Per-Hour Best Strategies (top 3 each, n≥5)\n")

            hour_strats = c.execute("""
                SELECT setup_key, strategy_key, sample_size, win_rate,
                       ev_per_trade_c, ci_lower, fdr_significant
                FROM btc15m_strategy_results
                WHERE setup_type = 'hour' AND sample_size >= 5
                ORDER BY setup_key, ev_per_trade_c DESC
            """).fetchall()
            hour_strats = rows_to_list(hour_strats)

            current_hour = None
            count = 0
            for s in hour_strats:
                if s['setup_key'] != current_hour:
                    current_hour = s['setup_key']
                    count = 0
                    h = current_hour.replace('hour:', '')
                    parts.append(f"\n**Hour {h}:00 ET**")
                count += 1
                if count > 3:
                    continue
                fdr = " ✓FDR" if s.get('fdr_significant') else ""
                parts.append(f"  {count}. {s['strategy_key']} — n={s['sample_size']} "
                             f"WR={((s['win_rate'] or 0)*100):.0f}% "
                             f"EV={((s['ev_per_trade_c'] or 0)):+.1f}¢ "
                             f"CI↓={((s['ci_lower'] or 0)*100):.0f}%{fdr}")

        # ── 7. BTC Probability Surface ──
        parts.append(f"\n## 7. BTC Probability Surface\n")
        surface = get_btc_surface_data()
        if surface:
            parts.append(f"Total cells: {len(surface)}\n")
            parts.append(f"| Vol Bucket | Distance | Time | Samples | P(YES) | Avg YES¢ | Avg NO¢ |")
            parts.append(f"|------------|----------|------|---------|--------|----------|---------|")
            for cell in surface:
                ay = f"{cell['avg_yes_price']:.0f}" if cell.get('avg_yes_price') else "—"
                an = f"{cell['avg_no_price']:.0f}" if cell.get('avg_no_price') else "—"
                parts.append(f"| {cell['vol_bucket']} | {cell['distance_bucket']} | "
                             f"{cell['time_bucket']} | {cell['total']} | "
                             f"{cell['yes_win_rate']*100:.1f}% | {ay} | {an} |")
        else:
            parts.append("No surface data available yet.\n")

        # ── 8. Feature Importance ──
        parts.append(f"\n## 8. Feature Importance (predicting market outcome)\n")
        features = get_feature_importance()
        if features:
            parts.append(f"| Feature | Importance | Correlation | Sample Size |")
            parts.append(f"|---------|-----------|-------------|-------------|")
            for f in features:
                parts.append(f"| {f['feature_name']} | {f['importance']:.4f} | "
                             f"{f['correlation']:+.4f} | {f['sample_size']} |")
        else:
            parts.append("No feature importance data yet.\n")

        # ── 9. Regime Analysis ──
        parts.append(f"\n## 9. Regime Analysis\n")

        # Regime stats from real trades
        regimes = get_all_regime_stats()
        if regimes:
            parts.append(f"### Regime Stats (from real trades only)")
            parts.append(f"| Regime | Trades | Wins | WR | CI | Risk | PnL |")
            parts.append(f"|--------|--------|------|----|----|------|-----|")
            for r in regimes:
                rl = 'extreme' if r.get('risk_level') == 'terrible' else r.get('risk_level', '?')
                parts.append(f"| {r['regime_label']} | {r.get('total_trades',0)} | "
                             f"{r.get('wins',0)} | {(r.get('win_rate',0)*100):.0f}% | "
                             f"{(r.get('ci_lower',0)*100):.0f}-{(r.get('ci_upper',1)*100):.0f}% | "
                             f"{rl} | ${r.get('total_pnl',0):.2f} |")
        else:
            parts.append("No real trade data for regime stats (expected during data collection phase).\n")

        # Regime stability
        stability = get_regime_stability_summary(hours=168)  # 7 days
        parts.append(f"\n### Regime Stability (last 7 days)")
        parts.append(f"- Snapshots analyzed: {stability.get('n', 0)}")
        parts.append(f"- Fine label persistence: {stability.get('label_persistence_pct', 'N/A')}%")
        parts.append(f"- Coarse label persistence: {stability.get('coarse_persistence_pct', 'N/A')}%")
        parts.append(f"- Fine label changes: {stability.get('label_changes', 0)}")
        parts.append(f"- Coarse label changes: {stability.get('coarse_changes', 0)}")

        # Regime effectiveness
        try:
            from strategy import compute_regime_effectiveness
            eff = compute_regime_effectiveness()
            if eff and eff.get("fine_accuracy") is not None:
                parts.append(f"\n### Regime Effectiveness (fine vs coarse)")
                parts.append(f"- Fine-grained accuracy: {eff.get('fine_accuracy', 0)*100:.1f}%")
                parts.append(f"- Coarse accuracy: {eff.get('coarse_accuracy', 0)*100:.1f}%")
                parts.append(f"- Baseline (all same): {eff.get('baseline_accuracy', 0)*100:.1f}%")
                parts.append(f"- Fine lift over baseline: {eff.get('fine_lift', 0)*100:+.1f}pp")
                parts.append(f"- Coarse lift over baseline: {eff.get('coarse_lift', 0)*100:+.1f}pp")
        except Exception:
            pass

        # ── 10. Shadow Trading ──
        try:
            # get_shadow_trade_analysis already imported from market_db
            shadow = get_shadow_trade_analysis()
            if shadow and shadow.get("n", 0) > 0:
                parts.append(f"\n## 10. Shadow Trading — Execution Reality Check\n")
                parts.append(f"- Shadow trades completed: {shadow['n']}")
                parts.append(f"- Win rate: {shadow['win_rate']*100:.0f}%")
                parts.append(f"- Avg PnL per trade: {shadow['avg_pnl_per_trade_c']:+.1f}¢")
                slip = shadow.get("slippage", {})
                parts.append(f"- Fill slippage: avg {slip.get('avg_c', 0):+.1f}¢, "
                              f"max {slip.get('max_c', 0):+d}¢, "
                              f"{slip.get('pct_zero_or_better', 0)*100:.0f}% at-or-better "
                              f"(n={slip.get('n_measured', 0)})")
                lat = shadow.get("latency", {})
                parts.append(f"- Fill latency: avg {lat.get('avg_ms', 0):.0f}ms, "
                              f"max {lat.get('max_ms', 0):.0f}ms")
                gap = shadow.get("sim_reality_gap_c")
                if gap is not None:
                    parts.append(f"- **Sim-reality gap: {gap:+.2f}¢/trade** "
                                  f"({'sim optimistic — EVs inflated' if gap > 0.5 else 'sim pessimistic' if gap < -0.5 else 'well aligned'})")
                by_spread = shadow.get("by_spread", {})
                if by_spread:
                    parts.append(f"\n### Slippage by Spread Bucket")
                    for bucket, data in by_spread.items():
                        parts.append(f"- {bucket}: avg {data['avg_slippage_c']:+.2f}¢, "
                                      f"{data['pct_zero_or_better']*100:.0f}% at-or-better (n={data['n']})")
        except Exception:
            pass

        # ── 11. Simulation Assumptions ──
        parts.append(f"\n## 11. Current Simulation Assumptions\n")
        from config import KALSHI_FEE_RATE
        parts.append(f"- Fee rate: {KALSHI_FEE_RATE:.1%} of contract price (buy only)")
        parts.append(f"- Fee per contract: max(1¢, round(price × {KALSHI_FEE_RATE}))")
        parts.append(f"- Slippage model: spread-based — slippage = max(1, spread//2) from actual bid/ask spread")
        parts.append(f"- Entry timing: absolute seconds (early=0s, mid=300s, late=600s)")
        parts.append(f"- Fill delay: 2-snapshot minimum (≥5s) on both entry and exit")
        parts.append(f"- Quality filter: simulates on 'full' and 'short' quality observations (excludes 'partial' and 'few')")
        parts.append(f"- Walk-forward: 5-fold rolling with expanding training window")
        parts.append(f"- FDR: Benjamini-Hochberg at α=0.10, one-sample t-test on PnL")
        parts.append(f"- Time-weighted decay: 14-day half-life exponential")
        parts.append(f"- Min samples for advisor: {cfg.get('auto_strategy_min_samples', 30)}")
        parts.append(f"- Fee buffer for advisor: {cfg.get('min_breakeven_fee_buffer', 0.03):.1%}")

        # ── 12. Questions for Analysis ──
        parts.append(f"\n## 12. Key Questions for Analysis\n")
        parts.append("When reviewing this data, consider:")
        parts.append("1. Do any side rules systematically outperform? Does 'model' add value over 'cheaper'?")
        parts.append("2. Is there a clear optimal entry timing (early/mid/late)?")
        parts.append("3. What entry price range shows the best risk-adjusted returns?")
        parts.append("4. Is hold-to-expiry or sell-at-target more profitable? At what sell levels?")
        parts.append("5. How big is the walk-forward IS→OOS gap? Are the best strategies overfitting?")
        parts.append("6. Do FDR-significant strategies hold up in OOS validation?")
        parts.append("7. Is the BTC probability surface well-populated enough for the fair value model?")
        parts.append("8. Which features actually predict outcomes? What does feature importance tell us?")
        parts.append("9. Are regimes adding signal or just adding noise? Compare fine vs coarse effectiveness.")
        parts.append("10. Is the 7% fee assumption still correct? Check breakeven_fee_rate distribution.")
        parts.append("11. Are there time-of-day patterns strong enough to trade on?")
        parts.append("12. If shadow trading ran, how large is the sim-vs-real gap? Does it invalidate the simulation?")

        # Write file
        content = "\n".join(parts)
        fname = f"ai_analysis_{ts.strftime('%Y%m%d_%H%M%S')}.md"
        fpath = os.path.join(_report_dir(), fname)
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content)

        return jsonify({"url": f"/reports/{fname}", "filename": fname})

    except Exception as e:
        import traceback
        return jsonify({"error": f"Export failed: {e}\n{traceback.format_exc()[:500]}"}), 500


@app.route("/reports/<path:filename>")
@requires_auth
def serve_report(filename):
    """Serve generated report files."""
    from flask import send_from_directory
    download = request.args.get("dl", "0") == "1"
    return send_from_directory(
        _report_dir(), filename,
        as_attachment=download,
        download_name=filename if download else None,
    )

@app.route("/logout")
def logout():
    resp = make_response(render_template_string(LOGIN_HTML))
    resp.delete_cookie("platform_auth")
    return resp


@app.route("/api/deploy/upload", methods=["POST"])
@requires_auth
def api_deploy_upload():
    """Upload .py files to the bot directory. Backs up existing files first."""
    import subprocess, shutil
    bot_dir = os.environ.get("BOT_DIR", "/opt/trading-platform")
    backup_dir = os.path.join(bot_dir, "_backup")
    os.makedirs(backup_dir, exist_ok=True)

    uploaded = []
    errors = []
    backed_up = []

    for key in request.files:
        f = request.files[key]
        if not f.filename.endswith('.py'):
            errors.append(f"{f.filename}: not a .py file")
            continue
        content = f.read()
        try:
            compile(content, f.filename, 'exec')
        except SyntaxError as e:
            errors.append(f"{f.filename}: syntax error line {e.lineno}: {e.msg}")
            continue
        # Backup existing file
        dest = os.path.join(bot_dir, f.filename)
        if os.path.exists(dest):
            shutil.copy2(dest, os.path.join(backup_dir, f.filename))
            backed_up.append(f.filename)
        with open(dest, 'wb') as out:
            out.write(content)
        uploaded.append(f.filename)

    # Write backup manifest
    if backed_up:
        with open(os.path.join(backup_dir, "_manifest.json"), "w") as mf:
            json.dump({"files": backed_up, "ts": now_utc()}, mf)

    if uploaded:
        insert_audit_log("deploy_upload", f"files={','.join(uploaded)}", ip=request.remote_addr or "")

    return jsonify({"uploaded": uploaded, "errors": errors, "backed_up": backed_up})


@app.route("/api/deploy/rollback", methods=["POST"])
@requires_auth
def api_deploy_rollback():
    """Restore backed up files and restart services."""
    import subprocess, shutil
    bot_dir = os.environ.get("BOT_DIR", "/opt/trading-platform")
    backup_dir = os.path.join(bot_dir, "_backup")
    manifest_path = os.path.join(backup_dir, "_manifest.json")

    if not os.path.exists(manifest_path):
        return jsonify({"error": "No backup found"}), 404

    with open(manifest_path) as mf:
        manifest = json.load(mf)

    restored = []
    errors = []
    for fname in manifest.get("files", []):
        src = os.path.join(backup_dir, fname)
        dest = os.path.join(bot_dir, fname)
        if os.path.exists(src):
            try:
                shutil.copy2(src, dest)
                restored.append(fname)
            except Exception as e:
                errors.append(f"{fname}: {e}")
        else:
            errors.append(f"{fname}: backup not found")

    # Restart services
    for svc in ["plugin-btc-15m", "platform-dashboard"]:
        try:
            subprocess.run(["supervisorctl", "restart", svc],
                           capture_output=True, text=True, timeout=10)
        except Exception:
            pass

    return jsonify({"restored": restored, "errors": errors})


@app.route("/api/deploy/backup_info")
@requires_auth
def api_deploy_backup_info():
    bot_dir = os.environ.get("BOT_DIR", "/opt/trading-platform")
    manifest_path = os.path.join(bot_dir, "_backup", "_manifest.json")
    if not os.path.exists(manifest_path):
        return jsonify({"has_backup": False})
    with open(manifest_path) as mf:
        manifest = json.load(mf)
    manifest["has_backup"] = True
    manifest["ts_ct"] = to_central(manifest.get("ts", ""))
    return jsonify(manifest)


ROLLBACK_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rollback</title>
<style>
  body { font-family: -apple-system, sans-serif; background: #0d1117; color: #c9d1d9;
         padding: 24px; max-width: 400px; margin: 0 auto; }
  h1 { color: #f0883e; font-size: 20px; margin-bottom: 16px; }
  .info { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
          padding: 16px; margin-bottom: 16px; font-size: 13px; }
  .dim { color: #8b949e; }
  .btn { display: block; width: 100%; padding: 14px; border: none; border-radius: 8px;
         font-size: 15px; font-weight: 600; cursor: pointer; margin-bottom: 10px;
         -webkit-tap-highlight-color: transparent; }
  .btn-orange { background: #f0883e; color: #000; }
  .btn-dim { background: #21262d; color: #c9d1d9; border: 1px solid #30363d; }
  .btn:disabled { opacity: 0.5; }
  #status { margin-top: 12px; font-size: 13px; }
  .green { color: #3fb950; }
  .red { color: #f85149; }
</style>
</head><body>
<h1>⚠ Emergency Rollback</h1>
<div class="info">
  <div id="backupInfo" class="dim">Checking for backup...</div>
</div>
<button class="btn btn-orange" id="rollbackBtn" onclick="doRollback()" disabled>
  Restore Last Working Version
</button>
<button class="btn btn-dim" onclick="doRestart()">Just Restart Services</button>
<div id="status"></div>
<div style="margin-top:24px;text-align:center">
  <a href="/" style="color:#58a6ff;font-size:13px">← Back to Dashboard</a>
</div>
<script>
async function load() {
  try {
    const r = await fetch('/api/deploy/backup_info');
    const d = await r.json();
    const el = document.getElementById('backupInfo');
    const btn = document.getElementById('rollbackBtn');
    if (d.has_backup) {
      el.innerHTML = '<strong style="color:#c9d1d9">Backup available</strong><br>'
        + 'Files: ' + (d.files||[]).join(', ')
        + '<br>Backed up: ' + (d.ts_ct || d.ts || '?');
      btn.disabled = false;
    } else {
      el.textContent = 'No backup found. Nothing to rollback.';
    }
  } catch(e) {
    document.getElementById('backupInfo').textContent = 'Error loading backup info: ' + e;
  }
}
async function doRollback() {
  const btn = document.getElementById('rollbackBtn');
  const st = document.getElementById('status');
  btn.disabled = true;
  btn.textContent = 'Rolling back...';
  st.innerHTML = '<span class="dim">Restoring files and restarting...</span>';
  try {
    const r = await fetch('/api/deploy/rollback', {method:'POST', headers:{'X-CSRF-Token':_getCsrfToken()}});
    const d = await r.json();
    if (d.error) {
      st.innerHTML = '<span class="red">' + d.error + '</span>';
      btn.disabled = false;
      btn.textContent = 'Restore Last Working Version';
    } else {
      let html = '';
      if (d.restored && d.restored.length) html += '<span class="green">Restored: ' + d.restored.join(', ') + '</span><br>';
      if (d.errors && d.errors.length) html += d.errors.map(e => '<span class="red">' + e + '</span>').join('<br>');
      html += '<br><span class="dim">Services restarting. Page will reload...</span>';
      st.innerHTML = html;
      setTimeout(() => location.href = '/', 4000);
    }
  } catch(e) {
    st.innerHTML = '<span class="red">Error: ' + e + '</span>';
    btn.disabled = false;
    btn.textContent = 'Restore Last Working Version';
  }
}
async function doRestart() {
  const st = document.getElementById('status');
  st.innerHTML = '<span class="dim">Restarting services...</span>';
  try {
    await fetch('/api/deploy/restart', {method:'POST',
      headers:{'Content-Type':'application/json','X-CSRF-Token':_getCsrfToken()},
      body: JSON.stringify({services:['plugin-btc-15m','platform-dashboard']})});
    st.innerHTML = '<span class="green">Restarted. Redirecting...</span>';
    setTimeout(() => location.href = '/', 4000);
  } catch(e) {
    st.innerHTML = '<span class="red">Error: ' + e + '</span>';
  }
}
load();
</script>
</body></html>"""


@app.route("/rollback")
@requires_auth
def rollback_page():
    return render_template_string(ROLLBACK_HTML)


@app.route("/api/deploy/paste", methods=["POST"])
@requires_auth
def api_deploy_paste():
    """Deploy code from pasted text."""
    import shutil
    bot_dir = os.environ.get("BOT_DIR", "/opt/trading-platform")
    backup_dir = os.path.join(bot_dir, "_backup")
    os.makedirs(backup_dir, exist_ok=True)

    data = request.get_json() or {}
    filename = data.get("filename", "")
    code = data.get("code", "")
    force = data.get("force", False)

    if not filename or not filename.endswith('.py'):
        return jsonify({"error": "Invalid filename"}), 400
    if not code.strip():
        return jsonify({"error": "No code provided"}), 400

    # Sanitize iOS/mobile Unicode replacements that break Python
    replacements = {
        '\u201c': '"', '\u201d': '"',   # smart double quotes
        '\u2018': "'", '\u2019': "'",   # smart single quotes
        '\u2013': '-', '\u2014': '-',   # en/em dashes
        '\u2015': '-',                  # horizontal bar
        '\u00a0': ' ',                  # non-breaking space
        '\u2003': ' ', '\u2002': ' ',   # em/en space
        '\u2009': ' ', '\u200a': ' ',   # thin/hair space
        '\u200b': '',                   # zero-width space
        '\u200c': '', '\u200d': '',     # zero-width non/joiner
        '\ufeff': '',                   # BOM
        '\u2026': '...',               # ellipsis
        '\u2032': "'", '\u2033': '"',   # prime/double prime
        '\uff08': '(', '\uff09': ')',   # fullwidth parens
        '\uff1a': ':', '\uff1b': ';',   # fullwidth colon/semicolon
        '\uff0c': ',', '\uff0e': '.',   # fullwidth comma/period
        '\uff1d': '=',                 # fullwidth equals
    }
    for old, new in replacements.items():
        code = code.replace(old, new)

    # Validate Python syntax (skip with force)
    if not force:
        try:
            compile(code, filename, 'exec')
        except SyntaxError as e:
            lines = code.split('\n')
            bad_line = lines[e.lineno - 1] if e.lineno and e.lineno <= len(lines) else ''
            non_ascii = [(i, c, hex(ord(c))) for i, c in enumerate(bad_line) if ord(c) > 127]
            extra = ''
            if non_ascii:
                extra = ' | Non-ASCII chars: ' + ', '.join(f"col {i} {h}" for i, c, h in non_ascii[:5])
            return jsonify({
                "error": f"{filename} line {e.lineno}: {e.msg}{extra}",
                "can_force": True,
                "size": len(code),
            }), 400

    # Backup existing
    dest = os.path.join(bot_dir, filename)
    if os.path.exists(dest):
        shutil.copy2(dest, os.path.join(backup_dir, filename))
        # Merge into manifest (don't overwrite if multi-file deploy)
        manifest_path = os.path.join(backup_dir, "_manifest.json")
        try:
            with open(manifest_path) as mf:
                manifest = json.load(mf)
            files = list(set(manifest.get("files", []) + [filename]))
        except Exception:
            files = [filename]
        with open(manifest_path, "w") as mf:
            json.dump({"files": files, "ts": now_utc()}, mf)

    # Write new file
    with open(dest, 'w') as f:
        f.write(code)

    return jsonify({"ok": True, "filename": filename, "size": len(code)})


@app.route("/api/deploy/restart", methods=["POST"])
@requires_auth
def api_deploy_restart():
    """Restart bot and/or dashboard services.
    Sends response BEFORE restarting dashboard to avoid 502."""
    import subprocess, threading
    data = request.get_json() or {}
    services = data.get("services", ["plugin-btc-15m", "platform-dashboard"])

    # Restart bot immediately (won't kill this process)
    results = {}
    bot_services = [s for s in services if s != "platform-dashboard"]
    for svc in bot_services:
        try:
            r = subprocess.run(["supervisorctl", "restart", svc],
                               capture_output=True, text=True, timeout=10)
            results[svc] = r.stdout.strip() or r.stderr.strip()
        except Exception as e:
            results[svc] = str(e)

    # Dashboard restart: delay so the HTTP response reaches the client first
    if "platform-dashboard" in services:
        results["platform-dashboard"] = "restarting in 1s..."
        def _delayed_restart():
            import time as _t
            _t.sleep(1)
            try:
                subprocess.run(["supervisorctl", "restart", "platform-dashboard"],
                               capture_output=True, text=True, timeout=10)
            except Exception:
                pass
        threading.Thread(target=_delayed_restart, daemon=True).start()

    return jsonify(results)


@app.route("/api/deploy/recheck_email", methods=["POST"])
@requires_auth
def api_deploy_recheck_email():
    """Force the IMAP thread to reconnect and recheck for pending deploy emails."""
    global _email_deploy_conn
    conn = _email_deploy_conn
    if conn is None:
        return jsonify({"status": "no_connection", "msg": "Email deploy not connected — will check on next reconnect"})
    try:
        # Close the socket to break out of IDLE — the thread's except
        # handler will catch it, reconnect, and check UNSEEN
        conn.shutdown()
    except Exception:
        pass
    try:
        conn.socket().close()
    except Exception:
        pass
    _email_deploy_conn = None
    insert_log("INFO", "[EmailDeploy] Manual recheck triggered", "deploy")
    return jsonify({"status": "ok", "msg": "IMAP reconnecting — will process pending emails"})


@app.route("/")
@requires_auth
def index():
    html = MAIN_HTML
    resp = Response(html, content_type="text/html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/logs")
@requires_auth
def logs_page():
    return render_template_string(LOGS_HTML)


# ═══════════════════════════════════════════════════════════════
#  HTML TEMPLATES
# ═══════════════════════════════════════════════════════════════

MAIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<link rel="manifest" href="/manifest.json">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0d1117">
<link rel="apple-touch-icon" href="/icon-192.png">
<title>Kalshi BTC Bot</title>
<style>
  :root { --bg: #0d1117; --card: #161b22; --border: #30363d; --text: #c9d1d9;
          --green: #3fb950; --red: #f85149; --yellow: #d29922; --blue: #58a6ff;
          --dim: #8b949e; --orange: #f0883e; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { overflow: hidden; position: fixed; width: 100%; height: 100%; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: var(--bg); color: var(--text);
         font-size: 14px; margin: 0; padding: 0; }
  #stickyHeader { position: fixed; top: 0; left: 0; right: 0; z-index: 50;
                  background: var(--card);
                  border-bottom: 1px solid var(--border);
                  padding: 10px 14px;
                  box-shadow: 0 4px 12px rgba(0,0,0,0.5); }
  .tab-bar { position: fixed; bottom: 0; left: 0; right: 0; z-index: 100;
    display: flex; align-items: stretch; justify-content: space-around;
    padding-top: 8px; padding-bottom: 30px;
    background: var(--card); border-top: 1px solid var(--border); }
  #contentWrap { position: fixed; top: 58px; left: 0; right: 0; bottom: 76px;
    overflow-y: auto; overscroll-behavior-y: contain;
    -webkit-overflow-scrolling: touch; padding: 8px 12px; }
  .hdr-row { display: flex; justify-content: space-between; align-items: center; }
  #hdrBankroll:active { background: rgba(255,255,255,0.08); }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
          padding: 14px; margin-bottom: 12px; }
  .page { display: none; }
  .page.active { display: block; }
  .card h3 { color: var(--blue); font-size: 13px; text-transform: uppercase;
             letter-spacing: 0.5px; margin-bottom: 10px; }
  .status-bar { display: flex; justify-content: space-between; align-items: center;
                flex-wrap: wrap; gap: 8px; }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
  .dot-purple { background: #a371f7; box-shadow: 0 0 6px rgba(163,113,247,0.5); }
  /* ── Mode Selector Strip ── */
  .mode-strip { display: flex; gap: 4px; margin-top: 8px; }
  .mode-btn { flex: 1; padding: 5px 2px 4px; border: 1px solid var(--border); border-radius: 6px;
    background: var(--card); cursor: pointer; text-align: center; font-size: 10px;
    font-weight: 600; color: var(--dim); letter-spacing: 0.3px; transition: all 0.15s;
    -webkit-tap-highlight-color: transparent; line-height: 1.2; }
  .mode-btn:active { filter: brightness(1.2); }
  .mode-btn .mode-icon { font-size: 11px; display: block; margin-bottom: 1px; opacity: 0.7; }
  .mode-btn.m-active-observe { background: rgba(88,166,255,0.12); color: var(--blue);
    border-color: rgba(88,166,255,0.4); }
  .mode-btn.m-active-shadow { background: rgba(163,113,247,0.12); color: #a371f7;
    border-color: rgba(163,113,247,0.4); }
  .mode-btn.m-active-hybrid { background: rgba(88,166,255,0.12); color: var(--blue);
    border-color: rgba(88,166,255,0.4); }
  .mode-btn.m-active-auto { background: rgba(63,185,80,0.12); color: var(--green);
    border-color: rgba(63,185,80,0.4); }
  .mode-btn.m-active-manual { background: rgba(248,81,73,0.08); color: var(--text);
    border-color: rgba(248,81,73,0.3); }
  .dot-green { background: var(--green); box-shadow: 0 0 4px var(--green), 0 0 8px rgba(63,185,80,0.3);
               animation: live-pulse-green 2s ease-in-out infinite; }
  .dot-red { background: var(--red); box-shadow: 0 0 4px var(--red), 0 0 8px rgba(248,81,73,0.3);
             animation: live-pulse-red 2s ease-in-out infinite; }
  .dot-yellow { background: var(--yellow); box-shadow: 0 0 4px var(--yellow), 0 0 8px rgba(210,153,34,0.3);
                animation: live-pulse-yellow 2s ease-in-out infinite; }
  @keyframes live-pulse-yellow {
    0%,100% { box-shadow: 0 0 4px var(--yellow); opacity: 1; }
    50% { box-shadow: 0 0 10px var(--yellow), 0 0 18px rgba(210,153,34,0.4); opacity: 0.75; }
  }
  .dot-purple { background: #a371f7; box-shadow: 0 0 4px #a371f7, 0 0 8px rgba(163,113,247,0.3);
                animation: live-pulse-purple 2s ease-in-out infinite; }
  @keyframes live-pulse-purple {
    0%,100% { box-shadow: 0 0 4px #a371f7; opacity: 1; }
    50% { box-shadow: 0 0 10px #a371f7, 0 0 18px rgba(163,113,247,0.4); opacity: 0.75; }
  }
  .bot-offline-banner {
    display: none; background: rgba(248,81,73,0.12); border-bottom: 1px solid var(--red);
    padding: 6px 12px; text-align: center; font-size: 12px; font-weight: 600;
    color: var(--red); letter-spacing: 0.3px; animation: offline-pulse 3s ease-in-out infinite;
  }
  .bot-offline-banner .offline-dot {
    display: inline-block; width: 6px; height: 6px; border-radius: 50%;
    background: var(--red); margin-right: 6px; vertical-align: middle;
  }
  @keyframes offline-pulse {
    0%,100% { opacity: 1; }
    50% { opacity: 0.6; }
  }
  .big-num { font-size: 28px; font-weight: 700; font-family: 'SF Mono', monospace; }
  .pos { color: var(--green); }
  .neg { color: var(--red); }
  .dim { color: var(--dim); font-size: 12px; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .grid3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; }
  .stat { text-align: center; padding: 6px; }
  .stat .label { color: var(--dim); font-size: 11px; text-transform: uppercase; }
  .stat .val { font-size: 18px; font-weight: 600; font-family: monospace; margin-top: 2px;
    white-space: nowrap; overflow: hidden; }
  .btn { padding: 10px 16px; border: none; border-radius: 6px; font-size: 14px;
         font-weight: 600; cursor: pointer; width: 100%; margin-top: 6px; }
  .btn-green { background: var(--green); color: #000; }
  .btn-red { background: var(--red); color: #fff; }
  .btn-yellow { background: var(--yellow); color: #000; }
  .btn-blue { background: var(--blue); color: #000; }
  .btn-dim { background: var(--border); color: var(--text); }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  /* Tinted action buttons for trade controls */
  .act-btn { display: flex; align-items: center; justify-content: center; gap: 6px;
    width: 100%; padding: 10px 14px; border-radius: 6px; font-size: 13px; font-weight: 600;
    cursor: pointer; -webkit-tap-highlight-color: transparent; transition: filter 0.15s;
    border: 1px solid; }
  .act-btn:active { filter: brightness(1.3); }
  .act-btn svg { width: 16px; height: 16px; flex-shrink: 0; }
  .act-btn-red { background: rgba(248,81,73,0.1); color: var(--red); border-color: rgba(248,81,73,0.3); }
  .act-btn-green { background: rgba(63,185,80,0.1); color: var(--green); border-color: rgba(63,185,80,0.3); }
  .act-btn-blue { background: rgba(88,166,255,0.1); color: var(--blue); border-color: rgba(88,166,255,0.3); }
  .act-btn-yellow { background: rgba(210,153,34,0.1); color: var(--yellow); border-color: rgba(210,153,34,0.3); }
  .act-btn-dim { background: rgba(48,54,61,0.3); color: var(--dim); border-color: var(--border); }
  .act-btn-sm { padding: 6px 12px; font-size: 12px; width: auto; }
  .trade-live { border-left: 3px solid var(--green); }
  .input-row { display: flex; gap: 8px; align-items: center; margin-top: 8px; }
  .input-row label { color: var(--dim); font-size: 12px; min-width: 90px; }
  .input-row input, .input-row select { background: var(--bg); border: 1px solid var(--border);
    color: var(--text); padding: 6px 8px; border-radius: 4px; font-size: 16px; flex: 1; }
  .toggle { display: flex; align-items: center; gap: 8px; margin-top: 6px; }
  .tog { display: inline-flex; align-items: center; cursor: pointer; -webkit-tap-highlight-color: transparent; }
  .tog input { position: absolute; opacity: 0; width: 0; height: 0; pointer-events: none; }
  .tog .tpill { display: inline-block; padding: 3px 8px; border-radius: 4px;
    font-size: 10px; font-weight: 700; letter-spacing: 0.5px; text-transform: uppercase;
    transition: all 0.15s ease; user-select: none; min-width: 36px; text-align: center;
    background: #2a1a1a; color: var(--red); border: 1px solid rgba(248,81,73,0.25); }
  .tog input:checked ~ .tpill {
    background: #1a2a1a; color: var(--green); border-color: rgba(63,185,80,0.3); }
  .tog .tpill::before { content: 'OFF'; }
  .tog input:checked ~ .tpill::before { content: 'ON'; }
  .tog:active .tpill { filter: brightness(1.3); }
  a { color: var(--blue); }
  .trade-row { display: flex; justify-content: space-between; padding: 6px 0;
               border-bottom: 1px solid var(--border); font-size: 13px; align-items: center; }
  .regime-tag { display: inline-block; padding: 2px 6px; border-radius: 3px;
                font-size: 11px; font-weight: 600; white-space: nowrap; }
  .risk-low { background: #1a3a2a; color: var(--green); }
  .risk-moderate { background: #3a3a1a; color: var(--yellow); }
  .risk-high { background: #3a2a1a; color: var(--orange); }
  .risk-terrible { background: #3a1a1a; color: var(--red); }
  .risk-unknown { background: #1a2a3a; color: var(--blue); }
  .confirm-overlay { position: fixed; top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.8); display: none; align-items: center;
    justify-content: center; z-index: 100; overflow: hidden;
    overscroll-behavior: contain; padding: 0 12px;
    -webkit-overflow-scrolling: auto; }
  .confirm-box { background: var(--card); border: 1px solid var(--red); border-radius: 10px;
    padding: 24px; max-width: 300px; text-align: center; }
  .modal-panel { background: var(--card); border-radius: 12px; padding: 16px; width: 95%;
    border: 1px solid var(--border);
    max-height: 70vh; overflow-y: auto;
    overscroll-behavior: contain; }
  .confirm-btns { display: flex; gap: 8px; margin-top: 16px; }
  .confirm-btns .btn { flex: 1; }
  .price-display { font-family: 'SF Mono', monospace; font-size: 22px; font-weight: 700; }
  .progress-bar { height: 6px; background: var(--border); border-radius: 3px; margin-top: 8px; overflow: hidden; }
  .progress-fill { height: 100%; border-radius: 3px; transition: width 0.5s; }
  .warning-box { background: #3a2a1a; border: 1px solid var(--orange); border-radius: 6px;
                 padding: 10px; margin-top: 8px; font-size: 12px; color: var(--orange); }
  .proj-table { width: 100%; font-size: 12px; margin-top: 8px; }
  .proj-table th { color: var(--dim); text-align: left; padding: 4px; font-weight: normal;
                   text-transform: uppercase; font-size: 10px; border-bottom: 1px solid var(--border); }
  .proj-table td { padding: 4px; font-family: monospace; }
  .proj-table .current-round { background: rgba(88,166,255,0.1); }
  .detail-toggle { color: var(--blue); font-size: 12px; cursor: pointer; margin-top: 8px;
                   display: block; }
  .detail-section { display: none; margin-top: 8px; padding-top: 8px;
                    border-top: 1px solid var(--border); }
  .settings-card { background: var(--card); border: 1px solid var(--border); border-radius: 10px;
    padding: 14px; margin-bottom: 12px; }
  .settings-card .sc-title { font-size: 11px; font-weight: 700; letter-spacing: 0.5px;
    color: var(--blue); margin-bottom: 10px; display: flex; align-items: center; gap: 6px; }
  .settings-card .sc-sub { font-size: 10px; font-weight: 600; letter-spacing: 0.3px;
    color: var(--dim); margin: 12px 0 6px; padding-top: 8px; border-top: 1px solid var(--border); }
  .settings-card .sc-hint { font-size: 10px; color: var(--dim); line-height: 1.4; margin-top: 2px; }
  .regime-detail-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 4px 12px;
    font-size: 12px; color: var(--dim); }
  .regime-detail-grid .rdg-val { color: var(--text); font-weight: 600; }
  .trade-card { background: var(--card); border: 1px solid var(--border); border-radius: 6px;
                padding: 10px; margin-bottom: 8px; border-left: 3px solid var(--border);
                -webkit-tap-highlight-color: rgba(88,166,255,0.15); }
  .trade-card.tc-win { border-left-color: var(--green); }
  .trade-card.tc-loss { border-left-color: var(--red); }
  .trade-card.tc-skip { border-left-color: var(--blue); }
  .trade-card.tc-open { border-left-color: var(--blue); }
  .trade-card.tc-shadow { border-left-color: #a371f7; }
  .trade-card.tc-error { border-left-color: #d29922; }
  .tc-header { display: flex; justify-content: space-between; align-items: center; }
  .tc-outcome { font-weight: 700; font-size: 15px; }
  .tc-pnl { font-family: monospace; font-size: 15px; font-weight: 700; }
  .tc-details { display: grid; grid-template-columns: 1fr 1fr; gap: 2px 12px;
                font-size: 12px; color: var(--dim); margin-top: 6px; }
  .tc-details strong { color: var(--text); }
  .tc-tags { display: flex; gap: 4px; flex-wrap: wrap; margin-top: 6px; }
  .tc-tag { font-size: 10px; padding: 1px 5px; border-radius: 3px;
            background: var(--border); color: var(--dim); cursor: pointer; border: 1px solid transparent; }
  .tc-tag:hover { border-color: var(--dim); }
  .tc-tag.data { background: #1a2a3a; color: var(--blue); }
  .tc-tag.ignored { background: #3a2a1a; color: var(--orange); }
  .tc-tag.tag-win { background: #1a2a1a; color: var(--green); }
  .tc-tag.tag-loss { background: #2a1a1a; color: var(--red); }
  .tc-tag.tag-skip { background: rgba(88,166,255,0.1); color: var(--blue); }
  .tc-tag.tag-incomplete { background: rgba(248,81,73,0.15); color: var(--red); }
  .tc-tag.tag-yes { background: #1a2a1a; color: var(--green); }
  .tc-tag.tag-no { background: #2a1a1a; color: var(--red); }
  .tc-tag.tc-tag.tag-open { background: #1a2a3a; color: var(--blue); }
  .tc-tag.tag-shadow { background: #2a1a3a; color: #a371f7; }
  .tc-tag.tag-error { background: #2a2010; color: #d29922; }
  .tc-tag.tag-early { background: #1a2a2a; color: #56d4dd; }
  .tc-tag.tag-mid { background: #1a2a2a; color: #56d4dd; }
  .tc-tag.tag-late { background: #1a2a2a; color: #56d4dd; }
  .tc-tag.tag-cheaper { background: #2a2a1a; color: var(--yellow); }
  .tc-tag.tag-model { background: #1a2a3a; color: var(--blue); }
  .tc-tag.tag-sold { background: #1a2a1a; color: var(--green); }
  .tc-tag.tag-hold { background: #1a1a2a; color: var(--dim); }
  .filter-sep { width: 1px; height: 16px; background: var(--border); margin: 0 2px; align-self: center; }
  .stat-row { display: flex; justify-content: space-between; padding: 4px 0;
              font-size: 13px; border-bottom: 1px solid rgba(48,54,61,0.3); }
  .stat-row:last-child { border-bottom: none; }
  .stat-row .sr-label { color: var(--dim); }
  .stat-row .sr-val { font-family: monospace; font-weight: 600; }
  .stat-section-title { font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px;
                        color: var(--dim); margin-top: 10px; margin-bottom: 4px; }
  .filter-chips { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 8px; }
  .chip { font-size: 11px; padding: 3px 8px; border-radius: 10px; cursor: pointer;
          border: 1px solid var(--border); color: var(--dim); background: none;
          -webkit-tap-highlight-color: transparent; transition: all 0.15s; }
  .chip.active { border-color: var(--blue); color: var(--blue); background: rgba(88,166,255,0.1); }
  .chip.active-green { border-color: var(--green); color: var(--green); background: rgba(63,185,80,0.1); }
  .chip.active-red { border-color: var(--red); color: var(--red); background: rgba(248,81,73,0.1); }
  .chip.active-yellow { border-color: var(--yellow); color: var(--yellow); background: rgba(210,153,34,0.1); }
  .chip.active-orange { border-color: var(--orange); color: var(--orange); background: rgba(240,136,62,0.1); }
  .chip.active-purple { border-color: #a371f7; color: #a371f7; background: rgba(163,113,247,0.1); }
  .chip.exclude { border-color: rgba(248,81,73,0.4); color: var(--red); background: rgba(248,81,73,0.08);
                   text-decoration: line-through; text-decoration-thickness: 1.5px; }
  .collapsible > h3 { cursor: pointer; display: flex; justify-content: space-between;
                       align-items: center; -webkit-tap-highlight-color: transparent;
                       user-select: none; }
  .card-arrow { font-size: 12px; color: var(--dim); transition: transform 0.2s; }
  .collapsible.collapsed .card-arrow { transform: rotate(-90deg); }
  .collapsible.collapsed .card-body { display: none; }
  .collapsible.collapsed { padding-bottom: 12px; }
  .collapsible > h3 { margin-bottom: 0; }
  .card-subtitle { font-size: 11px; color: var(--dim); margin-top: 2px; font-weight: normal;
                   text-transform: none; letter-spacing: 0; }
  .collapsible:not(.collapsed) .card-subtitle { display: none; }
  .live-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
              background: var(--red); margin-right: 4px;
              box-shadow: 0 0 4px var(--red), 0 0 8px rgba(248,81,73,0.3);
              animation: live-pulse-red 2s ease-in-out infinite; }
  .live-dot-green { background: var(--green) !important;
              box-shadow: 0 0 4px var(--green), 0 0 8px rgba(63,185,80,0.3) !important;
              animation: live-pulse-green 2s ease-in-out infinite !important; }
  @keyframes live-pulse-red {
    0%,100% { box-shadow: 0 0 4px var(--red); opacity: 1; }
    50% { box-shadow: 0 0 10px var(--red), 0 0 18px rgba(248,81,73,0.4); opacity: 0.75; }
  }
  @keyframes live-pulse-green {
    0%,100% { box-shadow: 0 0 4px var(--green); opacity: 1; }
    50% { box-shadow: 0 0 10px var(--green), 0 0 18px rgba(63,185,80,0.4); opacity: 0.75; }
  }
  .side-yes { color: var(--green); font-weight: 700; }
  .side-no { color: var(--red); font-weight: 700; }
  .icon-btn { display: flex; align-items: center; justify-content: center; gap: 6px; }
  .icon-btn svg { width: 18px; height: 18px; flex-shrink: 0; }
  .tab-btn { background: none; border: none; cursor: pointer;
    display: flex; flex-direction: column; align-items: center; justify-content: flex-start;
    gap: 3px; padding: 8px 0 6px;
    -webkit-tap-highlight-color: transparent;
    flex: 1; font-size: 10px; color: var(--dim); }
  .tab-btn:active { opacity: 0.7; }
  .tab-btn.tab-active { color: var(--blue); }
  .tab-btn.tab-active svg { stroke: var(--blue);
    filter: drop-shadow(0 0 6px rgba(88,166,255,0.5)); }
  .tab-btn.tab-active span { text-shadow: 0 0 8px rgba(88,166,255,0.4); }
  .stat-summary-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 12px; }
  .stat-summary-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
    padding: 10px 12px; text-align: center; }
  .stat-summary-card .ssc-val { font-size: 22px; font-weight: 700; font-family: monospace; line-height: 1.2; }
  .stat-summary-card .ssc-label { font-size: 10px; color: var(--dim); text-transform: uppercase;
    letter-spacing: 0.5px; margin-top: 2px; }
  .stat-section { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
    margin-bottom: 8px; overflow: hidden; }
  .stat-section-header { padding: 10px 12px; cursor: pointer; display: flex;
    justify-content: space-between; align-items: center;
    -webkit-tap-highlight-color: transparent; user-select: none; }
  .stat-section-header:active { opacity: 0.7; }
  .stat-section-header h3 { font-size: 12px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.5px; margin: 0; color: var(--text); }
  .stat-section-header .ssh-sub { font-size: 11px; color: var(--dim); font-weight: normal; }
  .stat-section-header .ssh-arrow { font-size: 10px; color: var(--dim); transition: transform 0.2s; }
  .stat-section.collapsed .ssh-arrow { transform: rotate(-90deg); }
  .stat-section.collapsed .stat-section-body { display: none; }
  .stat-section-body { padding: 0 12px 12px; }
  .stat-section-body .proj-table { margin-top: 0; }
  .stat-category { font-size: 10px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.5px; color: var(--blue); margin: 16px 0 6px; padding-bottom: 4px;
    border-bottom: 1px solid rgba(88,166,255,0.2); }
  .stat-category:first-child { margin-top: 4px; }
  .stat-bar { height: 6px; border-radius: 3px; background: var(--border); overflow: hidden; margin-top: 4px; }
  .stat-bar-fill { height: 100%; border-radius: 3px; transition: width 0.3s; }
  .stat-bar-fill.bar-green { background: var(--green); }
  .stat-bar-fill.bar-red { background: var(--red); }
  .stat-bar-fill.bar-blue { background: var(--blue); }
  .stat-mini-chart { width: 100%; height: 60px; margin-top: 6px; }
  .opp-stat { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 8px; }
  .opp-stat .opp-pill { font-size: 11px; padding: 3px 8px; border-radius: 6px;
    background: var(--bg); border: 1px solid var(--border); }
  .opp-stat .opp-pill strong { font-family: monospace; }
  /* ── Stats Hub Navigation ── */
  .stats-nav-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .stats-nav-card { background: var(--card); border: 1px solid var(--border); border-radius: 10px;
    padding: 12px; cursor: pointer; -webkit-tap-highlight-color: transparent; transition: border-color 0.15s; }
  .stats-nav-card:active { opacity: 0.8; }
  .stats-nav-card:hover { border-color: var(--blue); }
  .snc-icon { color: var(--blue); margin-bottom: 6px; }
  .snc-title { font-size: 13px; font-weight: 700; margin-bottom: 2px; }
  .snc-desc { font-size: 10px; color: var(--dim); line-height: 1.3; }
  .snc-preview { font-size: 11px; font-family: monospace; margin-top: 6px; min-height: 14px; }

  /* ── Stats Sub-page ── */
  .stats-sub-header { display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 12px; padding: 0; }
  .stats-back-btn { background: none; border: 1px solid var(--border); border-radius: 6px;
    padding: 4px 10px; font-size: 12px; color: var(--blue); cursor: pointer; display: flex;
    align-items: center; gap: 4px; -webkit-tap-highlight-color: transparent; }
  .stats-back-btn:active { opacity: 0.7; }
  .stats-sub-header h3 { font-size: 14px; font-weight: 700; margin: 0; }
  .stats-csv-btn { background: none; border: 1px solid var(--border); border-radius: 6px;
    padding: 3px 8px; font-size: 10px; color: var(--dim); cursor: pointer;
    -webkit-tap-highlight-color: transparent; }
  .stats-csv-btn:active { opacity: 0.7; }

  /* ── Stats action buttons (run tests etc) ── */
  .stats-action-btn { background: none; border: 1px solid var(--border); border-radius: 6px;
    padding: 6px 12px; font-size: 11px; color: var(--dim); cursor: pointer;
    -webkit-tap-highlight-color: transparent; display: inline-flex; align-items: center; gap: 4px; }
  .stats-action-btn:active { opacity: 0.7; }
  .stats-action-btn.running { color: var(--yellow); border-color: var(--yellow); pointer-events: none; }

  /* ── Validation & Execution page ── */
  .val-readiness { text-align: center; padding: 16px 0 12px; }
  .val-ring { position: relative; width: 100px; height: 100px; margin: 0 auto; }
  .val-ring svg { transform: rotate(-90deg); }
  .val-ring-label { position: absolute; inset: 0; display: flex; align-items: center;
    justify-content: center; font-size: 28px; font-weight: 700; font-family: monospace; }
  .val-verdict { font-size: 14px; font-weight: 700; margin-top: 6px; }
  .val-sub { font-size: 11px; color: var(--dim); margin-top: 2px; }

  .val-test-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
    margin-bottom: 8px; overflow: hidden; }
  .val-test-card.collapsed .val-test-body { display: none; }
  .val-test-card.collapsed .vtc-arrow { transform: rotate(-90deg); }
  .val-test-header { padding: 10px 12px; cursor: pointer; display: flex;
    justify-content: space-between; align-items: center;
    -webkit-tap-highlight-color: transparent; }
  .val-test-header:active { opacity: 0.7; }
  .val-test-title { font-size: 12px; font-weight: 600; }
  .vtc-arrow { font-size: 10px; color: var(--dim); transition: transform 0.2s; }
  .val-test-body { padding: 0 12px 12px; }

  .val-test-badge { display: inline-block; font-size: 9px; font-weight: 700; padding: 1px 6px;
    border-radius: 3px; text-transform: uppercase; letter-spacing: 0.5px; line-height: 1.6; }
  .val-test-badge.pass { background: rgba(63,185,80,0.12); color: var(--green); }
  .val-test-badge.fail { background: rgba(248,81,73,0.12); color: var(--red); }
  .val-test-badge.ready { background: rgba(88,166,255,0.12); color: var(--blue); }
  .val-test-badge.nodata { background: rgba(139,148,158,0.12); color: var(--dim); }

  .val-desc { font-size: 11px; color: var(--dim); line-height: 1.5; margin-bottom: 10px; }

  .val-req-bar { margin: 8px 0; }
  .val-req-label { display: flex; justify-content: space-between; font-size: 10px;
    color: var(--dim); margin-bottom: 3px; }
  .val-req-track { height: 4px; background: var(--border); border-radius: 2px; overflow: hidden; }
  .val-req-fill { height: 100%; border-radius: 2px; transition: width 0.3s ease; }

  .val-run-btn { display: flex; align-items: center; justify-content: center; gap: 6px;
    width: 100%; padding: 8px; font-size: 12px; font-weight: 600; border: 1px solid var(--border);
    border-radius: 6px; background: none; color: var(--text); cursor: pointer;
    -webkit-tap-highlight-color: transparent; }
  .val-run-btn:active { opacity: 0.7; }
  .val-run-btn:disabled { opacity: 0.4; cursor: default; color: var(--dim); }
  .val-run-btn.running { color: var(--yellow); border-color: var(--yellow); pointer-events: none; }

  .val-progress-bar { height: 3px; background: var(--border); border-radius: 2px;
    overflow: hidden; margin: 8px 0; }
  .val-progress-fill { height: 100%; border-radius: 2px; background: var(--yellow); }
  .val-progress-fill.indeterminate { width: 40%; animation: valProg 1.2s ease-in-out infinite; }
  @keyframes valProg { 0% { transform: translateX(-100%); } 100% { transform: translateX(350%); } }

  .val-result { margin-top: 10px; }
  .val-result-verdict { font-size: 16px; font-weight: 700; margin-bottom: 4px; }
  .val-result-detail { font-size: 11px; color: var(--dim); line-height: 1.5; }
  .val-result-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(70px, 1fr));
    gap: 6px; margin-top: 10px; }
  .val-result-stat { text-align: center; padding: 6px 4px; background: var(--bg);
    border-radius: 6px; }
  .vrs-val { font-size: 14px; font-weight: 700; font-family: monospace; }
  .vrs-label { font-size: 8px; color: var(--dim); text-transform: uppercase;
    letter-spacing: 0.3px; margin-top: 1px; }

  .val-checklist { display: flex; flex-direction: column; gap: 6px; }
  .val-check-item { display: flex; align-items: flex-start; gap: 8px; }
  .val-check-icon { font-size: 12px; font-weight: 700; width: 18px; height: 18px;
    display: flex; align-items: center; justify-content: center; border-radius: 50%;
    flex-shrink: 0; }
  .val-check-icon.pass { background: rgba(63,185,80,0.12); color: var(--green); }
  .val-check-icon.fail { background: rgba(248,81,73,0.12); color: var(--red); }

  @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }

  .tab-btn svg { width: 26px; height: 26px; }
                  cursor: pointer; transition: filter 0.15s; -webkit-tap-highlight-color: transparent; }
    background: var(--card); border-color: var(--border); }
  .delete-btn { background: transparent; background-color: transparent;
                border: none; cursor: pointer; padding: 2px;
                opacity: 0.35; transition: opacity 0.15s;
                -webkit-appearance: none; appearance: none;
                outline: none; box-shadow: none; }
  .delete-btn:hover { opacity: 1; }
  .delete-btn svg { width: 16px; height: 16px; stroke: var(--dim); }
  .delete-btn:hover svg { stroke: var(--red); }
  .mini-chart { width: 100%; height: 180px; margin: 8px 0; border-radius: 4px;
                background: var(--bg); border: 1px solid var(--border); }
  .risk-action-row { display: flex; align-items: center; justify-content: space-between;
    padding: 6px 0; }
  .risk-label { font-size: 12px; font-weight: 600; min-width: 80px; }
  .action-btns { display: flex; gap: 5px; }
  .abtn { background: none; border: 1px solid var(--border); border-radius: 4px;
    padding: 4px 10px; font-size: 10px; font-weight: 600; cursor: pointer;
    color: var(--dim); letter-spacing: 0.3px; text-transform: uppercase;
    -webkit-tap-highlight-color: transparent; transition: all 0.15s ease; }
  .abtn:active { filter: brightness(1.3); }
  .abtn[data-action="normal"].abtn-active {
    background: rgba(63,185,80,0.12); color: var(--green); border-color: rgba(63,185,80,0.35); }
  .abtn[data-action="skip"].abtn-active {
    background: rgba(248,81,73,0.12); color: var(--red); border-color: rgba(248,81,73,0.35); }
  .regime-action-sel { background: var(--bg); border: 1px solid var(--border); border-radius: 4px;
    color: var(--text); font-size: 11px; padding: 3px 6px; -webkit-appearance: none; }
  /* Quick-Trade regime row */
  .qt-row { cursor: pointer; -webkit-tap-highlight-color: transparent; transition: all 0.15s; }
  .qt-row:active { filter: brightness(1.15); }
  .qt-row.qt-selected { background: rgba(88,166,255,0.12) !important;
    border-left: 3px solid var(--blue) !important; }
  .qt-row .qt-badge { display: none; font-size: 9px; font-weight: 700; color: var(--blue);
    letter-spacing: 0.5px; }
  .qt-row.qt-selected .qt-badge { display: inline; }
  /* Quick-Trade / Auto-Fill banners */
  .override-banner { padding: 8px 10px; border-radius: 6px; font-size: 11px;
    display: flex; justify-content: space-between; align-items: center; gap: 8px;
    margin-bottom: 8px; animation: bannerIn 0.2s ease; }
  .override-banner-qt { background: rgba(88,166,255,0.08); border: 1px solid rgba(88,166,255,0.25);
    color: var(--blue); }
  .override-banner-af { background: rgba(163,113,247,0.08); border: 1px solid rgba(163,113,247,0.25);
    color: #a371f7; }
  .override-banner .ob-clear { background: none; border: 1px solid currentColor; border-radius: 4px;
    padding: 2px 8px; font-size: 10px; font-weight: 600; color: inherit; cursor: pointer;
    -webkit-tap-highlight-color: transparent; white-space: nowrap; }
  .override-banner .ob-clear:active { filter: brightness(1.3); }
  @keyframes bannerIn { from { opacity:0; transform:translateY(-4px); } to { opacity:1; transform:none; } }
  /* Locked controls */
  .qt-locked { opacity: 0.45; pointer-events: none; position: relative; }
  .qt-locked::after { content: ''; position: absolute; inset: 0; cursor: not-allowed; pointer-events: all; }
  .af-locked select, .af-locked input { border-color: rgba(163,113,247,0.35) !important;
    background: rgba(163,113,247,0.06) !important; pointer-events: none; }
  .af-locked { position: relative; }
  /* Best strategy tappable row in regime detail */
  .best-strat-row { cursor: pointer; -webkit-tap-highlight-color: transparent; transition: all 0.15s; }
  .best-strat-row:active { filter: brightness(1.15); }
  .best-strat-row.af-active { border-color: rgba(163,113,247,0.5) !important;
    background: rgba(163,113,247,0.08) !important; }
  /* Regime action selects: colored by value */
  .regime-action-sel.ras-trade { color: var(--green); border-color: rgba(63,185,80,0.35);
    background: rgba(63,185,80,0.06); }
  .regime-action-sel.ras-skip { color: var(--red); border-color: rgba(248,81,73,0.35);
    background: rgba(248,81,73,0.06); }
  .regime-action-sel.ras-auto { color: var(--blue); border-color: rgba(88,166,255,0.35);
    background: rgba(88,166,255,0.06); }
  .regime-action-sel.ras-inherit { color: var(--blue); border-color: rgba(88,166,255,0.25); }
  .regime-action-sel.ras-custom { color: var(--yellow); border-color: rgba(210,153,34,0.35);
    background: rgba(210,153,34,0.06); }
  .regime-action-sel { text-align: center; text-align-last: center; }
  .chat-dl-btn { display: inline-flex; align-items: center; gap: 6px;
    background: rgba(88,166,255,0.08); color: var(--blue); border: 1px solid rgba(88,166,255,0.3); border-radius: 8px;
    padding: 10px 16px; font-size: 13px; font-weight: 600; cursor: pointer;
    -webkit-tap-highlight-color: transparent; text-decoration: none; margin-top: 6px; }
  .chat-dl-btn svg { width: 18px; height: 18px; }
  #imgLightbox { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.95); z-index: 200; align-items: center; justify-content: center;
    flex-direction: column; padding: 60px 16px 30px; }
  #imgLightbox img { max-width: 100%; max-height: 100%; object-fit: contain; border-radius: 6px; }
  #imgLightbox .lb-close { position: absolute; top: 12px; right: 16px; background: none;
    border: none; color: #fff; font-size: 28px; cursor: pointer; padding: 8px;
    -webkit-tap-highlight-color: transparent; z-index: 201; }
  #imgLightbox .lb-dl { position: absolute; bottom: 30px; left: 50%; transform: translateX(-50%); }

  /* Skeleton loading */
  @keyframes shimmer {
    0% { background-position: -200px 0; }
    100% { background-position: 200px 0; }
  }
  .skel { background: linear-gradient(90deg, var(--border) 25%, rgba(48,54,61,0.6) 50%, var(--border) 75%);
    background-size: 400px 100%; animation: shimmer 1.5s ease-in-out infinite;
    border-radius: 4px; }
  .skel-line { height: 12px; margin-bottom: 8px; }
  .skel-line-sm { height: 8px; margin-bottom: 6px; }
  .skel-line-lg { height: 18px; margin-bottom: 10px; }
  .skel-block { border-radius: 8px; }
  .skel-circle { border-radius: 50%; }
  .skel-wrap { transition: opacity 0.3s ease; }
  .skel-wrap.skel-hidden { opacity: 0; pointer-events: none; position: absolute; }

</style>
</head>
<body>

<!-- Sticky Header -->
<div id="stickyHeader">
  <div class="hdr-row">
    <div style="display:flex;align-items:center;gap:6px;flex:1;min-width:0;overflow:hidden">
      <span class="status-dot" id="statusDot"></span>
      <strong id="statusText" style="font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">Loading...</strong>
    </div>
    <div id="hdrBankroll" onclick="openBankrollModal()" style="display:flex;align-items:center;gap:6px;font-family:monospace;font-size:13px;cursor:pointer;color:var(--text);background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:4px 10px;-webkit-tap-highlight-color:transparent;flex-shrink:0;margin-left:8px">
      <span id="hdrBal" style="font-weight:700;font-size:15px">—</span>
    </div>
  </div>
  <!-- Mode Selector Strip -->
  <div class="mode-strip" id="modeStrip">
    <div class="mode-btn" data-mode="observe" onclick="setTradingMode('observe')">
      <span class="mode-icon">◉</span>Observe</div>
    <div class="mode-btn" data-mode="shadow" onclick="setTradingMode('shadow')">
      <span class="mode-icon">◈</span>Shadow</div>
    <div class="mode-btn" data-mode="hybrid" onclick="setTradingMode('hybrid')">
      <span class="mode-icon">⬡</span>Hybrid</div>
    <div class="mode-btn" data-mode="auto" onclick="setTradingMode('auto')">
      <span class="mode-icon">◎</span>Auto</div>
    <div class="mode-btn" data-mode="manual" onclick="setTradingMode('manual')">
      <span class="mode-icon">▣</span>Manual</div>
  </div>
</div>
<div class="bot-offline-banner" id="offlineBanner">
  <span class="offline-dot"></span><span id="offlineText">Bot Offline</span>
</div>
<div id="contentWrap">

<!-- Bankroll Modal -->
<div class="confirm-overlay" id="bankrollModal" style="display:none">
  <div class="modal-panel" style="max-width:420px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <h3 style="color:var(--blue);font-size:14px;margin:0">Bankroll</h3>
      <button onclick="closeModal('bankrollModal')" style="background:none;border:none;color:var(--dim);font-size:20px;cursor:pointer;padding:10px;margin:-6px;-webkit-tap-highlight-color:transparent"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
    </div>

    <!-- Main balances -->
    <div style="text-align:center;margin-bottom:12px">
      <div style="font-size:28px;font-weight:700;font-family:monospace;color:var(--text)" id="bkmEffective">$0.00</div>
      <div class="dim" style="font-size:12px">Balance</div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px">
      <div style="text-align:center;padding:8px;background:var(--bg);border-radius:6px">
        <div style="font-size:16px;font-weight:600;font-family:monospace" id="bkmInTrade">$0.00</div>
        <div class="dim" style="font-size:10px">In Trade</div>
      </div>
      <div style="text-align:center;padding:8px;background:var(--bg);border-radius:6px">
        <div style="font-size:16px;font-weight:600;font-family:monospace" id="bkmTotal">$0.00</div>
        <div class="dim" style="font-size:10px">Kalshi Total</div>
      </div>
    </div>

    <!-- P&L -->
    <div style="padding:8px;background:var(--bg);border-radius:6px;margin-bottom:12px">
      <div class="dim" style="font-size:10px;margin-bottom:2px">Lifetime P&L</div>
      <div style="font-size:16px;font-weight:600;font-family:monospace" id="bkmLifetimePnl">$0.00</div>
      <div class="dim" style="font-size:10px" id="bkmLifetimeStats">0W–0L</div>
    </div>

    <!-- Trading info -->
    <!-- Bankroll info -->

    <!-- Warning -->
    <div id="bkmWarning" style="display:none;margin-bottom:12px;padding:8px;background:rgba(248,81,73,0.08);border:1px solid rgba(248,81,73,0.2);border-radius:6px;font-size:12px;color:var(--red)"></div>

    <!-- Charts -->
    <div style="margin-bottom:8px">
      <div class="dim" style="font-size:11px;margin-bottom:4px;font-weight:600">BANKROLL HISTORY</div>
      <div class="filter-chips" id="bkChartFilters" style="margin-bottom:4px">
        <button class="chip active" onclick="loadBankrollChart(null,this)">All</button>
        <button class="chip" onclick="loadBankrollChart(1,this)">1h</button>
        <button class="chip" onclick="loadBankrollChart(24,this)">1d</button>
        <button class="chip" onclick="loadBankrollChart(168,this)">1w</button>
        <button class="chip" onclick="loadBankrollChart(720,this)">30d</button>
      </div>
      <div style="position:relative">
        <canvas id="bankrollChart" style="width:100%;height:120px;background:var(--bg);border:1px solid var(--border);border-radius:4px"></canvas>
        <div id="bankrollChartLabel" style="position:absolute;top:4px;left:8px;font-size:11px;font-family:monospace;color:var(--dim);pointer-events:none;background:rgba(13,17,23,0.8);padding:1px 4px;border-radius:3px"></div>
      </div>
    </div>

    <div style="margin-bottom:12px">
      <div class="dim" style="font-size:11px;margin-bottom:4px;font-weight:600">P&L HISTORY</div>
      <div class="filter-chips" id="pnlChartFilters" style="margin-bottom:4px">
        <button class="chip active" onclick="loadPnlChart(null,this)">All</button>
        <button class="chip" onclick="loadPnlChart(1,this)">1h</button>
        <button class="chip" onclick="loadPnlChart(24,this)">1d</button>
        <button class="chip" onclick="loadPnlChart(168,this)">1w</button>
        <button class="chip" onclick="loadPnlChart(720,this)">30d</button>
      </div>
      <div style="position:relative">
        <canvas id="pnlChart" style="width:100%;height:120px;background:var(--bg);border:1px solid var(--border);border-radius:4px"></canvas>
        <div id="pnlChartLabel" style="position:absolute;top:4px;left:8px;font-size:11px;font-family:monospace;color:var(--dim);pointer-events:none;background:rgba(13,17,23,0.8);padding:1px 4px;border-radius:3px"></div>
      </div>
    </div>

    <!-- Kalshi Link -->
    <div style="border-top:1px solid var(--border);padding-top:10px;margin-top:4px">
      <a href="https://kalshi.com/portfolio" target="_blank" rel="noopener"
         class="act-btn act-btn-dim" style="display:block;text-align:center;text-decoration:none;font-size:12px;padding:10px">
        Open Kalshi
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="margin-left:3px;vertical-align:-1px"><path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6M15 3h6v6M10 14L21 3"/></svg>
      </a>
    </div>
  </div>
</div>

<!-- ═══ PAGE: HOME ═══ -->
<div id="pageHome" class="page active">

<!-- Skeleton placeholder (shown until first data load) -->
<div id="skelHome" class="skel-wrap">
  <div class="card" style="border-left:3px solid var(--border)">
    <div style="display:flex;justify-content:space-between;margin-bottom:12px">
      <div class="skel skel-line" style="width:40%"></div>
      <div class="skel skel-line" style="width:25%"></div>
    </div>
    <div class="grid2">
      <div class="skel skel-block" style="height:70px"></div>
      <div class="skel skel-block" style="height:70px"></div>
    </div>
    <div class="skel skel-block" style="height:100px;margin-top:10px"></div>
    <div style="display:flex;gap:8px;margin-top:10px">
      <div class="skel skel-line" style="width:60px"></div>
      <div class="skel skel-line" style="width:120px"></div>
    </div>
  </div>
</div>

<!-- Live Market Monitor (shown when NOT trading) -->
<div id="monitorCard" class="card" style="display:none;border-left:3px solid var(--blue)">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
    <div class="stat" style="flex:1;text-align:left">
      <div class="label">Market</div>
      <div class="val" id="monMarket" style="font-size:16px">—</div>
    </div>
    <div class="stat" style="text-align:right">
      <div class="label">Time Left</div>
      <div class="val" id="monTime" style="font-family:monospace">—</div>
    </div>
  </div>
  <!-- Prices -->
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
    <div style="text-align:center;padding:10px 8px;border-radius:8px;background:rgba(63,185,80,0.12);border:1px solid rgba(63,185,80,0.3)">
      <div class="dim" style="font-size:10px;margin-bottom:2px">YES</div>
      <div class="side-yes" style="font-size:22px;font-family:monospace" id="monYesAsk">—</div>
      <div class="dim" style="font-size:10px;margin-top:2px;font-family:monospace" id="monYesSpread"></div>
      <div style="font-size:9px;margin-top:1px;font-family:monospace;color:rgba(136,132,216,0.8);display:none" id="monYesFV"></div>
    </div>
    <div style="text-align:center;padding:10px 8px;border-radius:8px;background:rgba(248,81,73,0.12);border:1px solid rgba(248,81,73,0.3)">
      <div class="dim" style="font-size:10px;margin-bottom:2px">NO</div>
      <div class="side-no" style="font-size:22px;font-family:monospace" id="monNoAsk">—</div>
      <div class="dim" style="font-size:10px;margin-top:2px;font-family:monospace" id="monNoSpread"></div>
      <div style="font-size:9px;margin-top:1px;font-family:monospace;color:rgba(136,132,216,0.8);display:none" id="monNoFV"></div>
    </div>
  </div>
  <!-- Live price chart -->
  <div style="position:relative">
    <canvas id="liveChart" class="mini-chart" width="600" height="180"></canvas>
    <div id="liveChartLabel" style="position:absolute;top:12px;left:8px;font-size:11px;font-family:monospace;color:var(--dim);pointer-events:none;background:rgba(13,17,23,0.8);padding:1px 4px;border-radius:3px"></div>
  </div>
  <!-- Regime -->
  <div style="display:flex;align-items:center;gap:8px;margin-top:4px">
    <span class="regime-tag" id="monRisk">—</span>
    <span style="font-size:13px;font-weight:600" id="monRegimeLabel">—</span>
  </div>
  <div id="monRegimeDrift" style="display:none;font-size:10px;margin-top:2px"></div>
  <div class="regime-detail-grid" id="monRegimeGrid" style="margin-top:6px"></div>
  <div id="monAutoStrategy" style="display:none;margin-top:4px"></div>
  <div id="monFairValue" style="display:none;margin-top:6px;padding:6px 8px;border-radius:6px;background:rgba(136,132,216,0.10);border:1px solid rgba(136,132,216,0.25)"></div>

  <!-- Shadow Trade (shown when shadow trading is active on this market) -->
  <div id="shadowTradeSection" style="display:none;margin-top:10px;border-top:1px dashed #a371f7;padding-top:10px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
      <div style="display:flex;align-items:center;gap:6px">
        <span style="font-size:10px;font-weight:700;letter-spacing:.5px;color:#a371f7;text-transform:uppercase">Shadow Trade</span>
        <span style="font-size:9px;color:var(--dim)">(1 contract · execution data)</span>
      </div>
    </div>
    <div class="grid2" style="gap:6px">
      <div class="stat" style="padding:4px"><div class="label">Side</div><div class="val" id="shadowSide" style="font-size:16px">—</div></div>
      <div class="stat" style="padding:4px"><div class="label">Fill</div><div class="val" id="shadowFill" style="font-size:16px">—</div></div>
    </div>
    <div class="grid2" style="gap:6px;margin-top:4px">
      <div class="stat" style="padding:4px"><div class="label">Slippage</div><div class="val" id="shadowSlip" style="font-size:16px">—</div></div>
      <div class="stat" style="padding:4px"><div class="label">Est P&L</div><div class="val" id="shadowPnl" style="font-size:16px">—</div></div>
    </div>
    <div id="shadowStatus" class="dim" style="font-size:11px;margin-top:6px;text-align:center"></div>
  </div>

  <!-- Simulated Trade (shown when observing, no shadow trade) -->
  <div id="simTradeSection" style="display:none;margin-top:10px;border-top:1px dashed var(--yellow);padding-top:10px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
      <div style="display:flex;align-items:center;gap:6px">
        <span style="font-size:10px;font-weight:700;letter-spacing:.5px;color:var(--yellow);text-transform:uppercase">Simulated Trade</span>
      </div>
      <button class="btn btn-dim" style="width:auto;padding:4px 10px;font-size:10px" onclick="simShuffle()">Shuffle</button>
    </div>
    <div id="simStratLabel" class="dim" style="font-size:11px;margin-bottom:6px"></div>
    <div id="simTradeBody">
      <div class="grid2" style="gap:6px">
        <div class="stat" style="padding:4px"><div class="label">Side</div><div class="val" id="simSide" style="font-size:16px">—</div></div>
        <div class="stat" style="padding:4px"><div class="label">Entry</div><div class="val" id="simEntry" style="font-size:16px">—</div></div>
      </div>
      <div class="grid2" style="gap:6px;margin-top:4px">
        <div class="stat" style="padding:4px"><div class="label">Sell Target</div><div class="val" id="simSell" style="font-size:16px">—</div></div>
        <div class="stat" style="padding:4px"><div class="label">Est P&L</div><div class="val" id="simPnl" style="font-size:16px">—</div></div>
      </div>
      <div id="simStatus" class="dim" style="font-size:11px;margin-top:6px;text-align:center"></div>
    </div>
  </div>
</div>

<!-- Pending Trade (buy order waiting for fill) -->
<div class="card trade-live" id="pendingCard" style="display:none;border-left:3px solid var(--yellow)">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
    <div class="stat" style="flex:1;text-align:left">
      <div class="label">Market</div>
      <div class="val" id="pendMarket" style="font-size:16px">—</div>
    </div>
    <div class="stat" style="text-align:right">
      <div class="label">Time Left</div>
      <div class="val" id="pendTime" style="font-family:monospace">—</div>
    </div>
  </div>
  <div class="grid2">
    <div class="stat">
      <div class="label">Side / Price</div>
      <div class="val" id="pendSide">—</div>
    </div>
    <div class="stat">
      <div class="label">Fill Progress</div>
      <div class="val" id="pendFills" style="font-family:monospace">0/0</div>
    </div>
    <div class="stat">
      <div class="label">Cost So Far</div>
      <div class="val" id="pendCost">$0.00</div>
    </div>
  </div>
  <div class="progress-bar" style="margin-top:6px">
    <div class="progress-fill" id="pendProgress" style="width:0%;background:var(--yellow)"></div>
  </div>
  <!-- Pending price chart -->
  <div style="position:relative">
    <canvas id="pendChart" class="mini-chart" width="600" height="180"></canvas>
    <div id="pendChartLabel" style="position:absolute;top:12px;left:8px;font-size:11px;font-family:monospace;color:var(--dim);pointer-events:none;background:rgba(13,17,23,0.8);padding:1px 4px;border-radius:3px"></div>
  </div>
  <div style="margin-top:10px">
    <div class="input-row">
      <label>Preset Sell</label>
      <input type="number" id="pendSellPrice" min="2" max="99" placeholder="e.g. 85" style="width:60px">
      <span class="dim">¢</span>
    </div>
    <div class="dim" style="font-size:11px;margin-top:4px" id="pendSellInfo"></div>
  </div>
  <div style="margin-top:10px">
  </div>
</div>

<!-- Active Trade (shown when trading) -->
<div class="card trade-live" id="tradeCard" style="display:none">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
    <div class="stat" style="flex:1;text-align:left">
      <div class="label">Market</div>
      <div class="val" id="tradeMarket" style="font-size:16px">—</div>
    </div>
    <div class="stat" style="text-align:right">
      <div class="label">Time Left</div>
      <div class="val" id="tradeTime" style="font-family:monospace">—</div>
    </div>
  </div>
  <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
    <span class="regime-tag" id="tradeRisk">—</span>
    <span style="font-size:13px;font-weight:600" id="tradeRegimeLabel">—</span>
    <span class="dim" id="tradeRegimeStats"></span>
    <div id="tradeModelEdge" style="font-size:11px;margin-top:2px;display:none"></div>
    <div id="tradeDynamicSell" style="font-size:11px;margin-top:2px;display:none"></div>
  </div>
  <div id="tradeAutoStrategy" style="display:none;margin-bottom:6px"></div>
  <!-- Mini price chart -->
  <div style="position:relative">
    <canvas id="priceChart" class="mini-chart" width="600" height="180"></canvas>
    <div id="priceChartLabel" style="position:absolute;top:12px;left:8px;font-size:11px;font-family:monospace;color:var(--dim);pointer-events:none;background:rgba(13,17,23,0.8);padding:1px 4px;border-radius:3px"></div>
  </div>
  <div class="grid2">
    <div class="stat">
      <div class="label">Side / Entry</div>
      <div class="val" id="tradeSide">—</div>
    </div>
    <div class="stat">
      <div class="label">Current Bid</div>
      <div class="val price-display" id="tradeBid">—</div>
    </div>
    <div class="stat">
      <div class="label">Sell Target</div>
      <div class="val" id="tradeSell">—</div>
    </div>
    <div class="stat">
      <div class="label">Cost</div>
      <div class="val" id="tradeCost">—</div>
    </div>
    <div class="stat">
      <div class="label">HWM</div>
      <div class="val" id="tradeHwm">—</div>
    </div>
    <div class="stat">
      <div class="label">Shares</div>
      <div class="val" id="tradeShares">—</div>
    </div>
  </div>
  <div style="margin:8px 0 4px;display:flex;align-items:center;gap:8px">
    <span class="dim" style="font-size:11px;white-space:nowrap">Win est.</span>
    <div style="flex:1;height:6px;background:var(--border);border-radius:3px;overflow:hidden;position:relative">
      <div id="winProbBar" style="height:100%;border-radius:3px;transition:width 0.5s,background 0.3s;width:0%"></div>
    </div>
    <span id="winProbPct" style="font-size:13px;font-weight:700;font-family:monospace;min-width:38px;text-align:right">—</span>
  </div>
  <div class="progress-bar">
    <div class="progress-fill" id="tradeProgress" style="width:0%;background:var(--blue)"></div>
  </div>
  <div class="dim" style="margin-top:4px">
    <span id="sellProgress">0/0 sold</span> ·
    Est. P&L: <span id="tradeEstPnl">$0.00</span> ·
    <span class="dim" id="tradeSpread"></span>
  </div>
  <div class="dim" style="margin-top:2px;font-size:11px">
    <span id="tradeBankInfo"></span>
  </div>

  <span class="detail-toggle" onclick="toggleDetail('tradeDetail')">▸ More details</span>
  <div class="detail-section" id="tradeDetail">
    <div class="regime-detail-grid" id="tradeRegimeGrid"></div>
    <div class="grid2" style="font-size:12px;margin-top:8px;padding-top:8px;border-top:1px solid var(--border)">
      <!-- Trade detail fields -->
    </div>
  </div>

  <!-- Stopping banner -->
  <div id="stoppingBanner" style="display:none;margin-top:8px;padding:8px;border-radius:6px;background:rgba(248,81,73,0.08);border:1px solid rgba(248,81,73,0.3);text-align:center;font-size:12px;color:var(--red)">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px;margin-right:4px"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>Stopped — trade kept as ignored, sell order active
  </div>

</div>

<!-- Session Stats -->

</div> <!-- end pageHome -->

<!-- ═══ PAGE: STATS ═══ -->
<div id="pageStats" class="page" style="padding:0 16px">

  <!-- ── STATS HUB (main view) ── -->
  <div id="statsHub">
    <!-- Summary Cards -->
    <div id="statsSummaryCards" class="stat-summary-grid">
      <div class="stat-summary-card"><div class="ssc-val" id="ssWinRate">—</div><div class="ssc-label">Win Rate</div></div>
      <div class="stat-summary-card"><div class="ssc-val" id="ssTotalPnl">—</div><div class="ssc-label">Total P&L</div></div>
      <div class="stat-summary-card"><div class="ssc-val" id="ssROI">—</div><div class="ssc-label">ROI</div></div>
      <div class="stat-summary-card"><div class="ssc-val" id="ssProfitFactor">—</div><div class="ssc-label">Profit Factor</div></div>
    </div>

    <!-- Navigation Grid -->
    <div class="stats-nav-grid">
      <div class="stats-nav-card" onclick="statsNavTo('performance')">
        <div class="snc-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg></div>
        <div class="snc-title">Performance</div>
        <div class="snc-desc">Record, streaks, P&L, daily & hourly stats</div>
        <div class="snc-preview" id="hubPerfPreview"></div>
      </div>
      <div class="stats-nav-card" onclick="statsNavTo('conditions')">
        <div class="snc-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2v20M2 12h20"/><circle cx="12" cy="12" r="10"/></svg></div>
        <div class="snc-title">Market Conditions</div>
        <div class="snc-desc">Entry price, spread, BTC move, volatility</div>
        <div class="snc-preview" id="hubCondPreview"></div>
      </div>
      <div class="stats-nav-card" onclick="statsNavTo('regimes')">
        <div class="snc-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg></div>
        <div class="snc-title">Regime Analysis</div>
        <div class="snc-desc">Leaderboard, stability, fine vs coarse</div>
        <div class="snc-preview" id="hubRegimePreview"></div>
      </div>
      <div class="stats-nav-card" onclick="statsNavTo('shadow')">
        <div class="snc-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10"/></svg></div>
        <div class="snc-title">Shadow Trading</div>
        <div class="snc-desc">Real execution data, fills, slippage, outcomes</div>
        <div class="snc-preview" id="hubShadowPreview"></div>
      </div>
    </div>
    <div style="margin-top:12px;text-align:center">
      <button onclick="exportAIData(this)" style="background:rgba(88,166,255,0.08);border:1px solid rgba(88,166,255,0.3);border-radius:8px;color:var(--blue);cursor:pointer;padding:8px 16px;font-size:12px;font-weight:600;-webkit-tap-highlight-color:transparent;display:inline-flex;align-items:center;gap:6px">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/><polyline points="16 6 12 2 8 6"/><line x1="12" y1="2" x2="12" y2="15"/></svg>
        Export Data for AI Analysis
      </button>
    </div>
    <div style="height:20px"></div>
  </div>

  <!-- ── STATS SUB-PAGE (shown when navigated into a section) ── -->
  <div id="statsSubPage" style="display:none">
    <div class="stats-sub-header">
      <button class="stats-back-btn" onclick="statsGoBack()">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>
        Stats
      </button>
      <div style="display:flex;align-items:center;gap:6px">
        <h3 id="statsSubTitle"></h3>
        <button id="statsSubCsvBtn" class="stats-csv-btn" style="display:none" onclick="_statsExportCsv()">
          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-1px;margin-right:2px"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>CSV
        </button>
      </div>
    </div>
    <div id="statsSubContent"><div class="dim">Loading...</div></div>
    <div style="height:20px"></div>
  </div>

</div>

<!-- ═══ PAGE: SETTINGS ═══ -->
<div id="pageSettings" class="page" style="padding:0 12px">

  <!-- ─── TRADING ─── -->
  <div class="settings-card">
    <div class="sc-title">TRADING</div>

    <div class="sc-sub">STRATEGY</div>
    <div class="sc-hint" style="margin-bottom:6px;font-size:10px">Pick a strategy from the Observatory simulations. Model side uses the BTC fair value engine to pick the highest-edge side. Favored buys whichever side the market prices higher.</div>
    <div id="strategyPickerGrid" style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:6px;margin-bottom:8px">
      <div>
        <div class="dim" style="font-size:10px;margin-bottom:2px">Side</div>
        <select id="strategySide" onchange="_applyStrategyPicker()" style="font-size:12px;padding:4px;width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px">
          <option value="cheaper">Cheaper</option>
          <option value="model">Model</option>
          <option value="yes">YES</option>
          <option value="no">NO</option>
        </select>
      </div>
      <div>
        <div class="dim" style="font-size:10px;margin-bottom:2px">Timing</div>
        <select id="strategyTiming" onchange="_applyStrategyPicker()" style="font-size:12px;padding:4px;width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px">
          <option value="early">Early</option>
          <option value="mid">Mid</option>
          <option value="late">Late</option>
        </select>
      </div>
      <div>
        <div class="dim" style="font-size:10px;margin-bottom:2px" id="entryLabel">Buy ≤</div>
        <select id="strategyEntry" onchange="_applyStrategyPicker()" style="font-size:12px;padding:4px;width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px">
        </select>
      </div>
      <div>
        <div class="dim" style="font-size:10px;margin-bottom:2px">Sell @</div>
        <select id="strategySell" onchange="_applyStrategyPicker()" style="font-size:12px;padding:4px;width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px">
        </select>
      </div>
    </div>
    <div id="strategyKeyDisplay" style="font-size:10px;font-family:monospace;color:var(--dim);margin-bottom:4px;padding:4px 6px;background:rgba(48,54,61,0.3);border-radius:4px"></div>
    <div id="modelSuggestion" style="display:none;font-size:10px;padding:4px 6px;margin-bottom:8px;border-radius:4px;background:rgba(136,132,216,0.08);border:1px solid rgba(136,132,216,0.15)"></div>
    <div id="modelSideWarning" style="display:none;font-size:10px;padding:4px 6px;margin-bottom:4px;border-radius:4px;background:rgba(210,153,34,0.08);border:1px solid rgba(210,153,34,0.15);color:var(--yellow)">⚠ Model side is not validated by the Strategy Observatory. Strategy risk data will show as unknown.</div>
    <div id="modelEdgeRow" class="input-row" style="display:none">
      <label>Min Model Edge %</label>
      <input type="number" id="minModelEdge" step="0.5" min="0" max="25" value="3" style="width:60px"
             onchange="saveSetting('min_model_edge_pct',parseFloat(this.value))">
      <span class="dim" style="font-size:10px">Only enter when model edge ≥ this</span>
    </div>

    <!-- Per-Regime Breakdown (from Observatory sims) -->
    <div id="regimePreviewCard" style="margin-bottom:8px">
      <div onclick="document.getElementById('regimePreviewBody').style.display=document.getElementById('regimePreviewBody').style.display==='none'?'':'none';this.querySelector('.chevron').textContent=document.getElementById('regimePreviewBody').style.display==='none'?'▸':'▾'" style="cursor:pointer;font-size:11px;font-weight:600;color:var(--dim);padding:4px 0;display:flex;align-items:center;gap:4px">
        <span class="chevron">▸</span> Per-Regime Breakdown
        <span id="regimePreviewCount" style="font-weight:400;font-size:10px;color:var(--dim)"></span>
      </div>
      <div id="regimePreviewBody" style="display:none">
        <div id="regimePreviewContent" style="font-size:11px">
          <div class="dim" style="font-size:10px;padding:4px 0">Change strategy parameters above to see per-regime data from Observatory simulations and real trades. Needs ≥10 samples to classify risk.</div>
        </div>
      </div>
    </div>

    <div class="input-row">
      <label>Bet Mode</label>
      <select id="betMode" onchange="_onBetModeChange(this.value)">
        <option value="flat">Flat $</option>
        <option value="percent">% Bankroll</option>
        <option value="edge_scaled">Edge Scaled</option>
      </select>
    </div>
    <div class="input-row">
      <label>Bet Size</label>
      <input type="number" id="betSize" step="1" min="1" style="width:70px"
             onchange="saveSetting('bet_size',parseFloat(this.value))">
      <span class="dim" id="betSizeHint" style="font-size:10px">$ per trade</span>
    </div>

    <!-- Edge Scaled settings (shown when bet mode = edge_scaled) -->
    <div id="edgeScaledSettings" style="display:none;margin-top:4px;padding:8px;background:rgba(48,54,61,0.3);border-radius:6px">
      <div class="dim" style="font-size:10px;margin-bottom:6px">Scale bet size by FV model edge. Base bet × tier multiplier.</div>
      <div id="edgeTiersDisplay"></div>
      <div style="display:flex;gap:4px;margin-top:6px">
        <input type="number" id="newTierEdge" placeholder="Min edge %" step="1" min="0" max="30" style="width:80px;font-size:11px">
        <input type="number" id="newTierMult" placeholder="Multiplier" step="0.1" min="0.1" max="5" style="width:80px;font-size:11px">
        <button class="btn btn-dim" style="font-size:10px;padding:2px 8px" onclick="_addEdgeTier()">Add</button>
      </div>
    </div>

    <div class="sc-sub">LOSS PROTECTION</div>
    <div class="input-row" style="margin-top:0">
      <label>Loss Stop</label>
      <input type="number" id="maxConsecLosses" min="0" max="20" value="0"
             style="width:60px" onchange="saveSetting('max_consecutive_losses',parseInt(this.value))">
      <span class="dim">consec. losses (0=off)</span>
    </div>
    <div class="input-row">
      <label>Cooldown</label>
      <input type="number" id="cooldownAfterLoss" min="0" max="20" value="0"
             style="width:60px" onchange="saveSetting('cooldown_after_loss_stop',parseInt(this.value))">
      <span class="dim">markets to skip after stop</span>
    </div>

    <div class="sc-sub">EXECUTION</div>
    <div class="toggle" style="margin-top:0">
      <label class="tog"><input type="checkbox" id="adaptiveEntry"
             onchange="saveSetting('adaptive_entry',this.checked)"><span class="tpill"></span></label>
      <span class="dim">Adaptive Entry — start below ask, walk up on retries</span>
    </div>
    <div class="toggle">
      <label class="tog"><input type="checkbox" id="dynamicSellEnabled"
             onchange="saveSetting('dynamic_sell_enabled',this.checked);document.getElementById('dynamicSellFloor').closest('.input-row').style.display=this.checked?'':'none'"><span class="tpill"></span></label>
      <span class="dim">Dynamic Sell — model adjusts sell target during trade</span>
    </div>
    <div class="input-row" id="dynamicSellFloorRow" style="display:none">
      <label>Min Move ¢</label>
      <input type="number" id="dynamicSellFloor" min="1" max="15" value="3" style="width:60px"
             onchange="saveSetting('dynamic_sell_floor_c',parseInt(this.value))">
      <span class="dim">min change to replace sell order</span>
    </div>
    <div class="toggle">
      <label class="tog"><input type="checkbox" id="earlyExitEv"
             onchange="saveSetting('early_exit_ev',this.checked)"><span class="tpill"></span></label>
      <span class="dim">Early Exit — sell losing trades when holding is -EV</span>
    </div>
    <div class="input-row">
      <label>Trailing Stop</label>
      <input type="number" id="trailingStopPct" min="0" max="100" value="0" style="width:60px"
             onchange="saveSetting('trailing_stop_pct',parseInt(this.value))">
      <span class="dim">% of target progress (0=off)</span>
    </div>

    </div>

  <!-- ─── RISK & REGIME ─── -->
  <div class="settings-card">
    <div class="sc-title">RISK &amp; REGIME</div>

    <div class="sc-sub">ACTION PER RISK LEVEL</div>
    <div class="sc-hint" style="margin-bottom:8px">Risk is a composite score based on EV, confidence, OOS validation, downside, and robustness for the strategy being played. Override per-regime in Regimes tab.</div>
    <div id="riskActionGrid" style="display:flex;flex-direction:column;gap:6px">
      <div class="risk-action-row" data-risk="low">
        <span class="risk-label" style="color:var(--green)">Low</span>
        <div class="action-btns">
          <button class="abtn abtn-active" data-action="normal" onclick="setRiskAction('low','normal',this)">Trade</button>
          <button class="abtn" data-action="skip" onclick="setRiskAction('low','skip',this)">Skip</button>
        </div>
      </div>
      <div class="risk-action-row" data-risk="moderate">
        <span class="risk-label" style="color:var(--yellow)">Moderate</span>
        <div class="action-btns">
          <button class="abtn abtn-active" data-action="normal" onclick="setRiskAction('moderate','normal',this)">Trade</button>
          <button class="abtn" data-action="skip" onclick="setRiskAction('moderate','skip',this)">Skip</button>
        </div>
      </div>
      <div class="risk-action-row" data-risk="high">
        <span class="risk-label" style="color:var(--orange)">High</span>
        <div class="action-btns">
          <button class="abtn abtn-active" data-action="normal" onclick="setRiskAction('high','normal',this)">Trade</button>
          <button class="abtn" data-action="skip" onclick="setRiskAction('high','skip',this)">Skip</button>
        </div>
      </div>
      <div class="risk-action-row" data-risk="terrible">
        <span class="risk-label" style="color:var(--red)">Extreme</span>
        <div class="action-btns">
          <button class="abtn" data-action="normal" onclick="setRiskAction('terrible','normal',this)">Trade</button>
          <button class="abtn abtn-active" data-action="skip" onclick="setRiskAction('terrible','skip',this)">Skip</button>
        </div>
      </div>
      <div class="risk-action-row" data-risk="unknown">
        <span class="risk-label" style="color:var(--dim)">Unknown</span>
        <div class="action-btns">
          <button class="abtn" data-action="normal" onclick="setRiskAction('unknown','normal',this)">Trade</button>
          <button class="abtn abtn-active" data-action="skip" onclick="setRiskAction('unknown','skip',this)">Skip</button>
        </div>
      </div>
    </div>

    <div class="sc-hint" style="margin-top:6px">Per-regime filters (volatility, hour, day, side, round, stability) are set on individual regime cards in the Regimes tab.</div>
  </div>

  <!-- ─── AUTOMATION ─── -->
  <div class="settings-card">
    <div class="sc-title">AUTOMATION</div>

    <div class="sc-sub" style="margin-top:12px">AUTO-STRATEGY</div>
    <div class="sc-hint" style="margin-bottom:6px;font-size:10px">Strategy Observatory picks the highest EV strategy per regime. Active in Hybrid, Auto, and Shadow modes. These parameters control when a full-size trade is placed vs a shadow fallback.</div>
    <div id="autoStratParams">
    <div class="input-row">
      <label>Min observations</label>
      <input type="number" id="autoStrategyMinN" min="5" max="200" value="20"
             onchange="saveSetting('auto_strategy_min_samples',parseInt(this.value))">
    </div>
    <div class="input-row">
      <label>Min EV/trade</label>
      <input type="number" id="autoStrategyMinEv" min="0" max="50" step="0.5" value="0"
             onchange="saveSetting('auto_strategy_min_ev_c',parseFloat(this.value))">
      <span class="dim">¢ (0 = any positive)</span>
    </div>
    <div class="input-row">
      <label>Fee buffer</label>
      <input type="number" id="feeBuffer" min="0" max="0.1" step="0.01" value="0.03" style="width:60px"
             onchange="saveSetting('min_breakeven_fee_buffer',parseFloat(this.value))">
      <span class="dim">strategy must survive fees + this</span>
    </div>
    <div class="toggle" style="margin-top:6px">
      <label class="tog"><input type="checkbox" id="autoStratTradeAll"
             onchange="_onAutoStratTradeAllToggle(this.checked)"><span class="tpill"></span></label>
      <span class="dim">Trade all regimes — bypass risk levels and per-regime filters</span>
    </div>
    <div id="autoStratTradeAllBanner" style="display:none;font-size:10px;color:var(--blue);padding:6px 8px;margin-top:4px;background:rgba(88,166,255,0.06);border:1px solid rgba(88,166,255,0.15);border-radius:6px">All regime filters, overrides, and risk levels are bypassed. Auto-strategy's own filters (min obs, min EV, fee buffer) still apply.</div>
    </div>

    <div class="sc-sub" style="margin-top:12px">DEPLOY SAFETY</div>
    <div class="input-row" style="margin-top:0">
      <label>Deploy Cooldown</label>
      <input type="number" id="deployCooldown" min="0" max="30" value="0" style="width:60px"
             onchange="saveSetting('deploy_cooldown_minutes',parseInt(this.value))">
      <span class="dim">min after restart (0=none)</span>
    </div>

    <div class="sc-sub" style="margin-top:12px">POLLING</div>
    <div class="input-row" style="margin-top:0">
      <label>Price Poll</label>
      <input type="number" id="pricePollInterval" min="1" max="10" value="2" style="width:60px"
             onchange="saveSetting('price_poll_interval',parseInt(this.value))">
      <span class="dim">sec between price checks</span>
    </div>
    <div class="input-row">
      <label>Order Poll</label>
      <input type="number" id="orderPollInterval" min="1" max="15" value="3" style="width:60px"
             onchange="saveSetting('order_poll_interval',parseInt(this.value))">
      <span class="dim">sec between fill checks</span>
    </div>

    <div class="sc-sub" style="margin-top:12px">BOT HEALTH CHECK</div>
    <div class="toggle" style="margin-top:0">
      <label class="tog"><input type="checkbox" id="healthCheckEnabled"
             onchange="saveSetting('health_check_enabled',this.checked)"><span class="tpill"></span></label>
      <span class="dim">Enable health check</span>
    </div>
    <div class="input-row">
      <label>Timeout</label>
      <input type="number" id="healthCheckTimeout" min="1" max="60" value="5"
             onchange="saveSetting('health_check_timeout_min',parseInt(this.value))">
      <span class="dim">min of silence = alert</span>
    </div>
    <div class="sc-sub" style="margin-top:12px">TRADING MODE</div>
    <div class="sc-hint" style="margin-bottom:6px;font-size:10px">Use the mode selector at the top of the Home tab to switch between Observe, Shadow, Hybrid, Auto, and Manual modes. Auto-strategy parameters below apply to Hybrid, Auto, and Shadow modes.</div>
    <div id="settingsModeDisplay" class="dim" style="font-size:11px;margin-bottom:4px">Current: —</div>
  </div>

  <!-- ─── NOTIFICATIONS ─── -->
  <div class="settings-card">
    <div class="sc-title">NOTIFICATIONS</div>
    <div id="pushStatus" class="dim" style="margin-bottom:6px;font-size:11px">Checking...</div>
    <button class="btn btn-blue" id="pushToggleBtn" onclick="togglePush()" style="display:none;margin-bottom:8px">
      Enable Notifications
    </button>

    <div class="sc-hint" style="margin-bottom:6px">Trade events:</div>
    <div class="toggle" style="margin-top:0"><label class="tog"><input type="checkbox" id="notifyWins" checked onchange="saveSetting('push_notify_wins',this.checked)"><span class="tpill"></span></label><span class="dim">Wins</span></div>
    <div class="toggle"><label class="tog"><input type="checkbox" id="notifyLosses" checked onchange="saveSetting('push_notify_losses',this.checked)"><span class="tpill"></span></label><span class="dim">Losses</span></div>
    <div class="toggle"><label class="tog"><input type="checkbox" id="notifyBuys" onchange="saveSetting('push_notify_buys',this.checked)"><span class="tpill"></span></label><span class="dim">Buys</span></div>
    <div class="toggle"><label class="tog"><input type="checkbox" id="notifySkips" onchange="saveSetting('push_notify_observed',this.checked)"><span class="tpill"></span></label><span class="dim">Observed</span></div>

    <div class="toggle"><label class="tog"><input type="checkbox" id="notifyTradeUpdates" onchange="saveSetting('push_notify_trade_updates',this.checked)"><span class="tpill"></span></label><span class="dim">Trade updates (silent, every 1m)</span></div>

    <div class="sc-hint" style="margin-top:8px;margin-bottom:6px">System events:</div>
    <div class="toggle" style="margin-top:0"><label class="tog"><input type="checkbox" id="notifyErrors" checked onchange="saveSetting('push_notify_errors',this.checked)"><span class="tpill"></span></label><span class="dim">Errors & stops</span></div>
    <div class="toggle"><label class="tog"><input type="checkbox" id="notifyHealthCheck" onchange="saveSetting('push_notify_health_check',this.checked)" checked><span class="tpill"></span></label><span class="dim">Bot health alerts</span></div>

    <div class="sc-hint" style="margin-top:8px;margin-bottom:6px">Regime data:</div>
    <div class="toggle" style="margin-top:0"><label class="tog"><input type="checkbox" id="notifyNewRegime" onchange="saveSetting('push_notify_new_regime',this.checked)" checked><span class="tpill"></span></label><span class="dim">New regime discovered</span></div>
    <div class="toggle"><label class="tog"><input type="checkbox" id="notifyRegimeClassified" onchange="saveSetting('push_notify_regime_classified',this.checked)" checked><span class="tpill"></span></label><span class="dim">Regime risk classified</span></div>
    <div class="toggle"><label class="tog"><input type="checkbox" id="notifyStrategyDiscovery" onchange="saveSetting('push_notify_strategy_discovery',this.checked)" checked><span class="tpill"></span></label><span class="dim">Strategy discovered (+EV)</span></div>
    <div class="toggle"><label class="tog"><input type="checkbox" id="notifyGlobalBest" onchange="saveSetting('push_notify_global_best',this.checked)" checked><span class="tpill"></span></label><span class="dim">Global best strategy changed</span></div>

    <div class="sc-sub">QUIET HOURS</div>
    <div class="input-row" style="margin-top:0">
      <label>From</label>
      <input type="number" id="quietStart" min="0" max="23" value="0" style="width:50px"
             onchange="saveSetting('push_quiet_start',parseInt(this.value))">
      <span class="dim">to</span>
      <input type="number" id="quietEnd" min="0" max="23" value="0" style="width:50px"
             onchange="saveSetting('push_quiet_end',parseInt(this.value))">
      <span class="dim">CT (0-0 = off)</span>
    </div>

    <button onclick="showPushLog()" style="background:none;border:1px solid var(--border);border-radius:6px;padding:4px 10px;font-size:11px;color:var(--dim);cursor:pointer;margin-top:10px;-webkit-tap-highlight-color:transparent">
      Notification History
    </button>
  </div>

  <!-- ─── SECURITY ─── -->
  <div class="settings-card">
    <div class="sc-title">SECURITY</div>
    <div class="sc-sub">CHANGE PASSWORD</div>
    <div style="display:grid;grid-template-columns:1fr;gap:6px;margin-bottom:10px">
      <input type="password" id="secOldPass" placeholder="Current password" style="font-size:14px;padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px">
      <input type="password" id="secNewPass" placeholder="New password" style="font-size:14px;padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px">
      <input type="password" id="secConfPass" placeholder="Confirm new password" style="font-size:14px;padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px">
      <button class="act-btn act-btn-blue" onclick="_secChangePass()">Update Password</button>
    </div>
    <div class="sc-sub" style="margin-top:12px">SESSION CONTROL</div>
    <div class="sc-hint" style="margin-bottom:6px;font-size:10px">Invalidate all sessions everywhere. You stay logged in.</div>
    <button class="act-btn act-btn-yellow" onclick="_secInvalidate()">Invalidate All Sessions</button>
    <div style="margin-top:10px"><button class="act-btn act-btn-dim" onclick="_secLogout()">Log Out</button></div>
  </div>

  <!-- ─── SERVER RESOURCES ─── -->
  <div class="settings-card">
    <div class="sc-title" style="display:flex;justify-content:space-between;align-items:center">
      SERVER
      <span id="srvUptime" class="dim" style="font-size:10px;font-weight:400"></span>
    </div>
    <div id="srvStats" style="font-size:12px">
      <div class="dim" style="text-align:center;padding:8px">Loading...</div>
    </div>
  </div>

  <!-- ─── DEPLOY & CONTROL ─── -->
  <div class="settings-card">
    <div class="sc-title">SERVICES</div>

    <div id="svcStatus" style="font-size:11px;margin-bottom:10px">
      <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--border)">
        <div><strong>Trading Bot</strong> <span id="svcBotStatus" class="dim">—</span></div>
        <div style="display:flex;gap:4px">
          <button class="btn btn-dim" style="font-size:10px;padding:3px 8px" onclick="svcControl('start','plugin-btc-15m')">Start</button>
          <button class="btn btn-dim" style="font-size:10px;padding:3px 8px" onclick="svcControl('stop','plugin-btc-15m')">Stop</button>
          <button class="btn btn-dim" style="font-size:10px;padding:3px 8px" onclick="svcControl('restart','plugin-btc-15m')">Restart</button>
        </div>
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0">
        <div><strong>Dashboard</strong> <span id="svcDashStatus" class="dim">—</span></div>
        <div style="display:flex;gap:4px">
          <button class="btn btn-dim" style="font-size:10px;padding:3px 8px" onclick="svcControl('start','platform-dashboard')">Start</button>
          <button class="btn btn-dim" style="font-size:10px;padding:3px 8px" onclick="svcControl('stop','platform-dashboard')">Stop</button>
          <button class="btn btn-dim" style="font-size:10px;padding:3px 8px" onclick="svcControl('restart','platform-dashboard')">Restart</button>
        </div>
      </div>
    </div>
    <button class="btn btn-blue" style="width:100%;margin-bottom:8px" onclick="svcControl('restart','all')">Restart All Services</button>

    <div class="sc-sub">DEPLOY CODE</div>
    <div style="display:flex;gap:8px;align-items:center">
      <label style="flex:1;display:flex;align-items:center;justify-content:center;gap:6px;padding:12px;background:var(--bg);border:1px dashed var(--border);border-radius:6px;cursor:pointer;font-size:13px;color:var(--dim);-webkit-tap-highlight-color:transparent">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M17 8l-5-5-5 5M12 3v12"/></svg>
        <span id="deployFileLabel">Upload .py files</span>
        <input type="file" id="deployFiles" accept=".py" multiple style="display:none" onchange="onDeployFilesSelected(this)">
      </label>
    </div>
    <div id="deployFileList" style="margin-top:6px;font-size:11px"></div>
    <div style="display:flex;gap:8px;margin-top:8px">
      <button class="btn btn-blue" id="deployUploadBtn" onclick="doDeploy()" style="flex:1;display:none">Upload & Restart</button>
    </div>
    <div id="deployStatus" style="margin-top:6px;font-size:11px"></div>
    <div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px">
      <span id="deployBackupInfo" class="dim" style="font-size:10px"></span>
      <a href="/rollback" style="color:var(--orange);font-size:10px">Rollback</a>
    </div>

    <span class="detail-toggle" onclick="toggleDetail('emailDeploySection')">▸ Email Deploy Setup</span>
    <div class="detail-section" id="emailDeploySection">
      <div style="font-size:11px;color:var(--dim);line-height:1.6">
        <div style="font-weight:600;color:var(--text);margin-bottom:2px">Setup (one time):</div>
        <div>1. Create a Gmail with an App Password</div>
        <div>2. Add to <code style="background:rgba(255,255,255,0.06);padding:1px 4px;border-radius:3px;font-size:10px">.env</code>:</div>
        <div style="background:rgba(0,0,0,0.3);border-radius:4px;padding:6px 8px;margin:4px 0;font-family:monospace;font-size:10px;line-height:1.5;color:var(--text)">
          DEPLOY_EMAIL=bot@gmail.com<br>
          DEPLOY_EMAIL_PASS=xxxx xxxx xxxx xxxx<br>
          DEPLOY_ALLOWED_SENDERS=you@email.com
        </div>
        <div style="font-weight:600;color:var(--text);margin-top:10px;margin-bottom:2px">Usage:</div>
        <div>Email .py attachments to deploy. Add <code style="background:rgba(255,255,255,0.06);padding:1px 4px;border-radius:3px;font-size:10px">pip: package</code> or <code style="background:rgba(255,255,255,0.06);padding:1px 4px;border-radius:3px;font-size:10px">restart: all</code> in body.</div>
      </div>
    </div>

  </div>

  <!-- ─── OBSERVATION RULES ─── -->
  <div class="settings-card">
    <div class="sc-title" style="cursor:pointer" onclick="loadSkipConditions()">OBSERVATION RULES <span class="dim" style="font-weight:400;font-size:10px;margin-left:4px">tap to refresh</span></div>
    <div class="sc-hint" style="margin-bottom:8px">All active rules that can prevent a trade from being placed.</div>
    <div id="skipConditionsList" class="dim" style="font-size:12px">Tap title to load</div>
  </div>

  <!-- ─── SECURITY ─── -->
  <div class="settings-card">
    <div class="sc-title">SECURITY</div>

    <div class="sc-sub">DESTRUCTION PIN</div>
    <div class="sc-hint">A separate PIN required for deleting trades, wiping regimes, or full reset. Protects your accumulated data.</div>
    <div id="pinStatus" style="margin:8px 0;font-size:12px"></div>
    <div style="display:flex;gap:6px;align-items:center;margin-bottom:6px">
      <input type="password" id="currentPinInput" placeholder="Current" inputmode="numeric" maxlength="8" style="width:80px;font-size:14px;display:none">
      <input type="password" id="newPinInput" placeholder="New PIN (4-8 digits)" inputmode="numeric" maxlength="8" style="flex:1;font-size:14px">
      <button class="btn btn-blue" style="padding:6px 14px;font-size:12px" onclick="savePIN()">Set</button>
    </div>

    <span class="detail-toggle" onclick="toggleDetail('auditSection');if(!document.getElementById('auditSection').style.display||document.getElementById('auditSection').style.display==='none')return;loadAuditLog()">▸ Audit Log</span>
    <div class="detail-section" id="auditSection">
      <div id="auditLogContent" style="max-height:300px;overflow-y:auto"><div class="dim">Loading...</div></div>
      <button class="btn btn-dim" style="margin-top:6px;font-size:11px;padding:4px 10px" onclick="loadAuditLog()">Refresh</button>
    </div>
  </div>

  <!-- ─── RESET ─── -->
  <div class="settings-card" style="border-color:rgba(248,81,73,0.2)">
    <span class="detail-toggle" onclick="toggleDetail('resetSection')" style="color:var(--red);margin-top:0">▸ Reset Options</span>
    <div class="detail-section" id="resetSection">

    <div style="display:flex;flex-direction:column;gap:6px">
      <button class="btn btn-dim" style="text-align:left;font-size:12px;padding:10px" onclick="_resetConfirm1('settings','Reset Settings','Restores all settings to defaults. Credentials preserved.')">
        Reset Settings
        <div class="dim" style="font-size:10px;margin-top:2px">All config → defaults</div>
      </button>
      <button class="btn btn-dim" style="text-align:left;font-size:12px;padding:10px" onclick="_resetConfirm1('regime_filters','Clear Regime Filters','Removes all per-regime filters and overrides. Risk level actions preserved.')">
        Clear Regime Filters
        <div class="dim" style="font-size:10px;margin-top:2px">Per-regime filters &amp; overrides</div>
      </button>
      <button class="btn btn-dim" style="text-align:left;font-size:12px;padding:10px" onclick="_resetConfirm1('trades','Delete All Trades','Permanently removes all trade history, observatory data, price paths, and simulations. Recomputes stats.')">
        Delete All Trades
        <div class="dim" style="font-size:10px;margin-top:2px">Trade history, price paths, sims</div>
      </button>
    </div>

    <div style="border-top:1px solid var(--border);margin-top:10px;padding-top:10px">
      <button class="btn btn-dim" style="text-align:left;font-size:12px;padding:10px;width:100%;border-color:rgba(248,81,73,0.3)" onclick="_resetConfirm1('regime_engine','Wipe Regime Engine','Deletes all regime snapshots, candle data, baselines, and stat tables. The regime engine will rebuild from scratch. Trade history is preserved.')">
        <span style="color:var(--orange)">Wipe Regime Engine</span>
        <div class="dim" style="font-size:10px;margin-top:2px">Snapshots, candles, baselines, stats → gone (keeps trades)</div>
      </button>
    </div>

    <div style="border-top:1px solid var(--border);margin-top:10px;padding-top:10px">
      <button class="btn btn-dim" style="text-align:left;font-size:12px;padding:10px;width:100%;border-color:rgba(248,81,73,0.5)" onclick="_resetConfirm1('full','Complete Wipe','Deletes EVERYTHING — trades, regimes, snapshots, logs, bankroll history, settings. Only login credentials and push subscriptions are preserved. This cannot be undone.')">
        <span style="color:var(--red)">Complete Wipe</span>
        <div class="dim" style="font-size:10px;margin-top:2px">Everything gone. Fresh start.</div>
      </button>
    </div>
    </div>
  </div>

  <!-- ─── LINKS ─── -->
  <div style="display:flex;justify-content:space-between;align-items:center;padding:0 4px;margin-bottom:12px">
    <a href="/logs" id="logsLink" onclick="this.textContent='Loading...'" style="font-size:12px">View Full Logs</a>
    <button onclick="doLogout()" style="background:none;border:1px solid rgba(248,81,73,0.3);border-radius:6px;padding:6px 16px;color:var(--red);cursor:pointer;font-size:12px;-webkit-tap-highlight-color:transparent">Log Out</button>
  </div>
</div>

<!-- ═══ PAGE: TRADES ═══ -->
<div id="pageTrades" class="page" style="padding:0 16px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
      <div class="dim" style="font-size:10px;font-weight:600;letter-spacing:0.5px">TRADES</div>
      <button onclick="exportCSV()" style="background:none;border:1px solid var(--border);border-radius:6px;padding:3px 8px;font-size:10px;color:var(--dim);cursor:pointer;-webkit-tap-highlight-color:transparent">
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-1px;margin-right:2px"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>CSV
      </button>
    </div>

    <!-- Active trade card -->
    <div id="tradesActiveTrade" style="display:none"></div>

    <!-- Filter stats card -->
    <div id="tradeFilterStats" style="display:none;background:var(--card);border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:10px">
    </div>

    <!-- Outcome filters -->
    <div class="filter-chips" id="tradeFilters">
      <button class="chip active" data-filter="all" onclick="setTradeFilter('all')">All</button>
      <div class="filter-sep"></div>
      <button class="chip" data-filter="win" onclick="setTradeFilter('win')">Wins</button>
      <button class="chip" data-filter="loss" onclick="setTradeFilter('loss')">Losses</button>
      <button class="chip" data-filter="skipped" onclick="setTradeFilter('skipped')">Observed</button>
      <button class="chip" data-filter="error" onclick="setTradeFilter('error')">Errors</button>
      <div class="filter-sep"></div>
      <button class="chip" data-filter="yes" onclick="setTradeFilter('yes')">YES</button>
      <button class="chip" data-filter="no" onclick="setTradeFilter('no')">NO</button>
      <div class="filter-sep"></div>
      <button class="chip" data-filter="early" onclick="setTradeFilter('early')">Early</button>
      <button class="chip" data-filter="mid" onclick="setTradeFilter('mid')">Mid</button>
      <button class="chip" data-filter="late" onclick="setTradeFilter('late')">Late</button>
      <div class="filter-sep"></div>
      <button class="chip" data-filter="cheaper" onclick="setTradeFilter('cheaper')">Cheaper</button>
      <button class="chip" data-filter="model" onclick="setTradeFilter('model')">Model</button>
      <div class="filter-sep"></div>
      <button class="chip" data-filter="sold" onclick="setTradeFilter('sold')">Sold</button>
      <button class="chip" data-filter="hold" onclick="setTradeFilter('hold')">Hold</button>
      <div class="filter-sep"></div>
      <button class="chip" data-filter="shadow" onclick="setTradeFilter('shadow')">Shadow</button>
      <button class="chip" data-filter="ignored" onclick="setTradeFilter('ignored')">Ignored</button>
      <button class="chip" data-filter="incomplete" onclick="setTradeFilter('incomplete')">Incomplete</button>
    </div>

    <!-- Delete incomplete button (shown when incomplete filter active) -->
    <div id="deleteIncompleteBar" style="display:none;margin-bottom:8px">
      <button class="btn btn-dim" style="font-size:11px;padding:4px 10px;border-color:rgba(248,81,73,0.3);color:var(--red)" onclick="deleteAllIncomplete()">
        Delete all incomplete
      </button>
      <span class="dim" style="font-size:10px;margin-left:6px">Observatory data preserved</span>
    </div>

    <!-- Regime filter -->
    <div style="margin-bottom:10px">
      <select id="tradeRegimeFilter" onchange="resetTradeCache();loadTrades()" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:12px;padding:6px 10px;width:100%;-webkit-appearance:none">
        <option value="">All regimes</option>
      </select>
    </div>

    <div id="skelTrades" class="skel-wrap">
      <div class="card" style="padding:10px;margin-bottom:6px"><div style="display:flex;justify-content:space-between"><div class="skel skel-line" style="width:35%"></div><div class="skel skel-line" style="width:20%"></div></div><div class="skel skel-line-sm" style="width:70%;margin-top:6px"></div></div>
      <div class="card" style="padding:10px;margin-bottom:6px"><div style="display:flex;justify-content:space-between"><div class="skel skel-line" style="width:30%"></div><div class="skel skel-line" style="width:25%"></div></div><div class="skel skel-line-sm" style="width:65%;margin-top:6px"></div></div>
      <div class="card" style="padding:10px;margin-bottom:6px"><div style="display:flex;justify-content:space-between"><div class="skel skel-line" style="width:40%"></div><div class="skel skel-line" style="width:20%"></div></div><div class="skel skel-line-sm" style="width:60%;margin-top:6px"></div></div>
    </div>
    <div id="tradeList"></div>
    <div id="tradeLoadMore" style="display:none;text-align:center;padding:16px">
      <button onclick="loadMoreTrades()" class="btn btn-dim" style="font-size:12px;padding:8px 16px;width:auto">Load more</button>
    </div>
    <div id="tradeEndMarker" class="dim" style="display:none;text-align:center;padding:12px;font-size:11px">All trades loaded</div>
</div>

<!-- ═══ PAGE: BITCOIN ═══ -->
<div id="pageRegimes" class="page" style="padding:0 16px">

    <!-- BTC Price Header -->
    <div style="display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:8px">
      <div>
        <div class="dim" style="font-size:10px;font-weight:600;letter-spacing:0.5px">BITCOIN</div>
        <div id="btcPriceMain" style="font-size:24px;font-weight:700;font-family:monospace;color:var(--text)">—</div>
        <div id="btcReturns" class="dim" style="font-size:11px"></div>
      </div>
      <div class="filter-chips" style="margin:0">
        <button class="chip active" data-btcrange="60" onclick="loadBtcChart(60,this)">1h</button>
        <button class="chip" data-btcrange="240" onclick="loadBtcChart(240,this)">4h</button>
        <button class="chip" data-btcrange="1440" onclick="loadBtcChart(1440,this)">24h</button>
      </div>
    </div>

    <!-- BTC Chart -->
    <div style="position:relative;margin-bottom:12px">
      <canvas id="btcChart" style="width:100%;height:160px;border-radius:6px;background:var(--card);border:1px solid var(--border)"></canvas>
      <div id="btcChartLabel" style="position:absolute;top:8px;right:8px;font-size:10px;font-family:monospace;color:var(--dim);pointer-events:none;background:rgba(13,17,23,0.8);padding:1px 4px;border-radius:3px"></div>
    </div>

    <!-- Current Regime (live) -->
    <div style="background:var(--card);border:1px solid var(--border);border-radius:8px;padding:10px;margin-bottom:12px;border-left:3px solid var(--blue)" id="regimeCurrentBox">
      <div class="dim" style="font-size:10px;font-weight:600;letter-spacing:0.5px;margin-bottom:6px">CURRENT REGIME</div>
      <div id="regimeCurrentContent">
        <div class="skel-wrap" id="skelRegimeCurrent">
          <div class="skel skel-line-lg" style="width:60%"></div>
          <div style="display:flex;gap:8px"><div class="skel skel-line" style="width:30%"></div><div class="skel skel-line" style="width:30%"></div><div class="skel skel-line" style="width:30%"></div></div>
        </div>
      </div>
    </div>

    <!-- Engine Stats (collapsible) -->
    <div style="background:var(--card);border:1px solid var(--border);border-radius:8px;padding:8px 10px;margin-bottom:12px">
      <span class="detail-toggle" onclick="toggleDetail('regimeEngineSection')" style="margin:0;padding:0;line-height:1">▸ Engine Status</span>
      <div class="detail-section" id="regimeEngineSection" style="margin-top:8px">
        <div id="regimeEngineContent"><div class="dim">Loading...</div></div>
      </div>
    </div>

    <!-- Regime List -->
    <div>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
        <div class="dim" style="font-size:10px;font-weight:600;letter-spacing:0.5px">REGIMES</div>
        <button onclick="shareFile('/api/regimes/csv','regimes_'+new Date().toISOString().slice(0,10)+'.csv')" style="background:none;border:1px solid var(--border);border-radius:6px;padding:3px 8px;font-size:10px;color:var(--dim);cursor:pointer;-webkit-tap-highlight-color:transparent">
          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-1px;margin-right:2px"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>CSV
        </button>
      </div>
      <div class="filter-chips" id="regimeFilters">
        <button class="chip active" data-filter="all" onclick="setRegimeFilter('all',this)">All</button>
        <button class="chip" data-filter="has_ev" onclick="setRegimeFilter('has_ev',this)">Has EV</button>
        <button class="chip" data-filter="positive_ev" onclick="setRegimeFilter('positive_ev',this)">EV+</button>
      </div>
      <div id="skelRegimes" class="skel-wrap">
        <div style="background:var(--card);border:1px solid var(--border);border-radius:6px;padding:10px;margin-bottom:6px"><div style="display:flex;justify-content:space-between"><div class="skel skel-line" style="width:50%"></div><div class="skel skel-line" style="width:15%"></div></div><div class="skel skel-line-sm" style="width:80%;margin-top:6px"></div></div>
        <div style="background:var(--card);border:1px solid var(--border);border-radius:6px;padding:10px;margin-bottom:6px"><div style="display:flex;justify-content:space-between"><div class="skel skel-line" style="width:45%"></div><div class="skel skel-line" style="width:18%"></div></div><div class="skel skel-line-sm" style="width:75%;margin-top:6px"></div></div>
        <div style="background:var(--card);border:1px solid var(--border);border-radius:6px;padding:10px;margin-bottom:6px"><div style="display:flex;justify-content:space-between"><div class="skel skel-line" style="width:55%"></div><div class="skel skel-line" style="width:12%"></div></div><div class="skel skel-line-sm" style="width:70%;margin-top:6px"></div></div>
      </div>
      <div id="regimeList"></div>
    </div>
</div>

<!-- Regime Detail Modal -->
<div class="confirm-overlay" id="regimeDetailOverlay" style="display:none">
  <div class="modal-panel" style="max-width:480px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <h3 style="color:var(--blue);font-size:14px;margin:0" id="regimeDetailTitle">Regime</h3>
      <button onclick="closeModal('regimeDetailOverlay')" style="background:none;border:none;color:var(--dim);font-size:20px;cursor:pointer;padding:10px;margin:-6px;-webkit-tap-highlight-color:transparent"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
    </div>
    <div id="regimeDetailContent"></div>
  </div>
</div>

<div class="confirm-overlay" id="deleteOverlay">
  <div class="confirm-box" style="border-color:var(--orange)">
    <h3 style="color:var(--orange)">Delete Trade?</h3>
    <div id="deleteInfo" class="dim" style="margin:8px 0;text-align:left"></div>
    <p class="dim" style="font-size:11px">Regime stats and lifetime counters will be recomputed.</p>
    <div id="deleteBtns" style="margin-top:12px"></div>
  </div>
</div>

<!-- Reset Streak -->
<div class="confirm-overlay" id="resetStreakOverlay" style="display:none">
  <div class="confirm-box" style="border-color:var(--orange);max-width:300px">
    <h3 style="color:var(--orange)">Reset Streak?</h3>
    <div style="font-size:13px;color:var(--dim);margin:10px 0">Resets loss streak and cooldown. If there's an active trade, it will be marked as ignored.</div>
    <div class="confirm-btns">
      <button class="btn btn-dim" onclick="closeModal('resetStreakOverlay')">Cancel</button>
      <button class="btn btn-red" onclick="doResetStreak()" style="border-color:var(--orange);background:var(--orange);color:#000">Reset</button>
    </div>
  </div>
</div>

<!-- Reset Confirmation Step 1 -->
<div class="confirm-overlay" id="resetConfirm1" style="display:none">
  <div class="confirm-box" style="border-color:rgba(248,81,73,0.3);max-width:320px;text-align:left">
    <div id="resetConfirm1Content"></div>
  </div>
</div>

<!-- Reset Confirmation Step 2 -->
<div class="confirm-overlay" id="resetConfirm2" style="display:none">
  <div class="confirm-box" style="border-color:var(--red);max-width:320px;text-align:left">
    <div id="resetConfirm2Content"></div>
  </div>
</div>


</div> <!-- end contentWrap -->

<!-- Trade Detail Popup (outside contentWrap for reliable fixed positioning on iOS) -->
<div class="confirm-overlay" id="tradeDetailOverlay" style="display:none">
  <div class="modal-panel" style="max-width:560px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <h3 style="color:var(--blue);font-size:14px;margin:0">Trade Detail</h3>
      <button onclick="closeModal('tradeDetailOverlay')" style="background:none;border:none;color:var(--dim);font-size:20px;cursor:pointer;padding:10px;margin:-6px;-webkit-tap-highlight-color:transparent"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
    </div>
    <div id="tradeDetailContent"></div>
    <div style="position:relative">
      <canvas id="tradeDetailChart" style="width:100%;height:100px;border-radius:4px;background:var(--bg);border:1px solid var(--border);margin-top:8px"></canvas>
      <div id="tradeDetailChartLabel" style="position:absolute;top:12px;left:8px;font-size:11px;font-family:monospace;color:var(--dim);pointer-events:none;background:rgba(13,17,23,0.8);padding:1px 4px;border-radius:3px"></div>
    </div>
  </div>
</div>

<!-- Image Lightbox -->
<div id="imgLightbox" onclick="if(event.target===this)closeLightbox()">
  <button class="lb-close" onclick="closeLightbox()">✕</button>
  <img id="lbImg" src="">
  <div class="lb-dl">
    <button id="lbDownload" class="chat-dl-btn" onclick="event.stopPropagation()">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/><polyline points="16 6 12 2 8 6"/><line x1="12" y1="2" x2="12" y2="15"/></svg>
      Share
    </button>
  </div>
</div>

<!-- Bottom Tab Bar -->
<div class="tab-bar">
  <button class="tab-btn" data-tab="Trades" onclick="switchTab('Trades')">
    <svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7.5 7.5 3m0 0L12 7.5M7.5 3v13.5m13.5 0L16.5 21m0 0L12 16.5m4.5 4.5V7.5"/></svg>
    <span>Trades</span>
  </button>
  <button class="tab-btn" data-tab="Regimes" onclick="switchTab('Regimes')">
    <svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>
    <span>Regimes</span>
  </button>
  <button class="tab-btn tab-active" data-tab="Home" onclick="switchTab('Home')">
    <svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>
    <span>Home</span>
  </button>
  <button class="tab-btn" data-tab="Stats" onclick="switchTab('Stats')">
    <svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="12" width="4" height="9" rx="1"/><rect x="10" y="7" width="4" height="14" rx="1"/><rect x="17" y="3" width="4" height="18" rx="1"/></svg>
    <span>Stats</span>
  </button>
  <button class="tab-btn" data-tab="Settings" onclick="switchTab('Settings')">
    <svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.325.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 0 1 1.37.49l1.296 2.247a1.125 1.125 0 0 1-.26 1.431l-1.003.827c-.293.241-.438.613-.43.992a7.723 7.723 0 0 1 0 .255c-.008.378.137.75.43.991l1.004.827c.424.35.534.955.26 1.43l-1.298 2.247a1.125 1.125 0 0 1-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.47 6.47 0 0 1-.22.128c-.331.183-.581.495-.644.869l-.213 1.281c-.09.543-.56.94-1.11.94h-2.594c-.55 0-1.019-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 0 1-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 0 1-1.369-.49l-1.297-2.247a1.125 1.125 0 0 1 .26-1.431l1.004-.827c.292-.24.437-.613.43-.991a6.932 6.932 0 0 1 0-.255c.007-.38-.138-.751-.43-.992l-1.004-.827a1.125 1.125 0 0 1-.26-1.43l1.297-2.247a1.125 1.125 0 0 1 1.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.086.22-.128.332-.183.582-.495.644-.869l.214-1.28Z"/><circle cx="12" cy="12" r="3"/></svg>
    <span>Settings</span>
  </button>
</div>

<script>
const $ = s => document.querySelector(s);
function _cw() { return document.getElementById('contentWrap'); }
function scrollTop() { const c = _cw(); if (c) c.scrollTop = 0; }
function scrollBottom() { const c = _cw(); if (c) c.scrollTop = c.scrollHeight; }

// Format P&L with sign before $: +$5.00 or -$5.00
function fmtPnl(val) {
  const n = parseFloat(val) || 0;
  return (n >= 0 ? '+' : '-') + '$' + Math.abs(n).toFixed(2);
}

// Skeleton loading system
function hideSkel(id) {
  const el = document.getElementById(id);
  if (el && !el.classList.contains('skel-hidden')) {
    el.classList.add('skel-hidden');
    setTimeout(() => { el.style.display = 'none'; }, 300);
  }
}

let _modalScrollY = 0;
let _modalCount = 0;

function openModal(id) {
  const el = document.getElementById(id);
  // Don't double-open — prevents _modalCount from getting out of sync
  if (el.style.display === 'flex') return;
  el.style.display = 'flex';
  el.scrollTop = 0;
  _modalCount++;
  if (_modalCount === 1) {
    const tb = document.querySelector('.tab-bar');
    if (tb) tb.style.zIndex = '0';
    const cw = document.getElementById('contentWrap');
    if (cw) cw.style.overflow = 'hidden';
    document.body.style.overflow = 'hidden';
  }
}
function closeModal(id) {
  document.getElementById(id).style.display = 'none';
  _modalCount = Math.max(0, _modalCount - 1);
  if (_modalCount === 0) {
    const tb = document.querySelector('.tab-bar');
    if (tb) tb.style.zIndex = '100';
    const cw = document.getElementById('contentWrap');
    if (cw) cw.style.overflow = '';
    document.body.style.overflow = '';
  }
}

function closeAllModals() {
  document.querySelectorAll('.confirm-overlay').forEach(m => {
    if (m.style.display !== 'none' && m.style.display !== '') {
      closeModal(m.id);
    }
  });
}

// Tap outside modal to close
document.addEventListener('click', function(e) {
  if (_modalCount === 0) return;
  const overlay = e.target.closest('.confirm-overlay');
  if (overlay && overlay.style.display !== 'none' && e.target === overlay) {
    closeModal(overlay.id);
    return;
  }
  if (e.target.closest('#stickyHeader') && !e.target.closest('[onclick]') && !e.target.closest('button')) {
    closeAllModals();
  }
});

// Prevent background scroll when modal is open
document.addEventListener('touchmove', function(e) {
  if (_modalCount === 0) return;
  // Allow scrolling inside modal-panel and confirm-box
  if (e.target.closest('.modal-panel') || e.target.closest('.confirm-box')) return;
  e.preventDefault();
}, {passive: false});

// Clipboard with multiple fallbacks
function copyToClipboard(text) {
  return new Promise((resolve, reject) => {
    // Try modern API first
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(resolve).catch(() => fallbackCopy(text, resolve, reject));
    } else {
      fallbackCopy(text, resolve, reject);
    }
  });
}
function fallbackCopy(text, resolve, reject) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;left:0;top:0;width:100%;height:200px;z-index:9999;' +
    'font-size:12px;background:var(--card);color:var(--text);border:2px solid var(--blue);' +
    'padding:10px;border-radius:8px';
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try {
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    if (ok) resolve(); else reject(new Error('execCommand failed'));
  } catch(e) {
    // Leave textarea visible so user can manually copy
    ta.style.height = '150px';
    const hint = document.createElement('div');
    hint.style.cssText = 'position:fixed;left:0;top:155px;width:100%;text-align:center;z-index:9999;' +
      'padding:8px;background:var(--card);color:var(--yellow);font-size:13px';
    hint.innerHTML = 'Select all text above and copy manually. <button onclick="this.parentElement.remove();' +
      'document.querySelector(\'textarea\')?.remove()" style="margin-left:8px;padding:4px 8px">Done</button>';
    document.body.appendChild(hint);
    reject(e);
  }
}

// Pull-to-refresh with spinner
(function() {
  let startY = 0, pulling = false, triggered = false;
  const threshold = 180;  // pixels of pull needed to trigger
  const showAfter = 30;   // don't show bar until this much pull
  let bar = document.createElement('div');
  bar.id = '_ptrBar';
  bar.style.cssText = 'position:fixed;left:0;right:0;height:3px;background:var(--blue);' +
    'z-index:55;transform:scaleX(0);transform-origin:left;transition:transform 0.15s;display:none;top:0';
  document.body.appendChild(bar);

  function positionBar() {
    const hdr = document.getElementById('stickyHeader');
    if (hdr) bar.style.top = hdr.offsetHeight + 'px';
  }

  function getScroller() {
    return document.getElementById('contentWrap');
  }

  function isModalOpen() {
    return _modalCount > 0;
  }

  document.addEventListener('touchstart', e => {
    const scroller = getScroller();
    const atTop = scroller ? scroller.scrollTop <= 0 : true;
    if (atTop && e.touches.length === 1 && !isModalOpen() && !_chartTouchActive) {
      startY = e.touches[0].clientY;
      pulling = true;
      triggered = false;
      positionBar();
    }
  }, {passive: true});

  document.addEventListener('touchmove', e => {
    if (!pulling || isModalOpen() || _chartTouchActive) { pulling = false; return; }
    const dy = Math.max(0, e.touches[0].clientY - startY);
    if (dy > showAfter) {
      bar.style.display = '';
      // Resistance curve: progress slows down as you pull further
      const raw = (dy - showAfter) / (threshold - showAfter);
      const progress = Math.min(1, raw * raw);  // quadratic ease — slow start, accelerates
      bar.style.transform = `scaleX(${progress})`;
      bar.style.background = progress >= 1 ? 'var(--green)' : 'var(--blue)';
    }
  }, {passive: true});

  document.addEventListener('touchend', e => {
    if (!pulling || isModalOpen() || _chartTouchActive) { pulling = false; return; }
    pulling = false;
    const scroller = getScroller();
    const atTop = scroller ? scroller.scrollTop <= 0 : true;
    const dy = Math.max(0, (e.changedTouches[0] || {}).clientY - startY);
    if (dy >= threshold && atTop) {
      bar.style.transform = 'scaleX(1)';
      bar.style.background = 'var(--green)';
      bar.style.transition = 'none';
      // Animate loading
      let pos = 0;
      const anim = setInterval(() => {
        pos = (pos + 2) % 100;
        bar.style.transform = `scaleX(1)`;
        bar.style.background = `linear-gradient(90deg, var(--bg) ${pos}%, var(--green) ${pos+20}%, var(--bg) ${pos+40}%)`;
      }, 30);
      setTimeout(() => { clearInterval(anim); location.reload(); }, 300);
    } else {
      bar.style.transform = 'scaleX(0)';
      bar.style.transition = 'transform 0.2s';
      setTimeout(() => { bar.style.display = 'none'; bar.style.transition = 'transform 0.1s'; }, 200);
    }
  }, {passive: true});
})();
let currentBankroll = 0;
let lastStateData = {};  // Store for ticking countdown
let chartData = [];      // [{ts: Date, bid: number}] for timeline chart
let chartTradeId = null; // Reset chart on new trade
let chartStartMs = 0;    // Market open time (ms)
let chartEndMs = 0;      // Market close time (ms)
let cachedLifetimePnl = 0;

function openBankrollModal() {
  const s = _uiState;
  const rawBal = (s.bankroll_cents || 0) / 100;
  const at = s.active_trade;
  const inTrade = at ? (at.actual_cost || 0) : 0;

  // Main balances
  $('#bkmEffective').textContent = '$' + rawBal.toFixed(2);
  $('#bkmTotal').textContent = '$' + rawBal.toFixed(2);
  $('#bkmInTrade').textContent = inTrade > 0 ? '$' + inTrade.toFixed(2) : '—';
  $('#bkmInTrade').style.color = inTrade > 0 ? 'var(--blue)' : 'var(--dim)';

  // Lifetime P&L
  const lpnl = cachedLifetimePnl;
  const bkmLp = $('#bkmLifetimePnl');
  bkmLp.textContent = fmtPnl(lpnl);
  bkmLp.className = lpnl > 0 ? 'pos' : lpnl < 0 ? 'neg' : '';
  const lw = s.lifetime_wins || 0, ll = s.lifetime_losses || 0;
  const lTotal = lw + ll;
  const lWr = lTotal > 0 ? (lw / lTotal * 100).toFixed(0) : '—';
  $('#bkmLifetimeStats').textContent = `${lw}W–${ll}L · ${lTotal} trades · ${lWr}% WR`;

  // Warning
  const warnEl = $('#bkmWarning');
  warnEl.style.display = 'none';

  // Show modal then load charts
  const bkm = document.getElementById('bankrollModal');
  if (bkm && bkm.style.display === 'flex') return;  // Already open
  openModal('bankrollModal');
  setTimeout(() => {
    loadBankrollChart(null);
    loadPnlChart(null);
  }, 100);
}

// ── Ticking clock (updates every second) ────────────────
function updateClock() {
  const now = new Date();

  // Tick down time-left displays using stored close times
  tickCountdown('monTime', lastStateData._monCloseTime);
  tickCountdown('tradeTime', lastStateData._tradeCloseTime);

  // ── Detect market end → trigger immediate transition ──
  const tradeClose = lastStateData._tradeCloseTime;
  if (tradeClose && !lastStateData._tradeEndFired) {
    const diff = (new Date(tradeClose.replace('Z','+00:00')) - now) / 1000;
    if (diff <= 0) {
      lastStateData._tradeEndFired = true;
      onMarketEnd('trade');
    }
  }
  const monClose = lastStateData._monCloseTime;
  if (monClose && !lastStateData._monEndFired) {
    const diff = (new Date(monClose.replace('Z','+00:00')) - now) / 1000;
    if (diff <= 0) {
      lastStateData._monEndFired = true;
      onMarketEnd('monitor');
    }
  }
}

// Called when a market timer hits 0. Transition UI immediately.
function onMarketEnd(which) {
  const autoOn = _uiState.auto_trading;

  if (which === 'trade') {
    // Active trade market ended → resolve will happen server-side
    lastStateData._tradeFiredForClose = lastStateData._tradeCloseTime;
    patchUI({status_detail: 'Market closed — resolving trade...'});
    // Single extra poll after brief delay — regular 1s poll handles the rest
    setTimeout(pollState, 2000);
  } else if (which === 'monitor') {
    // Live market ended → transition to "between markets"
    lastStateData._monFiredForClose = lastStateData._monCloseTime;
    if (autoOn) {
      const _isObs = ['observe','shadow','hybrid'].includes(_uiState.trading_mode);
      patchUI({status_detail: _isObs ? 'Observing — waiting for next market...' : 'Market closed — waiting for next market...'});
    } else {
      $('#monMarket').textContent = '—';
      $('#monTime').textContent = '—';
      $('#monYesSpread').textContent = '';
      $('#monNoSpread').textContent = '';
    }
    lastStateData._monCloseTime = null;
    setTimeout(pollState, 2000);
  }
}

function tickCountdown(elId, closeTimeISO) {
  if (!closeTimeISO) return;
  const el = $('#' + elId);
  if (!el) return;
  const close = new Date(closeTimeISO.replace('Z', '+00:00'));
  const diff = Math.max(0, (close - new Date()) / 1000);
  if (diff <= 0) {
    el.textContent = 'Ended';
    el.style.color = 'var(--dim)';
  } else {
    el.textContent = fmtMmSs(diff);
    el.style.color = '';
  }
}

function fmtMmSs(totalSecs) {
  const m = Math.floor(totalSecs / 60);
  const s = Math.floor(totalSecs % 60);
  return m + ':' + String(s).padStart(2, '0');
}

function marketStartTime(closeTimeISO) {
  // Market is 15 min long, so start = close - 15 min. Display in CT.
  if (!closeTimeISO) return '—';
  try {
    const close = new Date(closeTimeISO.replace('Z', '+00:00'));
    const start = new Date(close.getTime() - 15 * 60 * 1000);
    return start.toLocaleString('en-US', {
      timeZone: 'America/Chicago', hour: 'numeric', minute: '2-digit', hour12: true
    });
  } catch(e) { return '—'; }
}

function buildRegimeGrid(data) {
  // data: {vol_regime, trend_regime, volume_regime, regime_win_rate, regime_trades, btc_price}
  const wr = ((data.regime_win_rate||0)*100).toFixed(0);
  const tl = trendLabel(data.trend_regime);
  const items = [
    ['Win Rate', data.regime_trades ? `${wr}% (n=${data.regime_trades})` : 'No data'],
    ['Volatility', (data.vol_regime||'?') + '/5'],
    ['Trend', tl],
    ['Volume', (data.volume_regime||'?') + '/5'],
    ['BTC', data.btc_price ? '$' + Math.round(data.btc_price).toLocaleString() : '—'],
  ];
  if (data.stability_c != null) {
    items.push(['Stability', data.stability_c + '¢ range']);
  }
  return items.map(([label, val]) =>
    `<div>${label}: <span class="rdg-val">${val}</span></div>`
  ).join('');
}

setInterval(updateClock, 1000);
updateClock();

function drawPriceChart(entry, sell, data, startMs, endMs) {
  const canvas = document.getElementById('priceChart');
  if (!canvas || !data.length || !startMs || !endMs) return;
  const ctx = canvas.getContext('2d');

  // Handle retina
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;

  const pad = {t: 10, b: 16, l: 4, r: 4};
  const totalMs = endMs - startMs;

  ctx.clearRect(0, 0, W, H);

  // Y range from entry/sell/bids with padding
  const bids = data.map(d => d.bid);
  const allVals = bids.concat([entry]);
  if (sell > 0) allVals.push(sell);
  let yMin = Math.min(...allVals) - 3;
  let yMax = Math.max(...allVals) + 3;
  if (yMax - yMin < 10) { yMin -= 5; yMax += 5; }

  const toX = (ms) => pad.l + ((ms - startMs) / totalMs) * (W - pad.l - pad.r);
  const toY = (v) => pad.t + (1 - (v - yMin) / (yMax - yMin)) * (H - pad.t - pad.b);

  // Time gridlines (every 5 min)
  ctx.strokeStyle = 'rgba(48,54,61,0.5)';
  ctx.setLineDash([2, 3]);
  ctx.lineWidth = 0.5;
  ctx.font = '9px sans-serif';
  ctx.fillStyle = 'rgba(139,148,158,0.4)';
  for (let m = 5; m < 15; m += 5) {
    const gx = toX(startMs + m * 60000);
    ctx.beginPath(); ctx.moveTo(gx, pad.t); ctx.lineTo(gx, H - pad.b); ctx.stroke();
    ctx.fillText(m + 'm', gx - 6, H - 3);
  }
  ctx.setLineDash([]);

  // "Now" marker
  const nowMs = Date.now();
  if (nowMs > startMs && nowMs < endMs) {
    const nx = toX(nowMs);
    ctx.strokeStyle = 'rgba(139,148,158,0.15)';
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(nx, pad.t); ctx.lineTo(nx, H - pad.b); ctx.stroke();
  }

  // Entry line (blue dashed)
  ctx.strokeStyle = 'rgba(88,166,255,0.35)';
  ctx.setLineDash([4, 3]);
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(pad.l, toY(entry)); ctx.lineTo(W - pad.r, toY(entry)); ctx.stroke();

  // Sell target line (green dashed)
  if (sell > 0) {
    ctx.strokeStyle = 'rgba(63,185,80,0.35)';
    ctx.beginPath(); ctx.moveTo(pad.l, toY(sell)); ctx.lineTo(W - pad.r, toY(sell)); ctx.stroke();
  }
  ctx.setLineDash([]);

  // Y-axis labels
  ctx.font = '9px monospace';
  ctx.fillStyle = 'rgba(88,166,255,0.5)';
  ctx.textAlign = 'right';
  ctx.fillText(entry + '¢', W - pad.r - 2, toY(entry) - 3);
  if (sell > 0) {
    ctx.fillStyle = 'rgba(63,185,80,0.5)';
    ctx.fillText(sell + '¢', W - pad.r - 2, toY(sell) - 3);
  }
  ctx.textAlign = 'left';

  // Price line
  const lastBid = data[data.length - 1].bid;
  const lineColor = lastBid >= entry ? '#3fb950' : '#f85149';

  ctx.strokeStyle = lineColor;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  // Start from left edge at first point's price (fills gap before data)
  const firstBid = data[0].bid;
  const leftX = pad.l;
  ctx.moveTo(leftX, toY(firstBid));
  for (let i = 0; i < data.length; i++) {
    const x = toX(data[i].ts);
    const y = toY(data[i].bid);
    ctx.lineTo(x, y);
  }
  ctx.stroke();

  // Fill under
  const lastX = toX(data[data.length - 1].ts);
  const lastY = toY(lastBid);
  ctx.lineTo(lastX, H - pad.b);
  ctx.lineTo(leftX, H - pad.b);
  ctx.closePath();
  ctx.fillStyle = lastBid >= entry ? 'rgba(63,185,80,0.06)' : 'rgba(248,81,73,0.06)';
  ctx.fill();

  // Current price dot + label
  ctx.beginPath();
  ctx.arc(lastX, lastY, 3, 0, Math.PI * 2);
  ctx.fillStyle = lineColor;
  ctx.fill();
  ctx.font = '10px monospace';
  ctx.fillStyle = lineColor;
  const labelX = lastX > W - 40 ? lastX - 30 : lastX + 5;
  ctx.fillText(lastBid + '¢', labelX, lastY - 6);

  // Store chart mapping for universal touch interaction
  const redrawFn = () => drawPriceChart(entry, sell, data, startMs, endMs);
  // Include synthetic left-edge point for touch if data starts late
  const chartPts = [];
  if (data[0].ts > startMs + 5000) chartPts.push({ts: startMs, bid: firstBid, x: startMs, val: firstBid});
  for (const d of data) chartPts.push({...d, x: d.ts, val: d.bid});
  canvas._chartMap = {
    data: chartPts,
    pad, W: W, H: H,
    toX: (ms) => pad.l + ((ms - startMs) / totalMs) * (W - pad.l - pad.r),
    toY: (v) => pad.t + (1 - (v - yMin) / (yMax - yMin)) * (H - pad.t - pad.b),
    fromX: (cssX) => startMs + ((cssX - pad.l) / (W - pad.l - pad.r)) * totalMs,
    redraw: redrawFn,
    formatLabel: (d) => {
      const secs = Math.round((d.ts - startMs) / 1000);
      const mins = Math.floor(secs / 60);
      const secsR = secs % 60;
      const color = d.bid >= entry ? 'var(--green)' : 'var(--red)';
      return `<span style="color:${color}">${d.bid}¢</span> · ${mins}:${String(secsR).padStart(2,'0')}`;
    }
  };
}

// ── Live Price Chart ─────────────────────────────────────
let _livePriceBuf = {ticker: null, data: [], closeTime: null};

function pushLivePrice(ticker, closeTime, yesAsk, noAsk, yesBid, noBid) {
  if (!ticker || (!yesAsk && !noAsk)) return;
  // Filter opening noise: if cheaper side is extreme (>=90 or <=2), skip —
  // this means both sides are near 99/1 from stale low-volume orders
  const cheaper = Math.min(yesAsk || 99, noAsk || 99);
  if (cheaper >= 90 || cheaper <= 2) return;
  if (ticker !== _livePriceBuf.ticker) {
    _livePriceBuf = {ticker, data: [], closeTime};
  }
  _livePriceBuf.closeTime = closeTime;
  const now = Date.now();
  // Dedupe: skip if same prices within 500ms (use abs to handle server/client clock skew after backfill)
  const last = _livePriceBuf.data[_livePriceBuf.data.length - 1];
  if (last && Math.abs(now - last.ts) < 500 && last.ya === yesAsk && last.na === noAsk) return;
  _livePriceBuf.data.push({ts: now, ya: yesAsk || 0, na: noAsk || 0, yb: yesBid || 0, nb: noBid || 0});
  // Keep max 900 points (~15min at 1/sec)
  if (_livePriceBuf.data.length > 900) _livePriceBuf.data.shift();
}

function drawLiveMarketChart(canvasId) {
  const buf = _livePriceBuf;
  const canvas = document.getElementById(canvasId);
  if (!canvas || buf.data.length < 2) {
    if (canvas) canvas.style.display = 'none';
    return;
  }
  canvas.style.display = '';

  const ctx = canvas.getContext('2d');
  const rect = canvas.getBoundingClientRect();
  if (rect.width === 0) return;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;
  const pad = {t: 10, b: 16, l: 4, r: 4};

  const data = buf.data;
  // Use cheaper side (lower ask = cheaper to buy)
  const bids = data.map(d => Math.min(d.ya || 99, d.na || 99));

  let yMin = Math.min(...bids) - 2;
  let yMax = Math.max(...bids) + 2;

  // Expand Y range to include sim trade lines if active
  if (_sim.active && _sim.side && _sim.entry > 0) {
    yMin = Math.min(yMin, _sim.entry - 2);
    yMax = Math.max(yMax, _sim.entry + 2);
    if (_sim.sellTarget > 0) {
      yMin = Math.min(yMin, _sim.sellTarget - 2);
      yMax = Math.max(yMax, _sim.sellTarget + 2);
    }
  }

  // Expand Y range to include fair value line
  if (canvasId === 'liveChart' && lastStateData._liveMarket) {
    const _fvLm = lastStateData._liveMarket.fv_model;
    if (_fvLm && _fvLm.fair_yes_c != null) {
      const _fvC = Math.min(_fvLm.fair_yes_c, _fvLm.fair_no_c);
      yMin = Math.min(yMin, _fvC - 2);
      yMax = Math.max(yMax, _fvC + 2);
    }
  }

  if (yMax - yMin < 6) { yMin -= 3; yMax += 3; }

  // Time range: use full 15-min market window if close time available
  let endMs = buf.closeTime ? new Date(buf.closeTime).getTime() : data[data.length - 1].ts;
  let startMs = buf.closeTime ? endMs - 15 * 60 * 1000 : data[0].ts;
  if (endMs <= startMs) endMs = data[data.length - 1].ts;
  if (startMs >= endMs) startMs = data[0].ts;
  const totalMs = endMs - startMs || 1;

  const toX = (ms) => pad.l + ((ms - startMs) / totalMs) * (W - pad.l - pad.r);
  const toY = (v) => pad.t + (1 - (v - yMin) / (yMax - yMin)) * (H - pad.t - pad.b);

  function draw() {
    ctx.clearRect(0, 0, W, H);

    // Time gridlines
    ctx.strokeStyle = 'rgba(48,54,61,0.5)';
    ctx.setLineDash([2, 3]);
    ctx.lineWidth = 0.5;
    ctx.font = '9px sans-serif';
    ctx.fillStyle = 'rgba(139,148,158,0.4)';
    const spanMin = totalMs / 60000;
    const step = spanMin > 10 ? 5 : spanMin > 4 ? 2 : 1;
    for (let m = step; m < spanMin; m += step) {
      const gx = toX(startMs + m * 60000);
      if (gx > pad.l + 10 && gx < W - pad.r - 10) {
        ctx.beginPath(); ctx.moveTo(gx, pad.t); ctx.lineTo(gx, H - pad.b); ctx.stroke();
        ctx.fillText(m + 'm', gx - 6, H - 3);
      }
    }
    ctx.setLineDash([]);

    // "Now" marker
    const nowMs = Date.now();
    if (nowMs > startMs && nowMs < endMs) {
      const nx = toX(nowMs);
      ctx.strokeStyle = 'rgba(139,148,158,0.15)';
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(nx, pad.t); ctx.lineTo(nx, H - pad.b); ctx.stroke();
    }

    // Price line
    const lastBid = bids[bids.length - 1];
    const firstBid = bids[0];
    const lineColor = lastBid >= firstBid ? '#3fb950' : '#f85149';

    ctx.strokeStyle = lineColor;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    for (let i = 0; i < data.length; i++) {
      const x = toX(data[i].ts);
      const y = toY(bids[i]);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();

    // Fill under
    const lastX = toX(data[data.length - 1].ts);
    const lastY = toY(lastBid);
    ctx.lineTo(lastX, H - pad.b);
    ctx.lineTo(toX(data[0].ts), H - pad.b);
    ctx.closePath();
    ctx.fillStyle = lastBid >= firstBid ? 'rgba(63,185,80,0.06)' : 'rgba(248,81,73,0.06)';
    ctx.fill();

    // Sim trade reference lines (entry + sell target)
    if (_sim.active && _sim.side && _sim.entry > 0 && canvasId === 'liveChart') {
      // Entry line — dashed yellow
      const entryY = toY(_sim.entry);
      ctx.strokeStyle = 'rgba(210,153,34,0.6)';
      ctx.setLineDash([4, 3]);
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(pad.l, entryY); ctx.lineTo(W - pad.r, entryY); ctx.stroke();
      ctx.setLineDash([]);
      ctx.font = '9px monospace';
      ctx.fillStyle = 'rgba(210,153,34,0.8)';
      ctx.fillText('sim ' + _sim.entry + '¢', pad.l + 2, entryY - 3);

      // Sell target line — dashed green (if not hold-to-expiry)
      if (_sim.sellTarget > 0) {
        const sellY = toY(_sim.sellTarget);
        ctx.strokeStyle = _sim.sold ? 'rgba(63,185,80,0.8)' : 'rgba(63,185,80,0.5)';
        ctx.setLineDash([4, 3]);
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(pad.l, sellY); ctx.lineTo(W - pad.r, sellY); ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = _sim.sold ? 'rgba(63,185,80,0.9)' : 'rgba(63,185,80,0.7)';
        ctx.fillText((_sim.sold ? '✓ ' : '') + _sim.sellTarget + '¢', pad.l + 2, sellY - 3);
      }
    }

    // Current price dot + label
    ctx.beginPath();
    ctx.arc(lastX, lastY, 3, 0, Math.PI * 2);
    ctx.fillStyle = lineColor;
    ctx.fill();
    ctx.font = '10px monospace';
    ctx.fillStyle = lineColor;
    const labelX = lastX > W - 40 ? lastX - 30 : lastX + 5;
    ctx.fillText(lastBid + '¢', labelX, lastY - 6);

    // Fair Value Model reference lines (purple dashed)
    if (canvasId === 'liveChart' && lastStateData._liveMarket) {
      const fv = lastStateData._liveMarket.fv_model;
      if (fv && fv.fair_yes_c != null) {
        const fvCheaper = Math.min(fv.fair_yes_c, fv.fair_no_c);
        if (fvCheaper > yMin && fvCheaper < yMax) {
          const fvY = toY(fvCheaper);
          ctx.strokeStyle = 'rgba(136,132,216,0.5)';
          ctx.setLineDash([3, 4]);
          ctx.lineWidth = 1;
          ctx.beginPath(); ctx.moveTo(pad.l, fvY); ctx.lineTo(W - pad.r, fvY); ctx.stroke();
          ctx.setLineDash([]);
          ctx.font = '9px monospace';
          ctx.fillStyle = 'rgba(136,132,216,0.8)';
          ctx.fillText('FV ' + Math.round(fvCheaper) + '¢', W - pad.r - 40, fvY - 3);
        }
      }
    }
  }
  draw();

  // Touch crosshair support
  canvas._chartMap = {
    data: data.map((d, i) => ({ts: d.ts, x: d.ts, bid: bids[i], val: bids[i]})),
    pad, W, H,
    toX, toY,
    fromX: (cssX) => startMs + ((cssX - pad.l) / (W - pad.l - pad.r)) * totalMs,
    redraw: draw,
    formatLabel: (d) => {
      const secs = Math.round((d.ts - startMs) / 1000);
      const mins = Math.floor(secs / 60);
      const secsR = secs % 60;
      return `<span style="color:var(--text)">${Math.round(d.val)}¢</span> · ${mins}:${String(secsR).padStart(2,'0')}`;
    }
  };
}

// ── Universal chart touch interaction ─────────────────────
let _chartTouchActive = false;
(function() {
  let activeCanvas = null;

  function interpolateAt(canvas, clientX) {
    const cm = canvas._chartMap;
    if (!cm || !cm.data || !cm.data.length) return null;
    const rect = canvas.getBoundingClientRect();
    const cssX = clientX - rect.left;
    const dataX = cm.fromX(cssX);
    const pts = cm.data;

    // Clamp to data range
    const firstX = pts[0].ts || pts[0].x;
    const lastX = pts[pts.length - 1].ts || pts[pts.length - 1].x;
    if (dataX <= firstX) {
      const v = pts[0].bid != null ? pts[0].bid : pts[0].val;
      return {x: firstX, val: v, cssX};
    }
    if (dataX >= lastX) {
      const v = pts[pts.length-1].bid != null ? pts[pts.length-1].bid : pts[pts.length-1].val;
      return {x: lastX, val: v, cssX};
    }

    // Find surrounding points and interpolate
    for (let i = 0; i < pts.length - 1; i++) {
      const ax = pts[i].ts || pts[i].x;
      const bx = pts[i+1].ts || pts[i+1].x;
      if (dataX >= ax && dataX <= bx) {
        const av = pts[i].bid != null ? pts[i].bid : pts[i].val;
        const bv = pts[i+1].bid != null ? pts[i+1].bid : pts[i+1].val;
        const t = (bx - ax) > 0 ? (dataX - ax) / (bx - ax) : 0;
        const val = av + t * (bv - av);
        // For price charts, round to integer cents
        const rounded = pts[0].bid != null ? Math.round(val) : Math.round(val * 100) / 100;
        return {x: dataX, val: rounded, cssX};
      }
    }
    return null;
  }

  function showCrosshair(canvas, clientX) {
    const cm = canvas._chartMap;
    if (!cm) return;
    const interp = interpolateAt(canvas, clientX);
    if (!interp) return;

    const cx = cm.toX(interp.x);
    const cy = cm.toY(interp.val);

    // Redraw chart, then overlay crosshair
    if (cm.redraw) cm.redraw();
    const ctx = canvas.getContext('2d');
    ctx.save();
    // Vertical line
    ctx.setLineDash([2, 2]);
    ctx.strokeStyle = 'rgba(139,148,158,0.5)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(cx, cm.pad.t);
    ctx.lineTo(cx, cm.H - cm.pad.b);
    ctx.stroke();
    // Dot
    ctx.beginPath();
    ctx.arc(cx, cy, 4, 0, Math.PI * 2);
    ctx.fillStyle = '#58a6ff';
    ctx.fill();
    ctx.restore();

    // Label
    const labelId = canvas.id + 'Label';
    const label = document.getElementById(labelId);
    if (label && cm.formatLabel) {
      // Build a fake point for formatLabel
      const fakePoint = {ts: interp.x, x: interp.x, bid: interp.val, val: interp.val};
      label.innerHTML = cm.formatLabel(fakePoint);
      label.style.color = 'var(--text)';
    }
  }

  function clearCrosshair(canvas) {
    const cm = canvas._chartMap;
    // Clear label first, then redraw — if redraw sets a default label, it persists
    const labelId = canvas.id + 'Label';
    const label = document.getElementById(labelId);
    if (label) label.innerHTML = '';
    if (cm && cm.redraw) cm.redraw();
  }

  document.addEventListener('touchstart', e => {
    const canvas = e.target.tagName === 'CANVAS' ? e.target : e.target.closest?.('canvas');
    if (canvas && canvas._chartMap) {
      activeCanvas = canvas;
      _chartTouchActive = true;
      showCrosshair(canvas, e.touches[0].clientX);
      e.preventDefault();
    }
  }, {passive: false});

  document.addEventListener('touchmove', e => {
    if (activeCanvas) {
      showCrosshair(activeCanvas, e.touches[0].clientX);
      e.preventDefault();
    }
  }, {passive: false});

  document.addEventListener('touchend', () => {
    if (activeCanvas) {
      clearCrosshair(activeCanvas);
      activeCanvas = null;
      _chartTouchActive = false;
    }
  });
})();

// ── Bankroll & PnL charts ────────────────────────────────
function drawLineChart(canvasId, data, opts) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || !data.length) return;
  const rect = canvas.getBoundingClientRect();
  if (rect.width < 10) {
    // Canvas not visible yet — defer drawing
    canvas._pendingDraw = {data, opts};
    return;
  }
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  canvas.width = rect.width * dpr;
  canvas.height = 120 * dpr;
  ctx.scale(dpr, dpr);
  const W = rect.width, H = 120;
  const pad = {t: 10, b: 18, l: 4, r: 4};

  ctx.clearRect(0, 0, W, H);

  // Extend data to fill requested time range
  const xMin = opts.xMin || data[0].x;
  const xMax = opts.xMax || data[data.length - 1].x;
  let plotData = [...data];
  if (plotData[0].x > xMin) {
    plotData.unshift({x: xMin, val: plotData[0].val, ts: xMin});
  }
  if (plotData[plotData.length - 1].x < xMax) {
    plotData.push({x: xMax, val: plotData[plotData.length - 1].val, ts: xMax});
  }

  const vals = plotData.map(d => d.val);
  let yMin = Math.min(...vals), yMax = Math.max(...vals);
  const yRange = yMax - yMin;
  if (yRange < 1) { yMin -= 5; yMax += 5; }
  else { yMin -= yRange * 0.05; yMax += yRange * 0.05; }
  const xRange = Math.max(xMax - xMin, 1);

  const toX = (x) => pad.l + ((x - xMin) / xRange) * (W - pad.l - pad.r);
  const toY = (v) => pad.t + (1 - (v - yMin) / (yMax - yMin)) * (H - pad.t - pad.b);
  const fromX = (px) => xMin + ((px - pad.l) / (W - pad.l - pad.r)) * xRange;

  // Zero line for PnL charts
  if (opts.zeroLine && yMin < 0 && yMax > 0) {
    ctx.strokeStyle = 'rgba(139,148,158,0.2)';
    ctx.setLineDash([3, 3]);
    ctx.lineWidth = 0.5;
    ctx.beginPath();
    ctx.moveTo(pad.l, toY(0));
    ctx.lineTo(W - pad.r, toY(0));
    ctx.stroke();
    ctx.setLineDash([]);
  }

  // Reference line (starting value)
  if (opts.refLine) {
    ctx.strokeStyle = 'rgba(88,166,255,0.2)';
    ctx.setLineDash([3, 3]);
    ctx.lineWidth = 0.5;
    ctx.beginPath();
    ctx.moveTo(pad.l, toY(data[0].val));
    ctx.lineTo(W - pad.r, toY(data[0].val));
    ctx.stroke();
    ctx.setLineDash([]);
  }

  // Main line
  const lastVal = plotData[plotData.length - 1].val;
  const firstVal = plotData[0].val;
  const lineColor = opts.colorByVal
    ? (lastVal >= (opts.zeroLine ? 0 : firstVal) ? '#3fb950' : '#f85149')
    : (opts.color || '#58a6ff');

  ctx.strokeStyle = lineColor;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  for (let i = 0; i < plotData.length; i++) {
    const x = toX(plotData[i].x), y = toY(plotData[i].val);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.stroke();

  // Fill
  const lastX = toX(plotData[plotData.length - 1].x);
  const baseY = opts.zeroLine ? toY(0) : H - pad.b;
  ctx.lineTo(lastX, baseY);
  ctx.lineTo(toX(plotData[0].x), baseY);
  ctx.closePath();
  ctx.fillStyle = lineColor === '#3fb950' ? 'rgba(63,185,80,0.06)' :
                  lineColor === '#f85149' ? 'rgba(248,81,73,0.06)' :
                  'rgba(88,166,255,0.06)';
  ctx.fill();

  // Current value dot
  ctx.beginPath();
  ctx.arc(lastX, toY(lastVal), 3, 0, Math.PI * 2);
  ctx.fillStyle = lineColor;
  ctx.fill();

  // Y labels (min/max)
  ctx.font = '9px monospace';
  ctx.fillStyle = 'rgba(139,148,158,0.5)';
  ctx.textAlign = 'right';
  ctx.fillText(opts.fmtVal(yMax), W - pad.r - 2, pad.t + 8);
  ctx.fillText(opts.fmtVal(yMin), W - pad.r - 2, H - pad.b - 2);
  ctx.textAlign = 'left';

  // Store chart map for touch interaction
  const redraw = () => drawLineChart(canvasId, data, opts);
  canvas._chartMap = {
    data: plotData, pad, W: W, H: H,
    toX: (x) => pad.l + ((x - xMin) / xRange) * (W - pad.l - pad.r),
    toY: (v) => pad.t + (1 - (v - yMin) / (yMax - yMin)) * (H - pad.t - pad.b),
    fromX: (cssX) => xMin + ((cssX - pad.l) / (W - pad.l - pad.r)) * xRange,
    redraw,
    formatLabel: (d) => {
      const dt = new Date(d.x);
      const timeStr = dt.toLocaleString('en-US', {
        timeZone: 'America/Chicago', hour: 'numeric', minute: '2-digit', hour12: true
      });
      const valStr = opts.fmtVal(d.val);
      const color = opts.colorByVal
        ? (d.val >= (opts.zeroLine ? 0 : firstVal) ? 'var(--green)' : 'var(--red)')
        : 'var(--text)';
      return `<span style="color:${color}">${valStr}</span> · ${timeStr}`;
    }
  };
}

async function loadBankrollChart(hours, btn) {
  if (btn) {
    document.querySelectorAll('#bkChartFilters .chip').forEach(c => c.className = 'chip');
    btn.className = 'chip active';
  }
  const url = hours ? `/api/chart/bankroll?hours=${hours}` : '/api/chart/bankroll';
  const data = await api(url);
  if (!data.length) return;
  const mapped = data.map(d => ({
    x: new Date(d.captured_at.replace('Z','+00:00')).getTime(),
    val: d.bankroll_cents / 100,
    ts: new Date(d.captured_at.replace('Z','+00:00')).getTime(),
  }));
  const chartOpts = {
    refLine: true, colorByVal: false, color: '#58a6ff',
    fmtVal: (v) => '$' + v.toFixed(2),
    xMax: Date.now(),
  };
  if (hours) {
    chartOpts.xMin = Date.now() - hours * 3600000;
  }
  drawLineChart('bankrollChart', mapped, chartOpts);
}

async function loadPnlChart(hours, btn) {
  if (btn) {
    document.querySelectorAll('#pnlChartFilters .chip').forEach(c => c.className = 'chip');
    btn.className = 'chip active';
  }
  const url = hours ? `/api/chart/pnl?hours=${hours}` : '/api/chart/pnl';
  const data = await api(url);
  if (!data.length) return;
  const mapped = data.map(d => ({
    x: new Date(d.ts.replace('Z','+00:00')).getTime(),
    val: d.pnl,
    ts: new Date(d.ts.replace('Z','+00:00')).getTime(),
  }));
  const chartOpts = {
    zeroLine: true, colorByVal: true,
    fmtVal: (v) => (v >= 0 ? '+' : '-') + '$' + Math.abs(v).toFixed(2),
    xMax: Date.now(),
  };
  if (hours) {
    chartOpts.xMin = Date.now() - hours * 3600000;
  }
  drawLineChart('pnlChart', mapped, chartOpts);
}

// ── API helpers ───────────────────────────────────────────
function _getCsrfToken() {
  const m = document.cookie.match(/bot_csrf=([^;]+)/);
  return m ? m[1] : '';
}
async function api(path, opts) {
  if (!opts) opts = {};
  if (opts.method && opts.method !== 'GET') {
    if (!opts.headers) opts.headers = {};
    if (!opts.headers['X-CSRF-Token']) opts.headers['X-CSRF-Token'] = _getCsrfToken();
  }
  const r = await fetch(path, opts);
  if (r.status === 401) { location.reload(); throw new Error('Unauthorized'); }
  if (r.status === 403) {
    const d = await r.clone().json().catch(() => ({}));
    if (d.error && d.error.includes('CSRF')) {
      showToast('Session expired, reloading...', 'yellow');
      setTimeout(() => location.reload(), 1000);
      throw new Error('CSRF expired');
    }
  }
  // Handle non-JSON responses (e.g. nginx 502/504 timeout HTML pages)
  const ct = (r.headers.get('content-type') || '');
  if (r.status >= 500 || !ct.includes('application/json')) {
    const txt = await r.text().catch(() => '');
    if (r.status === 502 || r.status === 504 || txt.includes('gateway') || txt.includes('timed out')) {
      throw new Error(`Server timeout (${r.status}) — this operation may need more time`);
    }
    // Try parsing as JSON anyway (some responses don't set content-type)
    try { return JSON.parse(txt); } catch(_) {}
    throw new Error(`Server error ${r.status}: ${txt.substring(0, 120)}`);
  }
  return r.json();
}
// Rapid poll burst after user actions to catch state transitions fast
async function cmd(command, params={}) {
  try {
    await api('/api/command', {
      method: 'POST',
      headers:{'Content-Type':'application/json','X-CSRF-Token':_getCsrfToken()},
      body: JSON.stringify({command, params})
    });
  } catch(e) {
    console.error('cmd error:', e);
  }
  // Server truth-sync after API processes (not for instant UI — patchUI handles that)
  setTimeout(pollState, 300);
}
async function saveSetting(key, val) {
  await api('/api/config', {
    method: 'POST',
    headers:{'Content-Type':'application/json','X-CSRF-Token':_getCsrfToken()},
    body: JSON.stringify({[key]: val})
  });
  // Refresh state after bot processes the config change
  setTimeout(pollState, 800);
}

function toggleObserveOnly(on, skipSave) {
  // Legacy — now handled by setTradingMode
  if (!skipSave) saveSetting('observe_only', on);
}

// ── Trading Mode Selector ────────────────────────────────
const MODE_META = {
  observe: { label: 'Observe', color: 'var(--blue)', toast: 'Observe mode — recording data', autoStart: true },
  shadow:  { label: 'Shadow',  color: '#a371f7',       toast: 'Shadow mode — 1-contract trades', autoStart: true },
  hybrid:  { label: 'Hybrid',  color: 'var(--blue)',    toast: 'Hybrid mode — auto + shadow fallback', autoStart: true },
  auto:    { label: 'Auto',    color: 'var(--green)',   toast: 'Auto mode — full trades only', autoStart: false },
  manual:  { label: 'Manual',  color: 'var(--text)',    toast: 'Manual mode — picker strategy', autoStart: false },
};

function _syncModeStrip(mode) {
  document.querySelectorAll('#modeStrip .mode-btn').forEach(b => {
    const m = b.dataset.mode;
    b.className = 'mode-btn' + (m === mode ? ` m-active-${m}` : '');
  });
  // Update settings page display
  const sd = document.getElementById('settingsModeDisplay');
  if (sd) {
    const meta = MODE_META[mode] || MODE_META.observe;
    sd.innerHTML = `Current: <strong style="color:${meta.color}">${meta.label}</strong>`;
  }
}

async function setTradingMode(mode) {
  if (!MODE_META[mode]) return;
  const meta = MODE_META[mode];
  // Save trading_mode (backend derives legacy booleans)
  await saveSetting('trading_mode', mode);
  _uiState.trading_mode = mode;
  // Derive observe_only locally for immediate UI update
  _uiState.observe_only = (mode === 'observe' || mode === 'shadow');
  _syncModeStrip(mode);
  // Auto-start in data-collecting modes if not already running
  if (meta.autoStart && !_uiState.auto_trading) {
    cmd('start', {mode: 'continuous', count: 0});
  }
  // Update strategy picker lock state
  _updateAutoStrategyLock();
  const toastColor = mode === 'observe' ? 'blue' : mode === 'shadow' ? 'purple' :
                     mode === 'hybrid' ? 'blue' : mode === 'auto' ? 'green' : 'yellow';
  showToast(meta.toast, toastColor);
  setTimeout(pollState, 800);
}

async function svcControl(action, service) {
  showToast(`${action}ing ${service === 'all' ? 'all services' : service}...`, 'blue');
  try {
    const resp = await fetch('/api/server/control', {
      method: 'POST',
      headers:{'Content-Type':'application/json','X-CSRF-Token':_getCsrfToken()},
      body: JSON.stringify({action, service})
    });
    if (!resp.ok) {
      const txt = await resp.text();
      showToast(`Error: ${resp.status}`, 'red');
      console.error('svcControl error:', txt);
      return;
    }
    const r = await resp.json();
    const msgs = Object.entries(r).map(([k,v]) => `${k}: ${v}`).join(', ');
    showToast(msgs.substring(0, 60), action === 'stop' ? 'red' : 'green');
    if (service === 'platform-dashboard' || service === 'all') {
      if (action === 'restart' || action === 'stop') {
        setTimeout(() => location.reload(), 3000);
      }
    }
    setTimeout(loadSvcStatus, 2000);
    setTimeout(loadSystemStats, 2500);
  } catch(e) {
    console.error('svcControl catch:', e);
    showToast('Service control failed: ' + e.message, 'red');
  }
}

async function loadSvcStatus() {
  try {
    const r = await api('/api/server/status');
    const botEl = $('#svcBotStatus');
    const dashEl = $('#svcDashStatus');
    if (r['plugin-btc-15m']) {
      const s = r['plugin-btc-15m'];
      const color = s.status === 'RUNNING' ? 'var(--green)' : 'var(--red)';
      botEl.innerHTML = `<span style="color:${color}">${s.status}</span> <span class="dim">${s.detail || ''}</span>`;
    }
    if (r['platform-dashboard']) {
      const s = r['platform-dashboard'];
      const color = s.status === 'RUNNING' ? 'var(--green)' : 'var(--red)';
      dashEl.innerHTML = `<span style="color:${color}">${s.status}</span> <span class="dim">${s.detail || ''}</span>`;
    }
  } catch(e) { console.debug('svc status error', e); }
}

async function loadSystemStats() {
  try {
    const r = await api('/api/system/stats');
    const el = $('#srvStats');
    const upEl = $('#srvUptime');
    if (!el) return;

    let html = '';

    // Helper: progress bar
    function bar(pct, label, detail, color) {
      const c = pct > 90 ? 'var(--red)' : pct > 75 ? 'var(--orange)' : (color || 'var(--blue)');
      return `<div style="margin-bottom:10px">
        <div style="display:flex;justify-content:space-between;margin-bottom:3px">
          <span style="font-weight:500">${label}</span>
          <span class="dim">${detail}</span>
        </div>
        <div style="background:var(--bg);border-radius:4px;height:6px;overflow:hidden">
          <div style="background:${c};height:100%;width:${Math.min(pct,100)}%;border-radius:4px;transition:width 0.3s"></div>
        </div>
      </div>`;
    }

    // Disk
    if (r.disk) {
      const d = r.disk;
      let detail = `${d.used_gb}/${d.total_gb} GB`;
      if (d.db_mb !== undefined) detail += ` · DB ${d.db_mb}MB`;
      if (d.log_mb !== undefined) detail += ` · Log ${d.log_mb}MB`;
      html += bar(d.pct, 'Disk', detail);
    }

    // Memory
    if (r.memory) {
      const m = r.memory;
      html += bar(m.pct, 'Memory', `${m.used_mb}/${m.total_mb} MB`);
    }

    // CPU
    if (r.cpu) {
      const c = r.cpu;
      html += bar(c.pct, 'CPU', `${c.pct}% · Load ${c.load_1m}/${c.load_5m}/${c.load_15m}`);
    }

    // Network
    if (r.network) {
      const n = r.network;
      html += `<div style="display:flex;justify-content:space-between;font-size:11px;color:var(--dim)">
        <span>↓ ${n.rx_kbps} KB/s · ${n.rx_total_gb} GB total</span>
        <span>↑ ${n.tx_kbps} KB/s · ${n.tx_total_gb} GB total</span>
      </div>`;
    }

    el.innerHTML = html || '<div class="dim">Unable to read system stats</div>';

    // Uptime
    if (upEl && r.uptime) {
      upEl.textContent = 'uptime ' + r.uptime.display;
    }
  } catch(e) { console.debug('system stats error', e); }
}

// ── UI helpers ───────────────────────────────────────────
function toggleDetail(id) {
  const el = document.getElementById(id);
  const toggle = el.previousElementSibling;
  if (el.style.display === 'block') {
    el.style.display = 'none';
    toggle.textContent = toggle.textContent.replace('▾', '▸');
  } else {
    el.style.display = 'block';
    toggle.textContent = toggle.textContent.replace('▸', '▾');
    // Flush pending chart draws
    setTimeout(() => {
      el.querySelectorAll('canvas').forEach(c => {
        if (c._pendingDraw) {
          drawLineChart(c.id, c._pendingDraw.data, c._pendingDraw.opts);
          c._pendingDraw = null;
        }
      });
    }, 50);
  }
}

function toggleCard(h3El) {
  const card = h3El.closest('.collapsible');
  if (card) {
    card.classList.toggle('collapsed');
    // Save state
    const key = h3El.textContent.trim().replace(/[▾▸]/g,'').trim();
    try {
      const saved = JSON.parse(localStorage.getItem('cardStates') || '{}');
      saved[key] = card.classList.contains('collapsed');
      localStorage.setItem('cardStates', JSON.stringify(saved));
    } catch(e) {}
    // Flush pending chart draws when card opens
    if (!card.classList.contains('collapsed')) {
      setTimeout(() => {
        card.querySelectorAll('canvas').forEach(c => {
          if (c._pendingDraw) {
            drawLineChart(c.id, c._pendingDraw.data, c._pendingDraw.opts);
            c._pendingDraw = null;
          }
        });
      }, 50);
    }
  }
}
// Restore card states on load
try {
  const saved = JSON.parse(localStorage.getItem('cardStates') || '{}');
  document.querySelectorAll('.collapsible > h3').forEach(h3 => {
    const key = h3.textContent.trim().replace(/[▾▸]/g,'').trim();
    if (saved[key] === true) h3.closest('.collapsible').classList.add('collapsed');
    else if (saved[key] === false) h3.closest('.collapsible').classList.remove('collapsed');
  });
} catch(e) {}

// ── Status detail SVG icons ──────────────────────────────
const SVG_SKIP = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px;margin-right:4px"><path d="M5 4l10 8-10 8V4zM19 5v14"/></svg>';
const SVG_WARN = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px;margin-right:4px"><path d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/></svg>';
const SVG_CHECK = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px;margin-right:4px"><path d="M20 6L9 17l-5-5"/></svg>';
const SVG_XMARK = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px;margin-right:4px"><path d="M18 6L6 18M6 6l12 12"/></svg>';
const SVG_PAUSE = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px;margin-right:4px"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>';
const SVG_BOLT = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px;margin-right:4px"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>';
const SVG_INFO = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px;margin-right:4px"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4m0-4h.01"/></svg>';

function statusIcon(detail) {
  if (detail.includes('Observing') || detail.includes('Observed')) return SVG_SKIP;
  if (detail.includes('Skipped') || detail.includes('Cooldown')) return SVG_SKIP;
  if (detail.includes('MAX LOSS') || detail.includes('insufficient') || detail.includes('below') || detail.includes('above') || detail.includes('Error') || detail.includes('CASHING')) return SVG_WARN;
  if (detail.includes('WIN') || detail.includes('+$') || detail.includes('completed')) return SVG_CHECK;
  if (detail.includes('LOSS')) return SVG_XMARK;
  if (detail.includes('Stopping') || detail.includes('Cancelling')) return SVG_PAUSE;
  if (detail.includes('Stopped')) return SVG_XMARK;
  if (detail.includes('Cashed out')) return SVG_BOLT;
  return SVG_INFO;
}

function riskTag(level) {
  const labels = {low:'LOW RISK', moderate:'MODERATE RISK', high:'HIGH RISK', terrible:'EXTREME RISK', unknown:'UNKNOWN RISK'};
  return `<span class="regime-tag risk-${level||'unknown'}">${labels[level] || 'UNKNOWN RISK'}</span>`;
}

function trendLabel(t) {
  if (t === null || t === undefined) return '—';
  const labels = {'-3':'Strong ↓','-2':'Mod ↓','-1':'Weak ↓','0':'Ranging',
                  '1':'Weak ↑','2':'Mod ↑','3':'Strong ↑'};
  return labels[String(t)] || String(t);
}

function confirmResetStreak() {
  openModal('resetStreakOverlay');
}

function doResetStreak() {
  closeModal('resetStreakOverlay');
  api('/api/command', {method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':_getCsrfToken()},
    body: JSON.stringify({command:'reset_streak', params:{}})});
  showToast('Loss streak reset', 'blue');
  setTimeout(pollState, 500);
}

async function copyLLMSummary(detailed) {
  showToast(detailed ? 'Generating detailed report...' : 'Generating summary...', 'blue');
  try {
    const r = await api(`/api/llm_summary?detailed=${detailed ? 1 : 0}`);
    if (r.text) {
      // Try clipboard first
      let copied = false;
      try {
        await copyToClipboard(r.text);
        copied = true;
      } catch(e) {}

      if (copied) {
        showToast(`Copied ${r.text.split('\\n').length} lines`, 'green');
      } else {
        // Fallback: show overlay
        const existing = document.getElementById('exportTextarea');
        if (existing) existing.remove();
        const wrap = document.createElement('div');
        wrap.id = 'exportTextarea';
        wrap.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,0.9);' +
          'display:flex;flex-direction:column;padding:12px';
        const info = document.createElement('div');
        info.style.cssText = 'color:var(--blue);font-size:13px;margin-bottom:6px;text-align:center';
        info.textContent = `${r.text.split('\\n').length} lines — select all and copy`;
        const ta = document.createElement('textarea');
        ta.value = r.text;
        ta.style.cssText = 'flex:1;font-size:11px;font-family:monospace;background:var(--card);' +
          'color:var(--text);border:1px solid var(--border);border-radius:6px;padding:10px;resize:none';
        ta.readOnly = true;
        const btn = document.createElement('button');
        btn.textContent = 'Close';
        btn.style.cssText = 'margin-top:8px;padding:10px;background:var(--border);color:var(--text);' +
          'border:none;border-radius:6px;font-size:14px';
        btn.onclick = () => wrap.remove();
        wrap.appendChild(info);
        wrap.appendChild(ta);
        wrap.appendChild(btn);
        document.body.appendChild(wrap);
        ta.focus();
        ta.select();
      }
    }
  } catch(e) {
    showToast('Failed: ' + e, 'red');
  }
}

function _refreshModalBalances() {
  const rawBal = (_uiState.bankroll_cents || 0) / 100;
  const el = $('#bkmEffective');
  if (el) {
    el.textContent = '$' + rawBal.toFixed(2);
    $('#bkmTotal').textContent = '$' + rawBal.toFixed(2);
  }
}

function showBtnLoading(selector, text) {
  const btn = $(selector);
  if (!btn) return;
  btn.disabled = true;
  // Auto-restore after 5s (pollState will also fix disabled state)
  setTimeout(() => { /* pollState handles re-enable */ }, 5000);
}

function showToast(msg, color) {
  let t = document.getElementById('mainToast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'mainToast';
    t.style.cssText = 'position:fixed;top:env(safe-area-inset-top,48px);left:50%;transform:translateX(-50%);' +
      'padding:6px 14px;border-radius:6px;border:1px solid transparent;' +
      'font-size:13px;font-weight:600;opacity:0;transition:opacity 0.3s;z-index:200;pointer-events:none;' +
      'backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);';
    document.body.appendChild(t);
  }
  const colors = {
    green:  {bg: 'rgba(63,185,80,0.12)',  border: 'rgba(63,185,80,0.4)',  fg: 'var(--green)'},
    red:    {bg: 'rgba(248,81,73,0.12)',   border: 'rgba(248,81,73,0.4)', fg: 'var(--red)'},
    yellow: {bg: 'rgba(210,153,34,0.12)',  border: 'rgba(210,153,34,0.4)', fg: 'var(--yellow)'},
    blue:   {bg: 'rgba(88,166,255,0.12)',  border: 'rgba(88,166,255,0.4)', fg: 'var(--blue)'},
    orange: {bg: 'rgba(240,136,62,0.12)',  border: 'rgba(240,136,62,0.4)', fg: 'var(--orange)'},
    purple: {bg: 'rgba(163,113,247,0.12)', border: 'rgba(163,113,247,0.4)', fg: '#a371f7'},
  };
  const c = colors[color] || colors.green;
  t.style.background = c.bg;
  t.style.borderColor = c.border;
  t.style.color = c.fg;
  t.textContent = msg;
  t.style.opacity = '1';
  setTimeout(() => t.style.opacity = '0', 2000);
}


// ── Win Probability Estimator ─────────────────────────────
function estimateWinProb(curBid, sellTarget, minsLeft, entryPrice, highWater) {
  // Already at or above target
  if (curBid >= sellTarget) return 0.99;
  // No time left
  if (minsLeft <= 0.1) return 0.01;
  // Bad inputs
  if (sellTarget <= 0 || curBid <= 0) return 0;

  // Base probability estimate
  // P(ever reach B | start at A) ≈ A/B
  const base = curBid / sellTarget;

  // Time factor: more time = more chances for price to reach target
  // Exponential approach — at 10+ min nearly full odds, decays toward close
  const timeFactor = 1 - Math.exp(-minsLeft / 3.5);

  // Momentum: if HWM shows price has already climbed toward target, slight boost
  let momentum = 1.0;
  if (highWater > entryPrice && sellTarget > entryPrice) {
    const hwProgress = Math.min((highWater - entryPrice) / (sellTarget - entryPrice), 1);
    momentum = 1 + 0.15 * hwProgress;
  }

  const prob = base * timeFactor * momentum;
  return Math.max(0.01, Math.min(0.99, prob));
}

// ── Exposure Calculator ──────────────────────────────────
// ═══════════════════════════════════════════════════════════
// UI STATE & RENDERING
// ═══════════════════════════════════════════════════════════
// Single source of truth for UI. Modified by:
// 1. Server polls (full state from /api/state)
// 2. Optimistic patches from user actions (immediate, before API call)
let _uiState = {};

function patchUI(partial) {
  // Merge partial into _uiState and re-render immediately
  if (partial.active_trade !== undefined) _uiState.active_trade = partial.active_trade;
  if (partial.active_skip !== undefined) _uiState.active_skip = partial.active_skip;
  if (partial.pending_trade !== undefined) _uiState.pending_trade = partial.pending_trade;
  if (partial.auto_trading !== undefined) _uiState.auto_trading = partial.auto_trading;
  if (partial.status !== undefined) _uiState.status = partial.status;
  if (partial.status_detail !== undefined) _uiState.status_detail = partial.status_detail;
  lastStateData._lastState = _uiState;
  renderUI(_uiState);
}

// ── Edge Scale Table ─────────────────────────────────────

// ── State polling ────────────────────────────────────────
let _polling = false;
async function pollState() {
  if (_polling) return;
  _polling = true;
  try {
    const s = await api('/api/state');
    _uiState = s;
    lastStateData._lastState = s;
    renderUI(s);
  } catch(e) {
    console.error('Poll error:', e);
  } finally {
    _polling = false;
  }
}

function renderUI(s) {
  try {

    // Hide skeleton on first real data
    hideSkel('skelHome');

    // Status bar
    const st = s.status || 'stopped';

    // Enable/disable start and stop buttons
    const autoOn = s.auto_trading;
    const isRunning = !!autoOn;
    const hasActiveTrade = !!s.active_trade;
    const hasPendingTrade = !!s.pending_trade;
    const observeOnly = !!s.observe_only;
    const tradingMode = s.trading_mode || 'observe';

    // Sync mode strip highlight
    _syncModeStrip(tradingMode);

    // ── Build rich status text ──
    const detail = s.status_detail || '';

    const at0 = s.active_trade;

    let statusMain = '';
    let statusColor = '';
    let dotClass = 'dot-red';

    // ── Bot staleness detection ──
    const _offBanner = $('#offlineBanner');
    let _botStale = false;
    if (s.last_updated) {
      const _luDt = new Date(s.last_updated.replace(' ', 'T').replace('Z', '+00:00'));
      const _staleSec = (Date.now() - _luDt.getTime()) / 1000;
      if (_staleSec > 90) {
        _botStale = true;
        const _staleMin = Math.floor(_staleSec / 60);
        const _staleLabel = _staleMin >= 60 ? `${Math.floor(_staleMin/60)}h ${_staleMin%60}m` : `${_staleMin}m`;
        $('#offlineText').textContent = `Bot Offline — no heartbeat for ${_staleLabel}`;
        _offBanner.style.display = '';
      } else {
        _offBanner.style.display = 'none';
      }
    }

    const _modeColors = {observe:'var(--blue)',shadow:'#a371f7',hybrid:'var(--blue)',auto:'var(--green)',manual:'var(--text)'};
    const _modeDots = {observe:'dot-blue',shadow:'dot-purple',hybrid:'dot-blue',auto:'dot-green',manual:'dot-yellow'};
    const _modeLabels = {observe:'Observing',shadow:'Shadow',hybrid:'Hybrid',auto:'Auto',manual:'Manual'};

    if (_botStale) {
      statusMain = 'Offline';
      statusColor = 'var(--red)';
      dotClass = 'dot-red';
    } else if (!isRunning) {
      if (tradingMode === 'observe' || tradingMode === 'shadow' || tradingMode === 'hybrid') {
        statusMain = _modeLabels[tradingMode] || 'Idle';
        dotClass = _modeDots[tradingMode] || 'dot-blue';
        statusColor = _modeColors[tradingMode] || 'var(--dim)';
      } else {
        statusMain = 'Stopped';
        dotClass = 'dot-red';
        statusColor = 'var(--dim)';
      }
    } else {
      statusMain = _modeLabels[tradingMode] || 'Running';
      dotClass = _modeDots[tradingMode] || 'dot-blue';
      statusColor = _modeColors[tradingMode] || 'var(--blue)';
      // Append countdown if a market is active
      var _closeT = (s.active_trade && s.active_trade.close_time) || (s.live_market && s.live_market.close_time);
      if (_closeT) {
        var _ctMs = new Date(_closeT.replace('Z','+00:00')).getTime() - Date.now();
        if (_ctMs > 0) {
          var _ctM = Math.floor(_ctMs / 60000);
          var _ctS = Math.floor((_ctMs % 60000) / 1000);
          statusMain += ' \u00b7 ' + _ctM + ':' + (_ctS < 10 ? '0' : '') + _ctS;
        }
      }
    }

    // Render
    $('#statusDot').className = 'status-dot ' + dotClass;
    const stEl = $('#statusText');
    stEl.textContent = statusMain;
    stEl.style.color = statusColor || '';

    // Bankroll
    const rawBal = (s.bankroll_cents || 0) / 100;
    currentBankroll = rawBal;
    $('#hdrBal').textContent = '$' + rawBal.toFixed(2);

    _adjustContentTop();

    // ── Active Trade vs Summary vs Live Monitor ───────────
    const tc = $('#tradeCard');
    const mc = $('#monitorCard');
    const pc = $('#pendingCard');
    const at = s.active_trade;
    const lct = s.last_completed_trade;
    const pt = s.pending_trade;

    // Refresh trades/charts when a new trade completes
    if (lct && lct.trade_id !== lastStateData._lastSummaryId) {
      lastStateData._lastSummaryId = lct.trade_id;
      lastStateData._lastTrade = lct;  // Cache for status display
      loadTrades(); loadRegimes(); loadLifetimeStats();
    }

    // Exactly ONE card visible: active > pending > live
    if (at) {
      tc.style.display = '';
      mc.style.display = 'none';
      pc.style.display = 'none';
      lastStateData._monCloseTime = null;
      lastStateData._nextMarketOpen = at.close_time;

      // Keep feeding live prices even during active trade so chart has data when trade ends
      const lmDuring = s.live_market;
      if (lmDuring && lmDuring.ticker) {
        pushLivePrice(lmDuring.ticker, lmDuring.close_time, lmDuring.yes_ask, lmDuring.no_ask, lmDuring.yes_bid, lmDuring.no_bid);
      }

      // Resolving state — dim the card
      const isResolving = at.resolving;
      tc.style.opacity = isResolving ? '0.6' : '1';

      // No manual controls in auto-only mode
      const isIgnoredStop = at.is_ignored && !autoOn;
      $('#stoppingBanner').style.display = isIgnoredStop ? '' : 'none';

      // Regime bar
      $('#tradeRisk').outerHTML = riskTag(at.risk_level);
      const newRisk = document.querySelector('#tradeCard .regime-tag');
      if (newRisk) newRisk.id = 'tradeRisk';
      const _trObsN = at.regime_obs_n || 0;
      const _trObsLabel = _trObsN > 0 ? ` <span class="dim" style="font-size:10px;font-weight:400">n=${_trObsN}</span>` : '';
      $('#tradeRegimeLabel').innerHTML = (at.regime_label || 'unknown').replace(/_/g, ' ') + _trObsLabel;
      const wr = ((at.regime_win_rate||0)*100).toFixed(0);
      $('#tradeRegimeStats').textContent = at.regime_trades ?
        `${wr}% win (n=${at.regime_trades})` : '';

      // Fair Value Model edge (shown when model side was used)
      const fvTradeEl = document.getElementById('tradeModelEdge');
      if (fvTradeEl) {
        if (at.model_edge != null) {
          const me = at.model_edge;
          const mev = at.model_ev || 0;
          fvTradeEl.innerHTML = `<span style="font-size:9px;background:rgba(136,132,216,0.25);color:rgba(136,132,216,0.9);padding:1px 5px;border-radius:3px;font-weight:600">MODEL</span> <span style="font-size:11px;color:var(--green)">+${me.toFixed(1)}% edge</span> <span class="dim" style="font-size:10px">EV ${mev >= 0 ? '+' : ''}${mev.toFixed(1)}¢</span>`;
          fvTradeEl.style.display = '';
        } else {
          fvTradeEl.style.display = 'none';
        }
      }

      // Dynamic sell status (shown when dynamic sell is active)
      const dsStat = document.getElementById('tradeDynamicSell');
      if (dsStat) {
        if (at.dynamic_sell && at.dynamic_fv != null) {
          const dsFv = at.dynamic_fv;
          const dsAdj = at.dynamic_adjustments || 0;
          const dsInit = at.dynamic_initial || at.sell_price_c;
          const dsCur = at.sell_price_c || dsInit;
          const dsChange = dsCur - dsInit;
          const dsColor = dsChange > 0 ? 'var(--green)' : dsChange < 0 ? 'var(--red)' : 'var(--dim)';
          dsStat.innerHTML = `<span style="font-size:9px;background:rgba(255,166,0,0.2);color:var(--orange);padding:1px 5px;border-radius:3px;font-weight:600">DYNAMIC</span> <span style="font-size:11px">FV ${dsFv}¢ → sell@${dsCur}¢</span> <span style="color:${dsColor};font-size:10px">${dsChange >= 0 ? '+' : ''}${dsChange}¢</span> <span class="dim" style="font-size:10px">(${dsAdj} adj)</span>`;
          dsStat.style.display = '';
        } else {
          dsStat.style.display = 'none';
        }
      }

      // Main stats
      const tradeSide = (at.side||'').toUpperCase();
      const tradeSideCls = at.side === 'yes' ? 'side-yes' : 'side-no';
      $('#tradeSide').innerHTML = `<span class="${tradeSideCls}">${tradeSide}</span> @ ${at.avg_price_c}¢`;
      const bid = at.current_bid != null ? at.current_bid : (at.avg_price_c || 0);
      const bidEl = $('#tradeBid');
      bidEl.textContent = bid + '¢';
      bidEl.className = 'val price-display ' + (bid >= (at.sell_price_c||999) ? 'pos' :
        bid > at.avg_price_c ? 'pos' : bid < at.avg_price_c ? 'neg' : '');
      $('#tradeSell').textContent = at.is_hold_to_expiry ? 'HOLD' : at.sell_price_c ? at.sell_price_c + '¢' : (at.hold_to_close ? 'Hold' : '—');

      // Strategy badge (auto or manual)
      const asEl = document.getElementById('tradeAutoStrategy');
      if (asEl) {
        if (at.auto_strategy) {
          asEl.innerHTML = `<span style="font-size:9px;background:var(--blue);color:#fff;padding:1px 5px;border-radius:3px;font-weight:600">AUTO</span> <span style="font-size:11px">${at.auto_strategy}</span> <span class="dim" style="font-size:10px">EV ${(at.auto_strategy_ev||0)>=0?'+':''}${(at.auto_strategy_ev||0).toFixed(1)}\u00a2</span>`;
          asEl.style.display = '';
        } else if (at.strategy_key) {
          const _sk = typeof _fmtStratKey === 'function' ? _fmtStratKey(at.strategy_key) : at.strategy_key;
          asEl.innerHTML = `<span style="font-size:10px;color:var(--dim);font-family:monospace">${_sk}</span>`;
          asEl.style.display = '';
        } else {
          asEl.style.display = 'none';
        }
      }

      // Market and time
      $('#tradeMarket').textContent = at.close_time ? marketStartTime(at.close_time) : '—';
      if (lastStateData._tradeCloseTime !== at.close_time) {
        lastStateData._tradeCloseTime = at.close_time;
        lastStateData._tradeEndFired = (lastStateData._tradeFiredForClose === at.close_time);
      }
      tickCountdown('tradeTime', at.close_time);

      $('#tradeCost').textContent = '$' + (at.actual_cost || 0).toFixed(2);
      $('#tradeHwm').textContent = (at.high_water_c || 0) + '¢';
      $('#tradeShares').textContent = at.fill_count || '—';

      // Win probability estimate
      const sellC = at.sell_price_c || 0;
      const entryC = at.avg_price_c || 0;
      const hwmC = at.high_water_c || entryC;
      const mLeft = at.minutes_left != null ? at.minutes_left : 15;
      const wpBar = $('#winProbBar');
      const wpPct = $('#winProbPct');
      if (sellC > 0 && bid > 0 && !at.hold_to_close && !at.is_hold_to_expiry) {
        const wp = estimateWinProb(bid, sellC, mLeft, entryC, hwmC);
        const wpVal = Math.round(wp * 100);
        wpPct.textContent = wpVal + '%';
        wpPct.style.color = wpVal >= 60 ? 'var(--green)' : wpVal >= 35 ? 'var(--yellow)' : 'var(--red)';
        wpBar.style.width = wpVal + '%';
        wpBar.style.background = wpVal >= 60 ? 'var(--green)' : wpVal >= 35 ? 'var(--yellow)' : 'var(--red)';
      } else {
        wpPct.textContent = '—';
        wpPct.style.color = '';
        wpBar.style.width = '0%';
      }

      // Progress bar (only if sell set)
      let pct = 0;
      if (at.sell_price_c && at.sell_price_c > at.avg_price_c) {
        pct = Math.max(0, Math.min(100, ((bid - at.avg_price_c) / (at.sell_price_c - at.avg_price_c)) * 100));
      }
      const pbar = $('#tradeProgress');
      pbar.style.width = pct + '%';
      pbar.style.background = pct >= 100 ? 'var(--green)' : pct >= 50 ? 'var(--yellow)' : 'var(--blue)';

      $('#sellProgress').textContent = `${at.sell_progress||0}/${at.fill_count||0} sold`;
      const estGross = bid > 0 ? (at.fill_count||0) * bid / 100 : 0;
      const estPnl = estGross - (at.actual_cost||0);
      const pnlEl = $('#tradeEstPnl');
      pnlEl.textContent = fmtPnl(estPnl);
      pnlEl.className = estPnl >= 0 ? 'pos' : 'neg';
      tc.style.borderLeftColor = estPnl >= 0 ? 'var(--green)' : 'var(--red)';

      // Detail section
      $('#tradeRegimeGrid').innerHTML = buildRegimeGrid(at);
      
      // hole/target removed



      // Spread info
      const lm2 = s.live_market;
      if (lm2) {
        const yb2 = lm2.yes_bid || 0, ya2 = lm2.yes_ask || 0;
        const nb2 = lm2.no_bid || 0, na2 = lm2.no_ask || 0;
        const spread2 = at.side === 'yes'
          ? `Bid/Ask: ${yb2}–${ya2}¢`
          : `Bid/Ask: ${nb2}–${na2}¢`;
        $('#tradeSpread').textContent = spread2;
      }

      // Bankroll in-trade info
      const atCost = at.actual_cost || 0;
      const atSellGross = (at.fill_count||0) * (at.sell_price_c||0) / 100;
      const atWinPnl = atSellGross - atCost;
      const atResolvePnl = (at.fill_count||0) * 1 - atCost; // 100¢ resolution
      const bkInfo = `If sell fills: <span class="pos">${fmtPnl(atWinPnl)}</span> · If resolves: <span class="pos">${fmtPnl(atResolvePnl)}</span>`;
      $('#tradeBankInfo').innerHTML = bkInfo;

      // Price chart — timeline based
      const tid = at.trade_id;
      if (tid !== chartTradeId) {
        chartData = [];
        chartTradeId = tid;
        // Compute market window from close time
        if (at.close_time) {
          const closeMs = new Date(at.close_time.replace('Z','+00:00')).getTime();
          chartEndMs = closeMs;
          chartStartMs = closeMs - 15 * 60 * 1000;
        }

        // Seed chart with pre-entry data from live price buffer
        // This fills the left side of the chart before our trade entered
        const tradeSide = at.side || 'yes';
        if (_livePriceBuf.data.length > 0) {
          for (const pt of _livePriceBuf.data) {
            const ptBid = tradeSide === 'yes' ? (pt.yb || 0) : (pt.nb || 0);
            if (ptBid > 0 && pt.ts >= chartStartMs) {
              chartData.push({ts: pt.ts, bid: ptBid});
            }
          }
        }

        // Load historical price path (from DB — recorded during trade monitoring)
        if (tid) {
          api(`/api/trade/${tid}/price_path`).then(path => {
            if (path && path.length && chartTradeId === tid) {
              const hist = [];
              for (const p of path) {
                if (p.our_side_bid > 0 && p.captured_at) {
                  hist.push({ts: new Date(p.captured_at.replace('Z','+00:00')).getTime(),
                             bid: p.our_side_bid});
                }
              }
              // Merge: keep pre-entry data, add price path, then live data
              const preEntryEnd = hist.length > 0 ? hist[0].ts : Infinity;
              const preEntry = chartData.filter(d => d.ts < preEntryEnd);
              const liveStart = hist.length > 0 ? hist[hist.length - 1].ts : 0;
              const live = chartData.filter(d => d.ts > liveStart && !hist.some(h => Math.abs(h.ts - d.ts) < 1000));
              chartData = [...preEntry, ...hist, ...live];
            }
          }).catch(() => {});
        }
      }
      if (bid > 0) {
        chartData.push({ts: Date.now(), bid});
        // Keep reasonable size
        if (chartData.length > 900) chartData = chartData.filter((_, i) => i % 2 === 0);
      }
      drawPriceChart(at.avg_price_c || 0, at.sell_price_c || 0, chartData, chartStartMs, chartEndMs);

    } else if (pt) {
      // ── PENDING TRADE ──
      tc.style.display = 'none';
      mc.style.display = 'none';
      pc.style.display = '';
      lastStateData._tradeCloseTime = null;
      chartTradeId = null;

      const pSide = (pt.side || 'yes').toUpperCase();
      const pSideCls = pt.side === 'yes' ? 'side-yes' : 'side-no';
      const pFilled = pt.shares_filled || 0;
      const pOrdered = pt.shares_ordered || 0;
      const pPct = pOrdered > 0 ? (pFilled / pOrdered * 100) : 0;
      const isPlaceholder = pt.placeholder;
      $('#pendMarket').textContent = pt.close_time ? marketStartTime(pt.close_time) : 'Placing order...';
      $('#pendSide').innerHTML = isPlaceholder
        ? `<span class="${pSideCls}">${pSide}</span> — placing order...`
        : `<span class="${pSideCls}">${pSide}</span> @ ${pt.price_c || 0}¢`;
      $('#pendFills').textContent = isPlaceholder ? '...' : `${pFilled}/${pOrdered}`;
      $('#pendFills').style.color = pFilled > 0 ? 'var(--green)' : '';
      $('#pendCost').textContent = '$' + (pt.cost_so_far || 0).toFixed(2);
      $('#pendProgress').style.width = pPct + '%';
      $('#pendProgress').style.background = pFilled > 0 ? 'var(--green)' : 'var(--yellow)';
      tickCountdown('pendTime', pt.close_time);
      const preset = pt.sell_price_preset_c || 0;
      $('#pendSellInfo').textContent = preset > 0 ? `Sell will be set at ${preset}¢ after fill` : 'No sell preset — will auto-calculate';
      if (preset > 0) $('#pendSellPrice').value = preset;

      // Store live market data
      lastStateData._liveMarket = s.live_market;

      // Draw live chart on pending card too
      const lm3 = s.live_market;
      if (lm3 && lm3.ticker) {
        pushLivePrice(lm3.ticker, lm3.close_time, lm3.yes_ask, lm3.no_ask, lm3.yes_bid, lm3.no_bid);
        drawLiveMarketChart('pendChart');
      }

    } else {
      // ── LIVE MARKET ──
      tc.style.display = 'none';
      pc.style.display = 'none';
      lastStateData._tradeCloseTime = null;
      chartTradeId = null;

      const lm = s.live_market;
      lastStateData._liveMarket = lm;
      if (lm && lm.ticker) {
        mc.style.display = '';
        var _hasShadowHint = (s.active_shadow && s.active_shadow.trade_id) || (s.shadow_trade && s.shadow_trade.ticker === lm.ticker);
        mc.style.borderLeftColor = _hasShadowHint ? '#a371f7' : 'var(--blue)';
        lastStateData._nextMarketOpen = lm.close_time;
        $('#monMarket').textContent = marketStartTime(lm.close_time);
        if (lastStateData._monCloseTime !== lm.close_time) {
          lastStateData._monCloseTime = lm.close_time;
          // Only arm timer if this is a genuinely new market, not stale data re-populating
          lastStateData._monEndFired = (lastStateData._monFiredForClose === lm.close_time);
        }
        tickCountdown('monTime', lm.close_time);

        const ya = lm.yes_ask, na = lm.no_ask;
        const yb = lm.yes_bid, nb = lm.no_bid;

        // Disable buttons at extreme prices
        const yesOff = !ya || ya >= 99 || ya <= 1;
        const noOff = !na || na >= 99 || na <= 1;

        // Show — for disabled prices, actual price otherwise
        $('#monYesAsk').textContent = yesOff ? '—' : ya + '¢';
        $('#monNoAsk').textContent = noOff ? '—' : na + '¢';
        $('#monYesSpread').textContent = (!yesOff && ya && yb) ? (ya - yb) + '¢ spread' : '';
        $('#monNoSpread').textContent = (!noOff && na && nb) ? (na - nb) + '¢ spread' : '';
        if (yesOff && ya) $('#monYesSpread').textContent = ya >= 99 ? 'Unavailable' : 'No offers';
        if (noOff && na) $('#monNoSpread').textContent = na >= 99 ? 'Unavailable' : 'No offers';

        // Fair value annotations under ask prices
        const fvYesEl = document.getElementById('monYesFV');
        const fvNoEl = document.getElementById('monNoFV');
        if (fvYesEl && fvNoEl) {
          const fvd = lm.fv_model;
          if (fvd && fvd.fair_yes_c != null) {
            const yFV = fvd.fair_yes_c;
            const nFV = fvd.fair_no_c;
            const yDiff = ya ? (yFV - ya).toFixed(0) : null;
            const nDiff = na ? (nFV - na).toFixed(0) : null;
            fvYesEl.innerHTML = `FV ${yFV}¢` + (yDiff != null ? ` <span style="color:${yDiff > 0 ? 'var(--green)' : yDiff < 0 ? 'var(--red)' : 'var(--dim)'}">${yDiff > 0 ? '+' : ''}${yDiff}</span>` : '');
            fvNoEl.innerHTML = `FV ${nFV}¢` + (nDiff != null ? ` <span style="color:${nDiff > 0 ? 'var(--green)' : nDiff < 0 ? 'var(--red)' : 'var(--dim)'}">${nDiff > 0 ? '+' : ''}${nDiff}</span>` : '');
            fvYesEl.style.display = '';
            fvNoEl.style.display = '';
          } else {
            fvYesEl.style.display = 'none';
            fvNoEl.style.display = 'none';
          }
        }

        // Update model suggestion for settings page
        const msgEl = document.getElementById('modelSuggestion');
        if (msgEl) {
          const fvs = lm.fv_model;
          if (fvs && fvs.recommended_side) {
            const msCls = fvs.recommended_side === 'yes' ? 'side-yes' : 'side-no';
            const msEdge = fvs.best_edge_pct || 0;
            const msEv = (fvs.recommended_side === 'yes' ? fvs.yes_ev_c : fvs.no_ev_c) || 0;
            msgEl.innerHTML = `<span style="color:rgba(136,132,216,0.8);font-weight:600">Model says:</span> <span class="${msCls}">${fvs.recommended_side.toUpperCase()}</span> <span style="color:var(--green)">+${msEdge.toFixed(1)}%</span> <span class="dim">EV ${msEv >= 0 ? '+' : ''}${msEv.toFixed(1)}¢ · FV Y=${fvs.fair_yes_c}¢ N=${fvs.fair_no_c}¢</span>`;
            msgEl.style.display = '';
          } else if (fvs && fvs.fair_yes_c != null) {
            msgEl.innerHTML = `<span style="color:rgba(136,132,216,0.8)">Model:</span> <span class="dim">No edge at current prices (FV Y=${fvs.fair_yes_c}¢ N=${fvs.fair_no_c}¢)</span>`;
            msgEl.style.display = '';
          } else {
            msgEl.style.display = 'none';
          }
        }

        // Accumulate live prices and draw chart
        pushLivePrice(lm.ticker, lm.close_time, ya, na, yb, nb);
        drawLiveMarketChart('liveChart');

        const riskHtml = lm.risk_level && lm.risk_level !== 'unknown' ? riskTag(lm.risk_level) : '<span class="regime-tag" id="monRisk" style="display:none"></span>';
        const monRiskEl = $('#monRisk');
        monRiskEl.outerHTML = riskHtml;
        // Re-acquire element after outerHTML replacement
        const newEl = document.querySelector('#monitorCard .regime-tag');
        if (newEl && !newEl.id) newEl.id = 'monRisk';
        const _monObsN = lm.regime_obs_n || 0;
        const _monObsLabel = _monObsN > 0 ? ` <span class="dim" style="font-size:10px;font-weight:400">n=${_monObsN}</span>` : '';
        $('#monRegimeLabel').innerHTML = (lm.regime_label || 'unknown').replace(/_/g, ' ') + _monObsLabel;
        // Show note when current regime differs from decision-time regime
        const _skipInfo = s.active_skip;
        const _driftEl = document.getElementById('monRegimeDrift');
        if (_driftEl) {
          if (_skipInfo && _skipInfo.regime_label && lm.regime_label
              && _skipInfo.regime_label !== lm.regime_label) {
            _driftEl.innerHTML = `<span style="color:#d29922">was ${_skipInfo.regime_label.replace(/_/g, ' ')} at decision</span>`;
            _driftEl.style.display = '';
          } else {
            _driftEl.style.display = 'none';
          }
        }
        $('#monRegimeGrid').innerHTML = buildRegimeGrid(lm);
        // Strategy badge on monitor card
        const masEl = document.getElementById('monAutoStrategy');
        if (masEl) {
          if (lm.auto_strategy) {
            masEl.innerHTML = `<span style="font-size:9px;background:var(--blue);color:#fff;padding:1px 5px;border-radius:3px;font-weight:600">AUTO</span> <span style="font-size:11px">${lm.auto_strategy}</span> <span class="dim" style="font-size:10px">EV ${(lm.auto_strategy_ev||0)>=0?'+':''}${(lm.auto_strategy_ev||0).toFixed(1)}\u00a2</span>`;
            masEl.style.display = '';
          } else if (lm.strategy_key) {
            const _msk = typeof _fmtStratKey === 'function' ? _fmtStratKey(lm.strategy_key) : lm.strategy_key;
            masEl.innerHTML = `<span style="font-size:10px;color:var(--dim);font-family:monospace">${_msk}</span>`;
            masEl.style.display = '';
          } else {
            masEl.style.display = 'none';
          }
        }
        // Fair Value Model display
        const fvEl = document.getElementById('monFairValue');
        if (fvEl) {
          const fv = lm.fv_model;
          if (fv && fv.fair_yes_c != null) {
            const recSide = fv.recommended_side;
            const yEdge = fv.yes_edge_pct != null ? fv.yes_edge_pct : 0;
            const nEdge = fv.no_edge_pct != null ? fv.no_edge_pct : 0;
            const yEv = fv.yes_ev_c != null ? fv.yes_ev_c : 0;
            const nEv = fv.no_ev_c != null ? fv.no_ev_c : 0;
            const dist = fv.btc_distance_pct || 0;
            const distColor = dist > 0 ? 'var(--green)' : dist < 0 ? 'var(--red)' : 'var(--dim)';
            const yColor = yEdge > 0 ? 'var(--green)' : yEdge < -5 ? 'var(--red)' : 'var(--dim)';
            const nColor = nEdge > 0 ? 'var(--green)' : nEdge < -5 ? 'var(--red)' : 'var(--dim)';
            let recHtml = '';
            if (recSide) {
              const recClass = recSide === 'yes' ? 'side-yes' : 'side-no';
              const recEdge = fv.best_edge_pct || 0;
              const recEv = recSide === 'yes' ? yEv : nEv;
              recHtml = `<div style="margin-top:4px;font-size:12px;font-weight:600">` +
                `Model says: <span class="${recClass}">${recSide.toUpperCase()}</span> ` +
                `<span style="color:var(--green)">+${recEdge.toFixed(1)}% edge</span> ` +
                `<span class="dim">(EV ${recEv >= 0 ? '+' : ''}${recEv.toFixed(1)}¢)</span></div>`;
            } else {
              recHtml = `<div style="margin-top:4px;font-size:11px;color:var(--dim)">No edge detected</div>`;
            }
            const srcLabel = fv.source === 'surface' ? '●' : fv.source === 'interpolated' ? '◐' : '○';
            const srcTitle = fv.source === 'surface' ? 'Empirical data' : fv.source === 'interpolated' ? 'Interpolated' : 'Analytical estimate';
            fvEl.innerHTML =
              `<div style="display:flex;justify-content:space-between;align-items:center">` +
              `<span style="font-size:10px;font-weight:700;letter-spacing:.5px;color:rgba(136,132,216,0.9);text-transform:uppercase">Fair Value Model</span>` +
              `<span class="dim" style="font-size:10px" title="${srcTitle}">${srcLabel} ${fv.confidence || ''}</span>` +
              `</div>` +
              `<div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;margin-top:4px">` +
              `<div style="font-size:11px">FV YES: <strong>${fv.fair_yes_c}¢</strong> <span style="color:${yColor};font-size:10px">${yEdge >= 0 ? '+' : ''}${yEdge.toFixed(1)}%</span></div>` +
              `<div style="font-size:11px">FV NO: <strong>${fv.fair_no_c}¢</strong> <span style="color:${nColor};font-size:10px">${nEdge >= 0 ? '+' : ''}${nEdge.toFixed(1)}%</span></div>` +
              `</div>` +
              `<div style="font-size:10px;color:var(--dim);margin-top:2px">BTC: <span style="color:${distColor}">${dist >= 0 ? '+' : ''}${dist.toFixed(3)}%</span> from open ($${(fv.btc_open||0).toLocaleString()} → $${(fv.btc_now||0).toLocaleString()})</div>` +
              recHtml;
            fvEl.style.display = '';
          } else {
            fvEl.style.display = 'none';
          }
        }
        // Update shadow trade or simulated trade
        var hasShadow = _shadowUpdate(lm, s);
        if (hasShadow) {
          _simHide();
          mc.style.borderLeftColor = '#a371f7';
        } else {
          _shadowHide();
          _simUpdate(lm);
        }
      } else {
        mc.style.display = '';
        mc.style.borderLeftColor = 'var(--blue)';
        $('#monMarket').textContent = '—';
        $('#monTime').textContent = '—';
        $('#monYesAsk').textContent = '—';
        $('#monNoAsk').textContent = '—';
        $('#monYesSpread').textContent = '';
        $('#monNoSpread').textContent = '';
        $('#monRegimeLabel').textContent = '—';
        $('#monRegimeGrid').innerHTML = '';
        const _driftClear = document.getElementById('monRegimeDrift');
        if (_driftClear) _driftClear.style.display = 'none';
        lastStateData._monCloseTime = null;
        lastStateData._nextMarketOpen = null;
        const lc = document.getElementById('liveChart');
        if (lc) lc.style.display = 'none';
        const fvClear = document.getElementById('monFairValue');
        if (fvClear) fvClear.style.display = 'none';
        const fvYC = document.getElementById('monYesFV');
        if (fvYC) fvYC.style.display = 'none';
        const fvNC = document.getElementById('monNoFV');
        if (fvNC) fvNC.style.display = 'none';
        const msC = document.getElementById('modelSuggestion');
        if (msC) msC.style.display = 'none';
        _simHide();
        _shadowHide();
      }
    }

  } catch(e) { console.error('Render error:', e); }
  _adjustContentTop();

  // Render Trades tab active card immediately from state (don't wait for loadTrades)
  _renderActiveTrade();

}

function _adjustContentTop() {
  const hdr = document.getElementById('stickyHeader');
  const cw = document.getElementById('contentWrap');
  const tb = document.querySelector('.tab-bar');
  if (hdr && cw) cw.style.top = hdr.offsetHeight + 'px';
  if (tb && cw) cw.style.bottom = tb.offsetHeight + 'px';
}
window.addEventListener('resize', _adjustContentTop);

// ── Strategy Picker ──────────────────────────────────────
// ── Bet mode UI handler ──
function _onBetModeChange(mode, skipSave) {
  if (!skipSave) saveSetting('bet_mode', mode);
  const hints = {flat: '$ per trade', percent: '% of bankroll', edge_scaled: '$ base bet'};
  $('#betSizeHint').textContent = hints[mode] || '$ per trade';
  document.getElementById('edgeScaledSettings').style.display = mode === 'edge_scaled' ? '' : 'none';
}

let _edgeTiers = [];
function _renderEdgeTiers(tiers) {
  _edgeTiers = tiers || [];
  const el = document.getElementById('edgeTiersDisplay');
  if (!el) return;
  if (_edgeTiers.length === 0) { el.innerHTML = '<div class="dim" style="font-size:10px">No tiers set</div>'; return; }
  el.innerHTML = _edgeTiers.map((t, i) =>
    `<div style="display:flex;justify-content:space-between;align-items:center;padding:2px 0;font-size:11px;border-bottom:1px solid var(--border)">
      <span>Edge ≥${t.min_edge}% → ${t.multiplier}× base</span>
      <button onclick="_removeEdgeTier(${i})" style="background:none;border:none;color:var(--red);cursor:pointer;font-size:14px;padding:0 4px">×</button>
    </div>`
  ).join('');
}
function _addEdgeTier() {
  const edge = parseFloat(document.getElementById('newTierEdge')?.value);
  const mult = parseFloat(document.getElementById('newTierMult')?.value);
  if (isNaN(edge) || isNaN(mult) || edge < 0 || mult <= 0) { showToast('Enter valid edge % and multiplier', 'red'); return; }
  _edgeTiers.push({min_edge: edge, multiplier: mult});
  _edgeTiers.sort((a,b) => a.min_edge - b.min_edge);
  saveSetting('edge_tiers', _edgeTiers);
  _renderEdgeTiers(_edgeTiers);
  document.getElementById('newTierEdge').value = '';
  document.getElementById('newTierMult').value = '';
}
function _removeEdgeTier(i) {
  _edgeTiers.splice(i, 1);
  saveSetting('edge_tiers', _edgeTiers);
  _renderEdgeTiers(_edgeTiers);
}

function _buildEntryOptions(side) {
  // Constrain entry price range based on side selection
  // Cheaper side is always ≤50¢
  let entries;
  if (side === 'cheaper') {
    entries = [5,10,15,20,25,30,35,40,45,50];
  } else {
    // yes, no, model — full range
    entries = [5,10,15,20,25,30,35,40,45,50,55,60,65,70,75,80,85,90,95];
  }
  return entries.map(v => ({v, l: v+'¢'}));
}

function _buildSellOptions(entryMax) {
  const sells = [];
  for (let s = entryMax + 5; s < 100; s += 5) sells.push({v: s, l: s+'¢'});
  sells.push({v: 99, l: '99¢'});
  sells.push({v: 'hold', l: 'Hold'});
  return sells;
}

function _applyStrategyPicker() {
  // If auto-fill is active, user is overriding it — clear it
  if (_isAutoFillActive()) {
    _strategyAutoFill = null;
    _renderAutoFillBanner();
    _refreshAutoFillVisuals();
    _lockStrategyPickers(false);
    api('/api/config', {
      method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':_getCsrfToken()},
      body:JSON.stringify({strategy_autofill: null})
    });
    showToast('Auto-fill cleared — manual strategy change', 'yellow');
  }
  // If quick-trade is active, clear it — selected regimes were for the previous strategy
  if (_isQuickTradeActive()) {
    _clearQuickTrade(true);
    showToast('Quick-trade cleared — strategy changed', 'yellow');
  }
  const side = $('#strategySide').value || 'cheaper';
  const timing = $('#strategyTiming').value;
  const entryEl = $('#strategyEntry');
  const sellEl = $('#strategySell');

  // Rebuild entry options (constrained by side)
  const entries = _buildEntryOptions(side);
  const curEntry = parseInt(entryEl.value) || 0;
  entryEl.innerHTML = entries.map(e => `<option value="${e.v}" ${e.v===curEntry?'selected':''}>${e.l}</option>`).join('');
  if (![...entryEl.options].some(o => o.selected)) {
    const closest = entries.reduce((a,b) => Math.abs(b.v-45) < Math.abs(a.v-45) ? b : a);
    entryEl.value = closest.v;
  }

  // Rebuild sell options based on entry
  const entryVal = parseInt(entryEl.value);
  const sells = _buildSellOptions(entryVal);
  const curSell = sellEl.value;
  sellEl.innerHTML = sells.map(s => `<option value="${s.v}" ${String(s.v)===curSell?'selected':''}>${s.l}</option>`).join('');
  if (![...sellEl.options].some(o => o.selected)) {
    const def90 = sells.find(s => s.v === 90) || sells[sells.length - 2];
    sellEl.value = def90.v;
  }

  const sellVal = sellEl.value;

  // Map timing to delay
  const timingMap = {early: 0, mid: 5, late: 10};
  const delay = timingMap[timing] || 0;

  // Save config values
  saveSetting('strategy_side', side);
  saveSetting('entry_delay_minutes', delay);
  saveSetting('entry_price_max_c', entryVal);
  if (sellVal === 'hold') {
    saveSetting('sell_target_c', 0);
  } else {
    saveSetting('sell_target_c', parseInt(sellVal));
  }

  // Display key
  const sideLabel = side === 'cheaper' ? '' : side.toUpperCase() + ':';
  const key = `${sideLabel}${timing}:${entryVal}:${sellVal}`;
  $('#strategyKeyDisplay').textContent = 'Strategy: ' + key;

  // Update entry label to reflect constraint
  const entryLabelEl = document.getElementById('entryLabel');
  if (entryLabelEl) {
    if (side === 'cheaper') entryLabelEl.textContent = 'Buy ≤ (≤50)';
    else entryLabelEl.textContent = 'Buy ≤';
  }

  // Show/hide model edge threshold
  const merEl = document.getElementById('modelEdgeRow');
  if (merEl) merEl.style.display = side === 'model' ? '' : 'none';

  // Show/hide model side validation warning
  const mswEl = document.getElementById('modelSideWarning');
  if (mswEl) mswEl.style.display = side === 'model' ? '' : 'none';

  // Fetch per-regime breakdown for this strategy
  const obsKey = `${side}:${timing}:${entryVal}:${sellVal}`;
  _fetchRegimePreview(obsKey);
}


function _fetchRegimePreview(stratKey) {
  const el = document.getElementById('regimePreviewContent');
  const countEl = document.getElementById('regimePreviewCount');
  if (!el) return;
  el.innerHTML = '<div class="dim" style="padding:4px 0;font-size:10px">Loading...</div>';
  fetch('/api/strategy_regime_preview?key=' + encodeURIComponent(stratKey))
    .then(r => r.json())
    .then(data => {
      const regimes = data.regimes || [];
      if (!regimes.length) {
        el.innerHTML = '<div class="dim" style="padding:4px 0;font-size:10px">No data yet for this strategy. Data populates as the Observatory runs simulations and real trades are placed.</div>';
        if (countEl) countEl.textContent = '';
        return;
      }
      if (countEl) countEl.textContent = `(${regimes.length} regime${regimes.length>1?'s':''})`;
      const riskColors = {low:'var(--green)',moderate:'var(--yellow)',high:'var(--orange)',terrible:'var(--red)',unknown:'var(--dim)'};
      const riskLabels = {low:'LOW',moderate:'MODERATE',high:'HIGH',terrible:'EXTREME',unknown:'UNKNOWN'};
      let html = '<div style="display:flex;flex-direction:column;gap:4px">';
      for (const r of regimes) {
        const rl = r.risk_level || 'unknown';
        const rc = riskColors[rl] || 'var(--dim)';
        const rlab = riskLabels[rl] || 'UNKNOWN';
        const n = r.sample_size || 0;
        const score = r.risk_score != null ? r.risk_score.toFixed(0) : '—';
        const riskMeta = rl === 'unknown' ? `n=${n}` : score;
        const dimStyle = rl === 'unknown' ? 'opacity:0.5;' : '';

        const wr = r.win_rate != null ? (r.win_rate * 100).toFixed(1) + '%' : '—';
        const ev = r.ev_per_trade_c != null ? (r.ev_per_trade_c >= 0 ? '+' : '') + r.ev_per_trade_c.toFixed(1) + '¢' : '—';
        const wev = r.weighted_ev_c != null ? (r.weighted_ev_c >= 0 ? '+' : '') + r.weighted_ev_c.toFixed(1) + '¢' : '—';
        const pf = r.profit_factor != null ? r.profit_factor.toFixed(2) : '—';
        const fdr = r.fdr_significant ? ' <span style="color:var(--green)">✓FDR</span>' : '';
        const oosVal = r.oos_ev_c;
        const oosC = oosVal != null ? (oosVal > 0 ? 'var(--green)' : oosVal < 0 ? 'var(--red)' : 'var(--dim)') : '';
        const oos = oosVal != null ? ` OOS:<span style="color:${oosC}">${oosVal >= 0 ? '+' : ''}${oosVal.toFixed(1)}¢</span>` : '';

        // Sample breakdown
        const simN = r.sim_n || 0;
        const liveN = r.live_n || 0;
        let nLabel = `${n}`;
        if (simN > 0 && liveN > 0) nLabel = `${n} (${simN}sim+${liveN}live)`;
        else if (liveN > 0) nLabel = `${n} (live)`;
        else if (simN > 0) nLabel = `${n} (sim)`;

        // Value colors for quick scanning
        const wrVal = r.win_rate != null ? r.win_rate : 0;
        const wrC = wrVal >= 0.55 ? 'var(--green)' : wrVal >= 0.45 ? 'var(--dim)' : 'var(--red)';
        const evVal = r.ev_per_trade_c != null ? r.ev_per_trade_c : 0;
        const evC = evVal > 0 ? 'var(--green)' : evVal < 0 ? 'var(--red)' : 'var(--dim)';
        const wevVal = r.weighted_ev_c != null ? r.weighted_ev_c : null;
        const wevC = wevVal != null ? (wevVal > 0 ? 'var(--green)' : wevVal < 0 ? 'var(--red)' : 'var(--dim)') : 'var(--dim)';
        const pfVal = r.profit_factor != null ? r.profit_factor : null;
        const pfC = pfVal != null ? (pfVal >= 1.5 ? 'var(--green)' : pfVal >= 1.0 ? 'var(--dim)' : 'var(--red)') : 'var(--dim)';

        const escLbl = r.regime_label.replace(/'/g, "\\'");
        const isSelected = _quickTradeRegimes.has(r.regime_label);
        const selCls = isSelected ? ' qt-selected' : '';

        html += `<div class="qt-row${selCls}" data-regime="${r.regime_label}" onclick="_toggleQuickTradeRegime('${escLbl}')" style="padding:5px 6px;background:rgba(48,54,61,0.3);border-radius:4px;border-left:3px solid ${rc};${dimStyle}">`;
        html += `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:2px">`;
        html += `<span style="font-weight:600;font-size:11px">${r.regime_label.replace(/_/g,' ')}</span>`;
        html += `<div style="display:flex;align-items:center;gap:6px">`;
        html += `<span class="qt-badge"><svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor" style="vertical-align:-1px"><path d="M13 2L3 14h8l-1 8 10-12h-8l1-8z"/></svg> TRADE</span>`;
        html += `<span style="font-size:9px;font-weight:600;color:${rc};letter-spacing:0.5px">${rlab} <span style="font-weight:400;color:var(--dim)">(${riskMeta})</span></span>`;
        html += `</div></div>`;
        html += `<div style="display:flex;flex-wrap:wrap;gap:8px;font-size:10px;color:var(--dim)">`;
        html += `<span>n=${nLabel}</span>`;
        html += `<span>WR <span style="color:${wrC}">${wr}</span></span>`;
        html += `<span>EV <span style="color:${evC}">${ev}</span></span>`;
        if (r.weighted_ev_c != null) html += `<span>wEV <span style="color:${wevC}">${wev}</span></span>`;
        if (r.profit_factor != null) html += `<span>PF <span style="color:${pfC}">${pf}</span></span>`;
        if (fdr || oos) html += `<span>${fdr}${oos}</span>`;
        html += `</div></div>`;
      }
      html += '</div>';
      if (!_isQuickTradeActive()) {
        html += '<div class="dim" style="font-size:9px;text-align:center;margin-top:6px;padding:2px 0">Tap a regime to activate quick-trade</div>';
      }
      el.innerHTML = html;
    })
    .catch(() => {
      el.innerHTML = '<div class="dim" style="padding:4px 0;font-size:10px">Failed to load regime data.</div>';
    });
}

function _loadStrategyPicker(cfg) {
  // Side
  const sideEl = document.getElementById('strategySide');
  if (sideEl) {
    sideEl.value = cfg.strategy_side || 'cheaper';
  }

  // Timing: reverse-map entry_delay_minutes
  const delay = parseInt(cfg.entry_delay_minutes) || 0;
  let timing = 'early';
  if (delay >= 8) timing = 'late';
  else if (delay >= 4) timing = 'mid';
  $('#strategyTiming').value = timing;

  // Build entry options (constrained by side)
  const curSide = sideEl ? sideEl.value : 'cheaper';
  const entries = _buildEntryOptions(curSide);
  const entryEl = $('#strategyEntry');
  const entryVal = parseInt(cfg.entry_price_max_c) || 45;
  entryEl.innerHTML = entries.map(e => `<option value="${e.v}" ${e.v===entryVal?'selected':''}>${e.l}</option>`).join('');
  if (![...entryEl.options].some(o => o.selected)) {
    const closest = entries.reduce((a,b) => Math.abs(b.v-entryVal) < Math.abs(a.v-entryVal) ? b : a);
    entryEl.value = closest.v;
  }

  // Build sell options
  const eVal = parseInt(entryEl.value);
  const sells = _buildSellOptions(eVal);
  const sellEl = $('#strategySell');
  const sellVal = parseInt(cfg.sell_target_c) || 0;
  const sellStr = sellVal > 0 ? String(sellVal) : 'hold';
  sellEl.innerHTML = sells.map(s => `<option value="${s.v}" ${String(s.v)===sellStr?'selected':''}>${s.l}</option>`).join('');
  if (![...sellEl.options].some(o => o.selected)) {
    const def90 = sells.find(s => s.v === 90);
    if (def90) sellEl.value = 90; else sellEl.value = 'hold';
  }

  // Model edge threshold
  const meEl = document.getElementById('minModelEdge');
  if (meEl) meEl.value = parseFloat(cfg.min_model_edge_pct) || 3;

  // Display key with side prefix
  const side = (sideEl ? sideEl.value : 'cheaper');
  const sideLabel = side === 'cheaper' ? '' : side.toUpperCase() + ':';
  const key = `${sideLabel}${timing}:${entryEl.value}:${sellEl.value}`;
  $('#strategyKeyDisplay').textContent = 'Strategy: ' + key;

  // Update entry label
  const entryLabelEl = document.getElementById('entryLabel');
  if (entryLabelEl) {
    if (side === 'cheaper') entryLabelEl.textContent = 'Buy ≤ (≤50)';
    else entryLabelEl.textContent = 'Buy ≤';
  }

  // Show/hide model edge row
  const merEl = document.getElementById('modelEdgeRow');
  if (merEl) merEl.style.display = side === 'model' ? '' : 'none';

  // Show/hide model side validation warning
  const mswEl = document.getElementById('modelSideWarning');
  if (mswEl) mswEl.style.display = side === 'model' ? '' : 'none';

  // Load per-regime breakdown for current strategy
  const obsKey = `${side}:${timing}:${entryEl.value}:${sellEl.value}`;
  _fetchRegimePreview(obsKey);
}

// ── Config loading ──────────────────────────────────────
async function loadConfig() {
  const cfg = await api('/api/config');
  if (cfg.bet_mode) {
    $('#betMode').value = cfg.bet_mode;
    _onBetModeChange(cfg.bet_mode, true);
  }
  if (cfg.bet_size) $('#betSize').value = cfg.bet_size;
  if (cfg.max_consecutive_losses) $('#maxConsecLosses').value = cfg.max_consecutive_losses;
  if (cfg.cooldown_after_loss_stop) $('#cooldownAfterLoss').value = cfg.cooldown_after_loss_stop;
  // Edge scaled tiers
  if (cfg.edge_tiers) _renderEdgeTiers(cfg.edge_tiers);
  // Adaptive entry
  const aeEl = document.getElementById('adaptiveEntry');
  if (aeEl && cfg.adaptive_entry !== undefined) aeEl.checked = cfg.adaptive_entry;
  // Automation
  if (cfg.min_breakeven_fee_buffer !== undefined) $('#feeBuffer').value = cfg.min_breakeven_fee_buffer;
  if (cfg.deploy_cooldown_minutes !== undefined) $('#deployCooldown').value = cfg.deploy_cooldown_minutes;
  if (cfg.price_poll_interval !== undefined) $('#pricePollInterval').value = cfg.price_poll_interval;
  if (cfg.order_poll_interval !== undefined) $('#orderPollInterval').value = cfg.order_poll_interval;
  
  if (cfg.risk_level_actions) _loadRiskActionButtons(cfg.risk_level_actions);
  if (cfg.regime_overrides) _regimeOverrides = cfg.regime_overrides;
  if (cfg.regime_filters) {
    _regimeFilters = typeof cfg.regime_filters === 'string' ? JSON.parse(cfg.regime_filters) : cfg.regime_filters;
  }

  // Execution settings
  const dsEl = document.getElementById('dynamicSellEnabled');
  if (dsEl && cfg.dynamic_sell_enabled !== undefined) {
    dsEl.checked = cfg.dynamic_sell_enabled;
    const dsFloorRow = document.getElementById('dynamicSellFloorRow');
    if (dsFloorRow) dsFloorRow.style.display = cfg.dynamic_sell_enabled ? '' : 'none';
  }
  if (cfg.dynamic_sell_floor_c) document.getElementById('dynamicSellFloor').value = cfg.dynamic_sell_floor_c;
  const eeEl = document.getElementById('earlyExitEv');
  if (eeEl && cfg.early_exit_ev !== undefined) eeEl.checked = cfg.early_exit_ev;
  const tsEl = document.getElementById('trailingStopPct');
  if (tsEl && cfg.trailing_stop_pct !== undefined) tsEl.value = cfg.trailing_stop_pct;
  _loadStrategyPicker(cfg);

  // Restore quick-trade state
  if (cfg.quick_trade_regimes && Array.isArray(cfg.quick_trade_regimes) && cfg.quick_trade_regimes.length) {
    _quickTradeRegimes = new Set(cfg.quick_trade_regimes);
    _quickTradeSavedState = cfg.quick_trade_saved_state || null;
    _renderQuickTradeBanner();
    _updateRiskActionLock();
    // Auto-expand the regime breakdown to show active selections
    const rpBody = document.getElementById('regimePreviewBody');
    if (rpBody) { rpBody.style.display = ''; }
    const chev = document.querySelector('#regimePreviewCard .chevron');
    if (chev) chev.textContent = '▾';
  } else {
    _quickTradeRegimes.clear();
    _quickTradeSavedState = null;
  }
  // Restore strategy auto-fill state
  if (cfg.strategy_autofill && cfg.strategy_autofill.stratKey) {
    _strategyAutoFill = cfg.strategy_autofill;
    _renderAutoFillBanner();
    _lockStrategyPickers(true);
    // Check if global best has changed since last save
    if (_strategyAutoFill.regime === 'global') _syncGlobalAutoFill();
  } else {
    _strategyAutoFill = null;
    _renderAutoFillBanner();
    _lockStrategyPickers(false);
  }
  if (cfg.push_notify_wins !== undefined) $('#notifyWins').checked = cfg.push_notify_wins;
  if (cfg.push_notify_losses !== undefined) $('#notifyLosses').checked = cfg.push_notify_losses;
  if (cfg.push_notify_errors !== undefined) $('#notifyErrors').checked = cfg.push_notify_errors;
  if (cfg.push_notify_buys !== undefined) $('#notifyBuys').checked = cfg.push_notify_buys;
  if (cfg.push_notify_observed !== undefined) $('#notifySkips').checked = cfg.push_notify_observed;

  if (cfg.push_notify_health_check !== undefined) $('#notifyHealthCheck').checked = cfg.push_notify_health_check;
  if (cfg.push_notify_new_regime !== undefined) $('#notifyNewRegime').checked = cfg.push_notify_new_regime;
  if (cfg.push_notify_regime_classified !== undefined) $('#notifyRegimeClassified').checked = cfg.push_notify_regime_classified;
  if (cfg.push_notify_strategy_discovery !== undefined) $('#notifyStrategyDiscovery').checked = cfg.push_notify_strategy_discovery;
  if (cfg.push_notify_global_best !== undefined) $('#notifyGlobalBest').checked = cfg.push_notify_global_best;
  if (cfg.push_notify_trade_updates !== undefined) $('#notifyTradeUpdates').checked = cfg.push_notify_trade_updates;
  if (cfg.health_check_enabled !== undefined) $('#healthCheckEnabled').checked = cfg.health_check_enabled;
  if (cfg.health_check_timeout_min !== undefined) $('#healthCheckTimeout').value = cfg.health_check_timeout_min;
  if (cfg.auto_strategy_min_samples !== undefined) $('#autoStrategyMinN').value = cfg.auto_strategy_min_samples;
  if (cfg.auto_strategy_min_ev_c !== undefined) $('#autoStrategyMinEv').value = cfg.auto_strategy_min_ev_c;
  _updateAutoStrategyLock();
  // Restore trade-all state
  if (cfg.auto_strat_trade_all) {
    const taEl = document.getElementById('autoStratTradeAll');
    if (taEl) taEl.checked = true;
    _tradeAllSavedState = cfg.auto_strat_trade_all_saved || null;
    _updateTradeAllVisuals();
    _updateRiskActionLock();
  }
  // Sync trading mode selector
  if (cfg.trading_mode) {
    _syncModeStrip(cfg.trading_mode);
  }
  if (cfg.push_quiet_start) $('#quietStart').value = cfg.push_quiet_start;
  if (cfg.push_quiet_end) $('#quietEnd').value = cfg.push_quiet_end;
}

// ── Trades + Regimes ────────────────────────────────────
// Filter state: Map of filter → 'include' | 'exclude'. Absent = default (no filter)
let _tradeFilterState = new Map();
const _tradeFilterIncColors = {
  win:'active-green', loss:'active-red',
  skipped:'active', error:'active-yellow', incomplete:'active-red', ignored:'active-yellow', shadow:'active-purple',
  yes:'active-green', no:'active-red',
  early:'active', mid:'active', late:'active',
  cheaper:'active-yellow', model:'active',
  sold:'active-green', hold:'active',
};
let _tradeOffset = 0;
let _tradeHasMore = false;
let _tradeLoading = false;
const _TRADE_PAGE_SIZE = 30;

async function exportCSV() {
  try {
    const resp = await fetch('/api/trades/csv');
    const blob = await resp.blob();
    const fname = 'trades_' + new Date().toISOString().split('T')[0] + '.csv';
    if (navigator.share && /mobile|iphone|android/i.test(navigator.userAgent)) {
      const file = new File([blob], fname, {type: 'text/csv'});
      try { await navigator.share({files: [file], title: fname}); return; }
      catch(e) { if (e.name === 'AbortError') return; }
    }
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = fname;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  } catch(e) {
    if (e.name !== 'AbortError') console.error('CSV export error:', e);
  }
}

function _getTradeFilterParams() {
  const includes = [];
  const excludes = [];
  for (const [f, state] of _tradeFilterState) {
    if (state === 'include') includes.push(f);
    else if (state === 'exclude') excludes.push(f);
  }
  const regime = ($('#tradeRegimeFilter') || {}).value || '';
  return { include: includes.join(','), exclude: excludes.join(','), regime };
}

function resetTradeCache() {
  _lastFilterStatsKey = '';
  _lastActiveTradeKey = '';
  const el = $('#tradeList');
  if (el) el.dataset.key = '';
}

function setTradeFilter(filter) {
  if (filter === 'all') {
    _tradeFilterState.clear();
  } else {
    const cur = _tradeFilterState.get(filter);
    if (!cur) {
      _tradeFilterState.set(filter, 'include');
    } else if (cur === 'include') {
      _tradeFilterState.set(filter, 'exclude');
    } else {
      _tradeFilterState.delete(filter);
    }
  }
  // Update chip visuals
  const hasAny = _tradeFilterState.size > 0;
  document.querySelectorAll('#tradeFilters .chip').forEach(c => {
    const f = c.dataset.filter;
    if (!f) return;
    if (f === 'all') {
      c.className = hasAny ? 'chip' : 'chip active';
      return;
    }
    const state = _tradeFilterState.get(f);
    if (state === 'include') {
      c.className = 'chip ' + (_tradeFilterIncColors[f] || 'active');
    } else if (state === 'exclude') {
      c.className = 'chip exclude';
    } else {
      c.className = 'chip';
    }
  });
  resetTradeCache();
  loadTrades();

  // Show/hide delete incomplete bar
  const delBar = document.getElementById('deleteIncompleteBar');
  if (delBar) delBar.style.display = _tradeFilterState.get('incomplete') === 'include' ? '' : 'none';
}

async function deleteAllIncomplete() {
  try {
    const r = await api('/api/trades/delete_incomplete', {method: 'POST'});
    showToast(`Deleted ${r.deleted || 0} incomplete`, 'green');
    resetTradeCache();
    loadTrades();
  } catch(e) {
    showToast('Delete failed', 'red');
  }
}

let _lastFilterStatsKey = '';
function _renderFilterStats(stats) {
  const el = $('#tradeFilterStats');
  if (!stats || stats.total === 0) {
    if (el.style.display !== 'none') el.style.display = 'none';
    return;
  }
  const key = stats.total + '|' + stats.wins + '|' + stats.losses + '|' + stats.pnl + '|' + (stats.errors||0) + '|' + (stats.wagered||0);
  if (key === _lastFilterStatsKey && el.style.display !== 'none') return;
  _lastFilterStatsKey = key;
  el.style.display = '';
  const wr = stats.win_rate > 0 ? stats.win_rate + '%' : '—';
  const wrCls = stats.win_rate >= 55 ? 'pos' : stats.win_rate > 0 && stats.win_rate < 45 ? 'neg' : '';
  const pnlCls = stats.pnl > 0 ? 'pos' : stats.pnl < 0 ? 'neg' : '';
  const real = stats.wins + stats.losses;
  const avgPnlCls = stats.avg_pnl > 0 ? 'pos' : stats.avg_pnl < 0 ? 'neg' : 'dim';
  const roiCls = stats.roi > 0 ? 'pos' : stats.roi < 0 ? 'neg' : 'dim';
  el.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
      <span style="font-size:12px;font-weight:600">${stats.total} trades</span>
      <span class="${pnlCls}" style="font-size:14px;font-weight:700;font-family:monospace">${fmtPnl(stats.pnl)}</span>
    </div>
    <div style="display:flex;gap:12px;font-size:11px;color:var(--dim);flex-wrap:wrap">
      <span><span class="pos">${stats.wins}W</span> \u00b7 <span class="neg">${stats.losses}L</span></span>
      <span>Win Rate: <strong class="${wrCls}">${wr}</strong></span>
      ${real > 0 ? '<span>Avg: <span class="' + avgPnlCls + '">' + fmtPnl(stats.avg_pnl) + '</span></span>' : ''}
      ${stats.wagered > 0 ? '<span>Wagered: $' + stats.wagered.toFixed(2) + '</span>' : ''}
      ${stats.wagered > 0 ? '<span>ROI: <span class="' + roiCls + '">' + stats.roi + '%</span></span>' : ''}
      ${stats.avg_entry > 0 ? '<span>Avg Entry: ' + Math.round(stats.avg_entry) + '\u00a2</span>' : ''}
      ${stats.skips ? '<span style="color:var(--blue)">Observed: ' + stats.skips + '</span>' : ''}
      ${stats.shadows ? '<span style="color:#a371f7">Shadow: ' + stats.shadows + '</span>' : ''}
      ${stats.errors ? '<span style="color:#d29922">Errors: ' + stats.errors + '</span>' : ''}
      ${stats.best > 0 ? '<span>Best: <span class="pos">' + fmtPnl(stats.best) + '</span></span>' : ''}
      ${stats.worst < 0 ? '<span>Worst: <span class="neg">' + fmtPnl(stats.worst) + '</span></span>' : ''}
    </div>`;
}

let _lastActiveTradeKey = '';
function _renderActiveTrade() {
  const el = $('#tradesActiveTrade');
  const state = _uiState || {};
  const at = state.active_trade;
  const skip = state.active_skip;

  if (at && at.ticker) {
    const key = 'at|' + at.trade_id + '|' + at.current_bid + '|' + Math.round(at.minutes_left || 0);
    if (key === _lastActiveTradeKey) return;
    _lastActiveTradeKey = key;
    el.style.display = '';
    const side = (at.side || '').toUpperCase();
    const entry = at.avg_price_c || 0;
    const sell = at.sell_price_c || 0;
    const bid = at.current_bid || 0;
    const mins = at.minutes_left || 0;
    const est_pnl = at.fill_count && bid > 0 ? (at.fill_count * bid / 100 - (at.actual_cost || 0)) : 0;
    const pCls = est_pnl >= 0 ? 'pos' : 'neg';
    el.innerHTML = `<div class="trade-card tc-open" style="margin-bottom:10px;border-left-width:3px" onclick="switchTab('Home')">
      <div class="tc-header">
        <div><span class="tc-outcome" style="color:var(--blue)">ACTIVE</span>${at.auto_strategy ? '<span style="font-size:9px;background:var(--blue);color:#fff;padding:1px 5px;border-radius:3px;margin-left:6px">AUTO</span>' : ''}</div>
        <span class="tc-pnl ${pCls}" style="font-size:14px">${fmtPnl(est_pnl)}</span>
      </div>
      <div style="display:flex;justify-content:space-between;margin-top:4px;font-size:12px;color:var(--dim)">
        <span>${side} @ ${entry}\u00a2 \u2192 ${at.is_hold_to_expiry ? 'HOLD' : sell+'\u00a2'}</span>
        <span>Bid: <strong>${bid}\u00a2</strong> \u00b7 ${mins.toFixed(1)}m left</span>
      </div>${at.auto_strategy ? `<div style="margin-top:3px;font-size:10px;color:var(--blue)">${at.auto_strategy} · EV ${(at.auto_strategy_ev||0)>=0?'+':''}${(at.auto_strategy_ev||0).toFixed(1)}¢ · ${at.auto_strategy_setup||''}</div>` : ''}
    </div>`;
  } else if (skip && skip.ticker) {
    const _aShd = state.active_shadow;
    const _isShadowActive = _aShd && (_aShd.ticker === skip.ticker || _aShd.status === 'pending_fill');
    const key = 'skip|' + skip.ticker + '|' + skip.reason + '|' + ((state.live_market||{}).regime_label||'') + '|' + (_isShadowActive ? 'shd' : '');
    if (key === _lastActiveTradeKey) return;
    _lastActiveTradeKey = key;
    el.style.display = '';
    const regime = (skip.regime_label || '').replace(/_/g, ' ');
    const risk = skip.risk_level || 'unknown';
    const reason = skip.reason || '';
    const _lmNow = state.live_market || {};
    const _nowRegime = (_lmNow.regime_label || '').replace(/_/g, ' ');
    const _drifted = skip.regime_label && _lmNow.regime_label && skip.regime_label !== _lmNow.regime_label;
    const _skipObsN = skip.regime_obs_n || _lmNow.regime_obs_n || 0;
    const _skipLabel = _isShadowActive ? '<span class="tc-outcome" style="color:#a371f7">SHADOW TRADE</span>' : '<span class="tc-outcome" style="color:var(--blue)">OBSERVING</span>';
    const _skipCardCls = _isShadowActive ? 'tc-shadow' : 'tc-skip';
    el.innerHTML = `<div class="trade-card ${_skipCardCls}" style="margin-bottom:10px;border-left-width:3px">
      <div class="tc-header">
        <div>${_skipLabel}</div>
      </div>
      <div style="margin-top:4px;font-size:12px;color:var(--dim)">${regime}${_skipObsN > 0 ? ` <span style="font-size:10px">n=${_skipObsN}</span>` : ''}</div>
      ${_drifted ? `<div style="margin-top:2px;font-size:10px;color:#d29922">now ${_nowRegime}</div>` : ''}
      ${skip.auto_skip_short ? `<div style="margin-top:3px;font-size:10px;color:var(--blue)">Auto: ${skip.auto_skip_short}</div>` : `<div style="margin-top:2px;font-size:11px;color:var(--dim)">${reason}</div>`}
    </div>`;
  } else {
    if (_lastActiveTradeKey !== 'none') {
      _lastActiveTradeKey = 'none';
      el.style.display = 'none';
    }
  }
}

async function loadTrades() {
  if (_tradeLoading) return;
  _tradeLoading = true;
  _tradeOffset = 0;

  const {include, exclude, regime} = _getTradeFilterParams();
  try {
    const d = await api(`/api/trades_v2?include=${encodeURIComponent(include)}&exclude=${encodeURIComponent(exclude)}&regime=${encodeURIComponent(regime)}&offset=0&limit=${_TRADE_PAGE_SIZE}`);
    hideSkel('skelTrades');

    // Populate regime dropdown (preserve current selection)
    const sel = $('#tradeRegimeFilter');
    const curVal = sel.value;
    if (d.regimes) {
      const existing = new Set([...sel.options].map(o => o.value));
      for (const r of d.regimes) {
        if (!existing.has(r)) {
          const opt = document.createElement('option');
          opt.value = r;
          opt.textContent = r.replace(/_/g, ' ');
          sel.appendChild(opt);
        }
      }
    }
    sel.value = curVal || regime;

    _renderFilterStats(d.stats);
    _renderActiveTrade();

    const el = $('#tradeList');
    if (!d.trades.length) {
      if (!el.querySelector('.dim')) {
        el.innerHTML = '<div class="dim" style="text-align:center;padding:20px 0">No matching trades</div>';
      }
      $('#tradeLoadMore').style.display = 'none';
      $('#tradeEndMarker').style.display = 'none';
    } else {
      // Only rebuild if trade list changed
      const tradeKey = d.trades.map(t => t.id + ':' + t.outcome).join(',');
      if (tradeKey !== el.dataset.key) {
        const html = d.trades.map(t => { try { return renderTradeCard(t); } catch(e) { console.error('renderTradeCard error:', e, t.id); return ''; } }).join('');
        el.innerHTML = html;
        el.dataset.key = tradeKey;
      }
      _tradeOffset = d.trades.length;
      _tradeHasMore = d.has_more;
      $('#tradeLoadMore').style.display = d.has_more ? '' : 'none';
      $('#tradeEndMarker').style.display = d.has_more ? 'none' : '';
    }
  } catch(e) {
    console.error('loadTrades error:', e);
    hideSkel('skelTrades');
  }
  _tradeLoading = false;
}

async function loadMoreTrades() {
  if (_tradeLoading || !_tradeHasMore) return;
  _tradeLoading = true;
  const {include, exclude, regime} = _getTradeFilterParams();
  try {
    const d = await api(`/api/trades_v2?include=${encodeURIComponent(include)}&exclude=${encodeURIComponent(exclude)}&regime=${encodeURIComponent(regime)}&offset=${_tradeOffset}&limit=${_TRADE_PAGE_SIZE}`);
    if (d.trades.length) {
      const el = $('#tradeList');
      el.innerHTML += d.trades.map(t => { try { return renderTradeCard(t); } catch(e) { console.error('renderTradeCard error:', e, t.id); return ''; } }).join('');
      _tradeOffset += d.trades.length;
      _tradeHasMore = d.has_more;
    } else {
      _tradeHasMore = false;
    }
    $('#tradeLoadMore').style.display = _tradeHasMore ? '' : 'none';
    $('#tradeEndMarker').style.display = _tradeHasMore ? 'none' : '';
  } catch(e) { console.error('loadMoreTrades error:', e); }
  _tradeLoading = false;
}

// Infinite scroll
document.getElementById('contentWrap').addEventListener('scroll', function() {
  if (_currentTab !== 'Trades' || !_tradeHasMore || _tradeLoading) return;
  if (this.scrollHeight - this.scrollTop - this.clientHeight < 300) {
    loadMoreTrades();
  }
});

// Event delegation for trade card taps (more reliable than inline onclick on iOS)
document.addEventListener('click', function(e) {
  const card = e.target.closest('.trade-card[data-tid]');
  if (card && !e.target.closest('.tc-tag')) {
    const tid = parseInt(card.dataset.tid);
    if (tid) {
      showTradeDetail(tid).catch(function(err) {
        showToast('Tap error: ' + err.message, 'red');
      });
    }
  }
});

function renderTradeCard(t) {
const o = t.outcome || 'unknown';
const pnl = t.pnl || 0;
const isShadow = t.is_shadow === 1 || t.is_shadow === true;
const cardCls = isShadow ? 'tc-shadow' :
                o === 'win' ? 'tc-win' : o === 'loss' ? 'tc-loss' :
                o === 'error' ? 'tc-error' :
                o === 'open' ? 'tc-open' : 'tc-skip';
const pnlCls = pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : 'dim';

// Outcome label
const outLabel = isShadow ? (o === 'win' ? 'SHADOW WIN' : o === 'loss' ? 'SHADOW LOSS' : 'SHADOW') :
  {
  win: 'WIN', loss: 'LOSS',
  skipped: 'OBSERVED', no_fill: 'NO FILL', error: 'ERROR', open: 'OPEN'
}[o] || o.toUpperCase();

// Side + prices
const side = (t.side || '').toUpperCase();
const entry = t.avg_fill_price_c || t.entry_price_c || 0;
const sell = t.sell_price_c || 0;
const hwm = t.price_high_water_c || 0;
const progress = t.pct_progress_toward_target || 0;
const filled = t.shares_filled || 0;
const cost = t.actual_cost || 0;
const fees = t.fees_paid || 0;

// Regime info
const riskLvl = t.regime_risk_level || 'unknown';
const regLabel = (t.regime_label || 'unknown').replace(/_/g, ' ');

// Tags — every filter corresponds to a tag, every tag is clickable
let tags = '';
function tag(label, cls, filter) {
  return `<span class="tc-tag ${cls}" onclick="event.stopPropagation();setTradeFilter('${filter}',null)">${label}</span>`;
}

// Outcome tag
if (o === 'win') tags += tag('WIN', 'tag-win', 'win');
else if (o === 'loss') tags += tag('LOSS', 'tag-loss', 'loss');
else if (o === 'skipped' || o === 'no_fill') tags += tag(o === 'no_fill' ? 'NO FILL' : 'OBSERVED', 'tag-skip', 'skipped');
else if (o === 'error') tags += tag('ERROR', 'tag-error', 'error');
else if (o === 'open') tags += tag('OPEN', 'tag-open', 'open');

// Side tag (real trades and shadow trades — observed trades don't have a meaningful side)
if (['win', 'loss', 'open'].includes(o) || isShadow) {
  if (t.side === 'yes') tags += tag('YES', 'tag-yes', 'yes');
  else if (t.side === 'no') tags += tag('NO', 'tag-no', 'no');
}

// Special tags
if (isShadow) tags += tag('SHADOW', 'tag-shadow', 'shadow');
else if (t.is_ignored) tags += tag('IGNORED', 'ignored', 'ignored');

// Incomplete: observed but no market result resolved yet
if (o === 'skipped' && !t.market_result) tags += tag('INCOMPLETE', 'tag-incomplete', 'incomplete');

// Strategy key tags — parse side:timing:entry_max:sell_target
const _sk = t.auto_strategy_key || '';
if (_sk) {
  const _skParts = _sk.split(':');
  // Strategy side rule
  if (_skParts[0] === 'cheaper') tags += tag('CHEAPER', 'tag-cheaper', 'cheaper');
  else if (_skParts[0] === 'model') tags += tag('MODEL', 'tag-model', 'model');
  // Entry timing
  if (_skParts[1] === 'early') tags += tag('EARLY', 'tag-early', 'early');
  else if (_skParts[1] === 'mid') tags += tag('MID', 'tag-mid', 'mid');
  else if (_skParts[1] === 'late') tags += tag('LATE', 'tag-late', 'late');
}

// Exit method tags (real trades only)
const _em = t.exit_method || '';
if (_em === 'sell_fill') tags += tag('SOLD', 'tag-sold', 'sold');
else if (_em === 'market_expiry') tags += tag('HOLD', 'tag-hold', 'hold');


// Mode tag
const mode = t.trade_mode || '';


// Skip reason
function escHtml(s) { return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/'/g,'&#39;'); }
const skipLine = (o === 'skipped' || o === 'no_fill' || o === 'error') && t.skip_reason ?
  `<div style="font-size:11px;color:${o === 'error' ? '#d29922' : 'var(--dim)'};margin-top:4px">${escHtml(t.skip_reason)}</div>` : '';

// Skip outcome — just show market result
let skipOutcomeLine = '';
if (o === 'skipped') {
  const mr = t.market_result;
  const _fmtS = (v) => v ? `<span class="${v==='yes'?'side-yes':'side-no'}">${v.toUpperCase()}</span>` : '?';
  if (mr) {
    skipOutcomeLine = `<div style="font-size:11px;color:var(--dim);margin-top:3px">Market result: ${_fmtS(mr)}</div>`;
  }
}

// Market time
const marketTime = t.market_ct || '';

// Only show full detail for real trades and shadow trades
const isReal = ['win', 'loss', 'open'].includes(o) || isShadow;

let detailHtml = '';
if (isReal) {
  let shadowExtra = '';
  if (isShadow) {
    const decPrice = t.shadow_decision_price_c || 0;
    const fillPrice = t.avg_fill_price_c || 0;
    const slip = fillPrice - decPrice;
    const latency = t.shadow_fill_latency_ms || 0;
    shadowExtra = `
      <div>Ask at decision: <strong>${decPrice}¢</strong></div>
      <div>Slippage: <strong>${slip >= 0 ? '+' : ''}${slip}¢</strong></div>
      <div>Fill latency: <strong>${latency}ms</strong></div>`;
  }
  detailHtml = `
    <div class="tc-details">
      <div>Side: <strong><span class="${t.side==='yes'?'side-yes':'side-no'}">${side}</span> @ ${entry}\u00a2</strong></div>
      ${isShadow ? '' : `<div>Sell target: <strong>${sell}\u00a2</strong></div>`}
      <div>Shares: <strong>${filled}</strong></div>
      <div>Cost: <strong>$${cost.toFixed(2)}</strong>${fees > 0 ? ` <span style="color:var(--dim)">(+$${fees.toFixed(2)} fees)</span>` : ''}</div>
      ${isShadow ? shadowExtra : `<div>HWM: <strong>${hwm}\u00a2</strong></div>
      <div>Progress: <strong>${progress.toFixed(0)}%</strong></div>`}
      
      <div>Stability: <strong>${t.price_stability_c != null ? t.price_stability_c + '\u00a2' : '\u2014'}</strong></div>
      <div>Vol Level: <strong>${t.vol_regime ? t.vol_regime + '/5' : '\u2014'}</strong></div>
      ${t.strategy_display ? `<div>Strategy: <strong style="font-family:monospace;font-size:10px">${typeof _fmtStratKey === 'function' ? _fmtStratKey(t.strategy_display) : t.strategy_display}</strong></div>` : ''}
      ${t.entry_ct || t.exit_ct || t.time_in_market_s != null ? `<div>${t.entry_ct ? 'In ' + t.entry_ct.split(' ').pop() : ''}${t.exit_ct ? ' \u2192 ' + t.exit_ct.split(' ').pop() : (!t.exit_ct && t.entry_ct ? ' \u2192 expiry' : '')}${t.time_in_market_s != null ? ' <span style="color:var(--dim)">(' + (t.time_in_market_s >= 60 ? Math.floor(t.time_in_market_s/60) + 'm ' + (t.time_in_market_s%60) + 's' : t.time_in_market_s + 's') + ')</span>' : ''}</div>` : ''}
    </div>`;
}

return `<div class="trade-card ${cardCls}" data-tid="${t.id}" role="button" style="cursor:pointer">
  <div class="tc-header">
    <div>
      <span class="tc-outcome ${o === 'skipped' ? '' : pnlCls}" ${o === 'skipped' ? 'style="color:var(--blue)"' : ''}>${outLabel}</span>
      ${isReal ? riskTag(riskLvl) : ''}
    </div>
    <span class="tc-pnl ${pnlCls}">${fmtPnl(pnl)}</span>
  </div>
  <div style="display:flex;justify-content:space-between;margin-top:4px">
    <span class="dim" style="font-size:12px">${regLabel}</span>
    <span class="dim" style="font-size:11px">${marketTime ? marketTime + ' · ' : ''}${t.created_ct || ''}</span>
  </div>
  ${detailHtml}
  ${skipLine}
  ${skipOutcomeLine}
  <div class="tc-tags">${tags}</div>
  <div style="text-align:right;margin-top:4px">
    <span style="opacity:0.35;cursor:pointer;padding:2px;display:inline-block" title="Delete trade"
          onclick="event.stopPropagation();showDeleteTrade(${t.id}, '${escHtml(outLabel)}', '${fmtPnl(pnl)}')">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--dim)" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="m14.74 9-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 0 1-2.244 2.077H8.084a2.25 2.25 0 0 1-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 0 0-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 0 1 3.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 0 0-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 0 0-7.5 0"/></svg>
    </span>
  </div>
</div>`;
}

// ── Trade detail popup ───────────────────────────────────
async function showTradeDetail(tradeId) {
  try {
    const d = await api(`/api/trade/${tradeId}/detail`);
    const t = d.trade;
    const path = d.price_path || [];
    const el = $('#tradeDetailContent');
    const o = t.outcome || 'open';
    const pnl = t.pnl || 0;
    const pCls = pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : '';
    const sideCls = t.side === 'yes' ? 'side-yes' : 'side-no';
    const entry = t.avg_fill_price_c || t.entry_price_c || 0;
    const sell = t.sell_price_c || 0;
    const hwm = t.price_high_water_c || 0;
    const lwm = t.price_low_water_c || 0;
    const osc = t.oscillation_count || 0;
    const prog = t.pct_progress_toward_target || 0;
    const stab = t.price_stability_c;
    const delay = t.entry_delay_minutes || 0;
    const regime = (t.regime_label || '—').replace(/_/g, ' ');
    const riskLvl = t.regime_risk_level || 'unknown';

    let html = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
      <span class="tc-outcome ${o === 'skipped' ? '' : pCls}" style="font-size:16px;${o === 'skipped' ? 'color:var(--blue)' : ''}">${o === 'skipped' ? 'OBSERVED' : o === 'error' ? 'ERROR' : o.toUpperCase()}</span>
      <span class="tc-pnl ${pCls}" style="font-size:18px">${fmtPnl(pnl)}</span>
    </div>`;

    if (o === 'skipped' || o === 'no_fill' || o === 'error') {
      // ── Observed / Error trade detail ──
      const mr = t.market_result;
      const _fs = (v) => v ? `<span class="${v==='yes'?'side-yes':'side-no'}">${v.toUpperCase()}</span>` : '?';

      // Error banner
      if (o === 'error') {
        html += `<div style="padding:10px;border-radius:8px;background:#2a2010;border:1px solid #d29922;margin-bottom:10px">
          <div style="font-size:12px;font-weight:600;color:#d29922;margin-bottom:4px">Order Error</div>
          <div style="font-size:11px;color:var(--text);word-break:break-word">${escHtml(t.skip_reason||'Unknown error')}</div>
        </div>`;
      }

      // Market result banner
      if (mr) {
        html += `<div style="padding:10px;border-radius:8px;background:var(--bg);border:1px solid var(--border);margin-bottom:10px;text-align:center">
          <div style="font-size:14px;font-weight:600">Market Result: ${_fs(mr)}</div>
        </div>`;
      } else {
        html += `<div style="padding:8px;border-radius:6px;background:var(--bg);margin-bottom:10px;font-size:12px;color:var(--dim);text-align:center">
          Market result not yet available
        </div>`;
      }

      html += `<div style="display:grid;grid-template-columns:1fr 1fr;gap:3px 12px;font-size:12px;color:var(--dim)">
        <div style="grid-column:1/-1">Reason: <strong>${escHtml(t.skip_reason||'—')}</strong></div>
        <div>Vol Level: <strong>${t.vol_regime ? t.vol_regime + '/5' : '—'}</strong></div>
        <div>Trend: <strong>${t.trend_regime != null ? (t.trend_regime > 0 ? '+' : '') + t.trend_regime : '—'}</strong></div>
        <div>Spread: <strong>${t.spread_at_entry_c != null ? t.spread_at_entry_c + '¢' : '—'}</strong></div>
        <div>Stability: <strong>${stab != null ? stab+'¢' : '—'}</strong></div>
        <div>Samples: <strong>${t.num_price_samples || '—'}</strong></div>
        <div>BTC: <strong>${t.btc_price_at_entry ? '$'+Math.round(t.btc_price_at_entry).toLocaleString() : '—'}</strong></div>
      </div>`;

    } else {
      // ── Normal trade detail ──
      const _fmr = t.market_result ? `<span class="${t.market_result==='yes'?'side-yes':'side-no'}">${t.market_result.toUpperCase()}</span>` : 'N/A';
      html += `<div style="display:grid;grid-template-columns:1fr 1fr;gap:3px 12px;font-size:12px;color:var(--dim)">
        <div>Side: <strong><span class="${sideCls}">${(t.side||'').toUpperCase()}</span> @ ${entry}¢</strong></div>
        <div>Sell Target: <strong>${sell}¢</strong></div>
        <div>Shares: <strong>${t.shares_filled||0}</strong></div>
        <div>Sold: <strong>${t.sell_filled||0}</strong></div>
        <div>Cost: <strong>$${(t.actual_cost||0).toFixed(2)}</strong></div>
        <div>Gross: <strong>$${(t.gross_proceeds||0).toFixed(2)}</strong></div>
        <div>Fees: <strong>$${(t.fees_paid||0).toFixed(2)}</strong></div>
        <div>Market Result: <strong>${_fmr}</strong></div>
        <div>HWM: <strong>${hwm}¢</strong></div>
        <div>LWM: <strong>${lwm}¢</strong></div>
        <div>Oscillations: <strong>${osc}</strong></div>
        <div>Progress: <strong>${prog.toFixed(0)}%</strong></div>
        <div>Stability: <strong>${stab != null ? stab+'¢' : '—'}</strong></div>
        <div>Entry Delay: <strong>${delay}m</strong></div>
      </div>`;
    }

    html += `<div style="margin-top:8px;padding-top:8px;border-top:1px solid var(--border);font-size:12px;color:var(--dim)">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:3px 12px">
        
        
        
        
      </div>
    </div>`;

    const _isRealTrade = ['win', 'loss', 'open'].includes(o);
    html += `<div style="margin-top:6px;display:flex;align-items:center;gap:6px">
      ${_isRealTrade ? riskTag(riskLvl) : ''} <span style="font-size:12px">${regime}</span>
    </div>`;

    let tags = [];
    if (t.is_ignored) tags.push('IGNORED');
    if (t.trade_mode) tags.push(t.trade_mode.toUpperCase());
    if (tags.length) {
      html += `<div style="margin-top:4px">${tags.map(tg => `<span class="tc-tag">${tg}</span>`).join(' ')}</div>`;
    }

    html += `<div style="margin-top:6px;font-size:11px;color:var(--dim)">
      Market: ${t.market_ct || '—'} · Traded: ${t.created_ct || '—'}
      ${t.notes ? '<br>Notes: ' + escHtml(t.notes) : ''}
    </div>`;

    el.innerHTML = html;

    // Open modal FIRST so canvas has dimensions
    openModal('tradeDetailOverlay');

    // Draw chart from price path (canvas is now visible)
    const canvas = document.getElementById('tradeDetailChart');
    if (path.length >= 2 && canvas) {
      canvas.style.display = '';
      const ctx = canvas.getContext('2d');
      const rect = canvas.getBoundingClientRect();
      if (rect.width > 0) {
        const dpr = window.devicePixelRatio || 1;
        canvas.width = rect.width * dpr;
        canvas.height = 100 * dpr;
        ctx.scale(dpr, dpr);
        const W2 = rect.width, H2 = 100;
        const pad2 = {t:8, b:14, l:4, r:4};
        ctx.clearRect(0, 0, W2, H2);

        const bids2 = path.map(p => p.our_side_bid || 0).filter(b => b > 0);

        // Add outcome point: where the trade actually resolved
        if (o === 'win' && sell > 0 && t.sell_filled > 0) {
          // Sell order filled at sell price
          bids2.push(sell);
        } else if (o === 'win' && t.market_result) {
          // Market expired in our favor — contract settled at 100¢
          bids2.push(99);
        } else if (o === 'loss' && t.sell_filled > 0 && t.exit_price_c) {
          // Early exit or sold at a loss
          bids2.push(t.exit_price_c);
        } else if (o === 'loss' && t.market_result) {
          // Market expired against us — contract settled at 0¢
          bids2.push(1);
        }

        if (bids2.length >= 2) {
          const allV = bids2.concat([entry]); if (sell > 0) allV.push(sell);
          let yMin2 = Math.min(...allV) - 3, yMax2 = Math.max(...allV) + 3;
          if (yMax2 - yMin2 < 10) { yMin2 -= 5; yMax2 += 5; }
          const toX2 = (i) => pad2.l + (i / (bids2.length - 1)) * (W2 - pad2.l - pad2.r);
          const toY2 = (v) => pad2.t + (1 - (v - yMin2) / (yMax2 - yMin2)) * (H2 - pad2.t - pad2.b);

          function drawDetailChart() {
            ctx.clearRect(0, 0, W2, H2);
            // Entry line
            ctx.strokeStyle = 'rgba(88,166,255,0.3)'; ctx.setLineDash([4,3]); ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(pad2.l, toY2(entry)); ctx.lineTo(W2-pad2.r, toY2(entry)); ctx.stroke();
            if (sell > 0) {
              ctx.strokeStyle = 'rgba(63,185,80,0.3)';
              ctx.beginPath(); ctx.moveTo(pad2.l, toY2(sell)); ctx.lineTo(W2-pad2.r, toY2(sell)); ctx.stroke();
            }
            ctx.setLineDash([]);
            // Price line
            const lastB = bids2[bids2.length-1];
            const lc2 = lastB >= entry ? '#3fb950' : '#f85149';
            ctx.strokeStyle = lc2; ctx.lineWidth = 1.5; ctx.beginPath();
            for (let i = 0; i < bids2.length; i++) {
              const x = toX2(i), y = toY2(bids2[i]);
              if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
            }
            ctx.stroke();
            // Labels
            ctx.font = '9px monospace'; ctx.fillStyle = 'rgba(88,166,255,0.5)';
            ctx.fillText(entry + '¢', W2-pad2.r-25, toY2(entry)-2);
            if (sell > 0) { ctx.fillStyle = 'rgba(63,185,80,0.5)'; ctx.fillText(sell + '¢', W2-pad2.r-25, toY2(sell)-2); }
          }
          drawDetailChart();

          // Enable touch crosshair
          canvas._chartMap = {
            data: bids2.map((b, i) => ({x: i, val: b})),
            toX: (x) => toX2(x),
            toY: (v) => toY2(v),
            fromX: (cssX) => (cssX - pad2.l) / (W2 - pad2.l - pad2.r) * (bids2.length - 1),
            pad: pad2, H: H2, W: W2,
            redraw: drawDetailChart,
            formatLabel: (p) => `${Math.round(p.val)}¢`,
          };
        }
      }
    } else if (canvas) {
      canvas.style.display = 'none';
    }
  } catch(e) { console.error('Trade detail error:', e); showToast('Detail error: ' + e.message, 'red'); }
}


const _pushTagColors = {
  'trade-result': 'var(--blue)', 'max-loss': 'var(--red)', 'auto-lock': 'var(--yellow)',
  'early-exit': 'var(--orange)', 'health-check': 'var(--red)', 'loss-stop': 'var(--blue)',
  'buy': 'var(--green)', 'skip': 'var(--dim)', 'withdrawal': 'var(--yellow)',
  'error': 'var(--red)', 'bankroll-limit': 'var(--red)',
  'deploy': 'var(--dim)',
  'auto-scale': 'var(--blue)',
};

function _renderPushLogs(logs) {
  if (!logs || !logs.length) return '<div class="dim" style="text-align:center;padding:20px">No notifications found.</div>';
  return logs.map(l => {
    const color = _pushTagColors[l.tag] || 'var(--dim)';
    return `<div style="padding:6px 0;border-bottom:1px solid rgba(255,255,255,0.04)">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <span style="font-size:13px;font-weight:600">${l.title}</span>
        <span class="dim" style="font-size:10px;white-space:nowrap;margin-left:8px">${l.sent_ct || ''}</span>
      </div>
      <div style="font-size:12px;color:var(--dim);margin-top:1px">${l.body || ''}</div>
      <span style="font-size:9px;color:${color};text-transform:uppercase;letter-spacing:0.5px">${(l.tag || '').replace(/-/g, ' ')}</span>
    </div>`;
  }).join('');
}

async function showPushLog(filterTag) {
  try {
    const url = filterTag ? `/api/push/log?tag=${encodeURIComponent(filterTag)}` : '/api/push/log';
    const logs = await api(url);
    const el = $('#tradeDetailContent');
    const canvas = document.getElementById('tradeDetailChart');
    if (canvas) canvas.style.display = 'none';

    // Build filter chips from known tags
    const tags = ['trade-result', 'buy', 'skip', 'max-loss', 'loss-stop', 'auto-lock',
                  'early-exit', 'withdrawal', 'health-check', 'auto-scale', 'error', 'deploy'];
    const chips = tags.map(t => {
      const active = filterTag === t ? ' active' : '';
      const label = t.replace(/-/g, ' ');
      return `<button class="chip${active}" onclick="showPushLog(${filterTag === t ? '' : "'" + t + "'"})" style="font-size:10px;padding:2px 6px">${label}</button>`;
    }).join('');

    let html = `<div class="dim" style="font-size:11px;font-weight:600;margin-bottom:6px">NOTIFICATION HISTORY</div>`;
    html += `<div class="filter-chips" style="margin-bottom:8px;flex-wrap:wrap">
      <button class="chip${!filterTag ? ' active' : ''}" onclick="showPushLog()" style="font-size:10px;padding:2px 6px">All</button>
      ${chips}
    </div>`;
    html += `<div style="max-height:60vh;overflow-y:auto;-webkit-overflow-scrolling:touch">${_renderPushLogs(logs)}</div>`;
    el.innerHTML = html;
    // Only open modal if not already showing (filter clicks re-render content in place)
    const overlay = document.getElementById('tradeDetailOverlay');
    if (overlay.style.display === 'none' || overlay.style.display === '') {
      openModal('tradeDetailOverlay');
    }
  } catch(e) { console.error('Push log error:', e); }
}

// ── Delete trade ─────────────────────────────────────────
let pendingDeleteId = null;

async function showDeleteTrade(tradeId, label, pnlStr) {
  pendingDeleteId = tradeId;
  const info = $('#deleteInfo');
  const btns = $('#deleteBtns');




  info.innerHTML = `
    <div style="font-size:13px;color:var(--text)">
      <strong>${label}</strong> ${pnlStr}
    </div>
  `;

  btns.innerHTML = `
    <div class="confirm-btns">
      <button class="btn btn-dim" onclick="hideDelete()">Cancel</button>
      <button class="btn btn-red" onclick="doDeleteSingle(${tradeId})">Delete This Trade</button>
    </div>
  `;

  openModal('deleteOverlay');
}

function hideDelete() {
  closeModal('deleteOverlay');
  pendingDeleteId = null;
}

async function doDeleteSingle(tradeId) {
  hideDelete();
  showToast('Deleting trade...', 'yellow');
  try {
    const r = await api(`/api/trade/${tradeId}/delete`, {method: 'POST'});
    if (r.ok) {
      showToast('Trade deleted — stats recomputed', 'green');
      loadTrades();
      loadRegimes();
    } else {
      showToast('Delete failed: ' + (r.error || ''), 'red');
    }
  } catch(e) {
    showToast('Delete error: ' + e, 'red');
  }
}

async function doDeleteTrade(tradeId, count) {
  hideDelete();
  showToast(`Deleting ${count} trades...`, 'yellow');
  try {
    const r = await api(`/api/trade/${tradeId}/delete`, {method: 'POST'});
    if (r.ok) {
      showToast(`${r.deleted} trades deleted — stats recomputed`, 'green');
      loadTrades();
      loadRegimes();
    } else {
      showToast('Delete failed: ' + (r.error || ''), 'red');
    }
  } catch(e) {
    showToast('Delete error: ' + e, 'red');
  }
}

let currentRegimeFilter = 'all';

function setRegimeFilter(filter, btn) {
  currentRegimeFilter = filter;
  document.querySelectorAll('#regimeFilters .chip').forEach(c => {
    c.className = 'chip';
    if (c.dataset.filter === filter) {
      const colorMap = {low:'active-green', moderate:'active-yellow', high:'active-orange',
                        terrible:'active-red', unknown:'active'};
      c.className = 'chip ' + (colorMap[filter] || 'active');
    }
  });
  loadRegimes();
}

// ── Risk Level Actions & Regime Overrides ─────────────────
let _riskLevelActions = {low:'normal',moderate:'normal',high:'normal',terrible:'skip',unknown:'skip'};
let _regimeOverrides = {};

function setRiskAction(risk, action, btn) {
  if (_isQuickTradeActive() || _isTradeAllActive()) {
    showToast('Risk actions locked \u2014 clear quick-trade or trade-all first', 'yellow'); return;
  }
  _riskLevelActions[risk] = action;
  btn.parentElement.querySelectorAll('.abtn').forEach(b => b.classList.remove('abtn-active'));
  btn.classList.add('abtn-active');
  api('/api/config', {
    method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':_getCsrfToken()},
    body:JSON.stringify({risk_level_actions: _riskLevelActions})
  });
}

// Map of base regime → child labels, populated by loadRegimes
let _regimeGroupMap = {};

function setRegimeOverride(label, action) {
  if (_isQuickTradeActive() || _isTradeAllActive()) {
    showToast('Overrides locked \u2014 clear quick-trade or trade-all first', 'yellow'); return;
  }

  const children = _regimeGroupMap[label];
  const isParent = children && children.length > 0;

  if (isParent) {
    // Parent set → propagate to ALL children
    if (action === 'default') {
      delete _regimeOverrides[label];
      for (const c of children) delete _regimeOverrides[c];
    } else {
      _regimeOverrides[label] = action;
      for (const c of children) _regimeOverrides[c] = action;
    }
  } else {
    // Child set → update child, then recompute parent state
    if (action === 'default') {
      delete _regimeOverrides[label];
    } else {
      _regimeOverrides[label] = action;
    }
    // Find parent and update its visual state
    const parentBase = _baseRegimeLabel(label);
    if (parentBase !== label && _regimeGroupMap[parentBase]) {
      _syncParentFromChildren(parentBase);
    }
  }

  api('/api/config', {
    method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':_getCsrfToken()},
    body:JSON.stringify({regime_overrides: _regimeOverrides})
  }).then(() => {
    const display = action === 'default' ? 'Auto' : action === 'normal' ? 'Trade' : 'Skip';
    showToast(`${label.replace(/_/g,' ')}: ${display}`, 'blue');
  }).catch(e => {
    showToast('Save failed', 'red');
    console.error('Override save error:', e);
  });

  // Refresh regime list to update all visuals
  loadRegimes(true);
}

function _syncParentFromChildren(parentBase) {
  const children = _regimeGroupMap[parentBase] || [];
  if (!children.length) return;
  // Check if all children have the same override
  const states = children.map(c => _regimeOverrides[c] || 'default');
  const allSame = states.every(s => s === states[0]);
  if (allSame) {
    // All match — parent takes that value
    if (states[0] === 'default') delete _regimeOverrides[parentBase];
    else _regimeOverrides[parentBase] = states[0];
  } else {
    // Mixed — parent becomes custom (store as special marker)
    _regimeOverrides[parentBase] = '_custom';
  }
}

function _getParentState(base, children) {
  // Compute parent display state from children
  if (!children || !children.length) return _regimeOverrides[base] || 'default';
  const states = children.map(c => _regimeOverrides[c.regime_label || c] || 'default');
  const allSame = states.every(s => s === states[0]);
  if (allSame) return states[0];
  return '_custom';
}

function _renderChildDots(children) {
  const dotStyles = {
    default: 'border-color:rgba(88,166,255,0.5);background:rgba(88,166,255,0.12)',
    normal: 'border-color:rgba(63,185,80,0.5);background:rgba(63,185,80,0.12)',
    skip: 'border-color:rgba(248,81,73,0.5);background:rgba(248,81,73,0.12)',
  };
  return children.map(c => {
    const st = _regimeOverrides[c.regime_label || c] || 'default';
    const s = dotStyles[st] || dotStyles.default;
    return `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;border:1.5px solid;${s}"></span>`;
  }).join('');
}

function _colorRegimeSelect(sel) {
  sel.classList.remove('ras-trade', 'ras-skip', 'ras-inherit', 'ras-custom', 'ras-auto');
  const v = sel.value;
  if (v === 'normal') sel.classList.add('ras-trade');
  else if (v === 'skip') sel.classList.add('ras-skip');
  else if (v === '_custom') sel.classList.add('ras-custom');
  else sel.classList.add('ras-auto');
}

function _loadRiskActionButtons(actions) {
  if (!actions) return;
  _riskLevelActions = actions;
  document.querySelectorAll('[data-risk]').forEach(row => {
    const risk = row.dataset.risk;
    let action = actions[risk] || 'normal';
    if (action === 'data') action = 'skip';  // data bets removed — treat as skip
    row.querySelectorAll('.abtn').forEach(b => {
      b.classList.toggle('abtn-active', b.dataset.action === action);
    });
  });
}

async function loadRegimes(force) {
  // Skip periodic background refresh if not on Regimes tab or regime detail modal is open
  if (!force && _currentTab !== 'Regimes') return;
  if (!force) {
    const detailOverlay = document.getElementById('regimeDetailOverlay');
    if (detailOverlay && detailOverlay.style.display !== 'none') return;
  }

  let regimes = await api('/api/regimes');
  hideSkel('skelRegimes');
  hideSkel('skelRegimeCurrent');
  const el = $('#regimeList');
  if (!regimes.length) { el.innerHTML = '<div class="dim">No regime data yet — waiting for market observations</div>'; return; }

  // Helper: strip modifiers to get base label (mirrors bot.py _base_regime_label)
  function baseLabel(label) {
    let b = label;
    for (const pfx of ['thin_', 'squeeze_']) { if (b.startsWith(pfx)) b = b.slice(pfx.length); }
    for (const sfx of ['_accel', '_decel']) { if (b.endsWith(sfx)) b = b.slice(0, -sfx.length); }
    return b;
  }

  // Helper: what modifier does this variant add?
  function modifierName(label, base) {
    if (label === base) return null;
    let name = label;
    for (const pfx of ['thin_', 'squeeze_']) { if (name.startsWith(pfx)) { name = pfx.slice(0,-1); return name; } }
    for (const sfx of ['_accel', '_decel']) { if (label.endsWith(sfx)) return sfx.slice(1); }
    return label.replace(base, '').replace(/^_|_$/g, '') || null;
  }

  // Helper: compute effective override mirroring bot.py logic
  function getEffectiveOverride(fineLabel) {
    let action = _regimeOverrides[fineLabel] || 'default';
    if (action === 'default') {
      const base = baseLabel(fineLabel);
      if (base !== fineLabel) action = _regimeOverrides[base] || 'default';
    }
    return action;
  }

  if (currentRegimeFilter === 'has_ev') {
    regimes = regimes.filter(r => r.best_ev_c != null);
  } else if (currentRegimeFilter === 'positive_ev') {
    regimes = regimes.filter(r => r.best_ev_c != null && r.best_ev_c > 0);
  }

  // Group by base label
  const groups = {};
  for (const r of regimes) {
    const base = baseLabel(r.regime_label || 'unknown');
    if (!groups[base]) groups[base] = {children: [], totalObs: 0, totalEnc: 0, totalTrades: 0, wins: 0, losses: 0, pnl: 0, bestEv: null};
    groups[base].children.push(r);
    groups[base].totalObs += (r.obs_count || 0);
    groups[base].totalEnc += (r.encounter_count || 0);
    groups[base].totalTrades += (r.total_trades || 0);
    groups[base].wins += (r.wins || 0);
    groups[base].losses += (r.losses || 0);
    groups[base].pnl += (r.total_pnl || 0);
    const ev = r.best_ev_c;
    if (ev != null && (groups[base].bestEv == null || ev > groups[base].bestEv)) groups[base].bestEv = ev;
  }

  // Sort groups by total sightings
  const sorted = Object.entries(groups).sort((a,b) =>
    Math.max(b[1].totalObs, b[1].totalEnc) -
    Math.max(a[1].totalObs, a[1].totalEnc));

  // Populate global parent→children map for override propagation
  _regimeGroupMap = {};
  for (const [base, g] of sorted) {
    const hasVariants = g.children.length > 1 || (g.children.length === 1 && g.children[0].regime_label !== base);
    if (hasVariants) {
      _regimeGroupMap[base] = g.children.map(r => r.regime_label);
    }
  }

  // Render helper for a single regime line (child within a group)
  function regimeLine(r, showDetail) {
    const label = r.regime_label || '';
    const obsN = r.obs_count || 0;
    const encN = r.encounter_count || 0;
    const realN = r.total_trades || 0;
    const w = r.wins || 0;
    const l = r.losses || 0;
    const finePnl = r.total_pnl || 0;
    const pnlCls = finePnl > 0 ? 'pos' : finePnl < 0 ? 'neg' : '';
    const countLabel = realN > 0 ? `${realN} trades \u00b7 ${obsN} obs` :
                       encN > 0 && obsN > 0 ? `${encN} seen \u00b7 ${obsN} obs` :
                       encN > 0 ? `${encN} seen` : obsN > 0 ? `${obsN} obs` : '\u2014';
    const fEv = r.best_ev_c;
    let evBadge = '';
    if (fEv != null) {
      const c = fEv > 0 ? 'var(--green)' : fEv < 0 ? 'var(--red)' : 'var(--dim)';
      evBadge = `<span style="font-size:10px;color:${c};font-family:monospace;font-weight:600">${fEv>=0?'+':''}${fEv.toFixed(1)}\u00a2</span>`;
    }
    const direct = _regimeOverrides[label] || 'default';
    const escLabel = label.replace(/'/g, "\\'");
    const mod = modifierName(label, baseLabel(label));
    const displayName = showDetail ? (mod ? mod.replace(/_/g, ' ') : 'base') : label.replace(/_/g, ' ');
    const rf = _regimeFilters[label] || {};
    const fc = (rf.blocked_hours ? rf.blocked_hours.length : 0) + (rf.blocked_days ? rf.blocked_days.length : 0) + (rf.vol_min > 1 || rf.vol_max < 5 ? 1 : 0) + (rf.stability_max > 0 ? 1 : 0) + (rf.blocked_sides ? rf.blocked_sides.length : 0) + (rf.max_spread_c > 0 ? 1 : 0);
    const fBadge = fc > 0 ? ` <span style="font-size:9px;background:var(--blue);color:#fff;padding:1px 4px;border-radius:6px">${fc}</span>` : '';

    return `<div style="padding:5px 8px;border-bottom:1px solid rgba(48,54,61,0.3);cursor:pointer" onclick="showRegimeDetail('${escLabel}')">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div style="display:flex;align-items:center;gap:5px;flex:1;min-width:0">
          <span style="font-size:11px;color:var(--text)">${displayName}</span>${fBadge}
        </div>
        <div style="display:flex;align-items:center;gap:6px;flex-shrink:0">
          ${evBadge}
          <select class="regime-action-sel${direct==='normal'?' ras-trade':direct==='skip'?' ras-skip':' ras-auto'}" data-regime="${label.replace(/"/g,'&quot;')}" onchange="event.stopPropagation();setRegimeOverride('${escLabel}',this.value);_colorRegimeSelect(this)" onclick="event.stopPropagation()" style="font-size:10px;padding:1px 2px">
            <option value="default" ${direct==='default'?'selected':''}>Auto</option>
            <option value="normal" ${direct==='normal'?'selected':''}>Trade</option>
            <option value="skip" ${direct==='skip'?'selected':''}>Skip</option>
          </select>
        </div>
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-top:2px">
        <span style="font-size:10px;color:var(--dim)">${w}W / ${l}L \u00b7 <span class="${pnlCls}">${fmtPnl(finePnl)}</span></span>
        <span class="dim" style="font-size:10px">${countLabel}</span>
      </div>
    </div>`;
  }

  // Remember which groups are currently expanded before rebuild
  const _expandedGroups = new Set();
  el.querySelectorAll('[id^="rg_"]').forEach(div => {
    if (div.style.display !== 'none') _expandedGroups.add(div.id);
  });

  el.innerHTML = sorted.map(([base, g]) => {
    const baseDisplay = base.replace(/_/g, ' ');
    const totalN = Math.max(g.totalObs, g.totalEnc);
    const hasVariants = g.children.length > 1 || (g.children.length === 1 && g.children[0].regime_label !== base);
    const bestEv = g.bestEv;
    const borderColor = bestEv > 5 ? 'var(--green)' : bestEv > 0 ? 'var(--blue)' : bestEv != null && bestEv < 0 ? 'var(--red)' : 'var(--border)';
    let evBadge = '';
    if (bestEv != null) {
      const evColor = bestEv > 0 ? 'var(--green)' : bestEv < 0 ? 'var(--red)' : 'var(--dim)';
      evBadge = `<span style="font-size:10px;color:${evColor};font-weight:600;font-family:monospace">${bestEv>=0?'+':''}${bestEv.toFixed(1)}¢</span>`;
    }
    const baseOverride = _regimeOverrides[base] || 'default';
    const escBase = base.replace(/'/g, "\\'");
    const pnlCls = g.pnl > 0 ? 'pos' : g.pnl < 0 ? 'neg' : '';
    const groupId = 'rg_' + base.replace(/[^a-zA-Z0-9]/g, '_');

    // Compute parent state from children for multi-variant groups
    const parentState = hasVariants ? _getParentState(base, g.children) : (baseOverride);
    const isCustom = parentState === '_custom';
    const parentSelCls = parentState === 'normal' ? ' ras-trade' : parentState === 'skip' ? ' ras-skip' : isCustom ? ' ras-custom' : ' ras-auto';

    // Parent override selector
    const overrideHtml = `<select class="regime-action-sel${parentSelCls}" data-regime="${base.replace(/"/g,'&quot;')}" onchange="setRegimeOverride('${escBase}',this.value);_colorRegimeSelect(this)" onclick="event.stopPropagation()" style="font-size:10px">
      <option value="default" ${parentState==='default'?'selected':''}>Auto</option>
      <option value="normal" ${parentState==='normal'?'selected':''}>Trade</option>
      <option value="skip" ${parentState==='skip'?'selected':''}>Skip</option>
      ${isCustom ? '<option value="_custom" selected disabled>Custom</option>' : ''}
    </select>`;

    // Colored dots when Custom (one dot per child)
    const dotsHtml = isCustom && hasVariants ? `<span style="display:inline-flex;align-items:center;gap:3px;margin-left:4px">${_renderChildDots(g.children)}</span>` : '';

    // Stats line — always visible
    const statsLine = `<div style="display:flex;justify-content:space-between;align-items:center;margin-top:4px">
      <span style="font-size:11px;color:var(--dim)">${g.wins}W / ${g.losses}L \u00b7 <span class="${pnlCls}">${fmtPnl(g.pnl)}</span> \u00b7 ${totalN} total</span>
      <span onclick="event.stopPropagation()">${overrideHtml}</span>
    </div>`;

    if (!hasVariants) {
      // Single regime — flat card, tappable for detail
      const r = g.children[0];
      const escLabel = (r.regime_label||'').replace(/'/g, "\\'");
      return `<div style="background:var(--card);border:1px solid var(--border);border-radius:6px;padding:8px;margin-bottom:6px;border-left:3px solid ${borderColor};cursor:pointer" onclick="showRegimeDetail('${escLabel}')">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <span style="font-size:13px;font-weight:600">${baseDisplay}</span>
          <div style="display:flex;align-items:center;gap:6px">
            ${evBadge}
          </div>
        </div>
        ${statsLine}
      </div>`;
    }

    // Multiple variants — expandable card
    const variants = g.children.sort((a,b) => (b.encounter_count||0) + (b.obs_count||0) - (a.encounter_count||0) - (a.obs_count||0));
    const variantHtml = variants.map(r => regimeLine(r, true)).join('');

    return `<div style="background:var(--card);border:1px solid var(--border);border-radius:6px;margin-bottom:6px;border-left:3px solid ${borderColor};overflow:hidden">
      <div style="padding:8px;cursor:pointer" onclick="const c=document.getElementById('${groupId}');const a=this.querySelector('.rg-chev');if(c.style.display==='none'){c.style.display='';a.textContent='\u25be'}else{c.style.display='none';a.textContent='\u25b8'}">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <div style="display:flex;align-items:center;gap:6px">
            <span class="rg-chev dim" style="font-size:10px;width:10px">\u25b8</span>
            <span style="font-size:13px;font-weight:600">${baseDisplay}</span>
            <span style="font-size:9px;color:var(--dim);background:rgba(48,54,61,0.5);padding:1px 5px;border-radius:8px">${g.children.length}</span>
            ${dotsHtml}
          </div>
          <div style="display:flex;align-items:center;gap:6px">
            ${evBadge}
          </div>
        </div>
        ${statsLine}
      </div>
      <div id="${groupId}" style="display:none;background:rgba(0,0,0,0.15);border-top:1px solid var(--border)">
        ${variantHtml}
      </div>
    </div>`;
  }).join('');

  // Restore expanded groups
  _expandedGroups.forEach(id => {
    const div = document.getElementById(id);
    if (div) {
      div.style.display = '';
      const chev = div.parentElement.querySelector('.rg-chev');
      if (chev) chev.textContent = '▾';
    }
  });

  // Apply quick-trade visual lock to regime list
  _applyQuickTradeLockToRegimeList();
}

async function showRegimeDetail(label) {
  try {
    const d = await api('/api/regime/' + encodeURIComponent(label) + '/detail');
    const s = d.stats || {};
    const avgs = d.averages || {};
    const f = d.filters || {};
    const wr = ((s.win_rate||0)*100).toFixed(1);
    const ciL = ((s.ci_lower||0)*100).toFixed(0);
    const ciU = ((s.ci_upper||1)*100).toFixed(0);
    const pnl = s.total_pnl || 0;
    const pnlCls = pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : '';
    const override = (_regimeOverrides[label]) || 'default';
    const isCoarse = label.startsWith('coarse:');

    const obsCount = d.strategies ? '' : '';
    const obsLabel = (s.total_trades || 0) > 0 ? `<span class="dim" style="font-size:10px">(${s.total_trades} trades)</span> ` : '';
    $('#regimeDetailTitle').innerHTML = obsLabel + (isCoarse ? '<span style="color:var(--dim);font-size:11px">GROUP</span> ' : '') + label.replace(/^coarse:/, '').replace(/_/g, ' ');

    let html = '';

    // Override selector (not for coarse — overrides don't apply)
    if (!isCoarse) {
    const _qtLock = _isQuickTradeActive() || _isTradeAllActive();
    const _ovrDisabled = _qtLock ? ' disabled style="font-size:13px;padding:4px 8px;opacity:0.5"' : ' style="font-size:13px;padding:4px 8px"';
    const _selCls = override === 'normal' ? ' ras-trade' : override === 'skip' ? ' ras-skip' : ' ras-auto';
    const _lockLabel = _isTradeAllActive() ? 'Trade-all' : _isQuickTradeActive() ? 'Quick-trade' : '';
    html += `<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
      <span class="dim" style="font-size:11px">Action:</span>
      <select class="regime-action-sel${_selCls}" data-regime="${label.replace(/"/g,'&quot;')}" onchange="setRegimeOverride('${label.replace(/'/g,"\\'")}',this.value)"${_ovrDisabled}>
        <option value="default" ${override==='default'?'selected':''}>Auto (use risk level)</option>
        <option value="normal" ${override==='normal'?'selected':''}>Always Trade</option>
        <option value="skip" ${override==='skip'?'selected':''}>Always Skip</option>
      </select>
      ${_lockLabel ? `<span style="font-size:10px;color:var(--blue)">${_lockLabel}</span>` : ''}
    </div>`;
    } else {
      html += `<div class="dim" style="font-size:11px;margin-bottom:10px;padding:6px 8px;background:rgba(48,54,61,0.3);border-radius:6px">Stats only — overrides and filters apply to the specific regimes within this group, not the coarse label itself.</div>`;
    }

    // Overview stats
    const obsC = d.obs_count || 0;
    const resC = d.resolved_count || 0;
    const tradesVal = (s.total_trades||0) > 0 ? (s.total_trades||0) : (obsC > 0 ? `<span style="font-size:11px;color:var(--dim)">${obsC} obs</span>` : '0');
    html += `<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:12px">
      <div class="stat"><div class="label">Win Rate</div><div class="val" style="font-size:20px">${wr}%</div></div>
      <div class="stat"><div class="label">Trades</div><div class="val">${tradesVal}</div></div>
      <div class="stat"><div class="label">P&L</div><div class="val ${pnlCls}">${fmtPnl(pnl)}</div></div>
    </div>`;

    // Detail grid
    const obsLine = obsC > 0 ? `<div>Observations: <strong>${resC}/${obsC} resolved</strong></div>` : '';
    html += `<div style="display:grid;grid-template-columns:1fr 1fr;gap:3px 12px;font-size:12px;color:var(--dim);margin-bottom:12px">
      <div>CI: <strong>${ciL}–${ciU}%</strong></div>
      <div>Avg P&L: <strong>${(s.avg_pnl||0)>=0?'+':''}$${(s.avg_pnl||0).toFixed(2)}</strong></div>
      <div>Avg Entry: <strong>${avgs.avg_entry ? Math.round(avgs.avg_entry)+'¢' : '—'}</strong></div>
      <div>Avg Sell: <strong>${avgs.avg_sell ? Math.round(avgs.avg_sell)+'¢' : '—'}</strong></div>
      <div>Best: <strong class="pos">${avgs.best_pnl!=null ? fmtPnl(avgs.best_pnl) : '—'}</strong></div>
      <div>Worst: <strong class="neg">${avgs.worst_pnl!=null ? fmtPnl(avgs.worst_pnl) : '—'}</strong></div>
      ${obsLine}
    </div>`;

    const escLabel = label.replace(/'/g, "\\'");
    const DAY_NAMES = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];

    if (!isCoarse) {
    // Check if filters are locked (quick-trade or trade-all)
    const _qtLocked = _isQuickTradeActive() || _isTradeAllActive();
    if (_qtLocked) {
      const _lockSource = _isTradeAllActive() ? 'Trade-All Active' : 'Quick-Trade Active';
      const _lockHint = _isTradeAllActive() ? 'All per-regime filters are bypassed. Disable trade-all in Settings to edit.' : 'All per-regime filters are locked. Clear quick-trade from Settings to edit.';
      html += `<div style="border-top:1px solid var(--border);padding-top:8px;margin-bottom:10px;padding:8px;border-radius:6px;background:rgba(88,166,255,0.06);border:1px solid rgba(88,166,255,0.15)">
        <div style="font-size:11px;color:var(--blue);font-weight:600">${_lockSource}</div>
        <div style="font-size:10px;color:var(--dim);margin-top:2px">${_lockHint}</div>
      </div>`;
    }
    const _filterLockStyle = _qtLocked ? ' style="opacity:0.35;pointer-events:none"' : '';

    // ── BY VOLATILITY ──
    html += `<div${_filterLockStyle}>`;
    {
      const volMin = f.vol_min || 1;
      const volMax = f.vol_max || 5;
      const VOL_LABELS = {1:'calm',2:'low',3:'mid',4:'high',5:'extreme'};
      const VOL_COLORS = {1:'var(--green)',2:'var(--green)',3:'var(--yellow)',4:'var(--orange)',5:'var(--red)'};
      html += `<div style="border-top:1px solid var(--border);padding-top:8px;margin-bottom:10px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
          <div class="dim" style="font-size:11px;font-weight:600">BY VOLATILITY</div>
          <div style="display:flex;align-items:center;gap:4px;font-size:10px;color:var(--dim)">
            <select onchange="_saveRegimeFilter('${escLabel}','vol_min',parseInt(this.value))" style="background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px;padding:2px 4px;font-size:10px">
              ${[1,2,3,4,5].map(v => `<option value="${v}" ${v===volMin?'selected':''}>${v}</option>`).join('')}
            </select>
            <span>to</span>
            <select onchange="_saveRegimeFilter('${escLabel}','vol_max',parseInt(this.value))" style="background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px;padding:2px 4px;font-size:10px">
              ${[1,2,3,4,5].map(v => `<option value="${v}" ${v===volMax?'selected':''}>${v}</option>`).join('')}
            </select>
          </div>
        </div>`;
      for (let v = 1; v <= 5; v++) {
        const vd = (d.by_vol||[]).find(x => x.vol_regime === v);
        const vn = vd ? vd.n : 0; const vw = vd ? vd.wins : 0;
        const vwr = vn > 0 ? ((vw/vn)*100).toFixed(0) : '—';
        const active = v >= volMin && v <= volMax;
        html += `<div style="display:flex;justify-content:space-between;padding:3px 0;font-size:11px;opacity:${active?1:0.4}">
          <span style="color:${VOL_COLORS[v]||'var(--dim)'}">Vol ${v} <span class="dim">${VOL_LABELS[v]||''}</span></span>
          <span>${vn > 0 ? vn+' trades · '+vwr+'%' : 'no data'}</span>
        </div>`;
      }
      html += `</div>`;
    }

    // ── BY HOUR ──
    {
      const blockedHours = f.blocked_hours || [];
      html += `<div style="border-top:1px solid var(--border);padding-top:8px;margin-bottom:10px">
        <div class="dim" style="font-size:11px;font-weight:600;margin-bottom:6px">BY HOUR <span style="font-weight:400">(tap to block)</span></div>
        <div id="hourGrid_${escLabel.replace(/'/g,'')}" style="display:grid;grid-template-columns:repeat(6,1fr);gap:3px">`;
      for (let h = 0; h < 24; h++) {
        const hd = (d.by_hour||[]).find(x => x.hour_et === h);
        const hn = hd ? hd.n : 0; const hw = hd ? hd.wins : 0;
        const hwr = hn > 0 ? ((hw/hn)*100).toFixed(0) : '—';
        const blocked = blockedHours.includes(h);
        html += `<div data-hour="${h}" data-blocked="${blocked?1:0}" data-n="${hn}" data-wins="${hw}" onclick="_toggleRegimeHour('${escLabel}',${h},this)" style="text-align:center;padding:4px 2px;border-radius:4px;cursor:pointer;font-size:10px;${_hourCellStyle(blocked, hn, hw)}">
          <div style="font-weight:600">${h}:00</div>
          <div class="dim">${hn > 0 ? hwr+'%' : '—'}</div>
          <div class="dim">${hn > 0 ? '('+hn+')' : ''}</div>
        </div>`;
      }
      html += `</div></div>`;
    }

    // ── BY DAY ──
    {
      const blockedDays = f.blocked_days || [];
      html += `<div style="border-top:1px solid var(--border);padding-top:8px;margin-bottom:10px">
        <div class="dim" style="font-size:11px;font-weight:600;margin-bottom:6px">BY DAY <span style="font-weight:400">(tap to block)</span></div>
        <div id="dayGrid_${escLabel.replace(/'/g,'')}" style="display:grid;grid-template-columns:repeat(7,1fr);gap:3px">`;
      for (let dy = 0; dy < 7; dy++) {
        const dd = (d.by_day||[]).find(x => x.day_of_week === dy);
        const dn = dd ? dd.n : 0; const dw = dd ? dd.wins : 0;
        const dwr = dn > 0 ? ((dw/dn)*100).toFixed(0) : '—';
        const blocked = blockedDays.includes(dy);
        html += `<div data-day="${dy}" data-blocked="${blocked?1:0}" data-n="${dn}" data-wins="${dw}" onclick="_toggleRegimeDay('${escLabel}',${dy},this)" style="text-align:center;padding:6px 2px;border-radius:4px;cursor:pointer;font-size:10px;${_dayCellStyle(blocked, dn, dw)}">
          <div style="font-weight:600">${DAY_NAMES[dy]}</div>
          <div class="dim">${dn > 0 ? dwr+'%' : '—'}</div>
          <div class="dim">${dn > 0 ? '('+dn+')' : ''}</div>
        </div>`;
      }
      html += `</div></div>`;
    }

    // ── BY STABILITY ──
    {
      const stabMax = f.stability_max || 0;
      html += `<div style="border-top:1px solid var(--border);padding-top:8px;margin-bottom:10px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
          <div class="dim" style="font-size:11px;font-weight:600">BY STABILITY</div>
          <div style="display:flex;align-items:center;gap:4px;font-size:10px;color:var(--dim)">
            Max:
            <input type="number" min="0" max="50" value="${stabMax}" style="width:40px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px;padding:2px 4px;font-size:10px;text-align:center"
              onchange="_saveRegimeFilter('${escLabel}','stability_max',parseInt(this.value)||0)">
            <span>¢ (0=off)</span>
          </div>
        </div>`;
      if (d.by_stability && d.by_stability.length) {
        for (const sb of d.by_stability) {
          const sn = sb.n || 0; const sw = sb.wins || 0; const swr = sn > 0 ? ((sw/sn)*100).toFixed(0) : '—';
          const sp = sb.pnl || 0; const spc = sp > 0 ? 'pos' : sp < 0 ? 'neg' : '';
          html += `<div style="display:flex;justify-content:space-between;padding:3px 0;font-size:11px">
            <span>${sb.bucket}¢</span>
            <span>${sn} trades · ${swr}% · <span class="${spc}">${fmtPnl(sp)}</span></span>
          </div>`;
        }
      } else {
        html += `<div class="dim" style="font-size:10px">No stability data yet</div>`;
      }
      html += `</div>`;
    }

    // ── BY SIDE ──
    {
      const blockedSides = f.blocked_sides || [];
      const sideData = d.sides || [];
      html += `<div style="border-top:1px solid var(--border);padding-top:8px;margin-bottom:10px">
        <div class="dim" style="font-size:11px;font-weight:600;margin-bottom:6px">BY SIDE <span style="font-weight:400">(tap to block)</span></div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">`;
      for (const sideVal of ['yes', 'no']) {
        const sd = sideData.find(x => x.side === sideVal) || {side: sideVal, n: 0, wins: 0, pnl: 0};
        const swr = sd.n > 0 ? ((sd.wins/sd.n)*100).toFixed(0) : '—';
        const sCls = sd.side === 'yes' ? 'side-yes' : 'side-no';
        const sPnlCls = (sd.pnl||0) > 0 ? 'pos' : (sd.pnl||0) < 0 ? 'neg' : '';
        const sBlocked = blockedSides.includes(sd.side);
        const sBg = sBlocked ? 'rgba(248,81,73,0.15)' : 'var(--bg)';
        const sBorder = sBlocked ? '2px solid rgba(248,81,73,0.5)' : '1px solid transparent';
        html += `<div onclick="_toggleRegimeSide('${escLabel}','${sd.side}',this)" data-blocked="${sBlocked?1:0}" style="background:${sBg};padding:6px;border-radius:4px;border:${sBorder};cursor:pointer;opacity:${sBlocked?0.5:1};${sBlocked?'text-decoration:line-through;':''}">
          <span class="${sCls}" style="font-weight:600">${(sd.side||'').toUpperCase()}</span>
          <span class="dim" style="font-size:11px"> ${sd.n} · ${swr}% · <span class="${sPnlCls}">${fmtPnl(sd.pnl||0)}</span></span>
        </div>`;
      }
      html += `</div></div>`;
    }

    // ── BY SPREAD ──
    if (d.by_spread && d.by_spread.length) {
      const maxSpread = f.max_spread_c || 0;
      html += `<div style="border-top:1px solid var(--border);padding-top:8px;margin-bottom:10px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
          <div class="dim" style="font-size:11px;font-weight:600">BY SPREAD</div>
          <div style="display:flex;align-items:center;gap:4px;font-size:10px;color:var(--dim)">
            Max:
            <input type="number" min="0" max="50" value="${maxSpread}" style="width:40px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px;padding:2px 4px;font-size:10px;text-align:center"
              onchange="_saveRegimeFilter('${escLabel}','max_spread_c',parseInt(this.value)||0)">
            <span>¢ (0=off)</span>
          </div>
        </div>`;
      for (const sp of d.by_spread) {
        const spn = sp.n || 0; const spw = sp.wins || 0; const spwr = spn > 0 ? ((spw/spn)*100).toFixed(0) : '—';
        const spp = sp.pnl || 0; const sppc = spp > 0 ? 'pos' : spp < 0 ? 'neg' : '';
        const avgSp = sp.avg_spread ? sp.avg_spread.toFixed(1) + '¢' : '';
        html += `<div style="display:flex;justify-content:space-between;padding:3px 0;font-size:11px">
          <span>${sp.bucket}</span>
          <span>${spn} trades · ${spwr}% · <span class="${sppc}">${fmtPnl(spp)}</span></span>
        </div>`;
      }
      html += `</div>`;
    }

    html += `</div>`; // close filter lock wrapper
    } // end if (!isCoarse) — filter sections

    // ── BEST STRATEGIES (from Strategy Observatory) ──
    if (d.strategies && d.strategies.length) {
      html += `<div style="border-top:1px solid var(--border);padding-top:8px;margin-bottom:10px">
        <div class="dim" style="font-size:11px;font-weight:600;margin-bottom:4px">BEST STRATEGIES <span style="font-weight:400">(from Observatory · tap to apply)</span></div>`;
      for (let i = 0; i < Math.min(d.strategies.length, 5); i++) {
        const st = d.strategies[i];
        const ev = (st.ev_per_trade_c || 0);
        const evFmt = (ev >= 0 ? '+' : '') + ev.toFixed(1) + '¢';
        const evColor = ev > 0 ? 'var(--green)' : ev < 0 ? 'var(--red)' : 'var(--dim)';
        const wr = ((st.win_rate||0)*100).toFixed(0);
        const pf = st.profit_factor != null ? st.profit_factor.toFixed(1) : '—';
        const ciL = ((st.ci_lower||0)*100).toFixed(0);
        const ciU = ((st.ci_upper||1)*100).toFixed(0);
        const maxL = st.max_consecutive_losses || 0;
        const rank = i + 1;
        const rankColor = rank === 1 ? 'var(--green)' : rank <= 3 ? 'var(--blue)' : 'var(--dim)';
        const stratLabel = typeof _fmtStratKey === 'function' ? _fmtStratKey(st.strategy_key) : st.strategy_key;
        const isAfActive = _strategyAutoFill && _strategyAutoFill.stratKey === st.strategy_key && _strategyAutoFill.regime === label;
        const afCls = isAfActive ? ' af-active' : '';
        const escStratKey = (st.strategy_key||'').replace(/'/g, "\\'");
        html += `<div class="best-strat-row${afCls}" onclick="_applyStrategyAutoFill('${escLabel}',${rank},'${escStratKey}')" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px;margin-bottom:4px;border-left:3px solid ${evColor}">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px">
            <div>
              <span style="font-size:10px;font-weight:700;color:${rankColor}">#${rank}</span>
              <span class="dim" style="font-size:10px;margin-left:3px">${stratLabel}</span>
              ${isAfActive ? '<span style="font-size:9px;color:#a371f7;font-weight:700;margin-left:4px">ACTIVE</span>' : ''}
            </div>
            <span style="font-size:13px;font-weight:700;font-family:monospace;color:${evColor}">${evFmt}</span>
          </div>
          <div style="display:flex;gap:8px;font-size:10px;color:var(--dim)">
            <span>WR ${wr}%</span><span>n=${st.sample_size||0}</span><span>PF ${pf}</span><span>CI ${ciL}–${ciU}%</span><span>MaxL ${maxL}</span>
          </div>
        </div>`;
      }
      if (!d.strategies.length) {
        html += `<div class="dim" style="font-size:10px">No strategy data yet — Observatory needs more resolved markets</div>`;
      }
      html += `</div>`;
    } else if (!isCoarse) {
      html += `<div style="border-top:1px solid var(--border);padding-top:8px;margin-bottom:10px">
        <div class="dim" style="font-size:11px;font-weight:600;margin-bottom:4px">BEST STRATEGIES</div>
        <div class="dim" style="font-size:10px">No strategy data yet — Observatory needs resolved markets for this regime</div>
      </div>`;
    }

    // ── RECENT TRADES ──
    if (d.recent && d.recent.length) {
      html += `<div style="border-top:1px solid var(--border);padding-top:8px">
        <div class="dim" style="font-size:11px;font-weight:600;margin-bottom:4px">RECENT TRADES</div>`;
      for (const t of d.recent) {
        const tPnl = t.pnl || 0;
        const tCls = tPnl > 0 ? 'pos' : tPnl < 0 ? 'neg' : '';
        const tSideCls = t.side === 'yes' ? 'side-yes' : 'side-no';
        html += `<div style="display:flex;justify-content:space-between;padding:3px 0;font-size:11px;border-bottom:1px solid rgba(48,54,61,0.2);cursor:pointer" onclick="closeModal('regimeDetailOverlay');showTradeDetail(${t.id})">
          <div>
            <span class="${tCls}" style="font-weight:600">${(t.outcome||'').toUpperCase()}</span>
            <span class="${tSideCls}"> ${(t.side||'').toUpperCase()}</span>
            
          </div>
          <div>
            <span class="${tCls}">${fmtPnl(tPnl)}</span>
            <span class="dim" style="margin-left:6px">${t.created_ct||''}</span>
          </div>
        </div>`;
      }
      html += `</div>`;
    }

    $('#regimeDetailContent').innerHTML = html;
    openModal('regimeDetailOverlay');
  } catch(e) { console.error('Regime detail error:', e); showToast('Error loading regime', 'red'); }
}

// ── Per-regime filter management ─────────────────────────
let _regimeFilters = {};

// ── Quick-Trade regime selection ─────────────────────────
let _quickTradeRegimes = new Set();
let _quickTradeSavedState = null;  // {regimeFilters, riskLevelActions, regimeOverrides}

// ── Strategy Auto-Fill from regime cards ─────────────────
let _strategyAutoFill = null;  // {regime, rank, stratKey, savedStrategy}

// Global base-label helper (mirrors bot.py _base_regime_label)
function _baseRegimeLabel(label) {
  let b = label;
  for (const pfx of ['thin_', 'squeeze_']) { if (b.startsWith(pfx)) b = b.slice(pfx.length); }
  for (const sfx of ['_accel', '_decel']) { if (b.endsWith(sfx)) b = b.slice(0, -sfx.length); }
  return b;
}

function _isQuickTradeActive() { return _quickTradeRegimes.size > 0; }
function _isAutoFillActive() { return _strategyAutoFill !== null; }

function _toggleQuickTradeRegime(label) {
  if (_isTradeAllActive()) {
    showToast('Trade-all is active \u2014 disable it first', 'yellow');
    return;
  }
  if (_quickTradeRegimes.has(label)) {
    // Deselect
    _quickTradeRegimes.delete(label);
    if (_quickTradeRegimes.size === 0) {
      // Restore snapshot
      _clearQuickTrade(true);
      return;
    }
  } else {
    // First selection — snapshot current state
    if (_quickTradeRegimes.size === 0) {
      _quickTradeSavedState = {
        regimeFilters: JSON.parse(JSON.stringify(_regimeFilters)),
        riskLevelActions: JSON.parse(JSON.stringify(_riskLevelActions)),
        regimeOverrides: JSON.parse(JSON.stringify(_regimeOverrides)),
      };
    }
    _quickTradeRegimes.add(label);
  }
  _applyQuickTradeToConfig();
  _renderQuickTradeBanner();
  _refreshRegimePreviewSelection();
  showToast(`Quick-trade: ${_quickTradeRegimes.size} regime${_quickTradeRegimes.size>1?'s':''}`, 'blue');
}

function _applyQuickTradeToConfig() {
  // Exclusive whitelist: reset ALL overrides, then set only selected to 'normal'
  const qtList = [..._quickTradeRegimes];
  // Clear all existing overrides — everything goes to default (skip by bot logic)
  _regimeOverrides = {};
  // Set only selected regimes to trade
  for (const label of qtList) {
    _regimeOverrides[label] = 'normal';
    delete _regimeFilters[label];
  }
  // Atomic save: filters + overrides + quick_trade_regimes list
  api('/api/config', {
    method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':_getCsrfToken()},
    body:JSON.stringify({
      regime_filters: _regimeFilters,
      regime_overrides: _regimeOverrides,
      quick_trade_regimes: qtList,
      quick_trade_saved_state: _quickTradeSavedState,
    })
  });
  // Update risk action buttons visual (add lock indicator)
  _updateRiskActionLock();
  // Refresh regime list on Regimes tab to show updated overrides
  loadRegimes(true);
}

function _clearQuickTrade(silent) {
  if (_quickTradeSavedState) {
    _regimeFilters = _quickTradeSavedState.regimeFilters || {};
    _riskLevelActions = _quickTradeSavedState.riskLevelActions || {};
    _regimeOverrides = _quickTradeSavedState.regimeOverrides || {};
    _loadRiskActionButtons(_riskLevelActions);
  }
  _quickTradeRegimes.clear();
  _quickTradeSavedState = null;
  // Save restored state
  api('/api/config', {
    method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':_getCsrfToken()},
    body:JSON.stringify({
      regime_filters: _regimeFilters,
      regime_overrides: _regimeOverrides,
      risk_level_actions: _riskLevelActions,
      quick_trade_regimes: [],
      quick_trade_saved_state: null,
    })
  });
  _renderQuickTradeBanner();
  _refreshRegimePreviewSelection();
  _updateRiskActionLock();
  loadRegimes(true);
  if (!silent) showToast('Quick-trade cleared \u2014 settings restored', 'blue');
}

function _renderQuickTradeBanner() {
  const container = document.getElementById('regimePreviewCard');
  if (!container) return;
  let banner = document.getElementById('qtBanner');
  if (_quickTradeRegimes.size === 0) {
    if (banner) banner.remove();
    return;
  }
  const labels = [..._quickTradeRegimes].map(l => l.replace(/_/g,' ')).join(', ');
  const text = `Quick-Trade: ${_quickTradeRegimes.size} regime${_quickTradeRegimes.size>1?'s':''} active`;
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'qtBanner';
    banner.className = 'override-banner override-banner-qt';
    container.insertBefore(banner, container.firstChild);
  }
  const _qtSvg = '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" style="vertical-align:-2px;margin-right:3px"><path d="M13 2L3 14h8l-1 8 10-12h-8l1-8z"/></svg>';
  banner.innerHTML = `<div><div style="font-weight:600">${_qtSvg}${text}</div><div style="font-size:10px;color:var(--dim);margin-top:2px">${labels}</div></div><button class="ob-clear" onclick="event.stopPropagation();_clearQuickTrade()">\u2715 Clear</button>`;
}

function _refreshRegimePreviewSelection() {
  // Update qt-selected class on existing regime preview rows
  document.querySelectorAll('.qt-row').forEach(row => {
    const label = row.dataset.regime;
    row.classList.toggle('qt-selected', _quickTradeRegimes.has(label));
  });
}

function _applyQuickTradeLockToRegimeList() {
  const el = document.getElementById('regimeList');
  if (!el) return;
  const existing = document.getElementById('regimeListQtBanner');
  const locked = _isQuickTradeActive() || _isTradeAllActive();
  if (locked) {
    el.querySelectorAll('.regime-action-sel').forEach(sel => {
      sel.disabled = true;
      sel.style.opacity = '0.4';
    });
    if (!existing) {
      const banner = document.createElement('div');
      banner.id = 'regimeListQtBanner';
      banner.style.cssText = 'padding:8px 10px;margin-bottom:8px;border-radius:6px;background:rgba(88,166,255,0.08);border:1px solid rgba(88,166,255,0.2);display:flex;align-items:center;gap:8px;font-size:11px;color:var(--blue)';
      const msg = _isTradeAllActive() ? 'Trade-all active \u2014 all regime overrides and filters bypassed.' : 'Quick-trade active \u2014 regime overrides and filters locked. Manage from Settings tab.';
      banner.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" style="flex-shrink:0"><path d="M13 2L3 14h8l-1 8 10-12h-8l1-8z"/></svg><span>${msg}</span>`;
      el.parentElement.insertBefore(banner, el);
    }
  } else {
    el.querySelectorAll('.regime-action-sel').forEach(sel => {
      sel.disabled = false;
      sel.style.opacity = '';
    });
    if (existing) existing.remove();
  }
}

function _updateRiskActionLock() {
  const grid = document.getElementById('riskActionGrid');
  if (!grid) return;
  const lockEl = document.getElementById('riskActionLock');
  const locked = _isQuickTradeActive() || _isTradeAllActive();
  if (locked) {
    grid.style.opacity = '0.3';
    grid.style.pointerEvents = 'none';
    if (!lockEl) {
      const div = document.createElement('div');
      div.id = 'riskActionLock';
      div.style.cssText = 'font-size:11px;color:var(--blue);padding:8px 10px;margin-top:6px;background:rgba(88,166,255,0.08);border:1px solid rgba(88,166,255,0.2);border-radius:6px;display:flex;align-items:center;gap:6px';
      const reason = _isTradeAllActive() ? 'Trade-all is active \u2014 all risk levels set to Trade' : 'Quick-trade is active \u2014 controlling regime selection';
      div.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" style="flex-shrink:0"><path d="M13 2L3 14h8l-1 8 10-12h-8l1-8z"/></svg><span>${reason}</span>`;
      grid.parentElement.insertBefore(div, grid.nextSibling);
    }
  } else {
    grid.style.opacity = '';
    grid.style.pointerEvents = '';
    if (lockEl) lockEl.remove();
  }
}

// ── Strategy Auto-Fill ──────────────────────────────────
function _applyStrategyAutoFill(regimeLabel, rank, stratKey) {
  // If tapping same one, clear it
  if (_strategyAutoFill && _strategyAutoFill.stratKey === stratKey && _strategyAutoFill.regime === regimeLabel) {
    _clearStrategyAutoFill();
    return;
  }
  // Block if auto-strategy mode is active (hybrid or auto)
  const _afMode = (_uiState && _uiState.trading_mode) || 'observe';
  if (_afMode === 'hybrid' || _afMode === 'auto') {
    showToast('Auto-strategy mode is active — it controls strategy selection', 'yellow');
    return;
  }
  // If quick-trade is active, clear it first since strategy is changing
  if (_isQuickTradeActive()) {
    _clearQuickTrade(true);
    showToast('Quick-trade cleared \u2014 strategy changed', 'yellow');
  }
  // Snapshot current strategy
  const cfg = {
    strategy_side: $('#strategySide').value,
    entry_delay_minutes: {early:0, mid:5, late:10}[$('#strategyTiming').value] || 0,
    entry_price_max_c: parseInt($('#strategyEntry').value) || 45,
    sell_target_c: $('#strategySell').value === 'hold' ? 0 : (parseInt($('#strategySell').value) || 0),
  };
  // Parse strategy key: side:timing:entry:sell
  const parts = stratKey.split(':');
  let side, timing, entryMax, sellTarget;
  if (parts.length === 4) {
    [side, timing, entryMax, sellTarget] = parts;
  } else if (parts.length === 3) {
    side = 'cheaper'; [timing, entryMax, sellTarget] = parts;
  } else return;
  _strategyAutoFill = {
    regime: regimeLabel, rank: rank, stratKey: stratKey,
    savedStrategy: cfg,
  };
  // Apply to pickers
  $('#strategySide').value = side;
  $('#strategyTiming').value = timing;
  // Rebuild entry/sell options via _applyStrategyPicker internals but skip the save-trigger conflict
  _applyStrategyPickerNoConflict();
  // Now set the values
  $('#strategyEntry').value = entryMax;
  const sellStr = sellTarget === 'hold' ? 'hold' : sellTarget;
  // Rebuild sell options for the new entry
  const sells = _buildSellOptions(parseInt(entryMax));
  const sellEl = $('#strategySell');
  sellEl.innerHTML = sells.map(s => `<option value="${s.v}" ${String(s.v)===sellStr?'selected':''}>${s.l}</option>`).join('');
  sellEl.value = sellStr;
  // Save to bot config
  const timingMap = {early:0, mid:5, late:10};
  saveSetting('strategy_side', side);
  saveSetting('entry_delay_minutes', timingMap[timing] || 0);
  saveSetting('entry_price_max_c', parseInt(entryMax));
  saveSetting('sell_target_c', sellTarget === 'hold' ? 0 : parseInt(sellTarget));
  // Save auto-fill state so it survives reload
  api('/api/config', {
    method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':_getCsrfToken()},
    body:JSON.stringify({strategy_autofill: _strategyAutoFill})
  });
  // Update display
  const sideLabel = side === 'cheaper' ? '' : side.toUpperCase() + ':';
  $('#strategyKeyDisplay').textContent = 'Strategy: ' + sideLabel + timing + ':' + entryMax + ':' + sellTarget;
  _renderAutoFillBanner();
  _refreshAutoFillVisuals();
  _lockStrategyPickers(true);
  // Refresh regime preview for new strategy
  _fetchRegimePreview(stratKey);
  // Close regime detail modal if open
  const rdOverlay = document.getElementById('regimeDetailOverlay');
  if (rdOverlay && rdOverlay.style.display !== 'none') closeModal('regimeDetailOverlay');
  const sourceLabel = regimeLabel === 'global' ? 'Global Best' : regimeLabel.replace(/_/g,' ');
  showToast(`Strategy from ${sourceLabel} #${rank} applied`, 'purple');
}

function _clearStrategyAutoFill(silent) {
  if (!_strategyAutoFill) return;
  const saved = _strategyAutoFill.savedStrategy;
  _strategyAutoFill = null;
  // Restore saved strategy
  if (saved) {
    const timingRev = {0:'early', 5:'mid', 10:'late'};
    $('#strategySide').value = saved.strategy_side || 'cheaper';
    $('#strategyTiming').value = timingRev[saved.entry_delay_minutes] || 'early';
    _applyStrategyPickerNoConflict();
    $('#strategyEntry').value = saved.entry_price_max_c || 45;
    const sellStr = saved.sell_target_c > 0 ? String(saved.sell_target_c) : 'hold';
    const sells = _buildSellOptions(parseInt($('#strategyEntry').value));
    const sellEl = $('#strategySell');
    sellEl.innerHTML = sells.map(s => `<option value="${s.v}" ${String(s.v)===sellStr?'selected':''}>${s.l}</option>`).join('');
    sellEl.value = sellStr;
    // Save restored values
    saveSetting('strategy_side', saved.strategy_side);
    saveSetting('entry_delay_minutes', saved.entry_delay_minutes);
    saveSetting('entry_price_max_c', saved.entry_price_max_c);
    saveSetting('sell_target_c', saved.sell_target_c);
    const sideLabel = saved.strategy_side === 'cheaper' ? '' : saved.strategy_side.toUpperCase() + ':';
    const tLabel = timingRev[saved.entry_delay_minutes] || 'early';
    const sLabel = saved.sell_target_c > 0 ? String(saved.sell_target_c) : 'hold';
    $('#strategyKeyDisplay').textContent = 'Strategy: ' + sideLabel + tLabel + ':' + (saved.entry_price_max_c||45) + ':' + sLabel;
  }
  // Clear from config
  api('/api/config', {
    method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':_getCsrfToken()},
    body:JSON.stringify({strategy_autofill: null})
  });
  _renderAutoFillBanner();
  _refreshAutoFillVisuals();
  _lockStrategyPickers(false);
  _updateAutoStrategyLock();
  // Rebuild regime preview for restored strategy
  const obsKey = `${$('#strategySide').value}:${$('#strategyTiming').value}:${$('#strategyEntry').value}:${$('#strategySell').value}`;
  _fetchRegimePreview(obsKey);
  if (!silent) showToast('Strategy restored to previous settings', 'purple');
}

function _renderAutoFillBanner() {
  const container = document.getElementById('regimePreviewCard');
  if (!container) return;
  let banner = document.getElementById('afBanner');
  if (!_strategyAutoFill) {
    if (banner) banner.remove();
    return;
  }
  const af = _strategyAutoFill;
  const label = af.regime.replace(/_/g, ' ');
  const stratLabel = typeof _fmtStratKey === 'function' ? _fmtStratKey(af.stratKey) : af.stratKey;
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'afBanner';
    banner.className = 'override-banner override-banner-af';
    container.insertBefore(banner, container.firstChild);
  }
  const _afSvg = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px;margin-right:3px"><path d="M9 3h6M10 3v7.53l-4.83 7.25A1 1 0 006 19h12a1 1 0 00.83-1.22L14 10.53V3"/></svg>';
  const sourceLabel = af.regime === 'global' ? 'Global Best' : label;
  banner.innerHTML = `<div><div style="font-weight:600">${_afSvg}Strategy from ${sourceLabel}${af.regime !== 'global' ? ' #'+af.rank : ''}</div><div style="font-size:10px;color:var(--dim);margin-top:2px">${stratLabel}</div></div><button class="ob-clear" onclick="event.stopPropagation();_clearStrategyAutoFill()">\u2715 Restore</button>`;
}

function _refreshAutoFillVisuals() {
  // Update all best-strat-row elements to reflect current auto-fill state
  document.querySelectorAll('.best-strat-row').forEach(row => {
    const key = row.dataset.stratKey;
    const regime = row.dataset.stratRegime;
    const isActive = _strategyAutoFill && _strategyAutoFill.stratKey === key && _strategyAutoFill.regime === regime;
    row.classList.toggle('af-active', isActive);
    const label = row.querySelector('.af-status-label');
    if (label) {
      label.textContent = isActive ? 'ACTIVE' : 'tap to apply';
      label.style.color = isActive ? '#a371f7' : 'var(--dim)';
    }
  });
}

function _lockStrategyPickers(lock) {
  const grid = document.querySelector('#regimePreviewCard')?.previousElementSibling?.previousElementSibling;
  // The strategy picker grid is the 4-column grid right above the regime preview card
  // We'll target it via a wrapper id instead
  const wrapper = document.getElementById('strategyPickerGrid');
  if (wrapper) {
    if (lock) wrapper.classList.add('af-locked');
    else wrapper.classList.remove('af-locked');
  }
}

// Legacy — auto-strategy toggle is now controlled by trading mode selector
function _onAutoStrategyToggle(enabled) {
  _updateAutoStrategyLock();
}

// Trade-all saved state (same pattern as quick-trade)
let _tradeAllSavedState = null;
function _isTradeAllActive() { return document.getElementById('autoStratTradeAll')?.checked || false; }

function _onAutoStratTradeAllToggle(enabled) {
  if (enabled) {
    // Require auto-strategy mode (hybrid or auto)
    const mode = (_uiState && _uiState.trading_mode) || 'observe';
    if (mode !== 'hybrid' && mode !== 'auto') {
      showToast('Switch to Hybrid or Auto mode first', 'yellow');
      document.getElementById('autoStratTradeAll').checked = false;
      return;
    }
    // Clear quick-trade if active — trade-all supersedes it
    if (_isQuickTradeActive()) {
      _clearQuickTrade(true);
    }
    // Snapshot current state
    _tradeAllSavedState = {
      regimeFilters: JSON.parse(JSON.stringify(_regimeFilters)),
      riskLevelActions: JSON.parse(JSON.stringify(_riskLevelActions)),
      regimeOverrides: JSON.parse(JSON.stringify(_regimeOverrides)),
    };
    // Set everything to trade
    _riskLevelActions = {low:'normal', moderate:'normal', high:'normal', terrible:'normal', unknown:'normal'};
    _regimeOverrides = {};
    _regimeFilters = {};
    _loadRiskActionButtons(_riskLevelActions);
    // Save atomically
    api('/api/config', {
      method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':_getCsrfToken()},
      body:JSON.stringify({
        risk_level_actions: _riskLevelActions,
        regime_overrides: _regimeOverrides,
        regime_filters: _regimeFilters,
        auto_strat_trade_all: true,
        auto_strat_trade_all_saved: _tradeAllSavedState,
        quick_trade_regimes: [],
        quick_trade_saved_state: null,
      })
    });
    showToast('Trade-all active \u2014 all regime filters bypassed', 'blue');
  } else {
    // Restore snapshot
    if (_tradeAllSavedState) {
      _regimeFilters = _tradeAllSavedState.regimeFilters || {};
      _riskLevelActions = _tradeAllSavedState.riskLevelActions || {};
      _regimeOverrides = _tradeAllSavedState.regimeOverrides || {};
      _loadRiskActionButtons(_riskLevelActions);
    }
    _tradeAllSavedState = null;
    api('/api/config', {
      method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':_getCsrfToken()},
      body:JSON.stringify({
        risk_level_actions: _riskLevelActions,
        regime_overrides: _regimeOverrides,
        regime_filters: _regimeFilters,
        auto_strat_trade_all: false,
        auto_strat_trade_all_saved: null,
      })
    });
    showToast('Trade-all off \u2014 filters restored', 'blue');
  }
  _updateTradeAllVisuals();
  _updateRiskActionLock();
  loadRegimes(true);
}

function _updateTradeAllVisuals() {
  const on = _isTradeAllActive();
  const banner = document.getElementById('autoStratTradeAllBanner');
  if (banner) banner.style.display = on ? '' : 'none';
}

function _updateAutoStrategyLock() {
  const mode = (_uiState && _uiState.trading_mode) || 'observe';
  const autoStratActive = (mode === 'hybrid' || mode === 'auto');
  const wrapper = document.getElementById('strategyPickerGrid');
  const banner = document.getElementById('autoStratBanner');
  if (autoStratActive && !_isAutoFillActive()) {
    // Lock strategy pickers — auto-strategy overrides them
    if (wrapper) {
      wrapper.style.opacity = '0.3';
      wrapper.style.pointerEvents = 'none';
    }
    if (!banner) {
      const container = document.getElementById('regimePreviewCard');
      if (container) {
        const div = document.createElement('div');
        div.id = 'autoStratBanner';
        div.className = 'override-banner override-banner-qt';
        const modeLabel = mode === 'hybrid' ? 'Hybrid' : 'Auto';
        div.innerHTML = `<div><div style="font-weight:600">${modeLabel} Mode Active</div><div style="font-size:10px;color:var(--dim);margin-top:2px">Side, timing, entry, and sell are set per-regime by the Observatory. Manual values ignored.</div></div>`;
        container.insertBefore(div, container.firstChild);
      }
    }
  } else {
    if (wrapper && !_isAutoFillActive()) {
      wrapper.style.opacity = '';
      wrapper.style.pointerEvents = '';
    }
    if (banner) banner.remove();
  }
}

async function _syncGlobalAutoFill() {
  // When auto-fill is sourced from global best, keep it synced
  if (!_strategyAutoFill || _strategyAutoFill.regime !== 'global') return;
  try {
    const neData = await api('/api/net_edge');
    if (!neData || neData.error) return;
    const best = neData.best_fdr || neData.best_overall;
    if (!best || !best.strategy_key) return;
    if (best.strategy_key === _strategyAutoFill.stratKey) return;
    // Global best changed — update pickers silently
    const oldKey = _strategyAutoFill.stratKey;
    const parts = best.strategy_key.split(':');
    let side, timing, entryMax, sellTarget;
    if (parts.length === 4) [side, timing, entryMax, sellTarget] = parts;
    else if (parts.length === 3) { side = 'cheaper'; [timing, entryMax, sellTarget] = parts; }
    else return;
    _strategyAutoFill.stratKey = best.strategy_key;
    _strategyAutoFill.rank = 1;
    // Apply to pickers
    $('#strategySide').value = side;
    $('#strategyTiming').value = timing;
    _applyStrategyPickerNoConflict();
    $('#strategyEntry').value = entryMax;
    const sellStr = sellTarget === 'hold' ? 'hold' : sellTarget;
    const sells = _buildSellOptions(parseInt(entryMax));
    const sellEl = $('#strategySell');
    sellEl.innerHTML = sells.map(s => `<option value="${s.v}" ${String(s.v)===sellStr?'selected':''}>${s.l}</option>`).join('');
    sellEl.value = sellStr;
    // Save to bot
    const timingMap = {early:0, mid:5, late:10};
    saveSetting('strategy_side', side);
    saveSetting('entry_delay_minutes', timingMap[timing] || 0);
    saveSetting('entry_price_max_c', parseInt(entryMax));
    saveSetting('sell_target_c', sellTarget === 'hold' ? 0 : parseInt(sellTarget));
    // Update display
    const sideLabel = side === 'cheaper' ? '' : side.toUpperCase() + ':';
    $('#strategyKeyDisplay').textContent = 'Strategy: ' + sideLabel + timing + ':' + entryMax + ':' + sellTarget;
    // Save updated auto-fill state
    api('/api/config', {
      method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':_getCsrfToken()},
      body:JSON.stringify({strategy_autofill: _strategyAutoFill})
    });
    _renderAutoFillBanner();
    _refreshAutoFillVisuals();
    _fetchRegimePreview(best.strategy_key);
    const oldFmt = typeof _fmtStratKey === 'function' ? _fmtStratKey(oldKey) : oldKey;
    const newFmt = typeof _fmtStratKey === 'function' ? _fmtStratKey(best.strategy_key) : best.strategy_key;
    showToast(`Global best updated: ${newFmt}`, 'purple');
  } catch(e) { /* silent */ }
}

function _applyStrategyPickerNoConflict() {
  const side = $('#strategySide').value || 'cheaper';
  const entries = _buildEntryOptions(side);
  const entryEl = $('#strategyEntry');
  const curEntry = parseInt(entryEl.value) || 0;
  entryEl.innerHTML = entries.map(e => `<option value="${e.v}" ${e.v===curEntry?'selected':''}>${e.l}</option>`).join('');
  const entryLabelEl = document.getElementById('entryLabel');
  if (entryLabelEl) {
    if (side === 'cheaper') entryLabelEl.textContent = 'Buy ≤ (≤50)';
    else entryLabelEl.textContent = 'Buy ≤';
  }
  const merEl = document.getElementById('modelEdgeRow');
  if (merEl) merEl.style.display = side === 'model' ? '' : 'none';
  const mswEl = document.getElementById('modelSideWarning');
  if (mswEl) mswEl.style.display = side === 'model' ? '' : 'none';
}


function _saveRegimeFilter(label, key, value) {
  // Block all filter changes when quick-trade or trade-all is active
  if (_isQuickTradeActive() || _isTradeAllActive()) {
    showToast('Filters locked \u2014 clear quick-trade or trade-all first', 'yellow');
    return;
  }
  if (!_regimeFilters[label]) _regimeFilters[label] = {};
  if (value === 0 || value === null || (Array.isArray(value) && !value.length)) {
    delete _regimeFilters[label][key];
    if (Object.keys(_regimeFilters[label]).length === 0) delete _regimeFilters[label];
  } else {
    _regimeFilters[label][key] = value;
  }
  api('/api/config', {
    method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':_getCsrfToken()},
    body:JSON.stringify({regime_filters: _regimeFilters})
  });
  _updateRegimeCardBadge(label);
}

// Style helpers for filter cells
function _hourCellStyle(blocked, n, wins) {
  if (blocked) return 'background:rgba(248,81,73,0.15);border:2px solid rgba(248,81,73,0.5);opacity:0.5;text-decoration:line-through;';
  if (n === 0) return 'background:var(--bg);border:1px solid transparent;opacity:1;';
  const wr = wins / n;
  if (wr >= 0.55) return 'background:rgba(63,185,80,0.1);border:1px solid rgba(63,185,80,0.2);opacity:1;';
  if (wr < 0.45 && n >= 3) return 'background:rgba(248,81,73,0.1);border:1px solid rgba(248,81,73,0.2);opacity:1;';
  return 'background:var(--bg);border:1px solid transparent;opacity:1;';
}
function _dayCellStyle(blocked, n, wins) { return _hourCellStyle(blocked, n, wins); }

function _toggleRegimeHour(label, hour, el) {
  if (_isQuickTradeActive() || _isTradeAllActive()) {
    showToast('Filters locked \u2014 clear quick-trade or trade-all first', 'yellow'); return;
  }
  if (!_regimeFilters[label]) _regimeFilters[label] = {};
  const blocked = _regimeFilters[label].blocked_hours || [];
  const idx = blocked.indexOf(hour);
  const nowBlocked = idx < 0;  // toggling TO blocked
  if (idx >= 0) blocked.splice(idx, 1); else blocked.push(hour);
  blocked.sort((a,b) => a-b);

  // Instant visual update
  if (el) {
    el.dataset.blocked = nowBlocked ? '1' : '0';
    const n = parseInt(el.dataset.n) || 0;
    const w = parseInt(el.dataset.wins) || 0;
    const s = _hourCellStyle(nowBlocked, n, w);
    el.style.cssText = 'text-align:center;padding:4px 2px;border-radius:4px;cursor:pointer;font-size:10px;' + s;
  }

  _saveRegimeFilter(label, 'blocked_hours', blocked);
}

function _toggleRegimeDay(label, day, el) {
  if (_isQuickTradeActive() || _isTradeAllActive()) {
    showToast('Filters locked \u2014 clear quick-trade or trade-all first', 'yellow'); return;
  }
  if (!_regimeFilters[label]) _regimeFilters[label] = {};
  const blocked = _regimeFilters[label].blocked_days || [];
  const idx = blocked.indexOf(day);
  const nowBlocked = idx < 0;
  if (idx >= 0) blocked.splice(idx, 1); else blocked.push(day);
  blocked.sort((a,b) => a-b);

  // Instant visual update
  if (el) {
    el.dataset.blocked = nowBlocked ? '1' : '0';
    const n = parseInt(el.dataset.n) || 0;
    const w = parseInt(el.dataset.wins) || 0;
    const s = _dayCellStyle(nowBlocked, n, w);
    el.style.cssText = 'text-align:center;padding:6px 2px;border-radius:4px;cursor:pointer;font-size:10px;' + s;
  }

  _saveRegimeFilter(label, 'blocked_days', blocked);
}

function _toggleRegimeSide(label, side, el) {
  if (_isQuickTradeActive() || _isTradeAllActive()) {
    showToast('Filters locked \u2014 clear quick-trade or trade-all first', 'yellow'); return;
  }
  if (!_regimeFilters[label]) _regimeFilters[label] = {};
  const blocked = _regimeFilters[label].blocked_sides || [];
  const idx = blocked.indexOf(side);
  const nowBlocked = idx < 0;
  if (idx >= 0) blocked.splice(idx, 1); else blocked.push(side);

  // Instant visual update
  if (el) {
    el.dataset.blocked = nowBlocked ? '1' : '0';
    el.style.opacity = nowBlocked ? '0.5' : '1';
    el.style.textDecoration = nowBlocked ? 'line-through' : '';
    el.style.background = nowBlocked ? 'rgba(248,81,73,0.15)' : 'var(--bg)';
    el.style.border = nowBlocked ? '2px solid rgba(248,81,73,0.5)' : '1px solid transparent';
  }

  _saveRegimeFilter(label, 'blocked_sides', blocked);
}

function _updateRegimeCardBadge(label) {
  // Update the filter badge on the regime card in the list without full reload
  const rf = _regimeFilters[label] || {};
  const count = (rf.blocked_hours ? rf.blocked_hours.length : 0)
    + (rf.blocked_days ? rf.blocked_days.length : 0)
    + ((rf.vol_min > 1 || rf.vol_max < 5) ? 1 : 0)
    + (rf.stability_max > 0 ? 1 : 0)
    + (rf.blocked_sides ? rf.blocked_sides.length : 0)
    
    + (rf.max_spread_c > 0 ? 1 : 0);
  // Find the card by looking for the regime name in the list
  const cards = document.querySelectorAll('#regimeList > div');
  for (const card of cards) {
    if (card.textContent.includes(label.replace(/_/g, ' '))) {
      const existing = card.querySelector('.rf-badge');
      if (existing) existing.remove();
      if (count > 0) {
        const nameEl = card.querySelector('span[style*="font-weight:600"]');
        if (nameEl) {
          const badge = document.createElement('span');
          badge.className = 'rf-badge';
          badge.style.cssText = 'font-size:9px;background:var(--blue);color:#fff;padding:1px 5px;border-radius:8px;margin-left:4px';
          badge.textContent = count + ' filter' + (count > 1 ? 's' : '');
          nameEl.parentElement.appendChild(badge);
        }
      }
      break;
    }
  }
}

// ── BTC Chart ────────────────────────────────────────────
let _btcChartRange = 60;
let _btcChartData = [];

async function loadBtcChart(minutes, btn) {
  if (minutes) _btcChartRange = minutes;
  if (btn) {
    btn.closest('.filter-chips').querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
    btn.classList.add('active');
  }
  try {
    const data = await api(`/api/btc_chart?minutes=${_btcChartRange}`);
    if (!data || !data.length) return;
    _btcChartData = data;

    // Update price header
    const latest = data[data.length - 1];
    const first = data[0];
    const price = latest.close;
    const change = price - first.close;
    const changePct = (change / first.close * 100);
    const cls = change >= 0 ? 'pos' : 'neg';
    const sign = change >= 0 ? '+' : '';

    $('#btcPriceMain').textContent = '$' + Math.round(price).toLocaleString();
    $('#btcPriceMain').className = cls;

    drawBtcChart();
  } catch(e) { console.error('BTC chart error:', e); }
}

function drawBtcChart() {
  const data = _btcChartData;
  if (!data || data.length < 2) return;

  const canvas = document.getElementById('btcChart');
  if (!canvas || canvas.offsetWidth === 0) return;

  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.offsetWidth;
  const H = 160;
  canvas.width = W * dpr;
  canvas.height = H * dpr;
  canvas.style.height = H + 'px';
  ctx.scale(dpr, dpr);

  const pad = {t: 10, b: 16, l: 4, r: 4};
  const closes = data.map(d => d.close);
  const yMin = Math.min(...closes) * 0.9999;
  const yMax = Math.max(...closes) * 1.0001;
  const range = yMax - yMin || 1;

  const toX = (i) => pad.l + (i / (closes.length - 1)) * (W - pad.l - pad.r);
  const toY = (v) => pad.t + (1 - (v - yMin) / range) * (H - pad.t - pad.b);

  ctx.clearRect(0, 0, W, H);

  // Grid lines
  ctx.strokeStyle = 'rgba(48,54,61,0.4)';
  ctx.lineWidth = 0.5;
  for (let g = 0; g < 4; g++) {
    const gy = pad.t + (g / 3) * (H - pad.t - pad.b);
    ctx.beginPath(); ctx.moveTo(pad.l, gy); ctx.lineTo(W - pad.r, gy); ctx.stroke();
  }

  // Gradient fill
  const first = closes[0];
  const last = closes[closes.length - 1];
  const isUp = last >= first;
  const lineColor = isUp ? 'rgba(63,185,80,0.9)' : 'rgba(248,81,73,0.9)';
  const fillTop = isUp ? 'rgba(63,185,80,0.15)' : 'rgba(248,81,73,0.15)';

  const grad = ctx.createLinearGradient(0, pad.t, 0, H - pad.b);
  grad.addColorStop(0, fillTop);
  grad.addColorStop(1, 'rgba(0,0,0,0)');

  ctx.beginPath();
  ctx.moveTo(toX(0), toY(closes[0]));
  for (let i = 1; i < closes.length; i++) ctx.lineTo(toX(i), toY(closes[i]));
  ctx.strokeStyle = lineColor;
  ctx.lineWidth = 1.5;
  ctx.stroke();

  // Fill under
  ctx.lineTo(toX(closes.length - 1), H - pad.b);
  ctx.lineTo(toX(0), H - pad.b);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  // Current price line
  ctx.setLineDash([3, 3]);
  ctx.strokeStyle = 'rgba(255,255,255,0.15)';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(pad.l, toY(last));
  ctx.lineTo(W - pad.r, toY(last));
  ctx.stroke();
  ctx.setLineDash([]);

  // Price label
  const label = '$' + Math.round(last).toLocaleString();
  const changePct = ((last - first) / first * 100);
  const sign = changePct >= 0 ? '+' : '';
  $('#btcChartLabel').textContent = `${label} (${sign}${changePct.toFixed(2)}%)`;
  $('#btcChartLabel').style.color = isUp ? 'var(--green)' : 'var(--red)';

  // Hook into universal crosshair system
  if (canvas) {
    const startMs = data.length ? new Date(data[0].ts).getTime() : 0;
    const endMs = data.length ? new Date(data[data.length - 1].ts).getTime() : 1;
    const totalMs = endMs - startMs || 1;
    canvas._chartMap = {
      data: data.map((d, i) => ({ts: startMs + (i / Math.max(closes.length - 1, 1)) * totalMs, val: d.close})),
      pad, W, H,
      toX: (ts) => pad.l + ((ts - startMs) / totalMs) * (W - pad.l - pad.r),
      toY,
      fromX: (cssX) => startMs + ((cssX - pad.l) / (W - pad.l - pad.r)) * totalMs,
      redraw: drawBtcChart,
      formatLabel: (d) => {
        const time = new Date(d.ts).toLocaleTimeString('en-US', {hour:'numeric',minute:'2-digit'});
        return `<span style="color:var(--text)">$${Math.round(d.val).toLocaleString()}</span> · ${time}`;
      }
    };
  }
}

async function loadRegimeWorkerStatus() {
  try {
    const s = await api('/api/regime_status');
    hideSkel('skelRegimeCurrent');

    // Current regime box
    const curEl = document.getElementById('regimeCurrentContent');
    if (curEl) {
      const snap = s.latest_snapshot;
      if (snap) {
        const curBox = document.getElementById('regimeCurrentBox');
        if (curBox) curBox.style.borderLeftColor = 'var(--blue)';

        let ch = `<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
          <span style="font-size:14px;font-weight:700">${(snap.composite_label || '—').replace(/_/g,' ')}</span>
        </div>`;
        ch += `<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-bottom:6px">
          <div class="stat"><div class="label">Volatility</div><div class="val">${snap.vol_regime||'?'}/5</div></div>
          <div class="stat"><div class="label">Trend</div><div class="val">${trendLabel(snap.trend_regime)}</div></div>
          <div class="stat"><div class="label">Volume</div><div class="val">${snap.volume_regime||'?'}/5</div></div>
        </div>`;
        ch += `<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">
          <div class="stat"><div class="label">BTC Price</div><div class="val">${snap.btc_price ? '$' + Math.round(snap.btc_price).toLocaleString() : '—'}</div></div>
          <div class="stat"><div class="label">Confidence</div><div class="val">${((snap.regime_confidence||0)*100).toFixed(0)}%</div></div>
        </div>`;
        if (snap.btc_return_15m != null) {
          const r15 = snap.btc_return_15m;
          const r1h = snap.btc_return_1h;
          const r4h = snap.btc_return_4h;
          ch += `<div style="font-size:11px;color:var(--dim);margin-top:6px">
            15m: <span class="${r15>=0?'pos':'neg'}">${r15>=0?'+':''}${r15.toFixed(3)}%</span>
            ${r1h != null ? ` · 1h: <span class="${r1h>=0?'pos':'neg'}">${r1h>=0?'+':''}${r1h.toFixed(3)}%</span>` : ''}
          </div>`;
          // Also update header returns
          let retHtml = `15m: <span class="${r15>=0?'pos':'neg'}">${r15>=0?'+':''}${r15.toFixed(3)}%</span>`;
          if (r1h != null) retHtml += ` · 1h: <span class="${r1h>=0?'pos':'neg'}">${r1h>=0?'+':''}${r1h.toFixed(3)}%</span>`;
          if (r4h != null) retHtml += ` · 4h: <span class="${r4h>=0?'pos':'neg'}">${r4h>=0?'+':''}${r4h.toFixed(3)}%</span>`;
          $('#btcReturns').innerHTML = retHtml;
        }
        ch += `<div class="dim" style="font-size:10px;margin-top:4px">Updated: ${snap.captured_ct || '—'}</div>`;
        curEl.innerHTML = ch;
      } else {
        const p = s.engine_phase;
        const pLabel = {
          'backfilling': 'Backfilling candle history…',
          'computing_baselines': 'Computing baselines…',
          'updating_history': 'Fetching recent candles…',
          'first_snapshot': 'Computing first snapshot…',
        }[p] || 'Waiting for regime engine…';
        const pct = Math.min(100, s.candle_pct || 0);
        let noSnapHtml = `<div style="margin-bottom:4px">
          <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--yellow);margin-right:6px;animation:pulse 1.5s infinite"></span>
          <span style="font-size:12px;color:var(--yellow)">${pLabel}</span>
        </div>`;
        if (p === 'backfilling') {
          noSnapHtml += `<div style="margin-top:6px">
            <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--dim);margin-bottom:3px">
              <span>Progress</span><span>${pct.toFixed(1)}%</span>
            </div>
            <div style="width:100%;height:6px;background:var(--bg);border-radius:3px;overflow:hidden">
              <div style="width:${pct}%;height:100%;background:var(--blue);border-radius:3px;transition:width 0.5s ease"></div>
            </div>
          </div>`;
        }
        curEl.innerHTML = noSnapHtml;
      }
    }

    // Engine stats (compact)
    const engEl = document.getElementById('regimeEngineContent');
    if (engEl) {
      const phase = s.engine_phase || (s.snapshot_count > 0 ? 'running' : null);
      const phaseLbl = {
        'backfilling': 'Backfilling candles…',
        'computing_baselines': 'Computing baselines…',
        'updating_history': 'Updating history…',
        'first_snapshot': 'Computing first snapshot…',
        'running': 'Running',
      }[phase] || 'Not started';
      const phaseColor = phase === 'running' ? 'var(--green)' : phase ? 'var(--yellow)' : 'var(--dim)';
      const isReady = phase === 'running';
      const candlePct = Math.min(100, s.candle_pct || 0);

      let eh = `<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
        <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${phaseColor};flex-shrink:0${phase && phase !== 'running' ? ';animation:pulse 1.5s infinite' : ''}"></span>
        <span style="font-size:12px;font-weight:600;color:${phaseColor}">${phaseLbl}</span>
      </div>`;

      if (!isReady) {
        eh += `<div style="margin-bottom:8px">
          <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--dim);margin-bottom:3px">
            <span>Candle backfill</span>
            <span>${(s.candle_count||0).toLocaleString()} / ${(s.candles_expected||525600).toLocaleString()} (${candlePct.toFixed(1)}%)</span>
          </div>
          <div style="width:100%;height:6px;background:var(--bg);border-radius:3px;overflow:hidden">
            <div style="width:${candlePct}%;height:100%;background:${candlePct >= 100 ? 'var(--green)' : 'var(--blue)'};border-radius:3px;transition:width 0.5s ease"></div>
          </div>
        </div>`;
      }

      eh += `<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px">
        <div class="stat"><div class="label">Snapshots</div><div class="val">${s.snapshot_count || 0}</div></div>
        <div class="stat"><div class="label">Interval</div><div class="val">${s.avg_snapshot_interval_s ? '~' + Math.round(s.avg_snapshot_interval_s/60) + 'm' : '—'}</div></div>
        <div class="stat"><div class="label">Regimes</div><div class="val">${s.regime_labels_tracked || 0}</div></div>
      </div>`;
      eh += `<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:6px">
        <div class="stat"><div class="label">Baselines</div><div class="val">${s.baseline_count || 0}</div></div>
        <div class="stat"><div class="label">Candles</div><div class="val">${(s.candle_count||0).toLocaleString()}</div></div>
      </div>`;
      engEl.innerHTML = eh;

      // Auto-expand engine section when not ready so progress bar is visible
      if (!isReady) {
        const engSection = document.getElementById('regimeEngineSection');
        if (engSection) engSection.style.display = 'block';
      }
    }
  } catch(e) {
    console.error('Regime status error:', e);
    const engEl = document.getElementById('regimeEngineContent');
    if (engEl && engEl.innerHTML.includes('Loading')) {
      engEl.innerHTML = '<div class="dim">Engine status unavailable</div>';
    }
  }
}

// ── Security controls ──
async function _secChangePass() {
  const old = document.getElementById('secOldPass').value;
  const nw = document.getElementById('secNewPass').value;
  const conf = document.getElementById('secConfPass').value;
  if (!old || !nw) { showToast('All fields required', 'yellow'); return; }
  if (nw !== conf) { showToast('Passwords do not match', 'red'); return; }
  if (nw.length < 6) { showToast('Min 6 characters', 'red'); return; }
  try {
    const r = await api('/api/change_password', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({username:'admin', old_password:old, new_password:nw})});
    if (r.ok) { showToast('Password changed! Reloading...','green');
      document.getElementById('secOldPass').value=''; document.getElementById('secNewPass').value=''; document.getElementById('secConfPass').value='';
      setTimeout(()=>location.reload(),1500); }
    else showToast(r.error||'Failed','red');
  } catch(e) { showToast('Error: '+e.message,'red'); }
}
async function _secInvalidate() {
  if (!confirm('Invalidate all sessions?')) return;
  try { const r = await api('/api/invalidate_sessions',{method:'POST',headers:{'Content-Type':'application/json'}}); showToast(r.msg||'Done','green'); }
  catch(e) { showToast('Error: '+e.message,'red'); }
}
async function _secLogout() {
  try { await fetch('/api/logout',{method:'POST',headers:{'X-CSRF-Token':_getCsrfToken()}}); } catch(e) {}
  location.reload();
}

function toggleStatSection(id) {
  const el = document.getElementById(id);
  if (el) el.classList.toggle('collapsed');
}

async function loadLifetimeStats() {
  await _loadLifetimeStatsInner();
}
async function _loadLifetimeStatsInner() {
  try {
    const s = await api('/api/lifetime');
    const w = s.wins || 0, l = s.losses || 0, total = w + l;
    const wr = total > 0 ? (w/total*100).toFixed(1) : '0';
    const pnl = s.total_pnl || 0;
    cachedLifetimePnl = pnl;
    const pnlCls = pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : '';

    // ── Summary Cards ──
    $('#ssWinRate').textContent = wr + '%';
    $('#ssWinRate').className = 'ssc-val ' + (w > l ? 'pos' : l > w ? 'neg' : '');
    $('#ssTotalPnl').textContent = fmtPnl(pnl);
    $('#ssTotalPnl').className = 'ssc-val ' + pnlCls;
    $('#ssROI').textContent = (s.roi_pct || 0) + '%';
    $('#ssROI').className = 'ssc-val ' + ((s.roi_pct||0) > 0 ? 'pos' : (s.roi_pct||0) < 0 ? 'neg' : '');
    $('#ssProfitFactor').textContent = s.profit_factor || '—';
    $('#ssProfitFactor').className = 'ssc-val ' + ((s.profit_factor||0) > 1 ? 'pos' : 'neg');

    // ── Hub Previews ──
    const _fmt = (v) => v >= 0 ? '+$' + v.toFixed(2) : '-$' + Math.abs(v).toFixed(2);
    const pEl = document.getElementById('hubPerfPreview');
    if (pEl) {
      const streak = s.current_streak_type ? (s.current_streak_len||0) + (s.current_streak_type==='win'?'W':'L') : '';
      pEl.innerHTML = `<span class="${w>l?'pos':'neg'}">${w}W–${l}L</span> · ${streak ? streak + ' · ' : ''}<span class="${pnlCls}">${fmtPnl(pnl)}</span>`;
    }
    const cEl = document.getElementById('hubCondPreview');
    if (cEl) {
      const pb = s.price_breakdown || [];
      const bestP = pb.filter(p => (p.wins||0)+(p.losses||0) >= 3).sort((a,b) => {
        const awr = (a.wins||0)/((a.wins||0)+(a.losses||0));
        const bwr = (b.wins||0)/((b.wins||0)+(b.losses||0));
        return bwr - awr;
      })[0];
      cEl.innerHTML = bestP ? `Best price: ${bestP.price_c}¢ (${((bestP.wins||0)/((bestP.wins||0)+(bestP.losses||0))*100).toFixed(0)}% WR)` : '<span class="dim">—</span>';
    }
    const rEl = document.getElementById('hubRegimePreview');
    if (rEl) {
      const rp = s.regime_performance || [];
      rEl.innerHTML = rp.length > 0 ? `${rp.length} regimes · Best: <span class="pos">${fmtPnl(rp[0].net_pnl||0)}</span>` : '<span class="dim">—</span>';
    }

    // Shadow preview
    api('/api/shadow_stats').then(sh => {
      const sEl = document.getElementById('hubShadowPreview');
      if (sEl && sh && sh.has_data) {
        const ov = sh.overview || {};
        const wr = ((ov.wr || 0) * 100).toFixed(0);
        const wrCls = ov.wr >= 0.52 ? 'pos' : ov.wr >= 0.48 ? '' : 'neg';
        sEl.innerHTML = `<span class="${wrCls}" style="font-weight:700">${wr}%</span> WR · ${ov.n || 0} trades · <span class="${(ov.avg_pnl||0) >= 0 ? 'pos' : 'neg'}">${ov.avg_pnl >= 0 ? '+' : ''}${(ov.avg_pnl||0).toFixed(1)}¢</span>/trade`;
      }
    }).catch(() => {});

    // Cache lifetime data for sub-pages
    window._statsLifetimeCache = s;

  } catch(e) { console.error('Stats load error:', e); }
}

// ═══════════════════════════════════════════════════════════════
//  STATS HUB / SUB-PAGE NAVIGATION
// ═══════════════════════════════════════════════════════════════

let _statsCurrentPage = null;
let _statsPageLoaded = {};

function statsNavTo(page) {
  _statsCurrentPage = page;
  document.getElementById('statsHub').style.display = 'none';
  const sub = document.getElementById('statsSubPage');
  sub.style.display = '';
  const titles = {
    performance: 'Performance',
    conditions: 'Market Conditions',
    regimes: 'Regime Analysis',
    shadow: 'Shadow Trading',
  };
  document.getElementById('statsSubTitle').textContent = titles[page] || page;
  const content = document.getElementById('statsSubContent');
  content.innerHTML = '<div class="dim" style="padding:20px 0;text-align:center">Loading...</div>';
  // Show/hide CSV button
  const csvPages = ['performance','conditions','regimes'];
  document.getElementById('statsSubCsvBtn').style.display = csvPages.includes(page) ? '' : 'none';
  window._statsCurrentCsvPage = page;
  // Load the page
  _statsLoadPage(page);
  // Scroll to top
  const cw = document.getElementById('contentWrap');
  if (cw) cw.scrollTop = 0;
}

function statsGoBack() {
  _statsCurrentPage = null;
  document.getElementById('statsHub').style.display = '';
  document.getElementById('statsSubPage').style.display = 'none';
}

// ── Shared helpers ──
function _sRow(label, val, cls) {
  return `<div class="stat-row"><span class="sr-label">${label}</span><span class="sr-val ${cls||''}">${val}</span></div>`;
}
function _sFmt(v) { return v >= 0 ? '+$' + v.toFixed(2) : '-$' + Math.abs(v).toFixed(2); }
function _sTable(headers, rows) {
  return '<table class="proj-table"><thead><tr>' +
    headers.map(h => `<th${h.left ? ' style="text-align:left"' : ''}>${h.label}</th>`).join('') +
    '</tr></thead><tbody>' + rows + '</tbody></table>';
}
const _sStdHeaders = [{label:'',left:true},{label:'W'},{label:'L'},{label:'Win%'},{label:'Net'}];

function _sSection(id, title, body, collapsed) {
  return `<div class="stat-section${collapsed?' collapsed':''}" id="${id}">
    <div class="stat-section-header" onclick="toggleStatSection('${id}')">
      <h3>${title}</h3><span class="ssh-arrow">▾</span>
    </div>
    <div class="stat-section-body">${body}</div>
  </div>`;
}

// ── CSV Export ──
function _statsExportCsv() {
  const page = window._statsCurrentCsvPage;
  if (page === 'performance') { exportCSV(); return; }
  // Generic approach: export the lifetime data as CSV
  const s = window._statsLifetimeCache;
  if (!s) { showToast('No data loaded', 'yellow'); return; }
  let csv = '', fname = '';
  if (page === 'conditions') {
    // Export all breakdown tables
    csv = 'Type,Bucket,Wins,Losses,WinRate,NetPnl\n';
    (s.price_breakdown||[]).forEach(r => {
      const t = (r.wins||0)+(r.losses||0);
      csv += `EntryPrice,${r.price_c}c,${r.wins||0},${r.losses||0},${t>0?((r.wins||0)/t*100).toFixed(1):'0'}%,$${(r.net_pnl||0).toFixed(2)}\n`;
    });
    (s.spread_breakdown||[]).forEach(r => {
      const t = (r.wins||0)+(r.losses||0);
      csv += `Spread,"${r.spread_bucket}",${r.wins||0},${r.losses||0},${t>0?((r.wins||0)/t*100).toFixed(1):'0'}%,$${(r.net_pnl||0).toFixed(2)}\n`;
    });
    (s.btc_move_breakdown||[]).forEach(r => {
      const t = (r.wins||0)+(r.losses||0);
      csv += `BTCMove,"${r.btc_move_bucket}",${r.wins||0},${r.losses||0},${t>0?((r.wins||0)/t*100).toFixed(1):'0'}%,$${(r.net_pnl||0).toFixed(2)}\n`;
    });
    (s.vol_breakdown||[]).forEach(r => {
      const t = (r.wins||0)+(r.losses||0);
      csv += `Volatility,Vol${r.vol_level}/5,${r.wins||0},${r.losses||0},${t>0?((r.wins||0)/t*100).toFixed(1):'0'}%,$${(r.net_pnl||0).toFixed(2)}\n`;
    });
    (s.delay_breakdown||[]).forEach(r => {
      const t = (r.wins||0)+(r.losses||0);
      csv += `Delay,${r.delay_min}m,${r.wins||0},${r.losses||0},${t>0?((r.wins||0)/t*100).toFixed(1):'0'}%,$${(r.net_pnl||0).toFixed(2)}\n`;
    });
    (s.stability_breakdown||[]).forEach(r => {
      const t = (r.wins||0)+(r.losses||0);
      csv += `Stability,"${r.stability_bucket}",${r.wins||0},${r.losses||0},${t>0?((r.wins||0)/t*100).toFixed(1):'0'}%,$${(r.net_pnl||0).toFixed(2)}\n`;
    });
    fname = 'market_conditions_' + new Date().toISOString().split('T')[0] + '.csv';
  } else if (page === 'regimes') {
    csv = 'Regime,Trades,Wins,Losses,WinRate,NetPnl\n';
    (s.regime_performance||[]).forEach(r => {
      csv += `"${(r.regime_label||'').replace(/_/g,' ')}",${r.total},${r.wins||0},${r.losses||0},${r.win_rate||0}%,$${(r.net_pnl||0).toFixed(2)}\n`;
    });
    fname = 'regime_stats_' + new Date().toISOString().split('T')[0] + '.csv';
  }
  if (!csv) return;
  _downloadCsvBlob(csv, fname);
}

function _downloadCsvBlob(csv, fname) {
  const blob = new Blob([csv], {type: 'text/csv'});
  if (navigator.share && /mobile|iphone|android/i.test(navigator.userAgent)) {
    const file = new File([blob], fname, {type: 'text/csv'});
    navigator.share({files: [file], title: fname}).catch(e => {
      if (e.name !== 'AbortError') _dlLink(blob, fname);
    });
  } else { _dlLink(blob, fname); }
}
function _dlLink(blob, fname) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = fname; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

// ═══════════════════════════════════════════════════════════════
//  STATS SUB-PAGE LOADERS
// ═══════════════════════════════════════════════════════════════

async function _statsLoadPage(page) {
  const el = document.getElementById('statsSubContent');
  try {
    switch(page) {
      case 'performance': await _statsRenderPerformance(el); break;
      case 'conditions': await _statsRenderConditions(el); break;
      case 'regimes': await _statsRenderRegimes(el); break;
      case 'shadow': await _statsRenderShadowPage(el); break;
      default: el.innerHTML = '<div class="dim">Unknown page</div>';
    }
  } catch(e) {
    el.innerHTML = `<div class="dim" style="color:var(--red)">Error loading: ${e.message}</div>`;
    console.error('Stats page error:', e);
  }
}

// ────────────────────────────────────────────────────
//  PERFORMANCE PAGE
// ────────────────────────────────────────────────────
async function _statsRenderPerformance(el) {
  const s = window._statsLifetimeCache || await api('/api/lifetime');
  window._statsLifetimeCache = s;
  const w = s.wins||0, l = s.losses||0, total = w + l;
  const wr = total > 0 ? (w/total*100).toFixed(1) : '0';
  const pnl = s.total_pnl||0;
  let html = '';

  // Core Stats
  let core = '';
  core += '<div class="stat-category">Record</div>';
  core += _sRow('Record', `${w}W – ${l}L`);
  core += _sRow('Win Rate', wr + '%', w > l ? 'pos' : l > w ? 'neg' : '');
  core += _sRow('Trades Placed', total + (s.skips ? ` (+${s.skips} skips)` : ''));
  core += '<div class="stat-category">Streaks</div>';
  core += _sRow('Best Win Streak', s.best_win_streak||0, 'pos');
  core += _sRow('Worst Loss Streak', s.worst_loss_streak||0, 'neg');
  if (s.current_streak_type) {
    const stCls = s.current_streak_type === 'win' ? 'pos' : 'neg';
    core += _sRow('Current', (s.current_streak_len||0) + (s.current_streak_type === 'win' ? 'W' : 'L'), stCls);
  }
  core += '<div class="stat-category">Money</div>';
  core += _sRow('Total Wagered', '$' + (s.total_wagered||0).toFixed(2));
  core += _sRow('Total Fees', '$' + (s.total_fees||0).toFixed(2), 'neg');
  core += _sRow('Avg Win', _sFmt(s.avg_win_pnl||0), 'pos');
  core += _sRow('Avg Loss', _sFmt(s.avg_loss_pnl||0), 'neg');
  core += '<div class="stat-category">Extremes</div>';
  core += _sRow('Best Trade', _sFmt(s.best_trade_pnl||0), (s.best_trade_pnl||0) >= 0 ? 'pos' : 'neg');
  core += _sRow('Worst Trade', _sFmt(s.worst_trade_pnl||0), (s.worst_trade_pnl||0) >= 0 ? 'pos' : 'neg');
  core += _sRow('Peak P&L', _sFmt(s.peak_pnl||0), (s.peak_pnl||0) >= 0 ? 'pos' : 'neg');
  core += _sRow('Max Drawdown', '-$' + (s.max_drawdown||0).toFixed(2), 'neg');
  html += _sSection('secCoreStats', 'Core Stats', core, false);

  // Daily P&L
  const dp = s.daily_pnl || [];
  let dailyBody = '';
  if (dp.length > 0) {
    const greenDays = dp.filter(d => (d.pnl||0) > 0).length;
    dailyBody = `<div style="font-size:11px;color:var(--dim);margin-bottom:6px">${greenDays}/${dp.length} green days</div>`;
    dailyBody += dp.map(d => {
      const dpnl = d.pnl||0;
      return `<div class="stat-row"><span class="sr-label">${d.day} · ${d.wins||0}W/${d.losses||0}L</span><span class="sr-val ${dpnl>0?'pos':dpnl<0?'neg':''}">${fmtPnl(dpnl)}</span></div>`;
    }).join('');
  } else { dailyBody = '<div class="dim">No daily data yet</div>'; }
  html += _sSection('secDailyPnl', 'Daily P&L (14d)', dailyBody, false);

  // Hourly Performance
  const hb = s.hourly_breakdown || [];
  let hourlyBody = '';
  if (hb.length > 0) {
    const fmtHour = (h) => { const ampm = h >= 12 ? 'PM' : 'AM'; return `${h===0?12:h>12?h-12:h}${ampm}`; };
    let bestH = null, worstH = null;
    hb.forEach(h => { const ht = (h.wins||0)+(h.losses||0); if (ht<2) return; if (!bestH || h.net_pnl > bestH.net_pnl) bestH=h; if (!worstH || h.net_pnl < worstH.net_pnl) worstH=h; });
    if (bestH) hourlyBody += `<div style="margin-bottom:6px;font-size:12px"><span class="pos">Best: ${fmtHour(bestH.hour_ct)} ${fmtPnl(bestH.net_pnl)}</span>${worstH ? ' · <span class="neg">Worst: '+fmtHour(worstH.hour_ct)+' '+fmtPnl(worstH.net_pnl)+'</span>' : ''}</div>`;
    hourlyBody += _sTable(
      [{label:'Hour (CT)',left:true},{label:'W'},{label:'L'},{label:'Win%'},{label:'Net'}],
      hb.map(r => {
        const rt = (r.wins||0)+(r.losses||0); const rwr = rt>0?((r.wins||0)/rt*100).toFixed(0):'—'; const rpnl = r.net_pnl||0;
        const isBest = bestH && r.hour_ct === bestH.hour_ct;
        const isWorst = worstH && r.hour_ct === worstH.hour_ct;
        const bg = isBest ? 'background:rgba(63,185,80,0.08)' : isWorst ? 'background:rgba(248,81,73,0.08)' : '';
        return `<tr style="${bg}"><td>${fmtHour(r.hour_ct)}</td><td class="pos">${r.wins||0}</td><td class="neg">${r.losses||0}</td><td>${rwr}%</td><td class="${rpnl>=0?'pos':'neg'}">${fmtPnl(rpnl)}</td></tr>`;
      }).join('')
    );
  } else { hourlyBody = '<div class="dim">No hourly data yet</div>'; }
  html += _sSection('secHourly', 'Hourly Performance', hourlyBody, true);

  // YES vs NO
  const sb2 = s.side_breakdown || [];
  let sidesBody = '';
  if (sb2.length > 0) {
    sidesBody = sb2.map(r => {
      const rt = (r.wins||0)+(r.losses||0); const rwr = rt>0?((r.wins||0)/rt*100).toFixed(0):'—';
      const rpnl = r.net_pnl||0; const sideCls = r.side==='YES'?'side-yes':'side-no';
      return `<div style="padding:8px 0;border-bottom:1px solid var(--border)">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <span class="${sideCls}" style="font-size:16px;font-weight:700">${r.side}</span>
          <span class="${rpnl>=0?'pos':'neg'}" style="font-size:16px;font-weight:700;font-family:monospace">${fmtPnl(rpnl)}</span>
        </div>
        <div style="display:flex;gap:10px;margin-top:4px;font-size:12px;color:var(--dim)">
          <span>${r.wins||0}W–${r.losses||0}L</span><span>${rwr}%</span>
          <span>Avg W: ${r.avg_win!=null?fmtPnl(r.avg_win):'—'}</span><span>Avg L: ${r.avg_loss!=null?fmtPnl(r.avg_loss):'—'}</span>
        </div>
      </div>`;
    }).join('');
  } else { sidesBody = '<div class="dim">No side data yet</div>'; }
  html += _sSection('secSides', 'YES vs NO', sidesBody, true);

  el.innerHTML = html;
}

// ────────────────────────────────────────────────────
//  MARKET CONDITIONS PAGE
// ────────────────────────────────────────────────────
async function _statsRenderConditions(el) {
  const s = window._statsLifetimeCache || await api('/api/lifetime');
  window._statsLifetimeCache = s;
  let html = '';

  // Entry Price
  const pb = s.price_breakdown || [];
  let priceBody = '';
  if (pb.length > 0) {
    priceBody = _sTable(
      [{label:'Price',left:true},{label:'n'},{label:'Win%'},{label:'Implied'},{label:'Edge'},{label:'Net'}],
      pb.map(r => {
        const rt = (r.wins||0)+(r.losses||0); const wrN = rt>0?(r.wins||0)/rt:0;
        const wrStr = rt>0?(wrN*100).toFixed(0):'—';
        const implied = r.price_c > 0 ? (r.price_c).toFixed(0) : '—';
        const edge = rt>0 && r.price_c > 0 ? wrN*100 - r.price_c : null;
        const edgeStr = edge !== null ? (edge > 0 ? '+' : '') + edge.toFixed(0) : '—';
        const edgeCls = edge!==null?(edge>0?'pos':edge<0?'neg':'dim'):'dim';
        const rpnl = r.net_pnl||0;
        const bg = edge!==null && edge>10?'background:rgba(63,185,80,0.06)':edge!==null && edge<-10?'background:rgba(248,81,73,0.06)':'';
        return `<tr style="${bg}"><td>${r.price_c}c</td><td>${rt}</td><td>${wrStr}%</td><td class="dim">${implied}%</td><td class="${edgeCls}">${edgeStr}</td><td class="${rpnl>=0?'pos':'neg'}">${fmtPnl(rpnl)}</td></tr>`;
      }).join('')
    ) + '<div class="dim" style="font-size:10px;margin-top:4px">Edge = your win rate minus implied odds</div>';
  } else { priceBody = '<div class="dim">No data yet</div>'; }
  html += _sSection('secPrice', 'Entry Price', priceBody, false);

  // Spread
  const spb = s.spread_breakdown || [];
  let spreadBody = '';
  if (spb.length > 0 && spb.some(r => r.spread_bucket !== 'N/A')) {
    spreadBody = _sTable(_sStdHeaders.map((h,i) => i===0?{label:'Spread',left:true}:h),
      spb.filter(r => r.spread_bucket !== 'N/A').map(r => {
        const rt=(r.wins||0)+(r.losses||0); const rwr=rt>0?((r.wins||0)/rt*100).toFixed(0):'—'; const rpnl=r.net_pnl||0;
        return `<tr><td>${r.spread_bucket}</td><td class="pos">${r.wins||0}</td><td class="neg">${r.losses||0}</td><td>${rwr}%</td><td class="${rpnl>=0?'pos':'neg'}">${fmtPnl(rpnl)}</td></tr>`;
      }).join('')
    );
  } else { spreadBody = '<div class="dim">No spread data yet</div>'; }
  html += _sSection('secSpread', 'Spread at Entry', spreadBody, false);

  // Volatility
  const vb = s.vol_breakdown || [];
  let volBody = '';
  if (vb.length > 0) {
    volBody = _sTable(_sStdHeaders.map((h,i) => i===0?{label:'Vol Level',left:true}:h),
      vb.filter(r => r.vol_level > 0).map(r => {
        const rt=(r.wins||0)+(r.losses||0); const rwr=rt>0?((r.wins||0)/rt*100).toFixed(0):'—'; const rpnl=r.net_pnl||0;
        return `<tr><td>Vol ${r.vol_level}/5</td><td class="pos">${r.wins||0}</td><td class="neg">${r.losses||0}</td><td>${rwr}%</td><td class="${rpnl>=0?'pos':'neg'}">${fmtPnl(rpnl)}</td></tr>`;
      }).join('')
    );
  } else { volBody = '<div class="dim">No vol data yet</div>'; }
  html += _sSection('secVol', 'Volatility Level', volBody, true);

  // Entry Delay
  const db2 = s.delay_breakdown || [];
  let delayBody = '';
  if (db2.length > 0) {
    delayBody = _sTable(_sStdHeaders.map((h,i) => i===0?{label:'Delay',left:true}:h),
      db2.map(r => {
        const rt=(r.wins||0)+(r.losses||0); const rwr=rt>0?((r.wins||0)/rt*100).toFixed(0):'—'; const rpnl=r.net_pnl||0;
        return `<tr><td>${r.delay_min}m</td><td class="pos">${r.wins||0}</td><td class="neg">${r.losses||0}</td><td>${rwr}%</td><td class="${rpnl>=0?'pos':'neg'}">${fmtPnl(rpnl)}</td></tr>`;
      }).join('')
    );
  } else { delayBody = '<div class="dim">No delay data yet</div>'; }
  html += _sSection('secDelay', 'Entry Delay', delayBody, true);

  el.innerHTML = html;
}

// ────────────────────────────────────────────────────
//  REGIMES PAGE
// ────────────────────────────────────────────────────
async function _statsRenderRegimes(el) {
  const [s, rsData, reData] = await Promise.all([
    window._statsLifetimeCache || api('/api/lifetime'),
    api('/api/regime_stability').catch(() => null),
    api('/api/regime_effectiveness').catch(() => null),
  ]);
  window._statsLifetimeCache = s;
  let html = '';

  // Regime Leaderboard
  const rp = s.regime_performance || [];
  let regBody = '';
  if (rp.length > 0) {
    regBody = _sTable(
      [{label:'Regime',left:true},{label:'n'},{label:'Win%'},{label:'Net'}],
      rp.map((r, i) => {
        const rpnl = r.net_pnl||0;
        const label = (r.regime_label||'—').replace(/_/g, ' ');
        const medal = i===0?'🥇 ':i===1?'🥈 ':i===2?'🥉 ':'';
        const isBottom = i >= rp.length - 3 && rpnl < 0;
        const bg = i < 3 && rpnl > 0 ? 'background:rgba(63,185,80,0.06)' : isBottom ? 'background:rgba(248,81,73,0.06)' : '';
        return `<tr style="${bg}"><td style="text-align:left;max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${medal}${label}</td><td>${r.total}</td><td>${r.win_rate||0}%</td><td class="${rpnl>=0?'pos':'neg'}">${fmtPnl(rpnl)}</td></tr>`;
      }).join('')
    );
  } else { regBody = '<div class="dim">No regime data yet</div>'; }
  html += _sSection('secRegimes', `Regime Leaderboard (${rp.length})`, regBody, false);

  // Coarse Regime Performance
  const cp = s.coarse_regime_performance || [];
  let coarseBody = '';
  if (cp.length > 0) {
    coarseBody = _sTable(
      [{label:'Coarse Regime',left:true},{label:'n'},{label:'Win%'},{label:'Net'}],
      cp.map(r => {
        const rpnl = r.net_pnl||0;
        return `<tr><td style="text-align:left">${(r.coarse_regime||'—').replace(/_/g,' ')}</td><td>${r.total}</td><td>${r.win_rate||0}%</td><td class="${rpnl>=0?'pos':'neg'}">${fmtPnl(rpnl)}</td></tr>`;
      }).join('')
    );
  } else { coarseBody = '<div class="dim">No coarse regime data yet</div>'; }
  html += _sSection('secCoarseRegimes', 'Coarse Regimes', coarseBody, true);

  // Regime Stability
  let stabHtml = '';
  if (rsData && rsData.n > 0) {
    const lp = rsData.label_persistence_pct;
    const cpp = rsData.coarse_persistence_pct;
    const lpCls = lp >= 80 ? 'pos' : lp >= 60 ? '' : 'neg';
    const cpCls = cpp >= 90 ? 'pos' : cpp >= 70 ? '' : 'neg';
    stabHtml = `<div style="font-size:11px;color:var(--dim);margin-bottom:8px">How often the regime label stays the same between consecutive 5-min snapshots.</div>
      <div style="display:flex;gap:12px;margin-bottom:10px">
        <div class="stat-summary-card" style="flex:1"><div class="ssc-val ${lpCls}">${lp!=null?lp+'%':'—'}</div><div class="ssc-label">Fine-Grained</div></div>
        <div class="stat-summary-card" style="flex:1"><div class="ssc-val ${cpCls}">${cpp!=null?cpp+'%':'—'}</div><div class="ssc-label">Coarse</div></div>
      </div>
      <div style="font-size:11px;color:var(--dim)">Over ${rsData.hours}h: ${rsData.label_changes} fine changes, ${rsData.coarse_changes} coarse in ${rsData.n} snapshots</div>`;
  } else { stabHtml = '<div class="dim">Stability tracking starts after regime engine runs.</div>'; }
  html += _sSection('secRegimeStability', 'Regime Stability', stabHtml, true);

  // Regime Effectiveness
  let effHtml = '';
  if (reData && !reData.error) {
    const fg = reData.fine_grained || {};
    const co = reData.coarse || {};
    effHtml = `<div style="font-size:11px;color:var(--dim);margin-bottom:8px">Does regime classification predict outcomes? Compares fine-grained vs coarse labels.</div>`;
    effHtml += `<div style="display:flex;gap:12px;margin-bottom:10px">
      <div class="stat-summary-card" style="flex:1"><div class="ssc-val">${fg.accuracy != null ? (fg.accuracy*100).toFixed(1)+'%' : '—'}</div><div class="ssc-label">Fine Accuracy</div></div>
      <div class="stat-summary-card" style="flex:1"><div class="ssc-val">${co.accuracy != null ? (co.accuracy*100).toFixed(1)+'%' : '—'}</div><div class="ssc-label">Coarse Accuracy</div></div>
    </div>`;
    if (reData.verdict) effHtml += `<div style="font-size:11px;padding:6px 8px;background:rgba(48,54,61,0.3);border-radius:6px">${reData.verdict}</div>`;
  } else { effHtml = '<div class="dim">Need more observation data for effectiveness analysis.</div>'; }
  html += _sSection('secRegimeEff', 'Regime Effectiveness', effHtml, true);

  el.innerHTML = html;
}

// ────────────────────────────────────────────────────
//  SHADOW TRADING PAGE
// ────────────────────────────────────────────────────
var _shadowHours = 0;  // 0 = all time

async function _statsRenderShadowPage(el) {
  el.innerHTML = '<div class="dim" style="padding:20px 0;text-align:center">Loading...</div>';
  const d = await api(`/api/shadow_stats?hours=${_shadowHours}`).catch(e => ({error: e.message, has_data: false}));
  let html = '';

  if (!d.has_data) {
    html += `<div style="text-align:center;padding:40px 16px">
      <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="var(--dim)" stroke-width="1.5" style="margin-bottom:12px;opacity:0.5"><path d="M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10"/></svg>
      <div style="font-size:13px;color:var(--dim);line-height:1.5">${d.message || d.error || 'No shadow trades yet.'}</div>
      <div style="font-size:11px;color:var(--dim);margin-top:8px;opacity:0.7">Enable shadow trading in Settings → Trading to start collecting real execution data.</div>
    </div>`;
    el.innerHTML = html;
    return;
  }

  const ov = d.overview || {};
  const slip = d.slippage || {};
  const lat = d.latency || {};

  // ═══ Timeframe pills ═══
  const tfs = [{h:0,l:'All'},{h:24,l:'24h'},{h:48,l:'2d'},{h:168,l:'7d'},{h:720,l:'30d'}];
  html += '<div style="display:flex;justify-content:center;gap:6px;margin:4px 0 14px">';
  for (const tf of tfs) {
    const active = tf.h === _shadowHours;
    const ac = active ? 'var(--blue)' : 'var(--border)';
    html += `<button onclick="_shadowSetHours(${tf.h})" style="
      padding:4px 12px;border-radius:12px;font-size:11px;font-weight:${active?'700':'500'};
      border:1px solid ${ac};background:${active?'var(--blue)':'none'};
      color:${active?'#000':'var(--dim)'};cursor:pointer;-webkit-tap-highlight-color:transparent">${tf.l}</button>`;
  }
  html += '</div>';

  // ═══ Hero stats ═══
  const wrPct = ((ov.wr || 0) * 100).toFixed(1);
  const wrColor = ov.wr >= 0.52 ? 'var(--green)' : ov.wr >= 0.48 ? 'var(--yellow)' : 'var(--red)';
  const pnlColor = (ov.avg_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)';

  html += `<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:14px">
    <div style="background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px;text-align:center">
      <div style="font-size:26px;font-weight:700;font-family:monospace;color:${wrColor}">${wrPct}%</div>
      <div style="font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:0.3px;margin-top:2px">Win Rate</div>
    </div>
    <div style="background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px;text-align:center">
      <div style="font-size:26px;font-weight:700;font-family:monospace;color:${pnlColor}">${(ov.avg_pnl||0) >= 0 ? '+' : ''}${(ov.avg_pnl||0).toFixed(1)}¢</div>
      <div style="font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:0.3px;margin-top:2px">Per Trade</div>
    </div>
    <div style="background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px;text-align:center">
      <div style="font-size:26px;font-weight:700;font-family:monospace">${ov.n || 0}</div>
      <div style="font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:0.3px;margin-top:2px">Trades</div>
    </div>
  </div>`;

  // ═══ Secondary stats row ═══
  html += `<div style="display:grid;grid-template-columns:repeat(4, 1fr);gap:6px;margin-bottom:14px">
    <div style="background:var(--card);border:1px solid var(--border);border-radius:8px;padding:8px;text-align:center">
      <div style="font-size:15px;font-weight:700;font-family:monospace;color:var(--green)">${ov.wins||0}</div>
      <div style="font-size:8px;color:var(--dim);text-transform:uppercase">Wins</div>
    </div>
    <div style="background:var(--card);border:1px solid var(--border);border-radius:8px;padding:8px;text-align:center">
      <div style="font-size:15px;font-weight:700;font-family:monospace;color:var(--red)">${ov.losses||0}</div>
      <div style="font-size:8px;color:var(--dim);text-transform:uppercase">Losses</div>
    </div>
    <div style="background:var(--card);border:1px solid var(--border);border-radius:8px;padding:8px;text-align:center">
      <div style="font-size:15px;font-weight:700;font-family:monospace;color:${(ov.total_pnl||0)>=0?'var(--green)':'var(--red)'}">${(ov.total_pnl||0)>=0?'+':''}$${Math.abs(ov.total_pnl||0).toFixed(2)}</div>
      <div style="font-size:8px;color:var(--dim);text-transform:uppercase">Total P&L</div>
    </div>
    <div style="background:var(--card);border:1px solid var(--border);border-radius:8px;padding:8px;text-align:center">
      <div style="font-size:15px;font-weight:700;font-family:monospace">${((ov.fill_rate||0)*100).toFixed(0)}%</div>
      <div style="font-size:8px;color:var(--dim);text-transform:uppercase">Fill Rate</div>
    </div>
  </div>`;

  // ═══ P&L Curve (mini sparkline) ═══
  const curve = d.pnl_curve || [];
  if (curve.length >= 3) {
    const cMin = Math.min(0, ...curve);
    const cMax = Math.max(0, ...curve);
    const cRange = Math.max(cMax - cMin, 0.01);
    const w = 320, h2 = 60;
    const zeroY = h2 - ((0 - cMin) / cRange) * h2;
    let pathD = '';
    for (let i = 0; i < curve.length; i++) {
      const x = (i / (curve.length - 1)) * w;
      const y = h2 - ((curve[i] - cMin) / cRange) * h2;
      pathD += (i === 0 ? 'M' : 'L') + x.toFixed(1) + ',' + y.toFixed(1);
    }
    const lastVal = curve[curve.length - 1];
    const lineColor = lastVal >= 0 ? 'var(--green)' : 'var(--red)';
    html += `<div style="background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px;margin-bottom:14px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px">Cumulative P&L</div>
        <div style="font-size:12px;font-weight:700;font-family:monospace;color:${lineColor}">${lastVal>=0?'+':''}$${Math.abs(lastVal).toFixed(2)}</div>
      </div>
      <svg viewBox="0 0 ${w} ${h2}" style="width:100%;height:${h2}px">
        <line x1="0" y1="${zeroY.toFixed(1)}" x2="${w}" y2="${zeroY.toFixed(1)}" stroke="var(--border)" stroke-width="0.5" stroke-dasharray="3,3"/>
        <path d="${pathD}" fill="none" stroke="${lineColor}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    </div>`;
  }

  // ═══ Execution Quality (slippage + latency) ═══
  const slipColor = (slip.avg_c || 0) <= 0 ? 'var(--green)' : (slip.avg_c || 0) <= 1 ? 'var(--yellow)' : 'var(--red)';
  html += `<div style="background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px;margin-bottom:14px">
    <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:10px">Execution Quality</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
      <div>
        <div style="font-size:10px;color:var(--dim);margin-bottom:6px;font-weight:600">SLIPPAGE</div>
        <div style="font-size:20px;font-weight:700;font-family:monospace;color:${slipColor}">${(slip.avg_c||0)<=0?'':'+'}${(slip.avg_c||0).toFixed(1)}¢</div>
        <div style="font-size:9px;color:var(--dim);margin-top:4px;line-height:1.5">
          ${slip.pct_better||0}% better · ${slip.pct_equal||0}% equal · ${slip.pct_worse||0}% worse
        </div>
        <div style="font-size:9px;color:var(--dim)">Range: ${slip.min_c||0}¢ to ${slip.max_c > 0 ? '+' : ''}${slip.max_c||0}¢</div>
      </div>
      <div>
        <div style="font-size:10px;color:var(--dim);margin-bottom:6px;font-weight:600">LATENCY</div>
        <div style="font-size:20px;font-weight:700;font-family:monospace">${lat.avg_ms||0}<span style="font-size:11px;font-weight:400;color:var(--dim)">ms</span></div>
        <div style="font-size:9px;color:var(--dim);margin-top:4px;line-height:1.5">
          Median: ${lat.p50_ms||0}ms · P95: ${lat.p95_ms||0}ms
        </div>
        <div style="font-size:9px;color:var(--dim)">Range: ${lat.min_ms||0}ms – ${lat.max_ms||0}ms</div>
      </div>
    </div>
  </div>`;

  // ═══ Streaks + avg entry ═══
  html += `<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-bottom:14px">
    <div style="background:var(--card);border:1px solid var(--border);border-radius:8px;padding:8px;text-align:center">
      <div style="font-size:15px;font-weight:700;font-family:monospace;color:var(--green)">${ov.max_win_streak||0}</div>
      <div style="font-size:8px;color:var(--dim);text-transform:uppercase">Best Streak</div>
    </div>
    <div style="background:var(--card);border:1px solid var(--border);border-radius:8px;padding:8px;text-align:center">
      <div style="font-size:15px;font-weight:700;font-family:monospace;color:var(--red)">${ov.max_loss_streak||0}</div>
      <div style="font-size:8px;color:var(--dim);text-transform:uppercase">Worst Streak</div>
    </div>
    <div style="background:var(--card);border:1px solid var(--border);border-radius:8px;padding:8px;text-align:center">
      <div style="font-size:15px;font-weight:700;font-family:monospace">${ov.avg_entry_c||0}¢</div>
      <div style="font-size:8px;color:var(--dim);text-transform:uppercase">Avg Entry</div>
    </div>
  </div>`;

  // ═══ By Side ═══
  const sides = d.by_side || {};
  if (Object.keys(sides).length > 0) {
    html += `<div style="background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px;margin-bottom:14px">
      <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px">By Side</div>`;
    for (const [side, sd] of Object.entries(sides)) {
      const sWr = ((sd.wr||0)*100).toFixed(0);
      const sColor = sd.wr >= 0.52 ? 'var(--green)' : sd.wr >= 0.48 ? 'var(--yellow)' : 'var(--red)';
      const barW = Math.max(2, (sd.wr||0)*100);
      html += `<div style="margin-bottom:8px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px">
          <span style="font-size:12px;font-weight:600;text-transform:uppercase">${side}</span>
          <span style="font-size:11px;font-family:monospace"><span style="color:${sColor}">${sWr}%</span> · ${sd.n} trades · <span class="${(sd.avg_pnl||0)>=0?'pos':'neg'}">${(sd.avg_pnl||0)>=0?'+':''}${(sd.avg_pnl||0).toFixed(1)}¢</span></span>
        </div>
        <div style="height:4px;background:var(--border);border-radius:2px;overflow:hidden">
          <div style="height:100%;width:${barW}%;background:${sColor};border-radius:2px"></div>
        </div>
      </div>`;
    }
    html += '</div>';
  }

  // ═══ By Spread ═══
  const spreads = d.by_spread || {};
  if (Object.keys(spreads).length > 0) {
    html += `<div style="background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px;margin-bottom:14px">
      <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px">By Spread</div>`;
    html += _sTable([{label:'Spread',left:true},{label:'Trades'},{label:'WR'},{label:'Slip'}],
      Object.entries(spreads).map(([b,sd]) => {
        const sWr = ((sd.wr||0)*100).toFixed(0);
        const wrCls = sd.wr >= 0.52 ? 'pos' : sd.wr < 0.48 ? 'neg' : '';
        return `<tr><td style="text-align:left">${b}</td><td>${sd.n}</td>
          <td class="${wrCls}">${sWr}%</td>
          <td>${sd.avg_slip != null ? (sd.avg_slip <= 0 ? '' : '+') + sd.avg_slip.toFixed(1) + '¢' : '—'}</td></tr>`;
      }).join('')
    );
    html += '</div>';
  }

  // ═══ By Regime (top regimes) ═══
  const regimes = d.by_regime || {};
  if (Object.keys(regimes).length > 0) {
    html += `<div style="background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px;margin-bottom:14px">
      <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px">By Regime</div>`;
    html += _sTable([{label:'Regime',left:true},{label:'N'},{label:'WR'},{label:'Avg PnL'}],
      Object.entries(regimes).map(([r,rd]) => {
        const rWr = ((rd.wr||0)*100).toFixed(0);
        const wrCls = rd.wr >= 0.52 ? 'pos' : rd.wr < 0.48 ? 'neg' : '';
        const pCls = (rd.avg_pnl||0) >= 0 ? 'pos' : 'neg';
        return `<tr><td style="text-align:left;font-size:10px;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${r}</td>
          <td>${rd.n}</td><td class="${wrCls}">${rWr}%</td>
          <td class="${pCls}">${(rd.avg_pnl||0)>=0?'+':''}${(rd.avg_pnl||0).toFixed(1)}¢</td></tr>`;
      }).join('')
    );
    html += '</div>';
  }

  // ═══ By Strategy (top strategies) ═══
  const strats = d.by_strategy || {};
  if (Object.keys(strats).length > 0) {
    html += `<div style="background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px;margin-bottom:14px">
      <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px">By Strategy</div>`;
    html += _sTable([{label:'Strategy',left:true},{label:'N'},{label:'WR'},{label:'PnL'}],
      Object.entries(strats).map(([sk,sd]) => {
        const sWr = ((sd.wr||0)*100).toFixed(0);
        const wrCls = sd.wr >= 0.52 ? 'pos' : sd.wr < 0.48 ? 'neg' : '';
        const pCls = (sd.avg_pnl||0) >= 0 ? 'pos' : 'neg';
        return `<tr><td style="text-align:left;font-size:9px;max-width:110px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${sk.replace(/:/g,' · ')}</td>
          <td>${sd.n}</td><td class="${wrCls}">${sWr}%</td>
          <td class="${pCls}">${(sd.avg_pnl||0)>=0?'+':''}${(sd.avg_pnl||0).toFixed(1)}¢</td></tr>`;
      }).join('')
    );
    html += '</div>';
  }

  // ═══ Hourly heatmap ═══
  const byHour = d.by_hour || {};
  if (Object.keys(byHour).length >= 4) {
    html += `<div style="background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px;margin-bottom:14px">
      <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px">Win Rate by Hour (ET)</div>
      <div style="display:grid;grid-template-columns:repeat(12,1fr);gap:2px">`;
    for (let hr = 0; hr < 24; hr++) {
      const hd = byHour[hr] || byHour[String(hr)];
      if (!hd || !hd.n) {
        html += `<div style="text-align:center;padding:4px 0;font-size:8px;color:var(--dim);opacity:0.3">${hr}</div>`;
        continue;
      }
      const wr = hd.wr || 0;
      const bg = wr >= 0.6 ? 'rgba(63,185,80,0.25)' : wr >= 0.5 ? 'rgba(63,185,80,0.1)' : wr >= 0.4 ? 'rgba(248,81,73,0.1)' : 'rgba(248,81,73,0.25)';
      const tc = wr >= 0.5 ? 'var(--green)' : 'var(--red)';
      html += `<div style="text-align:center;padding:4px 0;background:${bg};border-radius:3px" title="Hour ${hr}: ${(wr*100).toFixed(0)}% WR (n=${hd.n})">
        <div style="font-size:7px;color:var(--dim)">${hr}</div>
        <div style="font-size:9px;font-weight:700;color:${tc}">${(wr*100).toFixed(0)}</div>
      </div>`;
    }
    html += '</div></div>';
  }

  // ═══ Recent trades ═══
  const recent = d.recent || [];
  if (recent.length > 0) {
    html += `<div style="background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px;margin-bottom:14px">
      <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px">Recent Trades</div>`;
    for (const t of recent) {
      const icon = t.outcome === 'win' ? '●' : '○';
      const ic = t.outcome === 'win' ? 'var(--green)' : 'var(--red)';
      const slipTxt = t.slip != null ? `${t.slip <= 0 ? '' : '+'}${t.slip}¢ slip` : '';
      html += `<div style="display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid var(--border);font-size:11px">
        <span style="color:${ic};font-size:8px">${icon}</span>
        <span style="font-weight:600;text-transform:uppercase;min-width:24px">${t.side||'?'}</span>
        <span style="font-family:monospace">${t.fill||'?'}¢</span>
        <span class="dim" style="font-size:9px">${slipTxt}</span>
        <span style="margin-left:auto;font-family:monospace;color:${(t.pnl||0)>=0?'var(--green)':'var(--red)'}">${(t.pnl||0)>=0?'+':''}$${Math.abs(t.pnl||0).toFixed(2)}</span>
      </div>`;
    }
    html += '</div>';
  }

  el.innerHTML = html;
}

function _shadowSetHours(h) {
  _shadowHours = h;
  const el = document.getElementById('statsSubContent');
  if (el) _statsRenderShadowPage(el);
}


// ── Simulated Trade (live market card) ──────────────────
var _simStrats = [];
// Build strategy space on init — matches Observatory simulation grid exactly
(function() {
  var times = ['early','mid','late'];
  var entryMaxes = [5,10,15,20,25,30,35,40,45,50,55,60,65,70,75,80,85,90,95];
  for (var ei = 0; ei < entryMaxes.length; ei++) {
    var eMax = entryMaxes[ei];
    // Sell targets: 5¢ steps above entry max, plus 99 and hold
    var sells = [];
    for (var s = eMax + 5; s < 100; s += 5) sells.push(s);
    sells.push(99);
    sells.push('hold');
    for (var si = 0; si < sells.length; si++)
      for (var ti = 0; ti < times.length; ti++)
        _simStrats.push({time:times[ti], maxP:eMax, sell:sells[si]});
  }
})();

var _sim = {active:false, strat:null, ticker:null, side:null, entry:0, fee:0, cost:0, sellTarget:0, shares:10};
var _simLabels = {early:'Early (ASAP)',mid:'Mid (after 5 min)',late:'Late (after 10 min)'};

function simShuffle() {
  _sim.active = false; _sim.ticker = null;
  _sim.strat = _simStrats[Math.floor(Math.random() * _simStrats.length)];
  _simEnter();
}

function _simEnter() {
  // Reset to watching state for the current market — don't buy yet
  var lm = lastStateData._liveMarket;
  if (!lm || !lm.ticker || !_sim.strat) { _simHide(); return; }

  _sim.active = true;
  _sim.ticker = lm.ticker;
  _sim.side = null;
  _sim.entry = 0;
  _sim.fee = 0;
  _sim.cost = 0;
  _sim.sellTarget = 0;
  _sim.sold = false;
  _sim.soldAt = 0;
  _sim.watching = true;  // Not yet bought — waiting for price
}

function _simTryBuy(lm) {
  // Called each tick while watching — buy when price comes into range
  if (!_sim.watching || !_sim.strat || !lm) return;
  var ya = lm.yes_ask || 0, na = lm.no_ask || 0;
  var st = _sim.strat;

  // Check timing window
  if (lm.close_time) {
    var closeMs = new Date(lm.close_time).getTime();
    var openMs = closeMs - 15 * 60 * 1000;
    var now = Date.now();
    var elapsed = (now - openMs) / 1000;
    var remaining = (closeMs - now) / 1000;
    var startAfter = st.time === 'late' ? 600 : st.time === 'mid' ? 300 : 0;
    if (elapsed < startAfter) return; // Not time yet
    if (remaining < 30) return; // Less than 30s left — matches simulation cutoff
  }

  // Find cheaper side
  var side, entry;
  if (ya <= 0 && na <= 0) return;
  if (ya <= 0) { side = 'no'; entry = na; }
  else if (na <= 0) { side = 'yes'; entry = ya; }
  else if (ya <= na) { side = 'yes'; entry = ya; }
  else { side = 'no'; entry = na; }

  if (entry <= 0 || entry > st.maxP) return; // Price not in range yet

  // Price hit! Lock in the buy
  var feeC = Math.max(1, Math.round(entry * 0.07));
  var costC = entry + feeC;
  var sellTarget;
  if (st.sell === 'hold') {
    sellTarget = 0;
  } else {
    sellTarget = parseInt(st.sell);
    if (sellTarget <= costC) sellTarget = 0; // Can't profit — treat as hold
  }

  _sim.side = side;
  _sim.entry = entry;
  _sim.fee = feeC;
  _sim.cost = costC;
  _sim.sellTarget = sellTarget;
  _sim.sold = false;
  _sim.soldAt = 0;
  _sim.watching = false;
}

function _simUpdate(lm) {
  var sec = document.getElementById('simTradeSection');
  if (!sec) return;

  // Show/hide based on whether we're in live market mode (no active trade)
  var s = lastStateData._lastState || {};
  if (s.active_trade || s.pending_trade || !lm || !lm.ticker) { _simHide(); return; }

  // Auto-init if no strategy picked yet
  if (!_sim.strat) simShuffle();

  // Market changed — reset to watching with same strategy
  if (lm.ticker !== _sim.ticker) _simEnter();

  // Try to buy if still watching
  if (_sim.watching) _simTryBuy(lm);

  if (!_sim.active) { _simHide(); return; }
  sec.style.display = '';

  // Render strategy label
  var st = _sim.strat;
  var sellLabel = st.sell === 'hold' ? 'Hold' : 'sell@' + st.sell + '¢';
  var label = (_simLabels[st.time]||st.time) + ' · ≤' + st.maxP + '¢ · ' + sellLabel;
  document.getElementById('simStratLabel').textContent = label;

  // Still watching — show what we're looking for
  if (_sim.watching) {
    var sideEl = document.getElementById('simSide');
    sideEl.textContent = 'Watching...';
    sideEl.className = 'val';
    document.getElementById('simEntry').textContent = '≤' + st.maxP + '¢';
    document.getElementById('simSell').textContent = st.sell === 'hold' ? 'Expiry' : st.sell + '¢';
    document.getElementById('simPnl').textContent = '—';
    document.getElementById('simPnl').className = 'val';
    // Show current cheaper price vs target
    var ya = lm.yes_ask || 0, na = lm.no_ask || 0;
    var cheaper = 0;
    if (ya > 0 && na > 0) cheaper = Math.min(ya, na);
    else if (ya > 0) cheaper = ya;
    else if (na > 0) cheaper = na;
    var statusEl = document.getElementById('simStatus');
    if (cheaper > 0) {
      // Check if we're too late or too early
      var timeNote = '';
      if (lm.close_time) {
        var closeMs = new Date(lm.close_time).getTime();
        var openMs = closeMs - 15 * 60 * 1000;
        var now = Date.now();
        var elapsed = (now - openMs) / 1000;
        var remaining = (closeMs - now) / 1000;
        var startAfter = st.time === 'late' ? 600 : st.time === 'mid' ? 300 : 0;
        if (remaining < 30) {
          timeNote = ' · too late — next market';
        } else if (elapsed < startAfter) {
          var waitMin = Math.ceil((startAfter - elapsed) / 60);
          timeNote = ' · starts in ' + waitMin + 'm';
        } else if (cheaper > st.maxP) {
          timeNote = ' · polling for ≤' + st.maxP + '¢';
        } else {
          timeNote = ' · ready';
        }
      }
      statusEl.textContent = 'Cheapest: ' + cheaper + '¢' + timeNote;
    } else {
      statusEl.textContent = 'Waiting for price data...';
    }
    return;
  }

  // Bought — show trade status
  var sideEl = document.getElementById('simSide');
  sideEl.textContent = _sim.side.toUpperCase() + ' @ ' + _sim.entry + '¢';
  sideEl.className = 'val ' + (_sim.side === 'yes' ? 'side-yes' : 'side-no');

  document.getElementById('simEntry').textContent = _sim.cost + '¢ cost';

  var sellEl = document.getElementById('simSell');
  sellEl.textContent = _sim.sellTarget > 0 ? _sim.sellTarget + '¢' : 'Expiry';

  // Check if sell would have filled
  var bidKey = _sim.side === 'yes' ? 'yes_bid' : 'no_bid';
  var bid = lm[bidKey] || 0;

  if (!_sim.sold && _sim.sellTarget > 0 && bid >= _sim.sellTarget) {
    _sim.sold = true;
    _sim.soldAt = _sim.sellTarget;
  }

  // Compute P&L
  var pnlEl = document.getElementById('simPnl');
  var statusEl = document.getElementById('simStatus');

  if (_sim.sold) {
    var pnl = _sim.soldAt - _sim.cost;
    pnlEl.textContent = (pnl >= 0 ? '+' : '') + pnl + '¢';
    pnlEl.className = 'val pos';
    statusEl.innerHTML = '<span style="color:var(--green)">Sell target hit at ' + _sim.soldAt + '¢!</span>';
  } else {
    var estPnl = bid - _sim.cost;
    pnlEl.textContent = (estPnl >= 0 ? '+' : '') + estPnl + '¢';
    pnlEl.className = 'val ' + (estPnl >= 0 ? 'pos' : 'neg');
    var bidLabel = _sim.side.toUpperCase() + ' bid: ' + bid + '¢';
    if (_sim.sellTarget > 0) {
      var progress = _sim.entry > 0 && _sim.sellTarget > _sim.entry
        ? Math.max(0, Math.min(100, ((bid - _sim.entry) / (_sim.sellTarget - _sim.entry)) * 100)).toFixed(0) : 0;
      statusEl.textContent = bidLabel + ' · ' + progress + '% to target';
    } else {
      statusEl.textContent = bidLabel + ' · holding to expiry';
    }
  }
}

function _simRenderSkip(reason) {
  var sec = document.getElementById('simTradeSection');
  if (sec) sec.style.display = '';
  var st = _sim.strat;
  if (st) {
    var sellLabel = st.sell === 'hold' ? 'Hold' : 'sell@' + st.sell + '¢';
    var label = (_simLabels[st.time]||st.time) + ' · ≤' + st.maxP + '¢ · ' + sellLabel;
    document.getElementById('simStratLabel').textContent = label;
  }
  document.getElementById('simSide').textContent = '—';
  document.getElementById('simSide').className = 'val';
  document.getElementById('simEntry').textContent = '—';
  document.getElementById('simSell').textContent = '—';
  document.getElementById('simPnl').textContent = '—';
  document.getElementById('simPnl').className = 'val';
  document.getElementById('simStatus').innerHTML = '<span style="color:var(--yellow)">' + reason + ' — would skip</span>';
}

function _simHide() {
  var sec = document.getElementById('simTradeSection');
  if (sec) sec.style.display = 'none';
}

function _shadowHide() {
  var sec = document.getElementById('shadowTradeSection');
  if (sec) sec.style.display = 'none';
}

var _shadowFallbackPending = false;
var _shadowFallbackData = null;
var _shadowFallbackTicker = '';

function _shadowUpdate(lm, state) {
  // Show shadow trade card if there's an active shadow trade for this market.
  // Returns true if shadow trade is displayed (so caller can skip sim card).
  var sec = document.getElementById('shadowTradeSection');
  if (!sec) return false;

  // Check for pending_fill from active_shadow (no DB row yet)
  var aShd = (state || {}).active_shadow;
  if (aShd && aShd.status === 'pending_fill' && lm && aShd.ticker === lm.ticker) {
    sec.style.display = '';
    var pSide = (aShd.side || '').toUpperCase();
    var pCls = aShd.side === 'yes' ? 'side-yes' : 'side-no';
    document.getElementById('shadowSide').innerHTML = '<span class="' + pCls + '">' + pSide + '</span>';
    document.getElementById('shadowFill').innerHTML = aShd.entry_price_c + '¢ <span style="color:var(--yellow);font-size:11px">limit</span>';
    document.getElementById('shadowSlip').textContent = '—';
    document.getElementById('shadowPnl').textContent = '—';
    document.getElementById('shadowPnl').className = 'val';
    document.getElementById('shadowStatus').innerHTML = '<span style="color:var(--yellow)">Waiting for fill\u2026</span>';
    return true;
  }

  var shadow = (state || {}).shadow_trade;
  // Use fallback data if primary shadow doesn't match current market
  if ((!shadow || !shadow.ticker || shadow.ticker !== (lm||{}).ticker) && _shadowFallbackData) {
    shadow = _shadowFallbackData;
  }
  if (!shadow || !shadow.ticker || !lm || shadow.ticker !== lm.ticker) {
    // Trigger async fallback fetch if we have a live market but no shadow match
    if (lm && lm.ticker && !_shadowFallbackPending && _shadowFallbackTicker !== lm.ticker) {
      _shadowFallbackPending = true;
      _shadowFallbackTicker = lm.ticker;
      api('/api/active_shadow?ticker=' + encodeURIComponent(lm.ticker)).then(function(r) {
        _shadowFallbackData = r && r.shadow ? r.shadow : null;
        _shadowFallbackPending = false;
      }).catch(function() { _shadowFallbackPending = false; });
    }
    sec.style.display = 'none';
    return false;
  }
  // Clear fallback when primary data matches
  if ((state || {}).shadow_trade && (state || {}).shadow_trade.ticker === lm.ticker) {
    _shadowFallbackData = null;
    _shadowFallbackTicker = '';
  }

  // We have a shadow trade for this market — show it
  sec.style.display = '';

  var side = (shadow.side || '').toUpperCase();
  var sideCls = shadow.side === 'yes' ? 'side-yes' : 'side-no';
  var fillPrice = shadow.avg_fill_price_c || 0;
  var decisionPrice = shadow.shadow_decision_price_c || 0;
  var slip = fillPrice - decisionPrice;
  var latency = shadow.shadow_fill_latency_ms;

  // Side
  var sideEl = document.getElementById('shadowSide');
  sideEl.innerHTML = '<span class="' + sideCls + '">' + side + '</span>';

  // Fill price with latency
  var fillEl = document.getElementById('shadowFill');
  var latStr = latency != null ? ' <span style="color:var(--dim);font-size:11px">(' + latency + 'ms)</span>' : '';
  fillEl.innerHTML = fillPrice + '¢' + latStr;

  // Slippage
  var slipEl = document.getElementById('shadowSlip');
  if (decisionPrice > 0) {
    var slipColor = slip <= 0 ? 'var(--green)' : slip <= 1 ? 'var(--yellow)' : 'var(--red)';
    slipEl.innerHTML = '<span style="color:' + slipColor + '">' + (slip >= 0 ? '+' : '') + slip + '¢</span>';
  } else {
    slipEl.textContent = '—';
  }

  // P&L estimate from current bid
  var pnlEl = document.getElementById('shadowPnl');
  var outcome = shadow.outcome;
  if (outcome === 'win' || outcome === 'loss') {
    // Resolved — show actual result
    var actualCost = shadow.actual_cost || 0;
    var gross = outcome === 'win' ? 1.00 : 0;  // 1 contract × $1
    var pnl = gross - actualCost;
    var pnlC = Math.round(pnl * 100);
    pnlEl.textContent = (pnlC >= 0 ? '+' : '') + pnlC + '¢';
    pnlEl.className = 'val ' + (pnlC >= 0 ? 'pos' : 'neg');
  } else {
    // Still open — estimate from current bid
    var bidKey = shadow.side === 'yes' ? 'yes_bid' : 'no_bid';
    var bid = (lm[bidKey] || 0);
    var fee = Math.max(1, Math.round(fillPrice * 0.07));
    var cost = fillPrice + fee;
    var estPnl = bid - cost;
    pnlEl.textContent = (estPnl >= 0 ? '+' : '') + estPnl + '¢';
    pnlEl.className = 'val ' + (estPnl >= 0 ? 'pos' : 'neg');
  }

  // Status
  var statusEl = document.getElementById('shadowStatus');
  if (outcome === 'win') {
    statusEl.innerHTML = '<span style="color:var(--green)">Settled — won!</span>';
  } else if (outcome === 'loss') {
    statusEl.innerHTML = '<span style="color:var(--red)">Settled — lost</span>';
  } else {
    var bidKey2 = shadow.side === 'yes' ? 'yes_bid' : 'no_bid';
    var bid2 = lm[bidKey2] || 0;
    statusEl.textContent = side + ' bid: ' + bid2 + '¢ · holding to expiry';
  }

  return true;  // Shadow trade displayed
}

// ── Strategy Lab ────────────────────────────────────────
async function _recomputeStrategies() {
  if (!confirm('Recompute all ~1,881 strategies from Observatory data? This may take a moment.')) return;
  showToast('Recomputing strategies...', 'yellow');
  try {
    const r = await api('/api/recompute_strategies', {method: 'POST'});
    if (r.ok) {
      showToast(`Recomputed from ${r.processed} markets`, 'green');
      _loadStrategyLab();
      loadRegimes();
    } else {
      showToast('Error: ' + (r.error || ''), 'red');
    }
  } catch(e) { showToast('Error: ' + e, 'red'); }
}

let _stratLabMode = 'best';
let _stratLabData = [];
let _stratLabLoaded = false;

function _setStratLabMode(mode, btn) {
  _stratLabMode = mode;
  document.querySelectorAll('#stratLabMode .chip').forEach(c => c.classList.remove('active'));
  if (btn) btn.classList.add('active');
  document.getElementById('stratLabFilters').style.display = mode === 'lookup' ? '' : 'none';
  _loadStrategyLab();
}

function _fmtStratKey(key) {
  if (!key) return '';
  const p = key.split(':');
  let side, timing, entry, sell;
  if (p.length === 4) {
    // Full format: side:timing:entry:sell
    side = p[0]; timing = p[1]; entry = p[2]; sell = p[3];
  } else if (p.length === 3) {
    // Legacy format: timing:entry:sell (assume cheaper)
    side = 'cheaper'; timing = p[0]; entry = p[1]; sell = p[2];
  } else return key;
  const sideLabel = side === 'cheaper' ? '' : side.toUpperCase() + ' \u00b7 ';
  const timingLabel = timing.charAt(0).toUpperCase() + timing.slice(1);
  const entryLabel = '\u2264' + entry + '\u00a2';
  const sellLabel = sell === 'hold' ? 'Hold' : 'sell@' + sell + '\u00a2';
  return `${sideLabel}${timingLabel} \u00b7 ${entryLabel} \u00b7 ${sellLabel}`;
}

async function _loadStrategyLab() {
  const el = document.getElementById('stratLabContent');
  el.innerHTML = '<div class="dim">Loading...</div>';
  try {
    let url;
    if (_stratLabMode === 'best') {
      url = '/api/strategies?mode=best&min=5';
    } else {
      const side = document.getElementById('slSide')?.value || '';
      const timing = document.getElementById('slTiming')?.value || '';
      const entry = document.getElementById('slEntry')?.value || '';
      const sell = document.getElementById('slSell')?.value || '';
      url = `/api/strategies?mode=lookup&min=3&side=${side}&timing=${timing}&entry=${entry}&sell=${sell}`;
    }
    const r = await api(url);
    _stratLabData = r.strategies || [];
    const sub = document.getElementById('stratLabSub');
    if (sub) sub.textContent = _stratLabData.length + ' results';
    _renderStrategyLab();
  } catch(e) {
    el.innerHTML = '<div class="dim" style="color:var(--red)">Error: ' + e.message + '</div>';
  }
}

function _renderStrategyLab() {
  const el = document.getElementById('stratLabContent');
  const data = _stratLabData;
  if (!data.length) {
    el.innerHTML = _stratLabMode === 'best'
      ? '<div class="dim">No +EV strategies found yet. Observatory needs more resolved markets.</div>'
      : '<div class="dim">No matching strategies. Try broader filters or wait for more data.</div>';
    return;
  }

  let html = '';

  if (_stratLabMode === 'best') {
    for (const s of data) {
      const regime = (s.setup_key || '').replace('coarse_regime:', '').replace(/_/g, ' ');
      const ev = s.ev_per_trade_c || 0;
      const evFmt = (ev >= 0 ? '+' : '') + ev.toFixed(1) + '\u00a2';
      const evColor = ev > 5 ? 'var(--green)' : ev > 0 ? 'var(--blue)' : 'var(--red)';
      const wr = ((s.win_rate||0)*100).toFixed(0);
      const wrColor = (s.win_rate||0) >= 0.55 ? 'var(--green)' : (s.win_rate||0) < 0.45 ? 'var(--red)' : 'var(--yellow)';
      const n = s.sample_size || 0;
      const pf = s.profit_factor != null ? s.profit_factor.toFixed(1) : '\u2014';
      const ciL = ((s.ci_lower||0)*100).toFixed(0);
      const ciU = ((s.ci_upper||1)*100).toFixed(0);
      const maxL = s.max_consecutive_losses || 0;
      const pnl = (s.total_pnl_c || 0);
      const pnlFmt = (pnl >= 0 ? '+' : '') + (pnl / 100).toFixed(2);
      const pnlColor = pnl > 0 ? 'var(--green)' : pnl < 0 ? 'var(--red)' : 'var(--dim)';

      html += `<div style="background:var(--card);border:1px solid var(--border);border-radius:8px;padding:10px;margin-bottom:6px;border-left:3px solid ${evColor}">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:4px">
          <div style="font-size:12px;font-weight:600">${regime}</div>
          <span style="font-size:14px;font-weight:700;font-family:monospace;color:${evColor}">${evFmt}</span>
        </div>
        <div style="font-size:11px;color:var(--dim);margin-bottom:6px">${_fmtStratKey(s.strategy_key)}</div>
        <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:4px;font-size:11px">
          <div><span class="dim">WR</span> <strong style="color:${wrColor}">${wr}%</strong></div>
          <div><span class="dim">n</span> <strong>${n}</strong></div>
          <div><span class="dim">P&L</span> <strong style="color:${pnlColor}">$${pnlFmt}</strong></div>
          <div><span class="dim">PF</span> <strong>${pf}</strong></div>
        </div>
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:4px;font-size:10px;color:var(--dim);margin-top:3px">
          <div>CI ${ciL}\u2013${ciU}%</div>
          <div>MaxL ${maxL}</div>
          <div>W${s.wins||0} L${s.losses||0}</div>
        </div>
      </div>`;
    }
  } else {
    for (const s of data) {
      const setup = (s.setup_key || '').replace(/_/g, ' ').replace('coarse_regime:', '').replace('global:', '').replace('hour:', 'Hour ');
      const ev = s.ev_per_trade_c || 0;
      const evFmt = (ev >= 0 ? '+' : '') + ev.toFixed(1) + '\u00a2';
      const evColor = ev > 5 ? 'var(--green)' : ev > 0 ? 'var(--blue)' : 'var(--red)';
      const wr = ((s.win_rate||0)*100).toFixed(0);
      const n = s.sample_size || 0;
      const pf = s.profit_factor != null ? s.profit_factor.toFixed(1) : '\u2014';
      const ciL = ((s.ci_lower||0)*100).toFixed(0);
      const ciU = ((s.ci_upper||1)*100).toFixed(0);
      const maxL = s.max_consecutive_losses || 0;

      html += `<div style="background:var(--card);border:1px solid var(--border);border-radius:6px;padding:8px;margin-bottom:4px;border-left:3px solid ${evColor}">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <div>
            <span style="font-size:11px;font-weight:600">${setup}</span>
            <span class="dim" style="font-size:10px;margin-left:4px">${_fmtStratKey(s.strategy_key)}</span>
          </div>
          <span style="font-size:12px;font-weight:700;font-family:monospace;color:${evColor}">${evFmt}</span>
        </div>
        <div style="display:flex;gap:8px;font-size:10px;color:var(--dim);margin-top:2px">
          <span>WR ${wr}%</span><span>n=${n}</span><span>PF ${pf}</span><span>CI ${ciL}\u2013${ciU}%</span><span>MaxL ${maxL}</span>
        </div>
      </div>`;
    }
  }

  el.innerHTML = html;
}

function doLogout() {
  window.location.href = '/logout';
}

// ── Deploy ──────────────────────────────────────────────
let _deployFiles = [];

function onDeployFilesSelected(input) {
  _deployFiles = Array.from(input.files);
  const label = $('#deployFileLabel');
  const list = $('#deployFileList');
  const btn = $('#deployUploadBtn');
  if (_deployFiles.length) {
    label.textContent = `${_deployFiles.length} file${_deployFiles.length > 1 ? 's' : ''} selected`;
    list.innerHTML = _deployFiles.map(f =>
      `<div style="color:var(--text)">· ${f.name} <span class="dim">(${(f.size/1024).toFixed(1)}KB)</span></div>`
    ).join('');
    btn.style.display = '';
  } else {
    label.textContent = 'Choose .py files';
    list.innerHTML = '';
    btn.style.display = 'none';
  }
}

async function doDeploy() {
  const status = $('#deployStatus');
  const btn = $('#deployUploadBtn');
  if (!_deployFiles.length) return;

  btn.disabled = true;
  btn.textContent = 'Uploading...';
  status.innerHTML = '<span style="color:var(--blue)">Uploading files...</span>';

  try {
    const form = new FormData();
    _deployFiles.forEach((f, i) => form.append('file' + i, f));

    const resp = await fetch('/api/deploy/upload', {method: 'POST', body: form, headers:{'X-CSRF-Token':_getCsrfToken()}});
    if (!resp.ok) {
      const txt = await resp.text();
      throw new Error(`Upload failed (${resp.status}): ${txt.substring(0, 120)}`);
    }
    const text = await resp.text();
    let data;
    try { data = JSON.parse(text); } catch(pe) {
      throw new Error('Server returned invalid response: ' + text.substring(0, 120));
    }

    let html = '';
    if (data.uploaded && data.uploaded.length) {
      html += `<div style="color:var(--green)">Uploaded: ${data.uploaded.join(', ')}</div>`;
    }
    if (data.errors && data.errors.length) {
      html += data.errors.map(e => `<div style="color:var(--red)">${e}</div>`).join('');
    }

    if (data.uploaded && data.uploaded.length && (!data.errors || !data.errors.length)) {
      // Smart restart: dashboard-only files skip bot restart
      const dashOnly = data.uploaded.every(f => f === 'dashboard.py');
      const svcs = dashOnly ? ['platform-dashboard'] : ['plugin-btc-15m', 'platform-dashboard'];
      const svcLabel = dashOnly ? 'dashboard' : 'services';
      html += `<span style="color:var(--blue)">Restarting ${svcLabel}...</span>`;
      status.innerHTML = html;

      // Auto-restart — fire and forget since dashboard kills itself
      fetch('/api/deploy/restart', {
        method: 'POST',
        headers:{'Content-Type':'application/json','X-CSRF-Token':_getCsrfToken()},
        body: JSON.stringify({services: svcs})
      }).catch(() => {});
      html += `<div style="color:var(--green);margin-top:4px">Restarted. Page will reload...</div>`;
      status.innerHTML = html;
      // Dashboard is restarting — wait then reload
      setTimeout(() => location.reload(), 3000);
    } else {
      status.innerHTML = html;
      btn.disabled = false;
      btn.textContent = 'Upload & Restart';
    }
  } catch(e) {
    status.innerHTML = `<div style="color:var(--red)">Error: ${e}</div>`;
    btn.disabled = false;
    btn.textContent = 'Upload & Restart';
  }
}

async function doRestart() {
  const status = $('#deployStatus');
  const btn = $('#deployRestartBtn');
  btn.disabled = true;
  btn.textContent = 'Restarting...';
  status.innerHTML = '<span style="color:var(--blue)">Restarting (pending deploys process on startup)...</span>';

  try {
    // Just restart — the email deploy thread starts fresh on boot and
    // checks UNSEEN emails, which processes any pending deploys.
    fetch('/api/deploy/restart', {
      method: 'POST',
      headers:{'Content-Type':'application/json','X-CSRF-Token':_getCsrfToken()},
      body: JSON.stringify({services: ['plugin-btc-15m', 'platform-dashboard']})
    }).catch(() => {});
    status.innerHTML = '<div style="color:var(--green)">Restarted. Page will reload...</div>';
    setTimeout(() => location.reload(), 5000);
  } catch(e) {
    status.innerHTML = `<div style="color:var(--red)">Error: ${e}</div>`;
    btn.disabled = false;
    btn.textContent = 'Restart Services';
  }
}

async function loadBackupInfo() {
  try {
    const d = await api('/api/deploy/backup_info');
    const el = $('#deployBackupInfo');
    if (d.has_backup) {
      el.innerHTML = `Last backup: ${(d.files||[]).join(', ')} · ${d.ts_ct || '?'}`;
    } else {
      el.textContent = 'No backup yet';
    }
  } catch(e) {}
}

// ── Skip conditions viewer ───────────────────────────────
async function loadSkipConditions() {
  const el = $('#skipConditionsList');
  el.innerHTML = '<span class="dim">Loading...</span>';
  try {
    const conds = await api('/api/skip_conditions');
    if (!conds.length) { el.innerHTML = '<span class="dim">No observation rules active</span>'; return; }
    const colorMap = {green:'var(--green)',yellow:'var(--yellow)',orange:'var(--orange)',red:'var(--red)',blue:'var(--blue)',dim:'var(--dim)'};
    el.innerHTML = conds.map(c => {
      const col = colorMap[c.color] || 'var(--dim)';
      const detail = c.detail ? `<div class="dim" style="font-size:10px;margin-top:1px">${c.detail}</div>` : '';
      return `<div style="padding:4px 0;border-bottom:1px solid rgba(48,54,61,0.3)">
        <div style="display:flex;align-items:center;gap:6px">
          <span style="width:6px;height:6px;border-radius:50%;background:${col};flex-shrink:0"></span>
          <span style="font-size:12px">${c.label}</span>
        </div>
        ${detail}
      </div>`;
    }).join('');
  } catch(e) { el.innerHTML = '<span style="color:var(--red)">Error loading</span>'; }
}

// ── Security: PIN, Audit Log ──
const _JS_DESTRUCTIVE_SCOPES = new Set(['trades', 'regime_engine', 'full']);
let _hasPIN = false;

async function _loadPinStatus() {
  try {
    const r = await api('/api/destruction_pin');
    _hasPIN = r.has_pin;
    const el = document.getElementById('pinStatus');
    if (el) {
      el.innerHTML = _hasPIN
        ? '<span class="pos">● PIN active</span> <span class="dim">— required for destructive resets</span>'
        : '<span class="neg">● No PIN set</span> <span class="dim">— set one to protect your data</span>';
      const cp = document.getElementById('currentPinInput');
      if (cp) cp.style.display = _hasPIN ? '' : 'none';
    }
  } catch(e) {}
}

async function savePIN() {
  const curr = document.getElementById('currentPinInput')?.value || '';
  const newP = document.getElementById('newPinInput')?.value || '';
  if (!newP || newP.length < 4 || newP.length > 8 || !/^\d+$/.test(newP)) {
    showToast('PIN must be 4-8 digits', 'red'); return;
  }
  try {
    const r = await api('/api/destruction_pin', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({pin: newP, current_pin: curr})
    });
    if (r.ok) {
      showToast('Destruction PIN set', 'green');
      document.getElementById('newPinInput').value = '';
      document.getElementById('currentPinInput').value = '';
      _loadPinStatus();
    } else { showToast(r.error || 'Failed', 'red'); }
  } catch(e) { showToast('Error: ' + e.message, 'red'); }
}

async function loadAuditLog() {
  const el = document.getElementById('auditLogContent');
  if (!el) return;
  el.innerHTML = '<div class="dim">Loading...</div>';
  try {
    const r = await api('/api/audit_log');
    const entries = r.entries || [];
    if (entries.length === 0) { el.innerHTML = '<div class="dim">No audit entries yet</div>'; return; }
    el.innerHTML = entries.map(e => {
      const ts = e.created_at ? e.created_at.replace('T', ' ').substring(0, 19) : '';
      const icon = e.success ? '' : '<span class="neg">✗ </span>';
      return `<div style="font-size:11px;padding:3px 0;border-bottom:1px solid var(--border)">
        ${icon}<strong>${e.action}</strong> <span class="dim">${ts}</span>
        ${e.detail ? `<span class="dim" style="font-size:10px"> · ${e.detail}</span>` : ''}
      </div>`;
    }).join('');
  } catch(e) { el.innerHTML = '<div class="dim">Error loading audit log</div>'; }
}

setTimeout(_loadPinStatus, 2000);

// ── Reset system (2-level confirmation + PIN) ──────────────────
let _resetPending = null;

function _resetConfirm1(scope, title, desc) {
  _resetPending = {scope, title};
  const el = $('#resetConfirm1Content');
  el.innerHTML = `<div style="font-size:14px;font-weight:600;margin-bottom:8px">${title}</div>
    <div style="font-size:12px;color:var(--dim);margin-bottom:16px;line-height:1.5">${desc}</div>
    <div style="display:flex;gap:8px">
      <button class="btn btn-dim" style="flex:1" onclick="closeModal('resetConfirm1')">Cancel</button>
      <button class="btn" style="flex:1;background:rgba(248,81,73,0.15);border:1px solid rgba(248,81,73,0.4);color:var(--red)" onclick="_resetConfirm2()">Continue</button>
    </div>`;
  openModal('resetConfirm1');
}

function _resetConfirm2() {
  closeModal('resetConfirm1');
  if (!_resetPending) return;
  const {scope, title} = _resetPending;
  const needsPin = _hasPIN && _JS_DESTRUCTIVE_SCOPES.has(scope);
  const el = $('#resetConfirm2Content');
  el.innerHTML = `<div style="font-size:14px;font-weight:600;color:var(--red);margin-bottom:8px">Are you absolutely sure?</div>
    <div style="font-size:12px;color:var(--dim);margin-bottom:${needsPin ? '10' : '16'}px">This will <strong>${title.toLowerCase()}</strong>. This cannot be undone.</div>
    ${needsPin ? '<input type="password" id="resetPinInput" placeholder="Enter destruction PIN" inputmode="numeric" maxlength="8" style="width:100%;padding:8px;margin-bottom:12px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px;font-size:14px;box-sizing:border-box">' : ''}
    <div style="display:flex;gap:8px">
      <button class="btn btn-dim" style="flex:1" onclick="closeModal('resetConfirm2')">Cancel</button>
      <button class="btn" style="flex:1;background:rgba(248,81,73,0.25);border:1px solid var(--red);color:var(--red)" onclick="_executeReset()">Confirm ${title}</button>
    </div>`;
  openModal('resetConfirm2');
  if (needsPin) setTimeout(() => { const pi = document.getElementById('resetPinInput'); if (pi) pi.focus(); }, 100);
}

async function _executeReset() {
  closeModal('resetConfirm2');
  if (!_resetPending) return;
  const {scope} = _resetPending;
  const pin = document.getElementById('resetPinInput')?.value || '';
  _resetPending = null;
  try {
    const body = {scope};
    if (_JS_DESTRUCTIVE_SCOPES.has(scope)) body.pin = pin;
    const resp = await fetch('/api/reset', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    let r;
    try { r = await resp.json(); } catch(e) { throw new Error('Server returned invalid response (status ' + resp.status + ')'); }
    if (!resp.ok || r.error) {
      if (r.pin_required) { showToast('Wrong PIN — reset blocked', 'red'); }
      else { showToast(r.msg || r.error || 'Reset failed', 'red'); }
      return;
    }
    showToast(r.msg || 'Reset complete', 'green');
    if (scope === 'full' || scope === 'settings') {
      setTimeout(() => location.reload(), 1500);
    } else {
      pollState();
      loadConfig();
      if (scope === 'trades') {
        lastStateData._lastTrade = null;
        lastStateData._nextMarketOpen = null;
        loadTrades(); loadRegimes(); loadLifetimeStats();
      }
      if (scope === 'regime_engine') { loadRegimes(); }
      if (scope === 'regime_filters') { loadRegimes(); }
    }
  } catch(e) { showToast('Reset failed: ' + e.message, 'red'); }
}
document.addEventListener('input', e => {
  if (e.target.id === 'pasteCode') {
    const len = e.target.value.length;
    const el = $('#pasteCharCount');
    if (el) el.textContent = len > 0 ? `${(len/1024).toFixed(1)}KB` : '';
  }
});

let _pasteQueue = [];  // [{filename, code}]

function renderPasteQueue() {
  const el = $('#pasteQueue');
  if (!_pasteQueue.length) { el.innerHTML = ''; return; }
  el.innerHTML = _pasteQueue.map((item, i) =>
    `<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 8px;margin-bottom:4px;background:var(--bg);border-radius:4px;font-size:12px">
      <span style="color:var(--text)">${item.filename} <span class="dim">(${(item.code.length/1024).toFixed(1)}KB)</span></span>
      <button onclick="_pasteQueue.splice(${i},1);renderPasteQueue()" style="background:none;border:none;color:var(--red);cursor:pointer;font-size:14px;padding:2px 6px">×</button>
    </div>`
  ).join('');
}

function addToQueue() {
  const filename = $('#pasteFilename').value;
  const code = $('#pasteCode').value;
  const status = $('#deployStatus');
  if (!code.trim()) { status.innerHTML = '<span style="color:var(--red)">Paste code first</span>'; return; }

  // Replace if same filename already queued
  _pasteQueue = _pasteQueue.filter(q => q.filename !== filename);
  _pasteQueue.push({filename, code});
  renderPasteQueue();

  // Clear textarea for next file
  $('#pasteCode').value = '';
  $('#pasteCharCount').textContent = '';
  status.innerHTML = `<span style="color:var(--green)">${filename} queued (${_pasteQueue.length} file${_pasteQueue.length>1?'s':''} ready)</span>`;
}

async function deployPasted() {
  const status = $('#deployStatus');

  // If textarea has content but not queued yet, add it first
  const code = $('#pasteCode').value;
  if (code.trim()) {
    addToQueue();
  }

  if (!_pasteQueue.length) { status.innerHTML = '<span style="color:var(--red)">Nothing to deploy</span>'; return; }

  const btn = $('#deployPasteBtn');
  btn.disabled = true;
  btn.textContent = '...';
  status.innerHTML = `<span style="color:var(--blue)">Deploying ${_pasteQueue.length} file${_pasteQueue.length>1?'s':''}...</span>`;

  let allOk = true;
  let results = [];
  let errorTexts = [];
  let hasForceableError = false;

  for (const item of _pasteQueue) {
    try {
      const resp = await fetch('/api/deploy/paste', {
        method: 'POST',
        headers:{'Content-Type':'application/json','X-CSRF-Token':_getCsrfToken()},
        body: JSON.stringify({filename: item.filename, code: item.code, force: item.force || false})
      });
      const data = await resp.json();
      if (!resp.ok || data.error) {
        const errMsg = data.error || 'Unknown error';
        const sizeInfo = data.size ? ` (received ${(data.size/1024).toFixed(1)}KB)` : '';
        results.push(`<span style="color:var(--red)">${errMsg}${sizeInfo}</span>`);
        errorTexts.push(errMsg + sizeInfo);
        if (data.can_force) hasForceableError = true;
        allOk = false;
      } else {
        results.push(`<span style="color:var(--green)">${item.filename} ✓ (${(data.size/1024).toFixed(1)}KB)</span>`);
      }
    } catch(e) {
      const errMsg = `${item.filename}: ${e}`;
      results.push(`<span style="color:var(--red)">${errMsg}</span>`);
      errorTexts.push(errMsg);
      allOk = false;
    }
  }

  let html = results.join('<br>');
  if (!allOk && errorTexts.length) {
    _lastDeployError = errorTexts.join('\n');
    html += `<div style="display:flex;gap:6px;margin-top:6px">`;
    html += `<button onclick="copyDeployError()" style="background:none;border:1px solid var(--border);border-radius:4px;padding:3px 8px;color:var(--dim);cursor:pointer;font-size:10px">Copy error</button>`;
    if (hasForceableError) {
      html += `<button onclick="forceDeployAll()" style="background:none;border:1px solid rgba(248,81,73,0.3);border-radius:4px;padding:3px 8px;color:var(--red);cursor:pointer;font-size:10px">Force deploy (skip validation)</button>`;
    }
    html += `</div>`;
  }
  status.innerHTML = html;

  if (allOk) {
    status.innerHTML += '<br><span style="color:var(--blue)">Restarting...</span>';
    try {
      await api('/api/deploy/restart', {
        method: 'POST',
        headers:{'Content-Type':'application/json','X-CSRF-Token':_getCsrfToken()},
        body: JSON.stringify({services: ['plugin-btc-15m', 'platform-dashboard']})
      });
      status.innerHTML += ' <span style="color:var(--green)">Done! Reloading...</span>';
      _pasteQueue = [];
      renderPasteQueue();
      setTimeout(() => location.reload(), 3000);
    } catch(e) {
      status.innerHTML += `<br><span style="color:var(--red)">Restart error: ${e}</span>`;
    }
  }

  btn.disabled = false;
  btn.textContent = 'Deploy';
}

let _lastDeployError = '';
function copyDeployError() {
  if (_lastDeployError) {
    navigator.clipboard.writeText(_lastDeployError).then(
      () => showToast('Error copied', 'blue'),
      () => {
        const ta = document.createElement('textarea');
        ta.value = _lastDeployError;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        showToast('Error copied', 'blue');
      }
    );
  }
}

function forceDeployAll() {
  $('#pasteCode').value = '';  // prevent re-queue without force
  _pasteQueue.forEach(item => item.force = true);
  deployPasted();
}

async function exportAIData(btn) {
  const orig = btn.textContent;
  btn.textContent = 'Generating...';
  btn.disabled = true;
  try {
    const resp = await api('/api/export/ai-analysis', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': _getCsrfToken()},
      body: '{}',
    });
    if (resp.error) {
      showToast('Export failed: ' + resp.error, 'red');
      return;
    }
    const url = resp.url;
    const filename = resp.filename || 'ai_analysis.md';
    await shareFile(url + '?dl=1', filename);
  } catch(e) {
    showToast('Export error: ' + e, 'red');
  } finally {
    btn.textContent = orig;
    btn.disabled = false;
  }
}

function openLightbox(url) {
  const lb = document.getElementById('imgLightbox');
  const img = document.getElementById('lbImg');
  const dl = document.getElementById('lbDownload');
  img.src = url;
  const fname = url.split('/').pop().split('?')[0];
  dl.onclick = function(e) { e.stopPropagation(); shareFile(url, fname); };
  lb.style.display = 'flex';
}

function closeLightbox() {
  const lb = document.getElementById('imgLightbox');
  lb.style.display = 'none';
  document.getElementById('lbImg').src = '';
}

async function shareFile(url, filename) {
  try {
    const resp = await fetch(url);
    const blob = await resp.blob();
    const file = new File([blob], filename, { type: blob.type || 'text/csv' });
    if (navigator.share && /mobile|iphone|android/i.test(navigator.userAgent)) {
      try { await navigator.share({ files: [file], title: filename }); return; }
      catch(e) { if (e.name === 'AbortError') return; }
    }
    // Fallback: download
    const u = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = u; a.download = filename; a.click();
    setTimeout(() => URL.revokeObjectURL(u), 5000);
  } catch(e) {
    if (e.name !== 'AbortError') window.open(url, '_blank');
  }
}


// ── Init ─────────────────────────────────────────────────
let _currentTab = 'Home';
function switchTab(tab) {
  // Close any open modals
  closeAllModals();

  // Teardown previous tab

  _currentTab = tab;
  try { sessionStorage.setItem('_tab', tab); } catch(e) {}

  // Toggle active class on pages
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  const page = document.getElementById('page' + tab);
  if (page) page.classList.add('active');

  // Update tab highlighting
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('tab-active'));
  document.querySelectorAll('.tab-btn[data-tab="' + tab + '"]').forEach(b => b.classList.add('tab-active'));

  // Scroll to top
  scrollTop();

  // Tab-specific setup
  if (tab === 'Stats') { statsGoBack(); _loadLifetimeStatsInner(); }
  else if (tab === 'Regimes') { loadBtcChart(); loadRegimes(); loadRegimeWorkerStatus(); }
  else if (tab === 'Trades') loadTrades();
  else if (tab === 'Settings') { loadConfig(); loadBackupInfo(); loadSvcStatus(); loadSystemStats(); }
}

// Backfill live price chart from server history
async function loadLivePriceHistory(force) {
  try {
    const prices = await api('/api/live_prices');
    if (!prices || !prices.length) return false;
    const currentTicker = prices[prices.length - 1].ticker;
    // Only skip backfill if buffer is full and not forced (e.g. from visibility change)
    if (!force && _livePriceBuf.data.length > 5 && _livePriceBuf.ticker === currentTicker) return true;
    const backfill = [];
    for (const p of prices) {
      if (p.ticker !== currentTicker) continue;
      // Filter opening noise: cheaper side >= 90 or <= 2
      const cheaper = Math.min(p.yes_ask || 99, p.no_ask || 99);
      if (cheaper >= 90 || cheaper <= 2) continue;
      const ts = new Date(p.ts).getTime();
      backfill.push({ts, ya: p.yes_ask || 0, na: p.no_ask || 0,
        yb: p.yes_bid || 0, nb: p.no_bid || 0});
    }
    if (!backfill.length) return false;
    // Merge: use backfill as base, then append any poll data newer than backfill
    const existing = _livePriceBuf.ticker === currentTicker ? _livePriceBuf.data : [];
    const lastBackfillTs = backfill[backfill.length - 1].ts;
    const newPollData = existing.filter(d => d.ts > lastBackfillTs + 500);
    _livePriceBuf = {ticker: currentTicker, data: [...backfill, ...newPollData], closeTime: _livePriceBuf.closeTime};

    // Immediately redraw charts with the fresh data
    try {
      drawLiveMarketChart('liveChart');
      drawLiveMarketChart('pendChart');
    } catch(e) {}
    return true;
  } catch(e) {
    console.error('Live price backfill error:', e);
    return false;
  }
}

// Full refresh when app comes back to foreground (e.g. tapping deploy notification)
let _lastForegroundRefresh = 0;
async function _refreshOnForeground() {
  const now = Date.now();
  if (now - _lastForegroundRefresh < 2000) return;
  _lastForegroundRefresh = now;

  const cw = document.getElementById('contentWrap');
  const savedScroll = cw ? cw.scrollTop : 0;

  await pollState();

  // Retry backfill up to 4 times — iOS networking may not be ready immediately
  let backfilled = false;
  for (let attempt = 0; attempt < 4 && !backfilled; attempt++) {
    if (attempt > 0) await new Promise(r => setTimeout(r, 1000));
    backfilled = await loadLivePriceHistory(true);
  }

  loadTrades();
  loadRegimes();
  _loadLifetimeStatsInner();

  // One more delayed retry in case server data wasn't complete yet
  if (!backfilled) setTimeout(() => loadLivePriceHistory(true), 5000);

  if (cw) {
    requestAnimationFrame(() => {
      cw.scrollTop = savedScroll;
      setTimeout(() => { cw.scrollTop = savedScroll; }, 300);
      setTimeout(() => { cw.scrollTop = savedScroll; }, 800);
    });
  }
}

document.addEventListener('visibilitychange', function() {
  if (!document.hidden) _refreshOnForeground();
});

// iOS: pageshow fires when returning via notification tap or back-forward cache
window.addEventListener('pageshow', function(e) {
  if (e.persisted) _refreshOnForeground();
});

// Fallback: window focus (covers desktop tab switching)
window.addEventListener('focus', _refreshOnForeground);

loadLivePriceHistory();
loadConfig();
loadTrades();
loadRegimes();
loadLifetimeStats();
loadRegimeWorkerStatus();
loadBtcChart();
pollState();
_adjustContentTop();
setTimeout(_adjustContentTop, 500);

// Restore last active tab
try {
  const savedTab = sessionStorage.getItem('_tab');
  if (savedTab && savedTab !== 'Home') {
    const _savedRaw = savedTab === 'Bitcoin' ? 'Regimes' : savedTab;
    const tab = (_savedRaw === 'Chat' || _savedRaw === 'Arcade') ? 'Home' : _savedRaw;
    if (document.getElementById('page' + tab)) switchTab(tab);
  }
} catch(e) {}

// Dynamic poll rate
let _pollRate = 1000;
let _lastGapCheck = 0;
function schedulePoll() {
  setTimeout(async () => {
    await pollState();
    _pollRate = 1000;

    // Periodic gap detection: if chart buffer seems sparse, backfill from server
    const now = Date.now();
    if (now - _lastGapCheck > 10000 && _livePriceBuf.closeTime && _livePriceBuf.data.length > 0) {
      _lastGapCheck = now;
      const closeMs = new Date(_livePriceBuf.closeTime).getTime();
      const openMs = closeMs - 15 * 60 * 1000;
      const elapsed = Math.min(now, closeMs) - openMs;
      if (elapsed > 30000) { // market open for >30s
        const expectedPts = elapsed / 1200; // ~1 point per 1.2s
        if (_livePriceBuf.data.length < expectedPts * 0.5) {
          loadLivePriceHistory(true);
        }
      }
    }

    schedulePoll();
  }, _pollRate);
}
schedulePoll();

setInterval(loadTrades, 15000);
setInterval(() => loadRegimes(), 30000);
setInterval(loadLifetimeStats, 30000);
setInterval(loadRegimeWorkerStatus, 30000);
setInterval(loadBtcChart, 30000);
// Sync global auto-fill strategy when it changes
setInterval(_syncGlobalAutoFill, 60000);

// ── Push Notifications ───────────────────────────────────
let pushSubscription = null;

async function initPush() {
  const statusEl = $('#pushStatus');
  const btnEl = $('#pushToggleBtn');

  // Check if push is supported
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
    statusEl.textContent = 'Push not supported on this browser/device';
    return;
  }

  // Check HTTPS
  if (location.protocol !== 'https:' && location.hostname !== 'localhost') {
    statusEl.textContent = 'Push requires HTTPS — visit via https://dash.btcbotapp.com';
    return;
  }

  try {
    // Register service worker
    const reg = await navigator.serviceWorker.register('/sw.js');
    await navigator.serviceWorker.ready;

    // Check existing subscription
    pushSubscription = await reg.pushManager.getSubscription();

    if (pushSubscription) {
      statusEl.innerHTML = '<span style="color:var(--green)">● Notifications enabled</span>';
      btnEl.textContent = 'Disable Notifications';
      btnEl.className = 'btn btn-dim';
    } else {
      statusEl.textContent = 'Notifications are off';
      btnEl.textContent = 'Enable Notifications';
      btnEl.className = 'btn btn-blue';
    }
    btnEl.style.display = '';

  } catch(e) {
    statusEl.textContent = 'Push setup error: ' + e.message;
    console.error('Push init error:', e);
  }
}

async function togglePush() {
  const btnEl = $('#pushToggleBtn');

  if (pushSubscription) {
    // Unsubscribe
    const endpoint = pushSubscription.endpoint;
    await pushSubscription.unsubscribe();
    pushSubscription = null;
    await api('/api/push/unsubscribe', {
      method: 'POST',
      headers:{'Content-Type':'application/json','X-CSRF-Token':_getCsrfToken()},
      body: JSON.stringify({endpoint})
    });
    showToast('Notifications disabled', 'yellow');
    initPush();
  } else {
    // Subscribe
    try {
      const keyResp = await api('/api/push/vapid-key');
      if (!keyResp.key) {
        showToast('VAPID key not configured on server', 'red');
        return;
      }

      // Convert VAPID key
      const rawKey = keyResp.key;
      const padding = '='.repeat((4 - rawKey.length % 4) % 4);
      const base64 = (rawKey + padding).replace(/-/g, '+').replace(/_/g, '/');
      const binary = atob(base64);
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);

      const reg = await navigator.serviceWorker.ready;
      pushSubscription = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: bytes
      });

      // Send subscription to server
      await api('/api/push/subscribe', {
        method: 'POST',
        headers:{'Content-Type':'application/json','X-CSRF-Token':_getCsrfToken()},
        body: JSON.stringify({subscription: pushSubscription.toJSON()})
      });

      showToast('Notifications enabled!', 'green');
      initPush();

    } catch(e) {
      if (e.name === 'NotAllowedError') {
        showToast('Permission denied — check iOS Settings', 'red');
      } else {
        showToast('Subscribe error: ' + e.message, 'red');
      }
      console.error('Push subscribe error:', e);
    }
  }
}

// Init push after page loads
setTimeout(initPush, 1000);

</script>
</body>
</html>"""


LOGS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>Bot Logs</title>
<style>
  :root { --bg: #0d1117; --card: #161b22; --border: #30363d; --text: #c9d1d9;
          --dim: #8b949e; --green: #3fb950; --red: #f85149; --yellow: #d29922;
          --blue: #58a6ff; --orange: #f0883e; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; overflow: hidden; }
  body { font-family: 'SF Mono', 'Menlo', monospace; background: var(--bg);
         color: var(--text); font-size: 12px; }
  .header { position: fixed; top: 0; left: 0; right: 0; background: var(--card); border-bottom: 1px solid var(--border);
            padding: 10px 14px; display: flex; justify-content: space-between; align-items: center;
            z-index: 10; gap: 8px; flex-wrap: wrap; }
  .header a { color: var(--blue); text-decoration: none; font-family: sans-serif; font-size: 14px; }
  .header .count { color: var(--dim); font-family: sans-serif; font-size: 12px; }
  .header-btn { background: var(--border); color: var(--text); border: none; padding: 4px 10px;
                border-radius: 4px; cursor: pointer; font-size: 12px; font-family: sans-serif; }
  .header-btn:active { background: var(--blue); color: #000; }
  .filter-bar { position: fixed; top: 44px; left: 0; right: 0; background: var(--bg); padding: 6px 14px;
                border-bottom: 1px solid var(--border); z-index: 9; display: flex;
                gap: 8px; align-items: center; font-family: sans-serif; font-size: 12px; }
  .filter-btn { background: none; border: 1px solid var(--border); color: var(--dim);
                padding: 2px 8px; border-radius: 3px; cursor: pointer; font-size: 11px; }
  .filter-btn.active { border-color: var(--blue); color: var(--blue); }
  #logScroll { position: fixed; top: 76px; left: 0; right: 0; bottom: 140px;
    overflow-y: auto; overscroll-behavior-y: contain; }
  #logContainer { padding: 8px; padding-bottom: 30px; }
  .log-line { padding: 3px 6px; border-bottom: 1px solid rgba(48,54,61,0.3);
              white-space: pre-wrap; word-break: break-all; line-height: 1.5; }
  .log-line .ts { color: var(--dim); }
  .log-line .lvl-INFO { color: var(--blue); }
  .log-line .lvl-WARNING { color: var(--yellow); }
  .log-line .lvl-ERROR { color: var(--red); font-weight: bold; }
  .log-line .msg { color: var(--text); }
  .log-line.error-line { background: rgba(248,81,73,0.08); border-left: 3px solid var(--red);
                         padding-left: 8px; }
  .log-line.warning-line { background: rgba(210,153,34,0.05); border-left: 3px solid var(--yellow);
                           padding-left: 8px; }
  #loadMore { text-align: center; padding: 16px; }
  #loadMore button { background: var(--border); color: var(--text); border: none;
                     padding: 8px 20px; border-radius: 4px; cursor: pointer; font-size: 14px; }
  .auto-indicator { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
                    background: var(--green); margin-left: 6px; animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
  .toast { position: fixed; bottom: 150px; left: 50%; transform: translateX(-50%);
           background: rgba(63,185,80,0.12); color: var(--green); border: 1px solid rgba(63,185,80,0.4);
           padding: 8px 16px; border-radius: 6px;
           font-family: sans-serif; font-size: 14px; font-weight: 600;
           opacity: 0; transition: opacity 0.3s; z-index: 100;
           backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); }
  .toast.show { opacity: 1; }
  #terminal { position: fixed; bottom: 0; left: 0; right: 0; background: var(--card);
    border-top: 1px solid var(--border); padding: 8px 12px 30px; z-index: 10; }
  #terminal .term-row { display: flex; gap: 6px; align-items: center; }
  #terminal input { flex: 1; background: var(--bg); border: 1px solid var(--border);
    border-radius: 4px; padding: 8px 10px; color: var(--text); font-size: 13px;
    font-family: 'SF Mono','Menlo',monospace; outline: none; }
  #terminal input:focus { border-color: var(--blue); }
  #terminal button { background: var(--blue); color: #000; border: none; border-radius: 4px;
    padding: 8px 14px; font-size: 12px; font-weight: 600; cursor: pointer;
    font-family: sans-serif; white-space: nowrap; }
  #terminal button:active { opacity: 0.7; }
  #termOutput { margin-top: 6px; display: none; }
  #termOutput pre { background: var(--bg); border: 1px solid var(--border); border-radius: 4px;
    padding: 8px; font-size: 11px; max-height: 150px; overflow-y: auto;
    white-space: pre-wrap; word-break: break-all; color: var(--text); }
  .term-restart-btn { display: inline-block; margin-top: 6px; background: var(--orange);
    color: #000; border: none; border-radius: 4px; padding: 6px 14px; font-size: 12px;
    font-weight: 600; cursor: pointer; font-family: sans-serif; }
  .term-hint { font-size: 10px; color: var(--dim); margin-top: 4px; font-family: sans-serif; }
</style>
</head>
<body>

<div class="header">
  <a href="/" onclick="this.textContent='Loading...'">← Dashboard</a>
  <div>
    <button class="header-btn" onclick="copyErrors()">Copy Errors</button>
    <button class="header-btn" onclick="copyRecent()">Copy Last 50</button>
  </div>
  <span class="count"><span id="logCount">0</span> entries<span class="auto-indicator"></span></span>
</div>

<div class="filter-bar">
  <span style="color:var(--dim)">Filter:</span>
  <button class="filter-btn active" onclick="setFilter('all',this)">All</button>
  <button class="filter-btn" onclick="setFilter('ERROR',this)">Errors</button>
  <button class="filter-btn" onclick="setFilter('WARNING',this)">Warnings</button>
  <button class="filter-btn" onclick="setFilter('trade',this)">Trades</button>
  <button class="filter-btn" onclick="setFilter('deploy',this)">Deploy</button>
</div>

<div id="logScroll">
<div id="loadMore">
  <button onclick="loadOlder()">Load Older</button>
</div>
<div id="logContainer"></div>
</div>

<div class="toast" id="toast"></div>

<div id="terminal">
  <div class="term-row">
    <input type="text" id="termInput" placeholder="pip install matplotlib, supervisorctl status..."
           onkeydown="if(event.key==='Enter')runCmd()">
    <button onclick="runCmd()">Run</button>
  </div>
  <div class="term-hint">Allowed: pip install, pip list, pip show, supervisorctl, python3 --version, df, free, uptime</div>
  <div id="termOutput"></div>
</div>

<script>
let oldestId = null;
let newestId = 0;
const container = document.getElementById('logContainer');
const logScroll = document.getElementById('logScroll');
const countEl = document.getElementById('logCount');
let totalCount = 0;
let allLogs = [];  // Keep all logs in memory for filtering/copying
let currentFilter = 'all';

function renderLine(l) {
  const div = document.createElement('div');
  const level = l.level || 'INFO';
  let cls = 'log-line';
  if (level === 'ERROR') cls += ' error-line';
  else if (level === 'WARNING') cls += ' warning-line';
  div.className = cls;
  div.dataset.level = level;
  div.dataset.msg = l.message || '';
  div.dataset.logId = l.id || 0;
  const lvlCls = 'lvl-' + level;
  div.innerHTML = `<span class="ts">${l.ts_ct}</span> ` +
    `<span class="${lvlCls}">[${level}]</span> ` +
    `<span class="msg">${escHtml(l.message)}</span>`;
  return div;
}

function escHtml(s) {
  return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function setFilter(filter, btn) {
  currentFilter = filter;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');

  if (filter === 'all') {
    // Show all loaded lines
    container.querySelectorAll('.log-line').forEach(el => el.style.display = '');
  } else if (filter === 'ERROR' || filter === 'WARNING') {
    // Server-side filtered load — replace visible content
    loadFiltered(filter);
  } else if (filter === 'trade' || filter === 'deploy') {
    // Client-side keyword filter on all loaded logs
    applyClientFilter();
    // Auto-load more if too few visible
    autoLoadForFilter();
  }
}

function applyClientFilter() {
  container.querySelectorAll('.log-line').forEach(el => {
    if (currentFilter === 'all') {
      el.style.display = '';
    } else if (currentFilter === 'trade') {
      const msg = el.dataset.msg || '';
      el.style.display = (msg.includes('WIN') || msg.includes('LOSS') ||
        msg.includes('Filled') || msg.includes('Buy placed') ||
        msg.includes('Sell placed') || msg.includes('Skipped')) ? '' : 'none';
    } else if (currentFilter === 'deploy') {
      const msg = el.dataset.msg || '';
      el.style.display = (msg.includes('[EmailDeploy]') || msg.includes('[ServerCmd]') ||
        msg.includes('pip') || msg.includes('Deploy') || msg.includes('deploy')) ? '' : 'none';
    } else {
      el.style.display = el.dataset.level === currentFilter ? '' : 'none';
    }
  });
}

async function loadFiltered(level) {
  // Server-side filtered load — get 200 entries of this level
  try {
    const logs = await (await fetch(`/api/logs?limit=200&level=${level}`)).json();
    logs.reverse();
    const existingIds = new Set(allLogs.map(l => l.id));
    let added = 0;
    for (const l of logs) {
      if (!existingIds.has(l.id)) {
        const line = renderLine(l);
        // Find insertion point to maintain order
        let inserted = false;
        for (const child of container.children) {
          const childId = parseInt(child.dataset.logId || '0');
          if (childId > l.id) {
            container.insertBefore(line, child);
            inserted = true;
            break;
          }
        }
        if (!inserted) container.appendChild(line);
        allLogs.push(l);
        if (oldestId === null || l.id < oldestId) oldestId = l.id;
        if (l.id > newestId) newestId = l.id;
        totalCount++;
        added++;
      }
    }
    if (added > 0) countEl.textContent = totalCount;
    // Now apply client filter to show only matching level
    applyClientFilter();
  } catch(e) {
    applyClientFilter();
  }
}

async function autoLoadForFilter() {
  // For client-side filters, auto-load older batches until we have 20+ visible or no more data
  let visible = container.querySelectorAll('.log-line:not([style*="display: none"])').length;
  let attempts = 0;
  while (visible < 20 && oldestId && attempts < 5) {
    const logs = await (await fetch(`/api/logs?before=${oldestId}&limit=200`)).json();
    if (logs.length === 0) break;
    logs.reverse();
    const scrollBefore = logScroll.scrollHeight;
    for (const l of logs) {
      container.insertBefore(renderLine(l), container.firstChild);
      allLogs.unshift(l);
      if (l.id < oldestId) oldestId = l.id;
      totalCount++;
    }
    countEl.textContent = totalCount;
    applyClientFilter();
    visible = container.querySelectorAll('.log-line:not([style*="display: none"])').length;
    attempts++;
  }
}

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2000);
}

function copyToClipboard(text) {
  return new Promise((resolve, reject) => {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(resolve).catch(() => fallbackCopy2(text, resolve, reject));
    } else { fallbackCopy2(text, resolve, reject); }
  });
}
function fallbackCopy2(text, resolve, reject) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;left:0;top:0;width:100%;height:200px;z-index:9999;' +
    'font-size:11px;background:#161b22;color:#c9d1d9;border:2px solid #58a6ff;padding:10px;border-radius:8px';
  document.body.appendChild(ta); ta.focus(); ta.select();
  try {
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    if (ok) resolve(); else { document.body.removeChild(ta); reject(); }
  } catch(e) {
    // Leave visible for copy
    const hint = document.createElement('div');
    hint.style.cssText = 'position:fixed;left:0;top:205px;width:100%;text-align:center;z-index:9999;' +
      'padding:8px;background:#161b22;color:#d29922;font-size:13px';
    hint.innerHTML = 'Select all and copy manually. <button onclick="this.parentElement.remove();' +
      'document.querySelector(\'textarea\')?.remove()" style="margin-left:8px;padding:4px 8px">Done</button>';
    document.body.appendChild(hint);
    reject(e);
  }
}

function copyErrors() {
  const errors = allLogs.filter(l => l.level === 'ERROR' || l.level === 'WARNING');
  if (!errors.length) { showToast('No errors to copy'); return; }
  const text = errors.slice(-30).map(l =>
    `${l.ts_ct} [${l.level}] ${l.message}`
  ).join('\n');
  copyToClipboard(text).then(() => showToast('Copied ' + Math.min(errors.length, 30) + ' errors'));
}

function copyRecent() {
  const recent = allLogs.slice(-50);
  const text = recent.map(l =>
    `${l.ts_ct} [${l.level}] ${l.message}`
  ).join('\n');
  copyToClipboard(text).then(() => showToast('Copied last ' + recent.length + ' logs'));
}

async function loadInitial() {
  const logs = await (await fetch('/api/logs?limit=200')).json();
  logs.reverse();
  for (const l of logs) {
    container.appendChild(renderLine(l));
    allLogs.push(l);
    if (oldestId === null || l.id < oldestId) oldestId = l.id;
    if (l.id > newestId) newestId = l.id;
  }
  totalCount = logs.length;
  countEl.textContent = totalCount;
  logScroll.scrollTop = logScroll.scrollHeight;
}

async function loadOlder() {
  if (!oldestId) return;
  const levelParam = (currentFilter === 'ERROR' || currentFilter === 'WARNING') ? `&level=${currentFilter}` : '';
  const logs = await (await fetch(`/api/logs?before=${oldestId}&limit=200${levelParam}`)).json();
  logs.reverse();
  const scrollBefore = logScroll.scrollHeight;
  for (const l of logs) {
    container.insertBefore(renderLine(l), container.firstChild);
    allLogs.unshift(l);
    if (l.id < oldestId) oldestId = l.id;
  }
  totalCount += logs.length;
  countEl.textContent = totalCount;
  logScroll.scrollTop += logScroll.scrollHeight - scrollBefore;
  if (currentFilter !== 'all') applyClientFilter();
}

async function pollNew() {
  try {
    const logs = await (await fetch(`/api/logs/new?after=${newestId}`)).json();
    for (const l of logs) {
      container.appendChild(renderLine(l));
      allLogs.push(l);
      if (l.id > newestId) newestId = l.id;
      totalCount++;
    }
    if (logs.length > 0) {
      countEl.textContent = totalCount;
      if (currentFilter !== 'all') applyClientFilter();
      const atBottom = (logScroll.scrollTop + logScroll.clientHeight) >= logScroll.scrollHeight - 100;
      if (atBottom) logScroll.scrollTop = logScroll.scrollHeight;
    }
  } catch(e) {}
}

loadInitial();
setInterval(pollNew, 2000);

// Adjust log scroll area when terminal height changes
function adjustLogScroll() {
  const term = document.getElementById('terminal');
  const ls = document.getElementById('logScroll');
  if (term && ls) ls.style.bottom = term.offsetHeight + 'px';
}
adjustLogScroll();
window.addEventListener('resize', adjustLogScroll);

// ── Terminal ──
async function runCmd() {
  const input = document.getElementById('termInput');
  const cmd = input.value.trim();
  if (!cmd) return;

  const output = document.getElementById('termOutput');
  output.style.display = 'block';

  // Local help command
  if (cmd.toLowerCase() === 'help') {
    output.innerHTML = `<pre style="color:var(--text)">` +
      `ALLOWED COMMANDS\n` +
      `================\n\n` +
      `pip install &lt;pkg&gt;     Install a Python package\n` +
      `pip list              List installed packages\n` +
      `pip show &lt;pkg&gt;        Show package details\n` +
      `pip --version         Show pip version\n\n` +
      `supervisorctl status          Service status\n` +
      `supervisorctl restart &lt;svc&gt;   Restart a service\n` +
      `supervisorctl stop &lt;svc&gt;      Stop a service\n` +
      `supervisorctl start &lt;svc&gt;     Start a service\n` +
      `  Services: plugin-btc-15m, platform-dashboard\n\n` +
      `python3 --version     Python version\n` +
      `which python          Python paths\n` +
      `which pip             Pip path\n` +
      `df -h                 Disk usage\n` +
      `free -m               Memory usage\n` +
      `uptime                Server uptime\n` +
      `help                  Show this message</pre>`;
    input.value = '';
    adjustLogScroll();
    return;
  }

  output.innerHTML = '<pre style="color:var(--dim)">Running...</pre>';

  try {
    const resp = await fetch('/api/server/exec', {
      method: 'POST',
      headers:{'Content-Type':'application/json','X-CSRF-Token':_getCsrfToken()},
      body: JSON.stringify({command: cmd}),
    });
    const data = await resp.json();

    if (data.error) {
      output.innerHTML = `<pre style="color:var(--red)">${escHtml(data.error)}</pre>`;
      adjustLogScroll();
      return;
    }

    let html = `<pre>${escHtml(data.output || '(no output)')}</pre>`;
    if (data.exit_code !== 0) {
      html += `<div style="color:var(--red);font-size:11px;margin-top:2px;font-family:sans-serif">Exit code: ${data.exit_code}</div>`;
    }
    if (data.needs_restart) {
      html += `<button class="term-restart-btn" onclick="doTermRestart(this)">Restart Services</button>`;
    }
    output.innerHTML = html;
    input.value = '';
    adjustLogScroll();
  } catch(e) {
    output.innerHTML = `<pre style="color:var(--red)">${escHtml(e.toString())}</pre>`;
    adjustLogScroll();
  }
}

async function doTermRestart(btn) {
  btn.textContent = 'Restarting...';
  btn.disabled = true;
  fetch('/api/deploy/restart', {
    method: 'POST',
    headers:{'Content-Type':'application/json','X-CSRF-Token':_getCsrfToken()},
    body: JSON.stringify({services: ['plugin-btc-15m', 'platform-dashboard']})
  }).catch(() => {});
  btn.insertAdjacentHTML('afterend',
    `<div style="color:var(--green);font-size:11px;margin-top:4px;font-family:sans-serif">Restarted. Page will reload...</div>`);
  setTimeout(() => location.reload(), 3000);
}
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════
#  EMAIL DEPLOY — IMAP IDLE watches for .py attachments
# ═══════════════════════════════════════════════════════════════

_email_deploy_conn = None  # Reference to current IMAP connection for recheck

def _start_email_deploy():
    """Start background thread that watches Gmail via IMAP IDLE for deploy emails."""
    import imaplib, email as email_mod, shutil, subprocess, threading, time, socket
    global _email_deploy_conn

    def elog(level, msg):
        """Log to both stdout and the bot_logs DB table."""
        print(f"[EmailDeploy] {msg}")
        try:
            insert_log(level, f"[EmailDeploy] {msg}", "deploy")
        except Exception:
            pass

    def deploy_notify(title, body):
        """Send push notification with deploy results."""
        try:
            from push import send_to_all
            send_to_all(title, body[:200], tag="deploy", url="/")
            elog("INFO", f"Push notification sent: {title}")
        except Exception as e:
            elog("WARN", f"Push notification failed: {e}")

    imap_user = os.environ.get("DEPLOY_EMAIL", "")
    imap_pass = os.environ.get("DEPLOY_EMAIL_PASS", "")
    allowed_senders = [s.strip().lower() for s in os.environ.get("DEPLOY_ALLOWED_SENDERS", "").split(",") if s.strip()]

    if not imap_user or not imap_pass or not allowed_senders:
        print("[EmailDeploy] Disabled — set DEPLOY_EMAIL, DEPLOY_EMAIL_PASS, DEPLOY_ALLOWED_SENDERS in .env")
        return

    bot_dir = os.environ.get("BOT_DIR", "/opt/trading-platform")
    imap_host = os.environ.get("DEPLOY_IMAP_HOST", "imap.gmail.com")

    def deploy_files(attachments):
        """Deploy .py attachments. Returns (uploaded, errors) lists."""
        backup_dir = os.path.join(bot_dir, "_backup")
        os.makedirs(backup_dir, exist_ok=True)
        uploaded, errors = [], []

        for fname, content in attachments:
            if not fname.endswith('.py'):
                elog("WARN", f"Skipping non-.py file: {fname}")
                errors.append(f"{fname}: not a .py file, skipped")
                continue
            try:
                compile(content, fname, 'exec')
            except SyntaxError as e:
                elog("ERROR", f"Syntax error in {fname} line {e.lineno}: {e.msg}")
                errors.append(f"{fname}: syntax error line {e.lineno}: {e.msg}")
                continue
            dest = os.path.join(bot_dir, fname)
            if os.path.exists(dest):
                shutil.copy2(dest, os.path.join(backup_dir, fname))
                elog("INFO", f"Backed up existing {fname}")
            with open(dest, 'wb') as out:
                out.write(content)
            elog("INFO", f"Deployed {fname} ({len(content)} bytes)")
            uploaded.append(fname)

        if uploaded:
            with open(os.path.join(backup_dir, "_manifest.json"), "w") as mf:
                json.dump({"files": uploaded, "ts": now_utc()}, mf)

        return uploaded, errors

    def restart_services(services=None):
        """Restart services. If services is None, restarts both."""
        if services is None:
            services = ["plugin-btc-15m", "platform-dashboard"]
        results = {}
        for svc in services:
            try:
                r = subprocess.run(["supervisorctl", "restart", svc],
                                   capture_output=True, text=True, timeout=10)
                results[svc] = r.stdout.strip() or r.stderr.strip()
            except Exception as e:
                results[svc] = str(e)
        return results

    def run_pip_installs(body_text):
        """Extract and run pip: lines from email body. Returns list of result strings."""
        results = []
        for line in body_text.splitlines():
            line = line.strip()
            if line.lower().startswith('pip:'):
                pkg = line[4:].strip()
                if not pkg:
                    continue
                import re, sys as _sys
                if not re.match(r'^[a-zA-Z0-9_.>=<!\-\[\],\s]+$', pkg):
                    elog("WARN", f"pip: rejected invalid package name: {pkg}")
                    results.append(f"✗ pip: {pkg} — invalid characters")
                    continue
                pip_cmd = [_sys.executable, "-m", "pip", "install", pkg, "--break-system-packages"]
                elog("INFO", f"Running: {' '.join(pip_cmd)}")
                try:
                    r = subprocess.run(
                        pip_cmd,
                        capture_output=True, text=True, timeout=120
                    )
                    stdout_clean = r.stdout.strip()
                    stderr_clean = r.stderr.strip()
                    full_output = (stdout_clean + "\n" + stderr_clean).strip()
                    # Log full output
                    for ol in full_output.splitlines()[-5:]:
                        if ol.strip():
                            elog("INFO", f"  pip: {ol.strip()}")
                    if r.returncode == 0:
                        last_lines = [l for l in full_output.splitlines() if l.strip()][-3:]
                        result_str = f"✓ pip install {pkg}\n  " + "\n  ".join(last_lines)
                        elog("INFO", f"pip install {pkg} succeeded (exit code 0)")
                    else:
                        last_lines = [l for l in full_output.splitlines() if l.strip()][-5:]
                        result_str = f"✗ pip install {pkg} (exit {r.returncode})\n  " + "\n  ".join(last_lines)
                        elog("ERROR", f"pip install {pkg} failed (exit code {r.returncode})")
                    results.append(result_str)
                except Exception as e:
                    elog("ERROR", f"pip install {pkg} exception: {e}")
                    results.append(f"✗ pip install {pkg}: {e}")
        return results

    def run_service_commands(body_text):
        """Extract and run service commands from email body. Returns list of result strings."""
        results = []
        allowed_services = {"bot": "plugin-btc-15m", "dashboard": "platform-dashboard",
                           "plugin-btc-15m": "plugin-btc-15m", "platform-dashboard": "platform-dashboard",
                           "all": None}  # None = apply to all
        for line in body_text.splitlines():
            line = line.strip().lower()
            for prefix in ['restart:', 'start:', 'stop:']:
                if line.startswith(prefix):
                    action = prefix[:-1]  # restart, start, stop
                    target = line[len(prefix):].strip() or 'all'
                    if target not in allowed_services:
                        results.append(f"✗ {action}: unknown service '{target}' (use: bot, dashboard, all)")
                        elog("WARN", f"Unknown service: {target}")
                        continue
                    svc_name = allowed_services[target]
                    targets = [svc_name] if svc_name else ["plugin-btc-15m", "platform-dashboard"]
                    for svc in targets:
                        elog("INFO", f"Running: supervisorctl {action} {svc}")
                        try:
                            r = subprocess.run(["supervisorctl", action, svc],
                                             capture_output=True, text=True, timeout=15)
                            out = (r.stdout.strip() or r.stderr.strip())
                            results.append(f"✓ {action} {svc}: {out}")
                            elog("INFO", f"  {svc}: {out}")
                        except Exception as e:
                            results.append(f"✗ {action} {svc}: {e}")
                            elog("ERROR", f"  {svc}: {e}")
        return results

    def process_email(mail, num):
        """Process a single email: extract .py attachments, pip commands, deploy, reply."""
        _, data = mail.fetch(num, '(RFC822)')
        raw = data[0][1]
        msg = email_mod.message_from_bytes(raw)

        # Check sender
        from_raw = msg.get("From", "")
        from_addr = from_raw
        if "<" in from_raw:
            from_addr = from_raw.split("<")[1].split(">")[0]
        from_addr = from_addr.strip().lower()

        elog("INFO", f"Email received — From: {from_raw!r} → parsed: {from_addr}")

        if from_addr not in allowed_senders:
            elog("WARN", f"Ignored email from unauthorized sender: {from_addr} (allowed: {allowed_senders})")
            return

        subj = msg.get("Subject", "")
        elog("INFO", f"Processing email from {from_addr}: {subj}")

        # Extract body text and .py attachments
        body_text = ""
        attachments = []
        for part in msg.walk():
            if part.get_content_maintype() == 'multipart':
                continue
            fname = part.get_filename()
            if fname and fname.endswith('.py'):
                content = part.get_payload(decode=True)
                if content:
                    attachments.append((fname, content))
                    elog("INFO", f"Found attachment: {fname} ({len(content)} bytes)")
            elif not fname and part.get_content_type() in ('text/plain', 'text/html'):
                payload = part.get_payload(decode=True)
                if payload:
                    decoded = payload.decode(errors='replace')
                    if '<' in decoded:
                        import re as _re
                        decoded = _re.sub(r'<br\s*/?>', '\n', decoded, flags=_re.IGNORECASE)
                        decoded = _re.sub(r'<[^>]+>', '', decoded)
                        decoded = decoded.replace('&nbsp;', ' ').replace('&amp;', '&')
                    body_text += decoded + "\n"

        # Log what we found
        elog("INFO", f"Email body ({len(body_text)} chars): {body_text[:200].strip()!r}")
        pip_lines = [l.strip() for l in body_text.splitlines() if l.strip().lower().startswith('pip:')]
        elog("INFO", f"Email contents: {len(attachments)} .py files, {len(pip_lines)} pip commands")
        for pl in pip_lines:
            elog("INFO", f"  Found pip line: {pl}")

        # Check for help request
        body_lower = body_text.strip().lower()
        if body_lower in ('help', 'help\n', '?') or body_lower.startswith('help'):
            if not attachments:
                elog("INFO", "Help requested")
                deploy_notify("Email Deploy Help",
                    "Commands: pip: pkg, restart: all/bot/dashboard, stop: bot, start: bot. "
                    "Attach .py to deploy. See Settings → Email Deploy.")
                return

        lines = []

        # Handle service commands from body
        svc_results = run_service_commands(body_text)
        if svc_results:
            lines.append("SERVICE COMMANDS:")
            for r in svc_results:
                lines.append(f"  {r}")

        # Handle pip installs from body
        pip_results = run_pip_installs(body_text)
        if pip_results:
            lines.append("\nPIP INSTALLS:" if lines else "PIP INSTALLS:")
            for r in pip_results:
                lines.append(f"  {r}")

        # Handle .py deployments
        if attachments:
            uploaded, errors = deploy_files(attachments)
            if uploaded:
                lines.append("\nDEPLOYED:" if lines else "DEPLOYED:")
                for f in uploaded:
                    lines.append(f"  ✓ {f}")
            if errors:
                lines.append("\nERRORS:")
                for e in errors:
                    lines.append(f"  ✗ {e}")

        if not lines:
            elog("WARN", "No actions found in email")
            deploy_notify("Deploy: No Actions", "No .py files, pip commands, or service commands found")
            return

        # Send push notification BEFORE restart (restart kills this process)
        parts = []
        if svc_results: parts.append(f"{len(svc_results)} svc cmd{'s' if len(svc_results)>1 else ''}")
        if pip_results: parts.append(f"{len(pip_results)} pip")
        if attachments:
            ok = len([f for f in lines if '✓' in f])
            parts.append(f"{ok}/{len(attachments)} files deployed")
        deploy_notify("Deploy Complete", " | ".join(parts) if parts else "Done")
        elog("INFO", f"Done: {len(attachments)} files, {len(pip_results)} pip, {len(svc_results)} svc")

        # Restart AFTER notification (if files were deployed)
        if attachments and uploaded:
            time.sleep(0.5)  # Let push notification send
            # If only dashboard-only files deployed, skip bot restart
            dashboard_only_files = {'dashboard.py'}
            if all(f in dashboard_only_files for f in uploaded):
                elog("INFO", "Dashboard-only deploy — skipping bot restart")
                results = restart_services(["platform-dashboard"])
            else:
                results = restart_services()
            for svc, r in results.items():
                elog("INFO", f"Restart: {svc}: {r}")

    def idle_loop():
        """Main IMAP IDLE loop — reconnects on failure."""
        global _email_deploy_conn
        while True:
            mail = None
            try:
                elog("DEBUG", f"Connecting to {imap_host}...")
                mail = imaplib.IMAP4_SSL(imap_host, timeout=30)
                mail.login(imap_user, imap_pass)
                mail.select("INBOX")
                _email_deploy_conn = mail
                elog("DEBUG", "Connected. Watching for deploy emails...")

                while True:
                    # Check for unread emails
                    _, nums = mail.search(None, 'UNSEEN')
                    unread = [n for n in nums[0].split() if n]
                    if unread:
                        elog("INFO", f"Found {len(unread)} unread email(s)")
                    for num in unread:
                        try:
                            process_email(mail, num)
                            mail.store(num, '+FLAGS', '\\Seen')
                        except Exception as e:
                            elog("ERROR", f"Error processing email: {e}")

                    # IDLE — wait for new mail
                    tag = mail._new_tag()
                    mail.send(tag + b' IDLE\r\n')
                    # Read the continuation "+" response
                    mail.readline()

                    # Block waiting for server notification (25 min max per RFC)
                    mail.sock.settimeout(25 * 60)
                    got_mail = False
                    try:
                        while True:
                            line = mail.readline().decode(errors='replace')
                            if not line:
                                # Empty read = connection dropped by server
                                elog("DEBUG", "IDLE: empty readline — connection lost")
                                raise OSError("IDLE connection dropped")
                            if 'EXISTS' in line or 'RECENT' in line:
                                got_mail = True
                                break
                    except (socket.timeout, OSError):
                        pass  # Timeout or disconnect — re-IDLE

                    # End IDLE
                    mail.send(b'DONE\r\n')
                    # Consume the tagged OK response
                    try:
                        mail.readline()
                    except Exception:
                        pass

                    # Reset socket timeout for normal commands
                    mail.sock.settimeout(30)

            except Exception as e:
                elog("DEBUG", f"Connection error: {e}")
            finally:
                _email_deploy_conn = None
                try:
                    if mail:
                        mail.logout()
                except Exception:
                    pass

            elog("DEBUG", "Reconnecting in 10s...")
            time.sleep(10)

    thread = threading.Thread(target=idle_loop, daemon=True, name="email-deploy")
    thread.start()
    elog("INFO", f"Started — watching {imap_user} for emails from {', '.join(allowed_senders)}")


# ═══════════════════════════════════════════════════════════════
#  HEALTH CHECK
# ═══════════════════════════════════════════════════════════════

def _start_health_check():
    """Background thread that monitors bot heartbeat and alerts if it goes silent."""
    import threading
    import time as _time
    from datetime import datetime, timezone

    _alerted = False
    _alert_time = None

    def _hc_log(level, msg):
        try:
            insert_log(level, f"[HealthCheck] {msg}", "btc_15m")
        except Exception:
            print(f"[HealthCheck] [{level}] {msg}")

    def check_loop():
        nonlocal _alerted, _alert_time
        _time.sleep(30)  # Initial delay — let bot start up

        while True:
            try:
                enabled = get_config("btc_15m.health_check_enabled", False)
                if not enabled:
                    _alerted = False
                    _alert_time = None
                    _time.sleep(60)
                    continue

                timeout_min = get_config("btc_15m.health_check_timeout_min", 5) or 5

                state = get_bot_state()
                last_updated = state.get("last_updated", "")
                auto_trading = state.get("auto_trading", False)

                # Only check when bot is supposed to be running
                if not auto_trading:
                    _alerted = False
                    _alert_time = None
                    _time.sleep(60)
                    continue

                if not last_updated:
                    _time.sleep(60)
                    continue

                try:
                    last_dt = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
                except Exception:
                    _time.sleep(60)
                    continue

                age_secs = (datetime.now(timezone.utc) - last_dt).total_seconds()
                silent_mins = age_secs / 60

                if silent_mins >= timeout_min:
                    if not _alerted:
                        _alerted = True
                        _alert_time = _time.time()
                        _hc_log("WARNING", f"Bot unresponsive for {silent_mins:.1f} min "
                                           f"(threshold: {timeout_min} min)")
                        try:
                            from push import notify_health_check_down
                            notify_health_check_down(silent_mins)
                        except Exception as e:
                            _hc_log("ERROR", f"Failed to send alert: {e}")
                else:
                    if _alerted:
                        down_mins = (_time.time() - _alert_time) / 60 if _alert_time else silent_mins
                        _hc_log("INFO", f"Bot recovered after {down_mins:.1f} min")
                        try:
                            from push import notify_health_check_recovered
                            notify_health_check_recovered(down_mins)
                        except Exception as e:
                            _hc_log("ERROR", f"Failed to send recovery alert: {e}")
                        _alerted = False
                        _alert_time = None

            except Exception as e:
                _hc_log("ERROR", f"Health check error: {e}")

            _time.sleep(60)

    thread = threading.Thread(target=check_loop, daemon=True, name="health-check")
    thread.start()


# ═══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def _fix_supervisor_config():
    """Ensure supervisor auto-restarts services after crashes (including OOM SIGKILL).
    Checks both plugin-btc-15m and platform-dashboard configs for autorestart=true.
    Only modifies if needed. Runs once on dashboard startup."""
    import subprocess, glob, re

    # Find all supervisor config files for kalshi services
    search_paths = [
        "/etc/supervisor/conf.d/*.conf",
        "/etc/supervisord.d/*.conf",
        "/etc/supervisord.d/*.ini",
    ]
    config_files = set()
    for pattern in search_paths:
        for m in glob.glob(pattern):
            try:
                with open(m) as f:
                    if "kalshi" in f.read():
                        config_files.add(m)
            except Exception:
                continue

    if not config_files:
        print("[Supervisor] No kalshi config files found — skipping autorestart fix")
        return

    any_changed = False
    for config_path in sorted(config_files):
        try:
            with open(config_path) as f:
                content = f.read()
        except Exception as e:
            print(f"[Supervisor] Cannot read {config_path}: {e}")
            continue

        changed = False

        # Fix autorestart for all program sections
        if "autorestart=true" not in content:
            if "autorestart=" in content:
                content = re.sub(r'autorestart=\S+', 'autorestart=true', content)
            else:
                content = re.sub(r'(\[program:kalshi[^\]]*\])',
                                 r'\1\nautorestart=true', content)
            changed = True

        # Fix startretries (ensure at least 3)
        if "startretries=" not in content:
            content = content.replace("autorestart=true",
                                      "autorestart=true\nstartretries=5")
            changed = True

        if changed:
            try:
                with open(config_path, 'w') as f:
                    f.write(content)
                print(f"[Supervisor] Updated {config_path}: autorestart=true, startretries=5")
                any_changed = True
            except Exception as e:
                print(f"[Supervisor] Error writing {config_path}: {e}")
        else:
            print(f"[Supervisor] {config_path} already correct")

    if any_changed:
        try:
            subprocess.run(["supervisorctl", "reread"], capture_output=True, timeout=10)
            subprocess.run(["supervisorctl", "update"], capture_output=True, timeout=10)
            print("[Supervisor] Reloaded supervisor config")
        except Exception as e:
            print(f"[Supervisor] Error reloading: {e}")


def _install_watchdog_cron():
    """Install a cron job that checks every 2 minutes if the bot is running
    and restarts it via supervisorctl if not. This is the ultimate safety net
    against OOM kills, supervisor failures, and any other crash scenario.
    Idempotent — won't add duplicate entries."""
    import subprocess

    bot_dir = os.environ.get("BOT_DIR", "/opt/trading-platform")
    watchdog_script = os.path.join(bot_dir, "watchdog.sh")
    cron_marker = "# plugin-btc-15m-watchdog"

    # Create watchdog script
    script_content = f"""#!/bin/bash
# Auto-generated watchdog for plugin-btc-15m
# Checks if the bot process is running and restarts if not
BOT_LOG="{bot_dir}/bot.log"
if ! supervisorctl status plugin-btc-15m | grep -q RUNNING; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [Watchdog] Bot not running — restarting" >> "$BOT_LOG"
    supervisorctl start plugin-btc-15m >> "$BOT_LOG" 2>&1
fi
if ! supervisorctl status platform-dashboard | grep -q RUNNING; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [Watchdog] Dashboard not running — restarting" >> "$BOT_LOG"
    supervisorctl start platform-dashboard >> "$BOT_LOG" 2>&1
fi
"""
    try:
        with open(watchdog_script, 'w') as f:
            f.write(script_content)
        os.chmod(watchdog_script, 0o755)
    except Exception as e:
        print(f"[Watchdog] Error writing script: {e}")
        return

    # Check if cron entry already exists
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        existing_cron = result.stdout if result.returncode == 0 else ""
    except Exception:
        existing_cron = ""

    if cron_marker in existing_cron:
        print("[Watchdog] Cron job already installed")
        return

    # Add cron entry — runs every 2 minutes
    new_cron = existing_cron.rstrip() + f"\n*/2 * * * * {watchdog_script} {cron_marker}\n"
    try:
        proc = subprocess.run(["crontab", "-"], input=new_cron, capture_output=True,
                              text=True, timeout=5)
        if proc.returncode == 0:
            print(f"[Watchdog] Cron job installed — checks every 2 minutes")
        else:
            print(f"[Watchdog] Cron install error: {proc.stderr}")
    except Exception as e:
        print(f"[Watchdog] Error installing cron: {e}")


def main():
    init_db()
    # Migration not needed — fresh schema
    _fix_supervisor_config()
    _install_watchdog_cron()
    _start_email_deploy()
    _start_health_check()
    print(f"Dashboard starting on {DASHBOARD_HOST}:{DASHBOARD_PORT}")
    app.run(host=DASHBOARD_HOST, port=DASHBOARD_PORT, debug=False)


if __name__ == "__main__":
    main()