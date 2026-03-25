"""
dashboard.py — Platform dashboard shell.
Unified 5-tab dashboard with plugin component injection.
"""
import json, os, importlib, shutil, subprocess, sys
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify, Response, make_response, render_template_string
from functools import wraps
from config import DASHBOARD_HOST, DASHBOARD_PORT, DASHBOARD_USER, DASHBOARD_PASS, CT, ET, PLATFORM_DIR, DB_PATH
from db import (
    init_db, get_all_config, set_config, get_config, now_utc,
    enqueue_command, get_plugin_state, get_all_plugin_states, update_plugin_state,
    get_logs, get_push_log, get_bankroll_chart_data,
    save_push_subscription, remove_push_subscription_by_endpoint,
    insert_audit_log, insert_log, get_conn, rows_to_list, row_to_dict,
    get_candles, get_latest_regime_snapshot, get_regime_heartbeat, is_regime_worker_running,
)

app = Flask(__name__)


# ═══════════════════════════════════════════════════════════════
#  PLUGIN DISCOVERY
# ═══════════════════════════════════════════════════════════════

PLUGINS = []

def _discover_plugins():
    plugins_dir = os.path.join(os.path.dirname(__file__), "plugins")
    if not os.path.isdir(plugins_dir):
        return
    for name in sorted(os.listdir(plugins_dir)):
        init_path = os.path.join(plugins_dir, name, "__init__.py")
        if not os.path.isfile(init_path):
            continue
        try:
            mod = importlib.import_module(f"plugins.{name}")
            plugin = None
            for attr_name in dir(mod):
                cls = getattr(mod, attr_name)
                if isinstance(cls, type) and hasattr(cls, 'plugin_id') and cls is not MarketPlugin:
                    try:
                        plugin = cls()
                        break
                    except Exception:
                        pass
            if not plugin:
                # Fallback: try importing plugin.py directly
                try:
                    pmod = importlib.import_module(f"plugins.{name}.plugin")
                    for attr_name in dir(pmod):
                        cls = getattr(pmod, attr_name)
                        if isinstance(cls, type) and hasattr(cls, 'plugin_id'):
                            try:
                                plugin = cls()
                                break
                            except Exception:
                                pass
                except Exception:
                    pass
            if plugin:
                PLUGINS.append(plugin)
                plugin.register_routes(app)
        except Exception as e:
            print(f"[dashboard] Failed to load plugin {name}: {e}")

from plugin_base import MarketPlugin
# Plugin discovery deferred to after all definitions — see bottom of file


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


# ═══════════════════════════════════════════════════════════════
#  TIME HELPERS
# ═══════════════════════════════════════════════════════════════

def to_central(iso_str):
    """Convert ISO timestamp string to Central Time display string."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(CT).strftime("%m/%d %I:%M:%S %p CT")
    except Exception:
        return iso_str

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
<title>Login — Trading Platform</title>
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
  <h2 id="formTitle">Trading Platform</h2>

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
  document.getElementById('formTitle').textContent = 'Trading Platform';
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
#  PLATFORM API ENDPOINTS
# ═══════════════════════════════════════════════════════════════

# ── Auth endpoints ────────────────────────────────────────────

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


# ── State ─────────────────────────────────────────────────────

@app.route("/api/state")
@requires_auth
def api_state():
    """Return merged state from all plugins."""
    plugins = get_all_plugin_states()
    config = get_all_config()

    # Build merged response
    result = {
        "plugins": [],
        "bankroll_cents": 0,
        "last_updated": None,
    }

    for ps in plugins:
        plugin_data = {
            "plugin_id": ps.get("plugin_id"),
            "status": ps.get("status", "stopped"),
            "status_detail": ps.get("status_detail", ""),
            "last_updated": ps.get("last_updated"),
            "last_updated_ct": to_central(ps.get("last_updated", "")),
            "state": ps.get("state", {}),
        }
        # Include plugin-specific config
        pid = ps.get("plugin_id", "")
        plugin_data["config"] = {k: v for k, v in config.items() if k.startswith(f"{pid}.")}
        result["plugins"].append(plugin_data)

        # Merge bankroll from plugin state
        state = ps.get("state", {})
        if "bankroll_cents" in state:
            result["bankroll_cents"] += state["bankroll_cents"]
        if ps.get("last_updated"):
            if not result["last_updated"] or ps["last_updated"] > result["last_updated"]:
                result["last_updated"] = ps["last_updated"]

    result["last_updated_ct"] = to_central(result.get("last_updated", ""))

    # Include regime heartbeat
    try:
        hb = get_regime_heartbeat("BTC")
        if hb:
            result["regime_heartbeat"] = hb
            result["regime_worker_running"] = is_regime_worker_running("BTC")
    except Exception:
        pass

    return jsonify(result)


@app.route("/api/config")
@requires_auth
def api_config():
    cfg = get_all_config()
    cfg.pop("anthropic_api_key", None)  # Never expose via config API
    cfg.pop("destruction_pin_hash", None)
    cfg.pop("_session_salt", None)
    cfg.pop("dashboard_pass_hash", None)
    return jsonify(cfg)


@app.route("/api/config", methods=["POST"])
@requires_auth
def api_set_config():
    data = request.get_json() or {}
    for k, v in data.items():
        set_config(k, v)
    # Notify relevant plugin(s) of config change
    for k in data:
        parts = k.split(".", 1)
        if len(parts) == 2:
            plugin_id = parts[0]
            enqueue_command(plugin_id, "update_config", data)
            break
    else:
        # Platform-level config, notify all plugins
        for p in PLUGINS:
            enqueue_command(p.plugin_id, "update_config", data)
    return jsonify({"ok": True})


@app.route("/api/command", methods=["POST"])
@requires_auth
def api_command():
    data = request.get_json() or {}
    plugin_id = data.get("plugin_id", "")
    command = data.get("command", "")
    params = data.get("params", {})

    if not plugin_id or not command:
        return jsonify({"error": "plugin_id and command required"}), 400

    cmd_id = enqueue_command(plugin_id, command, params)
    insert_audit_log("command", f"plugin={plugin_id} cmd={command}", ip=request.remote_addr or "")
    return jsonify({"ok": True, "command_id": cmd_id})


# ── Logs ──────────────────────────────────────────────────────

@app.route("/api/logs")
@requires_auth
def api_logs():
    before_id = request.args.get("before", type=int)
    limit = request.args.get("limit", 100, type=int)
    level = request.args.get("level", type=str)
    source = request.args.get("source", type=str)
    logs = get_logs(before_id=before_id, limit=limit, level=level, source=source)
    for l in logs:
        l["ts_ct"] = to_central(l.get("ts", ""))
    return jsonify(logs)


@app.route("/api/logs/after")
@requires_auth
def api_logs_after():
    after_id = request.args.get("after", 0, type=int)
    source = request.args.get("source", type=str)
    level = request.args.get("level", type=str)
    with get_conn() as c:
        sql = "SELECT * FROM log_entries WHERE id > ?"
        params = [after_id]
        if source:
            sql += " AND source = ?"
            params.append(source)
        if level:
            sql += " AND level = ?"
            params.append(level)
        sql += " ORDER BY id ASC LIMIT 200"
        rows = c.execute(sql, params).fetchall()
    logs = rows_to_list(rows)
    for l in logs:
        l["ts_ct"] = to_central(l.get("ts", ""))
    return jsonify(logs)


# ── Push notifications ────────────────────────────────────────

@app.route("/manifest.json")
def manifest_json():
    return jsonify({
        "name": "Trading Platform",
        "short_name": "Trading",
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
  let data = {title: 'Trading Platform', body: 'Notification', tag: 'default', url: '/'};
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


# ── Charts ────────────────────────────────────────────────────

@app.route("/api/bankroll/chart")
@requires_auth
def api_bankroll_chart():
    hours = request.args.get("hours", type=int)
    data = get_bankroll_chart_data(hours)
    return jsonify(data)


@app.route("/api/regime/current")
@requires_auth
def api_regime_current():
    snap = get_latest_regime_snapshot("BTC")
    if not snap:
        return jsonify({})
    snap["captured_ct"] = to_central(snap.get("captured_at", ""))
    return jsonify(snap)


@app.route("/api/regime/candles")
@requires_auth
def api_regime_candles():
    """Return recent BTC candles for chart. Default 60 min."""
    minutes = request.args.get("minutes", 60, type=int)
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    candles = get_candles(asset="BTC", since=since, limit=minutes + 5)
    return jsonify([{"ts": c["ts"], "close": c["close"], "high": c["high"],
                     "low": c["low"], "volume": c.get("volume", 0)} for c in candles])


# ── System ────────────────────────────────────────────────────

@app.route("/api/system_stats")
@requires_auth
def api_system_stats():
    """Get server resource usage: disk, CPU, memory, network bandwidth."""
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
        if os.path.isfile(DB_PATH):
            db_mb = os.path.getsize(DB_PATH) / (1024**2)
            wal_path = DB_PATH + "-wal"
            if os.path.isfile(wal_path):
                db_mb += os.path.getsize(wal_path) / (1024**2)
            stats["disk"]["db_mb"] = round(db_mb, 1)
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

        cpu1_total, cpu1_idle = _read_cpu()
        net1_rx, net1_tx = _read_net()
        _t.sleep(0.5)
        cpu2_total, cpu2_idle = _read_cpu()
        net2_rx, net2_tx = _read_net()

        dt = cpu2_total - cpu1_total
        di = cpu2_idle - cpu1_idle
        cpu_pct = round((1 - di / dt) * 100, 1) if dt > 0 else 0
        stats["cpu"] = {"pct": cpu_pct}
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        stats["cpu"]["load_1m"] = float(parts[0])
        stats["cpu"]["load_5m"] = float(parts[1])
        stats["cpu"]["load_15m"] = float(parts[2])

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


@app.route("/api/services")
@requires_auth
def api_services():
    """Get supervisor status for all services."""
    try:
        r = subprocess.run(["supervisorctl", "status"],
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


@app.route("/api/services/control", methods=["POST"])
@requires_auth
def api_services_control():
    """Control individual supervisor services: start, stop, restart."""
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
            r = subprocess.run(["supervisorctl", action, svc],
                              capture_output=True, text=True, timeout=10)
            results[svc] = r.stdout.strip() or r.stderr.strip()
            insert_log("INFO", f"[Services] {action} {svc}: {results[svc]}", "platform")
        except Exception as e:
            results[svc] = str(e)
            insert_log("ERROR", f"[Services] {action} {svc}: {e}", "platform")
    return jsonify(results)


@app.route("/api/deploy/upload", methods=["POST"])
@requires_auth
def api_deploy_upload():
    """Upload .py files to the platform directory. Backs up existing files first."""
    backup_dir = os.path.join(PLATFORM_DIR, "_backup")
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
        dest = os.path.join(PLATFORM_DIR, f.filename)
        if os.path.exists(dest):
            shutil.copy2(dest, os.path.join(backup_dir, f.filename))
            backed_up.append(f.filename)
        with open(dest, 'wb') as out:
            out.write(content)
        uploaded.append(f.filename)

    if backed_up:
        with open(os.path.join(backup_dir, "_manifest.json"), "w") as mf:
            json.dump({"files": backed_up, "ts": now_utc()}, mf)

    if uploaded:
        insert_audit_log("deploy_upload", f"files={','.join(uploaded)}", ip=request.remote_addr or "")

    return jsonify({"uploaded": uploaded, "errors": errors, "backed_up": backed_up})


@app.route("/api/backup", methods=["POST"])
@requires_auth
def api_backup():
    """Create a database backup."""
    try:
        backup_dir = os.path.join(PLATFORM_DIR, "backups")
        os.makedirs(backup_dir, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(backup_dir, f"platform_{ts}.db")
        shutil.copy2(DB_PATH, backup_path)
        # Also copy WAL if exists
        wal_path = DB_PATH + "-wal"
        if os.path.isfile(wal_path):
            shutil.copy2(wal_path, backup_path + "-wal")
        insert_audit_log("backup", f"path={backup_path}", ip=request.remote_addr or "")
        size_mb = round(os.path.getsize(backup_path) / (1024**2), 1)
        return jsonify({"ok": True, "path": backup_path, "size_mb": size_mb})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/deploy/restart", methods=["POST"])
@requires_auth
def api_deploy_restart():
    """Restart bot and/or dashboard services.
    Sends response BEFORE restarting dashboard to avoid 502."""
    import threading
    data = request.get_json() or {}
    services = data.get("services", ["plugin-btc-15m", "platform-dashboard"])

    results = {}
    bot_services = [s for s in services if s != "platform-dashboard"]
    for svc in bot_services:
        try:
            r = subprocess.run(["supervisorctl", "restart", svc],
                               capture_output=True, text=True, timeout=10)
            results[svc] = r.stdout.strip() or r.stderr.strip()
        except Exception as e:
            results[svc] = str(e)

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


# ── Security ──────────────────────────────────────────────────

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
            rows = c.execute("SELECT * FROM audit_log ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return jsonify({"entries": rows_to_list(rows)})
    except Exception as e:
        return jsonify({"entries": [], "error": str(e)})


# ── Reset ─────────────────────────────────────────────────────

@app.route("/api/reset", methods=["POST"])
@requires_auth
def api_reset():
    """Granular reset endpoint. Destructive scopes require PIN + auto-backup."""
    data = request.json or {}
    scope = data.get("scope", "")
    ip = request.remote_addr or ""

    # Destructive scopes need PIN + auto-backup
    if scope in _DESTRUCTIVE_SCOPES:
        pin = data.get("pin", "")
        if not _check_destruction_pin(pin):
            insert_audit_log("reset_pin_failed", f"scope={scope}", ip=ip, success=False)
            return jsonify({"error": "Invalid destruction PIN", "pin_required": True}), 403
        # Auto backup
        try:
            backup_dir = os.path.join(PLATFORM_DIR, "backups")
            os.makedirs(backup_dir, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            backup_path = os.path.join(backup_dir, f"pre_{scope}_{ts}.db")
            shutil.copy2(DB_PATH, backup_path)
            insert_audit_log("auto_backup", f"scope={scope}", ip=ip)
        except Exception:
            pass

    insert_audit_log("reset", f"scope={scope}", ip=ip)

    try:
        if scope == "settings":
            with get_conn() as c:
                keep = ('destruction_pin_hash', '_session_salt',
                        'dashboard_pass_hash')
                rows = c.execute("SELECT key FROM bot_config").fetchall()
                for r in rows:
                    if r["key"] not in keep:
                        c.execute("DELETE FROM bot_config WHERE key = ?", (r["key"],))
            return jsonify({"ok": True, "scope": "settings", "msg": "Settings reset to defaults"})

        elif scope == "regime_engine":
            with get_conn() as c:
                c.execute("DELETE FROM regime_snapshots")
                c.execute("DELETE FROM baselines")
                c.execute("DELETE FROM candles")
                try:
                    c.execute("DELETE FROM regime_stability_log")
                except Exception:
                    pass
                try:
                    c.execute("DELETE FROM regime_heartbeat")
                except Exception:
                    pass
            return jsonify({"ok": True, "scope": "regime_engine", "msg": "Regime engine data wiped"})

        elif scope == "full":
            with get_conn() as c:
                # Platform tables
                platform_tables = ['bankroll_snapshots', 'push_log', 'log_entries',
                                   'bot_commands', 'regime_snapshots', 'baselines',
                                   'candles', 'regime_stability_log', 'regime_heartbeat',
                                   'audit_log']
                # Plugin tables (btc_15m)
                plugin_tables = ['btc15m_trades', 'btc15m_price_path',
                                 'btc15m_exit_simulations', 'btc15m_regime_opportunities',
                                 'btc15m_regime_stats', 'btc15m_hourly_stats',
                                 'btc15m_market_observations', 'btc15m_strategy_results',
                                 'btc15m_live_prices', 'btc15m_markets',
                                 'btc15m_confidence_factors', 'btc15m_confidence_calibration',
                                 'btc15m_edge_calibration', 'btc15m_btc_probability_surface',
                                 'btc15m_feature_importance']
                for t in platform_tables + plugin_tables:
                    try:
                        c.execute(f"DELETE FROM {t}")
                    except Exception:
                        pass
                # Reset config but keep auth-related keys
                keep = ('destruction_pin_hash', '_session_salt',
                        'dashboard_pass_hash')
                rows = c.execute("SELECT key FROM bot_config").fetchall()
                for r in rows:
                    if r["key"] not in keep:
                        c.execute("DELETE FROM bot_config WHERE key = ?", (r["key"],))
            # Reset all plugin states
            for p in PLUGINS:
                update_plugin_state(p.plugin_id, {
                    "status": "stopped",
                    "status_detail": "Full reset",
                    "state": {}
                })
            return jsonify({"ok": True, "scope": "full", "msg": "Complete wipe — all data cleared"})

        else:
            return jsonify({"error": f"Unknown scope: {scope}"}), 400

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e), "msg": f"Reset failed: {e}"}), 500


# ═══════════════════════════════════════════════════════════════
#  MAIN PAGE ROUTE
# ═══════════════════════════════════════════════════════════════

@app.route("/app.js")
@requires_auth
def app_js():
    """Serve all JS as external file to avoid inline script size issues on iOS."""
    platform_js = _PLATFORM_JS
    plugin_js = "\n".join(p.render_js() for p in PLUGINS if hasattr(p, 'render_js'))
    js = platform_js + "\n" + plugin_js
    resp = Response(js, content_type="application/javascript")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/")
@requires_auth
def index():
    # Collect plugin components
    header_html = "\n".join(p.render_header_html() for p in PLUGINS)
    home_cards = "\n".join(p.render_home_card_html() for p in PLUGINS)
    trade_templates = "\n".join(p.render_trade_card_template() for p in PLUGINS)
    regime_configs = "\n".join(p.render_regime_config_html() for p in PLUGINS)
    stats_sections = "\n".join(p.render_stats_section_html() for p in PLUGINS)
    settings_sections = "\n".join(p.render_settings_html() for p in PLUGINS)

    html = MAIN_HTML
    html = html.replace("<!-- PLUGIN_HEADER -->", header_html)
    html = html.replace("<!-- PLUGIN_HOME_CARDS -->", home_cards)
    html = html.replace("<!-- PLUGIN_TRADE_TEMPLATES -->", trade_templates)
    html = html.replace("<!-- PLUGIN_REGIME_CONFIGS -->", regime_configs)
    html = html.replace("<!-- PLUGIN_STATS_SECTIONS -->", stats_sections)
    html = html.replace("<!-- PLUGIN_SETTINGS -->", settings_sections)

    resp = Response(html, content_type="text/html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


# ═══════════════════════════════════════════════════════════════
#  MAIN HTML TEMPLATE
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
<title>Trading Platform</title>
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
  .mode-btn.m-active-observe { background: rgba(227,179,65,0.12); color: var(--yellow);
    border-color: rgba(227,179,65,0.4); }
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
                padding: 10px; margin-bottom: 8px; border-left: 3px solid var(--border); }
  .trade-card.tc-win { border-left-color: var(--green); }
  .trade-card.tc-loss { border-left-color: var(--red); }
  .trade-card.tc-skip { border-left-color: var(--dim); }
  .trade-card.tc-cashout { border-left-color: var(--red); }
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
  .tc-tag.tag-cashout { background: rgba(248,81,73,0.1); color: var(--red); }
  .tc-tag.tag-skip { background: #1a1a2a; color: var(--dim); }
  .tc-tag.tag-incomplete { background: rgba(248,81,73,0.15); color: var(--red); }
  .tc-tag.tag-yes { background: #1a2a1a; color: var(--green); }
  .tc-tag.tag-no { background: #2a1a1a; color: var(--red); }
  .tc-tag.tc-tag.tag-open { background: #1a2a3a; color: var(--blue); }
  .tc-tag.tag-shadow { background: #2a1a3a; color: #a371f7; }
  .tc-tag.tag-error { background: #2a2010; color: #d29922; }
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
  .ctrl-icon { background: none; border: none; cursor: pointer; padding: 0;
               -webkit-tap-highlight-color: transparent; transition: transform 0.1s;
               display: flex; align-items: center; justify-content: center; }
  .ctrl-icon:active { transform: scale(0.9); }
  .ctrl-icon:disabled { opacity: 0.4; cursor: not-allowed; }
  .ctrl-icon:disabled:active { transform: none; }
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
  .stat-grid { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
    padding: 10px 8px; margin-bottom: 8px;
    display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 6px; }
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
  .proj-table { width: 100%; font-size: 12px; margin-top: 8px; }
  .proj-table th { color: var(--dim); text-align: left; padding: 4px; font-weight: normal;
                   text-transform: uppercase; font-size: 10px; border-bottom: 1px solid var(--border); }
  .proj-table td { padding: 4px; font-family: monospace; }
  .proj-table .current-round { background: rgba(88,166,255,0.1); }
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
  .tab-btn svg { width: 26px; height: 26px; }
  .tab-btn-main { flex: 0 0 auto; width: 62px; padding: 0; }
  .tab-btn-main:active { opacity: 0.8; }
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
  @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
</style>
</head>
<body>

<!-- Sticky Header -->
<div id="stickyHeader">
  <div class="hdr-row">
    <div style="display:flex;align-items:center;gap:6px;flex:1;min-width:0;overflow:hidden">
      <span class="status-dot" id="statusDot"></span>
      <strong id="statusText" style="font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">Platform v4...</strong>
    </div>
    <span id="hdrTimer" class="dim" style="font-size:11px;font-family:monospace;white-space:nowrap;flex-shrink:0;margin-left:6px;display:none"></span>
  </div>
  <div style="display:flex;justify-content:space-between;align-items:center;margin-top:4px">
    <div id="statusSub" class="dim" style="font-size:12px;line-height:1.4;flex:1;min-width:0"></div>
    <div id="hdrBankroll" onclick="openBankrollModal()" style="display:flex;align-items:center;gap:6px;font-family:monospace;font-size:13px;cursor:pointer;color:var(--text);background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:4px 10px;-webkit-tap-highlight-color:transparent;flex-shrink:0;margin-left:8px">
      <span id="hdrBal" style="font-weight:700;font-size:15px">&mdash;</span>
      <span id="hdrPnl" class="dim" style="font-size:11px"></span>
    </div>
  </div>
  <!-- PLUGIN_HEADER -->
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
      <div class="dim" style="font-size:12px">Effective Balance</div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:12px">
      <div style="text-align:center;padding:8px;background:var(--bg);border-radius:6px">
        <div style="font-size:16px;font-weight:600;font-family:monospace" id="bkmTotal">$0.00</div>
        <div class="dim" style="font-size:10px">Total</div>
      </div>
      <div style="text-align:center;padding:8px;background:var(--bg);border-radius:6px">
        <div style="font-size:16px;font-weight:600;font-family:monospace;color:var(--yellow)" id="bkmLocked">$0.00</div>
        <div class="dim" style="font-size:10px">Locked</div>
      </div>
      <div style="text-align:center;padding:8px;background:var(--bg);border-radius:6px">
        <div style="font-size:16px;font-weight:600;font-family:monospace" id="bkmInTrade">$0.00</div>
        <div class="dim" style="font-size:10px">In Trade</div>
      </div>
    </div>

    <!-- P&L per plugin -->
    <div id="bkmPluginPnl" style="margin-bottom:12px"></div>

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

    <!-- Lock/Unlock -->
    <div style="border-top:1px solid var(--border);padding-top:8px">
      <span class="detail-toggle" onclick="toggleDetail('bkmLockSection')">&#9656; Lock / Unlock Funds</span>
      <div class="detail-section" id="bkmLockSection">
        <div class="dim" style="font-size:11px;margin-bottom:8px">Locked funds excluded from trading.</div>
        <div class="input-row">
          <label>Amount $</label>
          <input type="number" id="lockAmount" min="0" step="10" value="100">
        </div>
        <div style="display:flex;gap:8px;margin-top:6px">
          <button class="act-btn act-btn-yellow act-btn-sm" style="flex:1"
                  onclick="lockFunds(parseFloat(document.getElementById('lockAmount').value))">+ Lock</button>
          <button class="act-btn act-btn-dim act-btn-sm" style="flex:1"
                  onclick="lockFunds(-parseFloat(document.getElementById('lockAmount').value))">- Unlock</button>
        </div>
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
  <!-- PLUGIN_HOME_CARDS -->
</div>

<!-- ═══ PAGE: TRADES ═══ -->
<div id="pageTrades" class="page" style="padding:0 16px">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
    <div class="dim" style="font-size:10px;font-weight:600;letter-spacing:0.5px">TRADES</div>
  </div>
  <!-- PLUGIN_TRADE_TEMPLATES -->
  <div id="tradeList"></div>
  <div id="tradeLoadMore" style="display:none;text-align:center;padding:16px">
    <button onclick="loadMoreTrades()" class="btn btn-dim" style="font-size:12px;padding:8px 16px;width:auto">Load more</button>
  </div>
</div>

<!-- ═══ PAGE: REGIMES ═══ -->
<div id="pageRegimes" class="page" style="padding:0 16px">
  <!-- BTC price and chart (platform-owned) -->
  <div style="display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:8px">
    <div>
      <div class="dim" style="font-size:10px;font-weight:600;letter-spacing:0.5px">BITCOIN</div>
      <div id="btcPriceMain" style="font-size:24px;font-weight:700;font-family:monospace">&mdash;</div>
      <div id="btcReturns" class="dim" style="font-size:11px"></div>
    </div>
    <div class="filter-chips" style="margin:0">
      <button class="chip active" data-btcrange="60" onclick="loadBtcChart(60,this)">1h</button>
      <button class="chip" data-btcrange="240" onclick="loadBtcChart(240,this)">4h</button>
      <button class="chip" data-btcrange="1440" onclick="loadBtcChart(1440,this)">24h</button>
    </div>
  </div>
  <div style="position:relative;margin-bottom:12px">
    <canvas id="btcChart" style="width:100%;height:160px;border-radius:6px;background:var(--card);border:1px solid var(--border)"></canvas>
  </div>
  <!-- Current regime box -->
  <div style="background:var(--card);border:1px solid var(--border);border-radius:8px;padding:10px;margin-bottom:12px;border-left:3px solid var(--blue)" id="regimeCurrentBox">
    <div class="dim" style="font-size:10px;font-weight:600;letter-spacing:0.5px;margin-bottom:6px">CURRENT REGIME</div>
    <div id="regimeCurrentContent"><div class="dim">Loading...</div></div>
  </div>
  <!-- Plugin regime configs -->
  <!-- PLUGIN_REGIME_CONFIGS -->
</div>

<!-- ═══ PAGE: STATS ═══ -->
<div id="pageStats" class="page" style="padding:0 16px">
  <!-- PLUGIN_STATS_SECTIONS -->
</div>

<!-- ═══ PAGE: SETTINGS ═══ -->
<div id="pageSettings" class="page" style="padding:0 12px">
  <!-- Plugin settings -->
  <!-- PLUGIN_SETTINGS -->

  <!-- ─── NOTIFICATIONS (platform-owned) ─── -->
  <div class="settings-card">
    <div class="sc-title">NOTIFICATIONS</div>
    <div id="pushStatus" class="dim" style="margin-bottom:6px;font-size:11px">Checking...</div>
    <button class="btn btn-blue" id="pushToggleBtn" onclick="togglePush()" style="display:none;margin-bottom:8px">Enable Notifications</button>
  </div>

  <!-- ─── SECURITY (platform-owned) ─── -->
  <div class="settings-card">
    <div class="sc-title">SECURITY</div>

    <div class="sc-sub">CHANGE PASSWORD</div>
    <div class="input-row"><label>Current</label><input type="password" id="secOldPass"></div>
    <div class="input-row"><label>New</label><input type="password" id="secNewPass"></div>
    <div class="input-row"><label>Confirm</label><input type="password" id="secConfPass"></div>
    <button class="act-btn act-btn-blue act-btn-sm" style="margin-top:8px" onclick="_secChangePass()">Update Password</button>

    <div class="sc-sub">SESSION CONTROL</div>
    <button class="act-btn act-btn-yellow act-btn-sm" onclick="_secInvalidate()">Invalidate All Sessions</button>
    <div class="sc-hint">Signs out all devices. You will stay logged in.</div>

    <div class="sc-sub">DESTRUCTION PIN</div>
    <div class="dim" style="font-size:11px;margin-bottom:6px">Required for destructive resets. <span id="pinStatus"></span></div>
    <div class="input-row"><label>Current PIN</label><input type="password" id="pinCurrent" maxlength="8" inputmode="numeric"></div>
    <div class="input-row"><label>New PIN</label><input type="password" id="pinNew" maxlength="8" inputmode="numeric"></div>
    <button class="act-btn act-btn-dim act-btn-sm" style="margin-top:6px" onclick="_secSetPin()">Set PIN</button>

    <div class="sc-sub">AUDIT LOG</div>
    <div id="auditLogContent" style="max-height:200px;overflow-y:auto;font-size:11px">
      <div class="dim">Loading...</div>
    </div>
    <button class="act-btn act-btn-dim act-btn-sm" style="margin-top:6px" onclick="loadAuditLog()">Refresh</button>
  </div>

  <!-- ─── SERVER (platform-owned) ─── -->
  <div class="settings-card">
    <div class="sc-title" style="display:flex;justify-content:space-between;align-items:center">
      SERVER
      <span id="srvUptime" class="dim" style="font-size:10px;font-weight:400"></span>
    </div>
    <div id="srvStats" style="font-size:12px"><div class="dim" style="text-align:center;padding:8px">Loading...</div></div>
  </div>

  <!-- ─── SERVICES (platform-owned) ─── -->
  <div class="settings-card">
    <div class="sc-title">SERVICES</div>
    <div id="svcStatus" style="font-size:11px;margin-bottom:10px">
      <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--border)">
        <div><strong>Dashboard</strong> <span id="svcDashStatus" class="dim">&mdash;</span></div>
        <div style="display:flex;gap:4px">
          <button class="btn btn-dim" style="font-size:10px;padding:3px 8px;width:auto;margin:0" onclick="svcControl('start','platform-dashboard')">Start</button>
          <button class="btn btn-dim" style="font-size:10px;padding:3px 8px;width:auto;margin:0" onclick="svcControl('stop','platform-dashboard')">Stop</button>
          <button class="btn btn-dim" style="font-size:10px;padding:3px 8px;width:auto;margin:0" onclick="svcControl('restart','platform-dashboard')">Restart</button>
        </div>
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0">
        <div><strong>BTC 15m Bot</strong> <span id="svcBotStatus" class="dim">&mdash;</span></div>
        <div style="display:flex;gap:4px">
          <button class="btn btn-dim" style="font-size:10px;padding:3px 8px;width:auto;margin:0" onclick="svcControl('start','plugin-btc-15m')">Start</button>
          <button class="btn btn-dim" style="font-size:10px;padding:3px 8px;width:auto;margin:0" onclick="svcControl('stop','plugin-btc-15m')">Stop</button>
          <button class="btn btn-dim" style="font-size:10px;padding:3px 8px;width:auto;margin:0" onclick="svcControl('restart','plugin-btc-15m')">Restart</button>
        </div>
      </div>
    </div>
    <button class="btn btn-blue" style="width:100%;margin-bottom:8px" onclick="svcControl('restart','all')">Restart All Services</button>

    <!-- Deploy section -->
    <div class="sc-sub">DEPLOY CODE</div>
    <div style="display:flex;gap:8px;align-items:center">
      <label style="flex:1;display:flex;align-items:center;justify-content:center;gap:6px;padding:12px;background:var(--bg);border:1px dashed var(--border);border-radius:6px;cursor:pointer;font-size:13px;color:var(--dim)">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M17 8l-5-5-5 5M12 3v12"/></svg>
        <span id="deployFileLabel">Upload .py files</span>
        <input type="file" id="deployFiles" accept=".py" multiple style="display:none" onchange="onDeployFilesSelected(this)">
      </label>
    </div>
    <div id="deployFileList" style="margin-top:6px;font-size:11px"></div>
    <div style="display:flex;gap:8px;margin-top:8px">
      <button class="btn btn-blue" id="deployUploadBtn" onclick="doDeploy()" style="flex:1;display:none">Upload &amp; Restart</button>
    </div>
    <div id="deployStatus" style="margin-top:6px;font-size:11px"></div>
  </div>

  <!-- ─── DATABASE (platform-owned) ─── -->
  <div class="settings-card">
    <div class="sc-title">DATABASE</div>
    <button class="act-btn act-btn-blue" onclick="doBackup()">Backup Now</button>
  </div>

  <!-- ─── RESET (platform-owned) ─── -->
  <div class="settings-card" style="border-color:rgba(248,81,73,0.2)">
    <span class="detail-toggle" onclick="toggleDetail('resetSection')" style="color:var(--red);margin-top:0">&#9656; Reset Options</span>
    <div class="detail-section" id="resetSection">
      <div style="margin-bottom:8px">
        <div class="dim" style="font-size:11px;margin-bottom:6px">Settings reset clears all config back to defaults.</div>
        <button class="act-btn act-btn-yellow act-btn-sm" onclick="doReset('settings')">Reset Settings</button>
      </div>
      <div style="margin-bottom:8px">
        <div class="dim" style="font-size:11px;margin-bottom:6px">Regime engine wipe clears candles, baselines, and regime snapshots.</div>
        <div class="input-row"><label>PIN</label><input type="password" id="resetPinRegime" maxlength="8" inputmode="numeric"></div>
        <button class="act-btn act-btn-red act-btn-sm" style="margin-top:6px" onclick="doReset('regime_engine', document.getElementById('resetPinRegime').value)">Wipe Regime Engine</button>
      </div>
      <div>
        <div class="dim" style="font-size:11px;margin-bottom:6px">Full wipe clears ALL data. Auto-backup created first.</div>
        <div class="input-row"><label>PIN</label><input type="password" id="resetPinFull" maxlength="8" inputmode="numeric"></div>
        <button class="act-btn act-btn-red act-btn-sm" style="margin-top:6px" onclick="doReset('full', document.getElementById('resetPinFull').value)">Full Wipe</button>
      </div>
    </div>
  </div>

  <!-- Links -->
  <div style="display:flex;justify-content:space-between;align-items:center;padding:0 4px;margin-bottom:12px">
    <a href="/api/logs" style="font-size:12px">View Logs</a>
    <button onclick="doLogout()" style="background:none;border:1px solid rgba(248,81,73,0.3);border-radius:6px;padding:6px 16px;color:var(--red);cursor:pointer;font-size:12px">Log Out</button>
  </div>
</div>

</div><!-- /contentWrap -->

<!-- Tab bar: 5 tabs: Trades, Regimes, Home (center/default), Stats, Settings -->
<div class="tab-bar">
  <button class="tab-btn" data-tab="Trades" onclick="switchTab('Trades')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4"/></svg>
    <span>Trades</span>
  </button>
  <button class="tab-btn" data-tab="Regimes" onclick="switchTab('Regimes')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
    <span>Regimes</span>
  </button>
  <button class="tab-btn tab-active" data-tab="Home" onclick="switchTab('Home')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
    <span>Home</span>
  </button>
  <button class="tab-btn" data-tab="Stats" onclick="switchTab('Stats')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/></svg>
    <span>Stats</span>
  </button>
  <button class="tab-btn" data-tab="Settings" onclick="switchTab('Settings')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/><circle cx="12" cy="12" r="3"/></svg>
    <span>Settings</span>
  </button>
</div>

<script src="/app.js"></script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════
#  PLATFORM JS (served as /app.js instead of inline)
# ═══════════════════════════════════════════════════════════════

_PLATFORM_JS = r"""
// ═══════════════════════════════════════════════════════════════
//  PLATFORM JAVASCRIPT
// ═══════════════════════════════════════════════════════════════

const $ = s => document.querySelector(s);
function _cw() { return document.getElementById('contentWrap'); }
function scrollTop() { const c = _cw(); if (c) c.scrollTop = 0; }

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

// ── Modal system ──
let _modalCount = 0;

function openModal(id) {
  const el = document.getElementById(id);
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
  if (e.target.closest('.modal-panel') || e.target.closest('.confirm-box')) return;
  e.preventDefault();
}, {passive: false});

// ── Tab system ──
let _currentTab = 'Home';
function switchTab(tab) {
  closeAllModals();
  _currentTab = tab;
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  const page = document.getElementById('page' + tab);
  if (page) page.classList.add('active');
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('tab-active'));
  document.querySelectorAll('.tab-btn[data-tab="' + tab + '"]').forEach(b => b.classList.add('tab-active'));
  scrollTop();
  // Tab-specific init callbacks
  if (tab === 'Settings') { loadSvcStatus(); loadSystemStats(); loadAuditLog(); loadPinStatus(); }
  if (tab === 'Regimes') { loadBtcChart(); loadRegimeCurrent(); }
}

// ── Toast system (translucent backdrop-filter blur) ──
function showToast(msg, color) {
  let t = document.getElementById('mainToast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'mainToast';
    t.style.cssText = 'position:fixed;top:env(safe-area-inset-top,48px);left:50%;transform:translateX(-50%);padding:6px 14px;border-radius:6px;border:1px solid transparent;font-size:13px;font-weight:600;opacity:0;transition:opacity 0.3s;z-index:200;pointer-events:none;backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);';
    document.body.appendChild(t);
  }
  const colors = {
    green:  {bg: 'rgba(63,185,80,0.12)',   border: 'rgba(63,185,80,0.4)',   fg: 'var(--green)'},
    red:    {bg: 'rgba(248,81,73,0.12)',    border: 'rgba(248,81,73,0.4)',   fg: 'var(--red)'},
    yellow: {bg: 'rgba(210,153,34,0.12)',   border: 'rgba(210,153,34,0.4)',  fg: 'var(--yellow)'},
    blue:   {bg: 'rgba(88,166,255,0.12)',   border: 'rgba(88,166,255,0.4)',  fg: 'var(--blue)'},
    orange: {bg: 'rgba(240,136,62,0.12)',   border: 'rgba(240,136,62,0.4)', fg: 'var(--orange)'},
    purple: {bg: 'rgba(163,113,247,0.12)',  border: 'rgba(163,113,247,0.4)', fg: '#a371f7'},
  };
  const c = colors[color] || colors.green;
  t.style.background = c.bg;
  t.style.borderColor = c.border;
  t.style.color = c.fg;
  t.textContent = msg;
  t.style.opacity = '1';
  setTimeout(() => t.style.opacity = '0', 2000);
}

// ── Pull-to-refresh ──
let _chartTouchActive = false;
(function() {
  let startY = 0, pulling = false, triggered = false;
  const threshold = 180;
  const showAfter = 30;
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
      const raw = (dy - showAfter) / (threshold - showAfter);
      const progress = Math.min(1, raw * raw);
      bar.style.transform = 'scaleX(' + progress + ')';
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
      let pos = 0;
      const anim = setInterval(() => {
        pos = (pos + 2) % 100;
        bar.style.transform = 'scaleX(1)';
        bar.style.background = 'linear-gradient(90deg, var(--bg) '+pos+'%, var(--green) '+(pos+20)+'%, var(--bg) '+(pos+40)+'%)';
      }, 30);
      setTimeout(() => { clearInterval(anim); location.reload(); }, 300);
    } else {
      bar.style.transform = 'scaleX(0)';
      bar.style.transition = 'transform 0.2s';
      setTimeout(() => { bar.style.display = 'none'; bar.style.transition = 'transform 0.1s'; }, 200);
    }
  }, {passive: true});
})();

// ── API helper ──
async function api(url, opts) {
  opts = opts || {};
  const r = await fetch(url, opts);
  if (r.status === 401) { window.location.reload(); return null; }
  return r.json();
}

// ── Detail toggle ──
function toggleDetail(id) {
  const el = document.getElementById(id);
  if (!el) return;
  const toggle = el.previousElementSibling;
  if (el.style.display === 'block') {
    el.style.display = 'none';
    if (toggle) toggle.textContent = toggle.textContent.replace('\u25BE', '\u25B8');
  } else {
    el.style.display = 'block';
    if (toggle) toggle.textContent = toggle.textContent.replace('\u25B8', '\u25BE');
  }
}

// ── Bankroll modal ──
function openBankrollModal() { openModal('bankrollModal'); loadBankrollChart(); }

async function loadBankrollChart(hours, btn) {
  if (btn) { btn.parentElement.querySelectorAll('.chip').forEach(c=>c.classList.remove('active')); btn.classList.add('active'); }
  const url = hours ? '/api/bankroll/chart?hours='+hours : '/api/bankroll/chart';
  const data = await api(url);
  if (!data || !data.length) return;
  drawLineChart('bankrollChart', data.map(d=>({x:d.captured_at, y:d.bankroll_cents/100})), 'var(--blue)');
  // Update label
  const last = data[data.length-1];
  const label = document.getElementById('bankrollChartLabel');
  if (label && last) label.textContent = '$' + (last.bankroll_cents/100).toFixed(2);
}

async function lockFunds(amount) {
  if (!amount || isNaN(amount)) return;
  // Find first plugin and send lock command
  const plugins = await api('/api/state');
  if (plugins && plugins.plugins && plugins.plugins.length > 0) {
    const pid = plugins.plugins[0].plugin_id;
    await api('/api/command', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({plugin_id: pid, command: 'lock_bankroll', params: {amount: amount}})});
    showToast(amount > 0 ? 'Locked $' + Math.abs(amount) : 'Unlocked $' + Math.abs(amount), 'yellow');
  }
}

// ── BTC chart ──
async function loadBtcChart(minutes, btn) {
  if (btn) { btn.parentElement.querySelectorAll('.chip').forEach(c=>c.classList.remove('active')); btn.classList.add('active'); }
  minutes = minutes || 60;
  const data = await api('/api/regime/candles?minutes='+minutes);
  if (!data || !data.length) return;
  drawLineChart('btcChart', data.map(d=>({x:d.ts, y:d.close})), 'var(--orange)');
  const last = data[data.length-1];
  const el = document.getElementById('btcPriceMain');
  if (el && last) el.textContent = '$' + last.close.toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2});
  // Show returns
  if (data.length > 1) {
    const first = data[0];
    const ret = ((last.close - first.close) / first.close * 100).toFixed(2);
    const retEl = document.getElementById('btcReturns');
    if (retEl) {
      retEl.textContent = (ret >= 0 ? '+' : '') + ret + '%';
      retEl.style.color = ret >= 0 ? 'var(--green)' : 'var(--red)';
    }
  }
}

// ── Current regime ──
async function loadRegimeCurrent() {
  const data = await api('/api/regime/current');
  const el = document.getElementById('regimeCurrentContent');
  if (!data || !el) return;
  if (!data.composite_label) { el.innerHTML = '<div class="dim">No regime data yet</div>'; return; }
  const label = data.composite_label || 'unknown';
  el.innerHTML = '<div style="font-size:16px;font-weight:700;margin-bottom:4px">' + label + '</div>' +
    '<div class="dim" style="font-size:11px">BTC: $' + (data.btc_price||0).toLocaleString() + '</div>' +
    (data.regime_confidence ? '<div class="dim" style="font-size:11px">Confidence: ' + (data.regime_confidence*100).toFixed(0) + '%</div>' : '') +
    (data.captured_ct ? '<div class="dim" style="font-size:10px;margin-top:2px">' + data.captured_ct + '</div>' : '');
}

// ── Simple canvas line chart utility ──
function drawLineChart(canvasId, points, color) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || !points.length) return;
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);
  const w = rect.width, h = rect.height;
  ctx.clearRect(0, 0, w, h);
  const vals = points.map(p => p.y);
  const mn = Math.min.apply(null, vals), mx = Math.max.apply(null, vals);
  const range = mx - mn || 1;
  const pad = 4;
  ctx.beginPath();
  // Resolve CSS variable color
  const tmp = document.createElement('span');
  tmp.style.color = color;
  document.body.appendChild(tmp);
  const resolved = getComputedStyle(tmp).color;
  document.body.removeChild(tmp);
  ctx.strokeStyle = resolved;
  ctx.lineWidth = 1.5;
  points.forEach((p, i) => {
    const x = pad + (i / (points.length - 1)) * (w - 2*pad);
    const y = h - pad - ((p.y - mn) / range) * (h - 2*pad);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

// ── System stats ──
async function loadSystemStats() {
  const stats = await api('/api/system_stats');
  if (!stats) return;
  const el = document.getElementById('srvStats');
  if (!el) return;
  let html = '';
  if (stats.cpu) html += '<div style="display:flex;justify-content:space-between;padding:3px 0"><span class="dim">CPU</span><span>' + stats.cpu.pct + '%</span></div>';
  if (stats.memory) html += '<div style="display:flex;justify-content:space-between;padding:3px 0"><span class="dim">Memory</span><span>' + stats.memory.pct + '% (' + stats.memory.used_mb + 'MB)</span></div>';
  if (stats.disk) html += '<div style="display:flex;justify-content:space-between;padding:3px 0"><span class="dim">Disk</span><span>' + stats.disk.pct + '% (' + stats.disk.used_gb + '/' + stats.disk.total_gb + 'GB)</span></div>';
  if (stats.disk && stats.disk.db_mb) html += '<div style="display:flex;justify-content:space-between;padding:3px 0"><span class="dim">Database</span><span>' + stats.disk.db_mb + ' MB</span></div>';
  if (stats.network) html += '<div style="display:flex;justify-content:space-between;padding:3px 0"><span class="dim">Network</span><span>' + stats.network.rx_kbps + '/' + stats.network.tx_kbps + ' KB/s</span></div>';
  if (stats.uptime) {
    const up = el.closest('.settings-card').querySelector('#srvUptime');
    if (up) up.textContent = stats.uptime.display;
  }
  el.innerHTML = html || '<div class="dim" style="text-align:center;padding:8px">No data</div>';
}

// ── Service status & control ──
async function loadSvcStatus() {
  const status = await api('/api/services');
  if (!status) return;
  for (const [name, info] of Object.entries(status)) {
    if (info && info.status) {
      const cls = info.status === 'RUNNING' ? 'color:var(--green)' : 'color:var(--red)';
      if (name.includes('dashboard')) {
        const el = document.getElementById('svcDashStatus');
        if (el) el.innerHTML = '<span style="'+cls+'">'+info.status+'</span> '+info.detail;
      }
      if (name.includes('btc') || name.includes('plugin')) {
        const el = document.getElementById('svcBotStatus');
        if (el) el.innerHTML = '<span style="'+cls+'">'+info.status+'</span> '+info.detail;
      }
    }
  }
}

async function svcControl(action, service) {
  showToast('Sending ' + action + '...', 'blue');
  await api('/api/services/control', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:action, service:service})});
  setTimeout(loadSvcStatus, 2000);
}

// ── Deploy ──
function onDeployFilesSelected(input) {
  const files = input.files;
  if (!files.length) return;
  document.getElementById('deployFileLabel').textContent = files.length + ' file(s) selected';
  document.getElementById('deployUploadBtn').style.display = '';
  let html = '';
  for (let i = 0; i < files.length; i++) {
    const f = files[i];
    html += '<div style="color:var(--text)">' + f.name + ' (' + (f.size/1024).toFixed(1) + 'KB)</div>';
  }
  document.getElementById('deployFileList').innerHTML = html;
}

async function doDeploy() {
  const input = document.getElementById('deployFiles');
  if (!input.files.length) return;
  const fd = new FormData();
  for (let i = 0; i < input.files.length; i++) {
    const f = input.files[i];
    fd.append(f.name, f);
  }
  document.getElementById('deployStatus').innerHTML = '<span style="color:var(--yellow)">Uploading...</span>';
  const r = await fetch('/api/deploy/upload', {method:'POST', body: fd});
  const d = await r.json();
  let html = '';
  if (d.uploaded && d.uploaded.length) html += '<span style="color:var(--green)">Uploaded: ' + d.uploaded.join(', ') + '</span><br>';
  if (d.errors && d.errors.length) html += '<span style="color:var(--red)">Errors: ' + d.errors.join('; ') + '</span><br>';
  document.getElementById('deployStatus').innerHTML = html;
  if (d.uploaded && d.uploaded.length) {
    showToast('Deployed ' + d.uploaded.length + ' file(s)', 'green');
  }
}

async function doBackup() {
  const d = await api('/api/backup', {method:'POST'});
  if (d && d.path) showToast('Backup saved (' + (d.size_mb || '?') + ' MB)', 'green');
  else showToast('Backup failed', 'red');
}

// ── Push notification setup ──
let pushSubscription = null;

async function initPush() {
  const el = document.getElementById('pushStatus');
  const btn = document.getElementById('pushToggleBtn');
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
    el.textContent = 'Push notifications not supported';
    return;
  }
  try {
    const reg = await navigator.serviceWorker.register('/sw.js');
    pushSubscription = await reg.pushManager.getSubscription();
    if (pushSubscription) {
      el.textContent = 'Notifications enabled';
      btn.textContent = 'Disable Notifications';
      btn.style.display = '';
    } else {
      el.textContent = 'Notifications disabled';
      btn.textContent = 'Enable Notifications';
      btn.style.display = '';
    }
  } catch (e) {
    el.textContent = 'Error: ' + e.message;
  }
}

async function togglePush() {
  const el = document.getElementById('pushStatus');
  const btn = document.getElementById('pushToggleBtn');
  if (pushSubscription) {
    // Unsubscribe
    const ep = pushSubscription.endpoint;
    await pushSubscription.unsubscribe();
    await api('/api/push/unsubscribe', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({endpoint:ep})});
    pushSubscription = null;
    el.textContent = 'Notifications disabled';
    btn.textContent = 'Enable Notifications';
    showToast('Notifications disabled', 'yellow');
  } else {
    // Subscribe
    try {
      const keyResp = await api('/api/push/vapid-key');
      if (!keyResp || !keyResp.key) { showToast('VAPID key not configured', 'red'); return; }
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: keyResp.key
      });
      await api('/api/push/subscribe', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({subscription:sub.toJSON()})});
      pushSubscription = sub;
      el.textContent = 'Notifications enabled';
      btn.textContent = 'Disable Notifications';
      showToast('Notifications enabled', 'green');
    } catch (e) {
      showToast('Failed: ' + e.message, 'red');
    }
  }
}

// ── Security handlers ──
async function _secChangePass() {
  const old = document.getElementById('secOldPass').value;
  const nw = document.getElementById('secNewPass').value;
  const conf = document.getElementById('secConfPass').value;
  if (nw !== conf) { showToast('Passwords do not match', 'red'); return; }
  if (nw.length < 6) { showToast('Min 6 characters', 'red'); return; }
  const r = await api('/api/change_password', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:'admin', old_password:old, new_password:nw})});
  if (r && r.ok) { showToast('Password changed', 'green'); document.getElementById('secOldPass').value=''; document.getElementById('secNewPass').value=''; document.getElementById('secConfPass').value=''; }
  else showToast((r && r.error) || 'Failed', 'red');
}

async function _secInvalidate() {
  await api('/api/invalidate_sessions', {method:'POST'});
  showToast('Sessions invalidated', 'yellow');
}

async function _secSetPin() {
  const current = document.getElementById('pinCurrent').value;
  const nw = document.getElementById('pinNew').value;
  const r = await api('/api/destruction_pin', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({current_pin:current, pin:nw})});
  if (r && r.ok) { showToast('PIN set', 'green'); document.getElementById('pinCurrent').value=''; document.getElementById('pinNew').value=''; loadPinStatus(); }
  else showToast((r && r.error) || 'Failed', 'red');
}

async function loadPinStatus() {
  const d = await api('/api/destruction_pin');
  const el = document.getElementById('pinStatus');
  if (el && d) el.textContent = d.has_pin ? '(PIN is set)' : '(No PIN set)';
}

async function loadAuditLog() {
  const d = await api('/api/audit_log?limit=20');
  const el = document.getElementById('auditLogContent');
  if (!el || !d) return;
  if (!d.entries || !d.entries.length) { el.innerHTML = '<div class="dim">No audit entries</div>'; return; }
  let html = '';
  for (const e of d.entries) {
    html += '<div style="padding:3px 0;border-bottom:1px solid var(--border)">' +
      '<span class="dim">' + (e.ts || '').substring(0,19) + '</span> ' +
      '<strong>' + (e.action || '') + '</strong> ' +
      '<span class="dim">' + (e.detail || '') + '</span>' +
      (e.success === 0 ? ' <span style="color:var(--red)">FAILED</span>' : '') +
      '</div>';
  }
  el.innerHTML = html;
}

// ── Reset ──
async function doReset(scope, pin) {
  if (scope === 'full' && !confirm('This will delete ALL data. Are you sure?')) return;
  if (scope === 'regime_engine' && !confirm('Wipe all regime data?')) return;
  const body = {scope: scope};
  if (pin) body.pin = pin;
  const r = await api('/api/reset', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if (r && r.ok) showToast(r.msg || 'Reset complete', 'green');
  else showToast((r && r.error) || 'Reset failed', 'red');
}

function doLogout() {
  fetch('/api/logout', {method:'POST'}).then(() => window.location.reload());
}

// ── Trade loading (placeholder for plugin JS) ──
let _tradeOffset = 0;
function loadMoreTrades() {
  // Plugins will override this
}

// ── Init ──
initPush();

// PLUGIN_JS
"""


# ═══════════════════════════════════════════════════════════════
#  PLUGIN DISCOVERY (must run after all definitions to avoid circular imports)
# ═══════════════════════════════════════════════════════════════

_discover_plugins()


# ═══════════════════════════════════════════════════════════════
#  APP STARTUP
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    init_db()
    app.run(host=DASHBOARD_HOST, port=DASHBOARD_PORT, debug=False)
