# Flask Dashboard Starter Kit

Extracted from a production Flask dashboard (mobile-first iOS PWA) running on a DigitalOcean droplet. Every pattern below is battle-tested across months of daily use. This is a reference for building new projects — not a runnable app. Adapt the pieces you need.

## Architecture Overview

Single-file Flask app (`dashboard.py`) serving both API endpoints and the HTML/CSS/JS UI as inline template strings. SQLite database with WAL mode for concurrent access (background workers + web dashboard). Supervisor manages processes. Nginx reverse-proxies with SSL via Let's Encrypt.

Key design decisions:
- **Single-file dashboard**: All HTML/CSS/JS lives inside `dashboard.py` as Python string templates. No build step, no static file serving complexity. Works great up to ~15,000 lines.
- **SQLite + WAL mode**: Perfect for single-server apps. WAL allows concurrent readers + one writer without blocking.
- **No localStorage**: iOS PWA doesn't reliably support it. All state lives in JS memory or server-side.
- **Mobile-first**: Fixed header + fixed tab bar + scrollable content area between them. Safe area insets for notch/home indicator.

---

## 1. Server Infrastructure

### Supervisor Config (`/etc/supervisor/conf.d/myapp.conf`)

```ini
[program:myapp-dashboard]
command=/usr/bin/python3 /opt/myapp/dashboard.py
directory=/opt/myapp
user=root
autostart=true
autorestart=true
stderr_logfile=/var/log/myapp-dashboard.err.log
stdout_logfile=/var/log/myapp-dashboard.out.log
environment=BOT_DIR="/opt/myapp"

; If you have a background worker process:
[program:myapp-worker]
command=/usr/bin/python3 /opt/myapp/worker.py
directory=/opt/myapp
user=root
autostart=true
autorestart=true
stderr_logfile=/var/log/myapp-worker.err.log
stdout_logfile=/var/log/myapp-worker.out.log
environment=BOT_DIR="/opt/myapp"
```

After creating: `supervisorctl reread && supervisorctl update`

### Nginx Config (`/etc/nginx/sites-available/myapp`)

```nginx
server {
    listen 80;
    server_name myapp.bbrooks.dev;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

# After running: certbot --nginx -d myapp.bbrooks.dev
# Certbot auto-adds the SSL server block below with cert paths.
# Then manually add the proxy and security headers:

server {
    listen 443 ssl http2;
    server_name myapp.bbrooks.dev;

    ssl_certificate /etc/letsencrypt/live/myapp.bbrooks.dev/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/myapp.bbrooks.dev/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    add_header X-Content-Type-Options nosniff;
    add_header X-Frame-Options DENY;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    location / {
        proxy_pass http://127.0.0.1:8050;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        proxy_connect_timeout 60s;
        proxy_read_timeout 120s;
    }
}
```

Setup sequence:
1. Create config in `/etc/nginx/sites-available/`
2. Symlink: `ln -s /etc/nginx/sites-available/myapp /etc/nginx/sites-enabled/`
3. Start with HTTP-only block first (no SSL block yet)
4. `nginx -t && systemctl reload nginx`
5. `certbot --nginx -d myapp.bbrooks.dev` (adds SSL block + cert paths)
6. Edit the certbot-generated SSL block to add proxy_pass and headers
7. `nginx -t && systemctl reload nginx`

### Cloudflare DNS

For `bbrooks.dev` subdomains: A record, name = subdomain, content = droplet IP, proxy OFF (gray cloud, DNS-only). Let's Encrypt handles SSL on the server.

---

## 2. Config File (`config.py`)

```python
"""
config.py — Constants, paths, and defaults.
"""
import os
from zoneinfo import ZoneInfo

# ── Load .env file ───────────────────────────────────────
def _load_env_file():
    bot_dir = os.environ.get("BOT_DIR", "/opt/myapp")
    for name in (".env", "_env"):
        path = os.path.join(bot_dir, name)
        if os.path.isfile(path):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = value
            break

_load_env_file()

# ── Timezones ────────────────────────────────────────────
CT = ZoneInfo("America/Chicago")       # Display timezone

# ── Paths ────────────────────────────────────────────────
BOT_DIR = os.environ.get("BOT_DIR", "/opt/myapp")
DB_PATH = os.path.join(BOT_DIR, "appdata.db")

# ── Dashboard ────────────────────────────────────────────
DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", 8050))
DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "admin")
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "CHANGE_ME")
```

### `.env` file on server

```
DASHBOARD_USER=admin
DASHBOARD_PASS=your_secure_password

# Email deploy (optional)
DEPLOY_EMAIL=myapp-deploy@gmail.com
DEPLOY_EMAIL_PASS=xxxx xxxx xxxx xxxx
DEPLOY_ALLOWED_SENDERS=your_personal@email.com

# Push notifications (optional)
# VAPID keys stored in vapid_keys.json
```

---

## 3. Database Layer (`db.py`)

### Connection Manager

```python
"""
db.py — Database layer. SQLite with WAL mode.
"""
import json, os, sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager
from config import DB_PATH

def _ensure_dir():
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)

@contextmanager
def get_conn():
    _ensure_dir()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()

def row_to_dict(row) -> dict | None:
    return dict(row) if row else None

def rows_to_list(rows) -> list:
    return [dict(r) for r in rows]
```

### Schema — Reusable Tables

```python
def init_db():
    with get_conn() as c:

        # ── App state (single-row, JSON columns for flexible data) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS app_state (
                id              INTEGER PRIMARY KEY DEFAULT 1,
                status          TEXT DEFAULT 'idle',
                status_detail   TEXT DEFAULT '',
                last_updated    TEXT
            )
        """)
        c.execute("INSERT OR IGNORE INTO app_state (id, last_updated) VALUES (1, ?)", (now_utc(),))

        # ── Key-value config store ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS app_config (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)

        # ── Command queue (dashboard → background worker) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS app_commands (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                command_type    TEXT NOT NULL,
                parameters      TEXT DEFAULT '{}',
                status          TEXT DEFAULT 'pending',
                created_at      TEXT NOT NULL,
                result          TEXT
            )
        """)

        # ── Structured log entries ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS log_entries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                level       TEXT NOT NULL,
                category    TEXT DEFAULT 'app',
                message     TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts ON log_entries(ts)")

        # ── Security audit log ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT NOT NULL,
                action      TEXT NOT NULL,
                detail      TEXT,
                ip          TEXT,
                success     INTEGER DEFAULT 1
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(created_at)")

        # ── Push notification subscriptions ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint        TEXT NOT NULL UNIQUE,
                subscription_json TEXT NOT NULL,
                created_at      TEXT NOT NULL
            )
        """)

        # ── Push notification log ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS push_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT NOT NULL,
                body        TEXT,
                tag         TEXT,
                sent_at     TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_pushlog_ts ON push_log(sent_at)")

    print(f"[db] Initialized at {DB_PATH}")
```

### Helper Functions

```python
# ── Config (key-value store with JSON serialization) ──

def get_config(key: str, default=None):
    with get_conn() as c:
        row = c.execute("SELECT value FROM app_config WHERE key = ?", (key,)).fetchone()
        if row:
            return json.loads(row["value"])
        return default

def set_config(key: str, value):
    with get_conn() as c:
        c.execute("""
            INSERT INTO app_config (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                           updated_at = excluded.updated_at
        """, (key, json.dumps(value), now_utc()))

def get_all_config() -> dict:
    with get_conn() as c:
        rows = c.execute("SELECT key, value FROM app_config").fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}


# ── App state (single-row with JSON columns) ──

def get_app_state() -> dict:
    with get_conn() as c:
        row = c.execute("SELECT * FROM app_state WHERE id = 1").fetchone()
        return row_to_dict(row) or {}

def update_app_state(data: dict):
    data["last_updated"] = now_utc()
    sets = ", ".join(f"{k} = ?" for k in data.keys())
    with get_conn() as c:
        c.execute(f"UPDATE app_state SET {sets} WHERE id = 1", list(data.values()))


# ── Command queue (dashboard → worker) ──

def enqueue_command(command_type: str, parameters: dict = None) -> int:
    with get_conn() as c:
        cur = c.execute("""
            INSERT INTO app_commands (command_type, parameters, created_at)
            VALUES (?, ?, ?)
        """, (command_type, json.dumps(parameters or {}), now_utc()))
        return cur.lastrowid

def get_pending_commands() -> list:
    """Atomically claim all pending commands."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT * FROM app_commands WHERE status = 'pending'
            ORDER BY created_at ASC
        """).fetchall()
        cmds = rows_to_list(rows)
        for cmd in cmds:
            c.execute("UPDATE app_commands SET status = 'executing' WHERE id = ? AND status = 'pending'",
                      (cmd["id"],))
        return cmds

def complete_command(cmd_id: int, result: dict = None):
    with get_conn() as c:
        c.execute("UPDATE app_commands SET status = 'completed', result = ? WHERE id = ?",
                  (json.dumps(result or {}), cmd_id))

def flush_pending_commands():
    """Cancel all pending/executing commands. Call on startup."""
    with get_conn() as c:
        c.execute("""
            UPDATE app_commands SET status = 'cancelled',
                result = '{"reason": "flushed on startup"}'
            WHERE status IN ('pending', 'executing')
        """)


# ── Logs ──

def insert_log(level: str, message: str, category: str = "app"):
    with get_conn() as c:
        c.execute("INSERT INTO log_entries (ts, level, category, message) VALUES (?, ?, ?, ?)",
                  (now_utc(), level, category, message))

def get_logs(before_id: int = None, limit: int = 100, level: str = None) -> list:
    with get_conn() as c:
        conditions, params = [], []
        if before_id:
            conditions.append("id < ?")
            params.append(before_id)
        if level:
            conditions.append("level = ?")
            params.append(level)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)
        rows = c.execute(f"SELECT * FROM log_entries {where} ORDER BY id DESC LIMIT ?", params).fetchall()
        return rows_to_list(rows)

def get_logs_after(after_id: int) -> list:
    with get_conn() as c:
        rows = c.execute("SELECT * FROM log_entries WHERE id > ? ORDER BY id ASC", (after_id,)).fetchall()
        return rows_to_list(rows)


# ── Push subscriptions ──

def save_push_subscription(endpoint: str, subscription_json: str):
    with get_conn() as c:
        c.execute("INSERT OR REPLACE INTO push_subscriptions (endpoint, subscription_json, created_at) VALUES (?, ?, ?)",
                  (endpoint, subscription_json, now_utc()))

def get_push_subscriptions() -> list:
    with get_conn() as c:
        return rows_to_list(c.execute("SELECT * FROM push_subscriptions").fetchall())

def remove_push_subscription(sub_id: int):
    with get_conn() as c:
        c.execute("DELETE FROM push_subscriptions WHERE id = ?", (sub_id,))

def remove_push_subscription_by_endpoint(endpoint: str):
    with get_conn() as c:
        c.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))

def insert_push_log(title: str, body: str, tag: str = ""):
    with get_conn() as c:
        c.execute("INSERT INTO push_log (title, body, tag, sent_at) VALUES (?, ?, ?, ?)",
                  (title, body or "", tag or "", now_utc()))
        c.execute("DELETE FROM push_log WHERE id NOT IN (SELECT id FROM push_log ORDER BY sent_at DESC LIMIT 500)")


# ── Audit log ──

def insert_audit_log(action: str, detail: str = "", ip: str = "", success: bool = True):
    try:
        with get_conn() as c:
            c.execute("INSERT INTO audit_log (created_at, action, detail, ip, success) VALUES (?,?,?,?,?)",
                      (now_utc(), action, detail, ip, 1 if success else 0))
    except Exception:
        pass  # Never block on audit failure


# ── Database backup ──

def backup_database(reason: str = "manual") -> str | None:
    import shutil
    try:
        backup_dir = os.path.join(os.path.dirname(DB_PATH), "_db_backups")
        os.makedirs(backup_dir, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(backup_dir, f"appdata_{reason}_{ts}.db")
        # WAL-safe: checkpoint flushes WAL into main DB first
        src_conn = sqlite3.connect(DB_PATH, timeout=10)
        src_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        src_conn.close()
        shutil.copy2(DB_PATH, backup_path)
        # Keep only last 10 backups
        backups = sorted([f for f in os.listdir(backup_dir) if f.endswith('.db')], reverse=True)
        for old in backups[10:]:
            try: os.remove(os.path.join(backup_dir, old))
            except: pass
        return backup_path
    except Exception as e:
        print(f"[db] Backup failed: {e}")
        return None
```

---

## 4. Push Notifications (`push.py`)

### VAPID Key Setup (one-time)

```bash
pip install pywebpush --break-system-packages
python3 -c "
from pywebpush import webpush
import json, subprocess
# Generate VAPID keys
result = subprocess.run(['openssl', 'ecparam', '-genkey', '-name', 'prime256v1', '-noout'],
                       capture_output=True)
# Easier: use py_vapid
from py_vapid import Vapid
v = Vapid()
v.generate_keys()
v.save_key('vapid_private.pem')
v.save_public_key('vapid_public.pem')
print('Public key:', v.public_key)
"
```

Or simpler — create `vapid_keys.json`:
```json
{
    "public_key": "YOUR_VAPID_PUBLIC_KEY_BASE64URL",
    "private_key_path": "/opt/myapp/vapid_private.pem",
    "admin_email": "mailto:admin@bbrooks.dev"
}
```

### Push Module

```python
"""
push.py — Web Push notification sender.
"""
import json, logging
from pathlib import Path

log = logging.getLogger("push")

try:
    from pywebpush import webpush, WebPushException
    PUSH_AVAILABLE = True
except ImportError:
    PUSH_AVAILABLE = False

VAPID_KEYS_PATH = Path(__file__).parent / "vapid_keys.json"
_vapid_config = None

def _load_vapid():
    global _vapid_config
    if _vapid_config:
        return _vapid_config
    if not VAPID_KEYS_PATH.exists():
        return None
    with open(VAPID_KEYS_PATH) as f:
        _vapid_config = json.load(f)
    return _vapid_config

def get_public_key() -> str | None:
    cfg = _load_vapid()
    return cfg.get("public_key") if cfg else None

def send_push(subscription_info: dict, title: str, body: str,
              tag: str = "default", url: str = "/", silent: bool = False) -> bool | None:
    """
    Returns True if sent, False if subscription is dead (remove it),
    None on temporary failure (keep subscription).
    """
    if not PUSH_AVAILABLE:
        return None
    cfg = _load_vapid()
    if not cfg:
        return None

    payload = json.dumps({
        "title": title, "body": body, "tag": tag, "url": url,
        "silent": silent, "timestamp": __import__("time").time(),
    })

    try:
        webpush(
            subscription_info=subscription_info,
            data=payload,
            vapid_private_key=cfg["private_key_path"],
            vapid_claims={"sub": cfg.get("admin_email", "mailto:admin@bbrooks.dev")},
            ttl=300,
        )
        return True
    except WebPushException as e:
        status = getattr(e, "response", None)
        if status and status.status_code in (404, 410):
            return False  # Dead subscription — caller should remove
        return None  # Temporary failure — keep subscription
    except Exception:
        return None

def send_to_all(title: str, body: str, tag: str = "default", url: str = "/",
                silent: bool = False):
    """Send to all subscriptions. Auto-removes dead ones."""
    if not PUSH_AVAILABLE:
        return
    from db import get_push_subscriptions, remove_push_subscription, insert_push_log

    sent = False
    for sub in get_push_subscriptions():
        try:
            sub_info = json.loads(sub["subscription_json"])
            result = send_push(sub_info, title, body, tag, url, silent=silent)
            if result is True:
                sent = True
            elif result is False:
                remove_push_subscription(sub["id"])
        except Exception:
            pass

    if sent:
        try: insert_push_log(title, body, tag)
        except: pass
```

---

## 5. Dashboard Security & Auth

### Login Rate Limiting

```python
import time as _time
import secrets as _secrets

_login_attempts = {}  # ip -> [(timestamp, success)]
_LOGIN_WINDOW = 600   # 10 minutes
_LOGIN_MAX = 5

def _check_login_rate(ip: str) -> str | None:
    now = _time.time()
    attempts = _login_attempts.get(ip, [])
    attempts = [(t, s) for t, s in attempts if now - t < _LOGIN_WINDOW]
    _login_attempts[ip] = attempts
    if sum(1 for t, s in attempts if not s) >= _LOGIN_MAX:
        return "Too many failed attempts. Try again in 10 minutes."
    return None

def _record_login_attempt(ip: str, success: bool):
    now = _time.time()
    if ip not in _login_attempts:
        _login_attempts[ip] = []
    _login_attempts[ip].append((now, success))
    _login_attempts[ip] = _login_attempts[ip][-20:]
```

### CSRF Protection

```python
_CSRF_EXEMPT = {"/api/login", "/api/change_password"}

@app.before_request
def _csrf_check():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if request.path in _CSRF_EXEMPT:
        return
    origin = request.headers.get("Origin", "")
    if not origin:
        return  # Same-origin or curl
    allowed = request.host_url.rstrip("/")
    if origin == allowed or origin == allowed.replace("http://", "https://"):
        return
    if "bbrooks.dev" in origin:
        return
    return jsonify({"error": "Request blocked (cross-origin)"}), 403
```

### Cookie-Based Auth with Session Invalidation

```python
from config import DASHBOARD_USER, DASHBOARD_PASS
from db import get_config, set_config, insert_audit_log

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
    except: pass
    salt = _secrets.token_hex(16)
    set_config("_session_salt", salt)
    return salt

def _auth_token():
    """Signed token including session salt — rotate salt to invalidate all sessions."""
    import hashlib
    salt = _get_session_salt()
    try:
        stored_hash = get_config("dashboard_pass_hash")
        if stored_hash:
            return hashlib.sha256(f"{DASHBOARD_USER}:{stored_hash}:{salt}".encode()).hexdigest()
    except: pass
    return hashlib.sha256(f"{DASHBOARD_USER}:{DASHBOARD_PASS}:{salt}".encode()).hexdigest()

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get("app_auth")
        if token and _secrets.compare_digest(token, _auth_token()):
            return f(*args, **kwargs)
        auth = request.authorization
        if auth and check_auth(auth.username, auth.password):
            resp = make_response(f(*args, **kwargs))
            resp.set_cookie("app_auth", _auth_token(), max_age=30*86400,
                            httponly=True, samesite="Lax", secure=request.is_secure)
            return resp
        if not request.path.startswith('/api/'):
            return render_template_string(LOGIN_HTML), 401
        return Response(json.dumps({"error": "Unauthorized"}), 401,
                        {"Content-Type": "application/json"})
    return decorated
```

### Auth API Endpoints

```python
@app.route("/api/login", methods=["POST"])
def api_login():
    ip = request.remote_addr or "unknown"
    rate_err = _check_login_rate(ip)
    if rate_err:
        return jsonify({"error": rate_err}), 429
    data = request.get_json() or {}
    if check_auth(data.get("username", ""), data.get("password", "")):
        _record_login_attempt(ip, True)
        insert_audit_log("login_success", "", ip=ip)
        resp = jsonify({"ok": True})
        resp.set_cookie("app_auth", _auth_token(), max_age=30*86400,
                        httponly=True, samesite="Lax", secure=request.is_secure)
        return resp
    _record_login_attempt(ip, False)
    insert_audit_log("login_failed", "", ip=ip, success=False)
    return jsonify({"error": "Invalid credentials"}), 401

@app.route("/api/logout", methods=["POST"])
def api_logout():
    resp = jsonify({"ok": True})
    resp.set_cookie("app_auth", "", max_age=0)
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
    set_config("dashboard_pass_hash", hashlib.sha256(new_pass.encode()).hexdigest())
    return jsonify({"ok": True})

@app.route("/api/invalidate_sessions", methods=["POST"])
@requires_auth
def api_invalidate_sessions():
    """Rotate session salt — invalidates ALL sessions everywhere."""
    set_config("_session_salt", _secrets.token_hex(16))
    resp = jsonify({"ok": True, "msg": "All sessions invalidated"})
    resp.set_cookie("app_auth", _auth_token(), max_age=30*86400,
                    httponly=True, samesite="Lax", secure=request.is_secure)
    return resp
```

### Destruction PIN (for dangerous operations)

```python
def _check_destruction_pin(pin: str) -> bool:
    import hashlib
    stored_hash = get_config("destruction_pin_hash")
    if not stored_hash:
        return True  # No PIN set = not enforced
    return hashlib.sha256(pin.encode()).hexdigest() == stored_hash

def _set_destruction_pin(pin: str):
    import hashlib
    set_config("destruction_pin_hash", hashlib.sha256(pin.encode()).hexdigest())
```

---

## 6. Login Page HTML

```python
LOGIN_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Login</title>
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
  .btn-green { background: #238636; }
  .btn-dim { background: #21262d; border: 1px solid #30363d; color: #8b949e; }
  .msg { font-size: 12px; text-align: center; margin-top: 8px; display: none; }
  .err { color: #f85149; }
  .ok { color: #3fb950; }
  .hidden { display: none; }
</style>
</head><body>
<div class="login-box">
  <h2 id="formTitle">My App</h2>

  <div id="loginForm">
    <input type="text" id="user" placeholder="Username" autocapitalize="off" autocomplete="username">
    <input type="password" id="pass" placeholder="Password" autocomplete="current-password">
    <button class="btn btn-green" onclick="doLogin()">Log In</button>
    <button class="btn btn-dim" onclick="showChangePass()">Change Password</button>
    <div class="msg err" id="loginErr">Invalid credentials</div>
  </div>

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
}
function showLogin() {
  document.getElementById('changeForm').classList.add('hidden');
  document.getElementById('loginForm').classList.remove('hidden');
  document.getElementById('formTitle').textContent = 'My App';
}

async function doLogin() {
  const r = await fetch('/api/login', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      username: document.getElementById('user').value,
      password: document.getElementById('pass').value
    })
  });
  if (r.ok) window.location.href = '/';
  else document.getElementById('loginErr').style.display = '';
}

async function doChangePass() {
  const errEl = document.getElementById('cpErr');
  const okEl = document.getElementById('cpOk');
  errEl.style.display = 'none';
  okEl.style.display = 'none';
  const nw = document.getElementById('cpNew').value;
  const confirm = document.getElementById('cpConfirm').value;
  if (nw !== confirm) { errEl.textContent = 'Passwords do not match'; errEl.style.display = ''; return; }
  if (nw.length < 6) { errEl.textContent = 'Min 6 characters'; errEl.style.display = ''; return; }
  const r = await fetch('/api/change_password', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      username: document.getElementById('cpUser').value,
      old_password: document.getElementById('cpOld').value,
      new_password: nw
    })
  });
  if (r.ok) { okEl.textContent = 'Password changed!'; okEl.style.display = ''; setTimeout(showLogin, 2000); }
  else { const d = await r.json(); errEl.textContent = d.error || 'Failed'; errEl.style.display = ''; }
}
</script>
</body></html>"""
```

---

## 7. UI Patterns — CSS

### CSS Variables (Dark Theme)

```css
:root {
    --bg: #0d1117;
    --card: #161b22;
    --border: #30363d;
    --text: #c9d1d9;
    --green: #3fb950;
    --red: #f85149;
    --yellow: #d29922;
    --blue: #58a6ff;
    --dim: #8b949e;
    --orange: #f0883e;
}
```

### Mobile-First App Shell

```css
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { overflow: hidden; position: fixed; width: 100%; height: 100%; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: var(--bg); color: var(--text);
    font-size: 14px;
}

/* Fixed header */
#stickyHeader {
    position: fixed; top: 0; left: 0; right: 0; z-index: 50;
    background: var(--card);
    border-bottom: 1px solid var(--border);
    padding: 10px 14px;
    box-shadow: 0 4px 12px rgba(0,0,0,0.5);
}

/* Scrollable content between header and tab bar */
#contentWrap {
    position: fixed; top: 58px; left: 0; right: 0; bottom: 76px;
    overflow-y: auto; overscroll-behavior-y: contain;
    -webkit-overflow-scrolling: touch; padding: 8px 12px;
}

/* Fixed bottom tab bar */
.tab-bar {
    position: fixed; bottom: 0; left: 0; right: 0; z-index: 100;
    display: flex; align-items: stretch; justify-content: space-around;
    padding-top: 8px; padding-bottom: 30px;  /* 30px for iOS home indicator */
    background: var(--card); border-top: 1px solid var(--border);
}
```

### Cards

```css
.card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 14px;
    margin-bottom: 12px;
}
.card h3 {
    color: var(--blue);
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 10px;
}
```

### Collapsible Cards

```css
.card-arrow { font-size: 12px; color: var(--dim); transition: transform 0.2s; }
.collapsible.collapsed .card-arrow { transform: rotate(-90deg); }
.collapsible.collapsed .card-body { display: none; }
.card-subtitle { font-size: 11px; color: var(--dim); margin-top: 2px; font-weight: normal; }
.collapsible:not(.collapsed) .card-subtitle { display: none; }
```

### Buttons

```css
.btn {
    padding: 10px 16px; border: none; border-radius: 6px;
    font-size: 14px; font-weight: 600; cursor: pointer; width: 100%; margin-top: 6px;
}
.btn-green { background: var(--green); color: #000; }
.btn-red { background: var(--red); color: #fff; }
.btn-blue { background: var(--blue); color: #000; }
.btn-dim { background: var(--border); color: var(--text); }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }

/* Tinted action buttons (softer, with matching borders) */
.act-btn {
    display: flex; align-items: center; justify-content: center; gap: 6px;
    width: 100%; padding: 10px 14px; border-radius: 6px;
    font-size: 13px; font-weight: 600; cursor: pointer;
    -webkit-tap-highlight-color: transparent; transition: filter 0.15s;
    border: 1px solid;
}
.act-btn:active { filter: brightness(1.3); }
.act-btn-green { background: rgba(63,185,80,0.1); color: var(--green); border-color: rgba(63,185,80,0.3); }
.act-btn-red { background: rgba(248,81,73,0.1); color: var(--red); border-color: rgba(248,81,73,0.3); }
.act-btn-blue { background: rgba(88,166,255,0.1); color: var(--blue); border-color: rgba(88,166,255,0.3); }
```

### Toggle Pill (ON/OFF)

```css
.tog { display: inline-flex; align-items: center; cursor: pointer;
       -webkit-tap-highlight-color: transparent; }
.tog input { position: absolute; opacity: 0; width: 0; height: 0; pointer-events: none; }
.tog .tpill {
    display: inline-block; padding: 3px 8px; border-radius: 4px;
    font-size: 10px; font-weight: 700; letter-spacing: 0.5px; text-transform: uppercase;
    transition: all 0.15s ease; user-select: none; min-width: 36px; text-align: center;
    background: #2a1a1a; color: var(--red); border: 1px solid rgba(248,81,73,0.25);
}
.tog input:checked ~ .tpill {
    background: #1a2a1a; color: var(--green); border-color: rgba(63,185,80,0.3);
}
.tog .tpill::before { content: 'OFF'; }
.tog input:checked ~ .tpill::before { content: 'ON'; }
```

Usage: `<label class="tog"><input type="checkbox" onchange="..."><span class="tpill"></span></label>`

### Input Rows

```css
.input-row { display: flex; gap: 8px; align-items: center; margin-top: 8px; }
.input-row label { color: var(--dim); font-size: 12px; min-width: 90px; }
.input-row input, .input-row select {
    background: var(--bg); border: 1px solid var(--border);
    color: var(--text); padding: 6px 8px; border-radius: 4px;
    font-size: 16px; flex: 1;  /* 16px prevents iOS zoom on focus */
}
```

### Stat Grids

```css
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.grid3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; }
.stat { text-align: center; padding: 6px; }
.stat .label { color: var(--dim); font-size: 11px; text-transform: uppercase; }
.stat .val { font-size: 18px; font-weight: 600; font-family: monospace; margin-top: 2px; }
```

### Status Dots (Animated)

```css
.status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.dot-green { background: var(--green);
    box-shadow: 0 0 4px var(--green), 0 0 8px rgba(63,185,80,0.3);
    animation: pulse-green 2s ease-in-out infinite; }
.dot-red { background: var(--red);
    box-shadow: 0 0 4px var(--red); }
.dot-yellow { background: var(--yellow);
    box-shadow: 0 0 4px var(--yellow);
    animation: pulse-yellow 2s ease-in-out infinite; }

@keyframes pulse-green {
    0%,100% { box-shadow: 0 0 4px var(--green); opacity: 1; }
    50% { box-shadow: 0 0 10px var(--green), 0 0 18px rgba(63,185,80,0.4); opacity: 0.75; }
}
@keyframes pulse-yellow {
    0%,100% { box-shadow: 0 0 4px var(--yellow); opacity: 1; }
    50% { box-shadow: 0 0 10px var(--yellow), 0 0 18px rgba(210,153,34,0.4); opacity: 0.75; }
}
```

---

## 8. UI Patterns — JavaScript

### Tab System

```html
<!-- Tab pages in content area -->
<div id="pageHome" class="page active">...</div>
<div id="pagePlanner" class="page">...</div>
<div id="pageSettings" class="page">...</div>

<!-- Tab bar -->
<div class="tab-bar">
  <button class="tab-btn tab-active" data-tab="Home" onclick="switchTab('Home')">
    <svg>...</svg><span>Home</span>
  </button>
  <button class="tab-btn" data-tab="Planner" onclick="switchTab('Planner')">
    <svg>...</svg><span>Planner</span>
  </button>
  <button class="tab-btn" data-tab="Settings" onclick="switchTab('Settings')">
    <svg>...</svg><span>Settings</span>
  </button>
</div>
```

```css
.tab-btn {
    flex: 1; display: flex; flex-direction: column; align-items: center;
    gap: 2px; background: none; border: none; color: var(--dim);
    font-size: 10px; cursor: pointer; padding: 0;
    -webkit-tap-highlight-color: transparent;
}
.tab-btn:active { opacity: 0.7; }
.tab-btn.tab-active { color: var(--blue); }
.tab-btn svg { width: 26px; height: 26px; }
.page { display: none; }
.page.active { display: block; }
```

```javascript
let _currentTab = 'Home';

function switchTab(tab) {
    closeAllModals();
    _currentTab = tab;
    try { sessionStorage.setItem('_tab', tab); } catch(e) {}

    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    const page = document.getElementById('page' + tab);
    if (page) page.classList.add('active');

    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('tab-active'));
    document.querySelectorAll('.tab-btn[data-tab="' + tab + '"]').forEach(b => b.classList.add('tab-active'));

    scrollTop();

    // Tab-specific setup callbacks
    if (tab === 'Settings') loadSettings();
    // Add more tab init callbacks as needed
}
```

### Modal System

```css
.confirm-overlay {
    position: fixed; top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.8); display: none; align-items: center;
    justify-content: center; z-index: 100; overflow: hidden;
    overscroll-behavior: contain; padding: 0 12px;
}
.modal-panel {
    background: var(--card); border-radius: 12px; padding: 16px;
    width: 95%; border: 1px solid var(--border);
    max-height: 70vh; overflow-y: auto; overscroll-behavior: contain;
}
```

```javascript
let _modalCount = 0;

function openModal(id) {
    const el = document.getElementById(id);
    if (el.style.display === 'flex') return;  // Prevent double-open
    el.style.display = 'flex';
    el.scrollTop = 0;
    _modalCount++;
    if (_modalCount === 1) {
        document.querySelector('.tab-bar').style.zIndex = '0';
        document.getElementById('contentWrap').style.overflow = 'hidden';
        document.body.style.overflow = 'hidden';
    }
}

function closeModal(id) {
    document.getElementById(id).style.display = 'none';
    _modalCount = Math.max(0, _modalCount - 1);
    if (_modalCount === 0) {
        document.querySelector('.tab-bar').style.zIndex = '100';
        document.getElementById('contentWrap').style.overflow = '';
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
    }
});
```

### Toast Notifications

Translucent design with backdrop blur and color-coded borders. Positioned below the iOS notch using `env(safe-area-inset-top)`.

```javascript
function showToast(msg, color) {
    let t = document.getElementById('mainToast');
    if (!t) {
        t = document.createElement('div');
        t.id = 'mainToast';
        t.style.cssText = 'position:fixed;top:env(safe-area-inset-top,48px);left:50%;' +
            'transform:translateX(-50%);padding:6px 14px;border-radius:6px;' +
            'border:1px solid transparent;' +
            'font-size:13px;font-weight:600;opacity:0;transition:opacity 0.3s;' +
            'z-index:200;pointer-events:none;' +
            'backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);';
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
```

### API Helper

```javascript
async function api(url, opts = {}) {
    const r = await fetch(url, opts);
    if (r.status === 401) { window.location.reload(); return null; }
    return r.json();
}
```

### Detail Toggle (Expandable Sections)

```html
<span class="detail-toggle" onclick="toggleDetail('mySection')">▸ More Details</span>
<div class="detail-section" id="mySection" style="display:none">
  Content here
</div>
```

```javascript
function toggleDetail(id) {
    const el = document.getElementById(id);
    const toggle = el.previousElementSibling;
    if (el.style.display === 'block') {
        el.style.display = 'none';
        toggle.textContent = toggle.textContent.replace('▾', '▸');
    } else {
        el.style.display = 'block';
        toggle.textContent = toggle.textContent.replace('▸', '▾');
    }
}
```

### Pull-to-Refresh

This was tricky to get right on iOS. Key challenges: it must only trigger when the content scroller is at the top (not just the page), it needs to be disabled when modals are open or when the user is interacting with charts/canvases, and it needs to coexist with the fixed header and tab bar without visual glitches.

The progress bar sits right below the sticky header, uses a resistance curve so the pull feels natural (slow start, accelerates), and changes color from blue to green when the threshold is reached.

**State variables needed** (defined elsewhere in the app):

```javascript
let _modalCount = 0;          // From the modal system (section 8)
let _currentTab = 'Home';     // From the tab system (section 8)
let _chartTouchActive = false; // Set true when user is touching a canvas/chart
```

**Disable modals' background scroll** (prevents pull-to-refresh and content scrolling while a modal is open):

```javascript
document.addEventListener('touchmove', function(e) {
    if (_modalCount === 0) return;
    // Allow scrolling inside modal content areas
    if (e.target.closest('.modal-panel') || e.target.closest('.confirm-box')) return;
    e.preventDefault();
}, {passive: false});  // passive: false is required to call preventDefault
```

**Pull-to-refresh implementation:**

```javascript
(function() {
    let startY = 0, pulling = false, triggered = false;
    const threshold = 180;  // pixels of pull needed to trigger refresh
    const showAfter = 30;   // don't show bar until this much pull (avoids flicker)

    // Create the progress bar element
    let bar = document.createElement('div');
    bar.id = '_ptrBar';
    bar.style.cssText = 'position:fixed;left:0;right:0;height:3px;background:var(--blue);' +
        'z-index:55;transform:scaleX(0);transform-origin:left;' +
        'transition:transform 0.15s;display:none;top:0';
    document.body.appendChild(bar);

    // Position bar right below the sticky header (not behind it)
    function positionBar() {
        const hdr = document.getElementById('stickyHeader');
        if (hdr) bar.style.top = hdr.offsetHeight + 'px';
    }

    // Get the scrollable content area (NOT document.body)
    function getScroller() {
        return document.getElementById('contentWrap');
    }

    function isModalOpen() {
        return _modalCount > 0;
    }

    // ── touchstart: begin tracking if conditions are met ──
    document.addEventListener('touchstart', e => {
        const scroller = getScroller();
        const atTop = scroller ? scroller.scrollTop <= 0 : true;

        // Only start if:
        //  - Content is scrolled to top
        //  - Single finger touch
        //  - No modal open
        //  - Not interacting with a chart/canvas
        //  - Not on a tab where pull-to-refresh should be disabled
        if (atTop && e.touches.length === 1
            && !isModalOpen()
            && !_chartTouchActive
            && _currentTab !== 'Chat'     // <-- Add tab names to disable on
        ) {
            startY = e.touches[0].clientY;
            pulling = true;
            triggered = false;
            positionBar();
        }
    }, {passive: true});

    // ── touchmove: show progress bar with resistance curve ──
    document.addEventListener('touchmove', e => {
        if (!pulling || isModalOpen() || _chartTouchActive) {
            pulling = false;
            return;
        }
        const dy = Math.max(0, e.touches[0].clientY - startY);
        if (dy > showAfter) {
            bar.style.display = '';
            // Resistance curve: quadratic ease — slow start, accelerates
            // Feels natural and prevents accidental triggers
            const raw = (dy - showAfter) / (threshold - showAfter);
            const progress = Math.min(1, raw * raw);
            bar.style.transform = `scaleX(${progress})`;
            // Color feedback: blue while pulling, green when ready to release
            bar.style.background = progress >= 1 ? 'var(--green)' : 'var(--blue)';
        }
    }, {passive: true});

    // ── touchend: trigger refresh or cancel ──
    document.addEventListener('touchend', e => {
        if (!pulling || isModalOpen() || _chartTouchActive) {
            pulling = false;
            return;
        }
        pulling = false;
        const scroller = getScroller();
        const atTop = scroller ? scroller.scrollTop <= 0 : true;
        const dy = Math.max(0, (e.changedTouches[0] || {}).clientY - startY);

        if (dy >= threshold && atTop) {
            // Threshold reached — show loading animation then reload
            bar.style.transform = 'scaleX(1)';
            bar.style.background = 'var(--green)';
            bar.style.transition = 'none';

            // Animated loading shimmer
            let pos = 0;
            const anim = setInterval(() => {
                pos = (pos + 2) % 100;
                bar.style.transform = 'scaleX(1)';
                bar.style.background = `linear-gradient(90deg, var(--bg) ${pos}%, var(--green) ${pos+20}%, var(--bg) ${pos+40}%)`;
            }, 30);

            // Brief delay so animation is visible, then reload
            setTimeout(() => { clearInterval(anim); location.reload(); }, 300);
        } else {
            // Didn't reach threshold — smoothly retract
            bar.style.transform = 'scaleX(0)';
            bar.style.transition = 'transform 0.2s';
            setTimeout(() => {
                bar.style.display = 'none';
                bar.style.transition = 'transform 0.1s';
            }, 200);
        }
    }, {passive: true});
})();
```

**Key design decisions & gotchas:**

- **`passive: true` on touch listeners**: Required for smooth scrolling on iOS. The modal touchmove blocker uses `passive: false` because it needs `preventDefault()`, but the pull-to-refresh listeners don't block — they just read touch position.
- **Checks `scroller.scrollTop` not `window.scrollY`**: Because the app uses a fixed-position `#contentWrap` div for scrolling (not the body), you must check the scroller element's scroll position.
- **`_chartTouchActive` guard**: If the user is dragging on a chart/canvas (crosshair, pan, etc.), pull-to-refresh must not interfere. Set this flag true on canvas touchstart, false on touchend.
- **Per-tab disabling**: `_currentTab !== 'Chat'` prevents pull-to-refresh on tabs where it doesn't make sense (e.g., a chat overlay or a tab with its own drag interactions). Add more tab names to the condition as needed.
- **Modal guard on every phase**: Modals can open mid-gesture (e.g., long-press triggers a modal while pulling), so check `isModalOpen()` in touchmove and touchend too, not just touchstart.
- **Resistance curve**: `raw * raw` (quadratic) makes the bar fill slowly at first and accelerate — feels natural and prevents accidental triggers from small swipes.
- **`showAfter` threshold**: The first 30px of pull shows nothing. This prevents the blue bar from flickering during normal scroll attempts.
- **Bar positioned below header**: `positionBar()` reads the header's actual height so the bar renders at the seam, not hidden behind the fixed header.
- **Reload vs. callback**: This implementation does a full `location.reload()`. If you want a soft refresh (just re-fetch data), replace `location.reload()` with your refresh function and reset the bar manually.

### Scroll Helpers

```javascript
const $ = s => document.querySelector(s);
function _cw() { return document.getElementById('contentWrap'); }
function scrollTop() { const c = _cw(); if (c) c.scrollTop = 0; }
function scrollBottom() { const c = _cw(); if (c) c.scrollTop = c.scrollHeight; }
```

---

## 9. PWA Setup

### Manifest Endpoint

```python
@app.route("/manifest.json")
def manifest_json():
    return jsonify({
        "name": "My App Name",
        "short_name": "My App",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0d1117",
        "theme_color": "#0d1117",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ]
    })
```

### Service Worker (Push Notifications)

```python
@app.route("/sw.js")
def service_worker():
    sw_code = """
self.addEventListener('push', event => {
  let data = {title: 'My App', body: 'Notification', tag: 'default', url: '/'};
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
    clients.matchAll({type: 'window', includeUncontrolled: true}).then(list => {
      for (const c of list) {
        if (c.url.includes(self.location.origin) && 'focus' in c) {
          c.navigate(url);
          return c.focus();
        }
      }
      return clients.openWindow(url);
    })
  );
});
"""
    return app.response_class(sw_code, mimetype='application/javascript',
                               headers={'Service-Worker-Allowed': '/'})
```

### HTML Meta Tags

```html
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<link rel="manifest" href="/manifest.json">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0d1117">
<link rel="apple-touch-icon" href="/icon-192.png">
```

### Push Subscription Flow (JavaScript)

```javascript
let pushSubscription = null;

async function initPush() {
    const statusEl = document.getElementById('pushStatus');
    const btnEl = document.getElementById('pushToggleBtn');
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
        statusEl.textContent = 'Push not supported';
        return;
    }
    if (location.protocol !== 'https:' && location.hostname !== 'localhost') {
        statusEl.textContent = 'Push requires HTTPS';
        return;
    }
    try {
        const reg = await navigator.serviceWorker.register('/sw.js');
        await navigator.serviceWorker.ready;
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
        statusEl.textContent = 'Push error: ' + e.message;
    }
}

async function togglePush() {
    if (pushSubscription) {
        const endpoint = pushSubscription.endpoint;
        await pushSubscription.unsubscribe();
        pushSubscription = null;
        await api('/api/push/unsubscribe', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({endpoint})
        });
        showToast('Notifications disabled', 'yellow');
        initPush();
    } else {
        try {
            const keyResp = await api('/api/push/vapid-key');
            if (!keyResp.key) { showToast('VAPID key not configured', 'red'); return; }
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
            await api('/api/push/subscribe', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({subscription: pushSubscription.toJSON()})
            });
            showToast('Notifications enabled!', 'green');
            initPush();
        } catch(e) {
            if (e.name === 'NotAllowedError') showToast('Permission denied — check Settings', 'red');
            else showToast('Subscribe error: ' + e.message, 'red');
        }
    }
}
```

### Push API Endpoints

```python
@app.route("/api/push/vapid-key")
@requires_auth
def api_vapid_key():
    try:
        from push import get_public_key
        return jsonify({"key": get_public_key()})
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
```

---

## 10. Service Management (from Dashboard)

### API Endpoints

```python
@app.route("/api/server/status")
@requires_auth
def api_server_status():
    import subprocess as _sp
    try:
        r = _sp.run(["supervisorctl", "status"],
                    capture_output=True, text=True, timeout=5)
        services = {}
        for line in r.stdout.strip().split("\n"):
            parts = line.split()
            if len(parts) >= 2:
                services[parts[0]] = {"status": parts[1], "detail": " ".join(parts[2:])}
        return jsonify(services)
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/api/server/control", methods=["POST"])
@requires_auth
def api_server_control():
    import subprocess as _sp
    data = request.get_json() or {}
    action = data.get("action", "restart")
    service = data.get("service", "all")

    if action not in ("start", "stop", "restart"):
        return jsonify({"error": "Invalid action"}), 400

    valid_services = {"myapp-dashboard", "myapp-worker"}
    services = list(valid_services) if service == "all" else [service]
    services = [s for s in services if s in valid_services]

    results = {}
    for svc in services:
        try:
            r = _sp.run(["supervisorctl", action, svc],
                       capture_output=True, text=True, timeout=10)
            results[svc] = r.stdout.strip() or r.stderr.strip()
        except Exception as e:
            results[svc] = str(e)
    return jsonify(results)
```

---

## 11. Email Deploy System

The most complex reusable piece. This runs as a background thread inside the dashboard process, watches a Gmail inbox via IMAP IDLE, and auto-deploys `.py` file attachments.

### How It Works

1. Dashboard starts a background thread on boot
2. Thread connects to Gmail via IMAP SSL
3. Uses IMAP IDLE to wait for new emails (no polling)
4. On new email: checks sender against allowlist → syntax-checks `.py` attachments → backs up existing files → deploys → restarts services
5. Also supports `pip: package_name` and `restart: bot/dashboard/all` commands in email body

### Setup

1. Create a Gmail account for the app
2. Enable 2FA, generate an App Password
3. Set `DEPLOY_EMAIL`, `DEPLOY_EMAIL_PASS`, `DEPLOY_ALLOWED_SENDERS` in `.env`
4. For nicer addresses: Cloudflare Email Routing forwards `myapp@bbrooks.dev` → Gmail

### Core Implementation

```python
_email_deploy_conn = None

def _start_email_deploy():
    """Background thread: watches Gmail via IMAP IDLE for deploy emails."""
    import imaplib, email as email_mod, shutil, subprocess, threading, time, socket
    global _email_deploy_conn

    imap_user = os.environ.get("DEPLOY_EMAIL", "")
    imap_pass = os.environ.get("DEPLOY_EMAIL_PASS", "")
    allowed_senders = [s.strip().lower() for s in
                       os.environ.get("DEPLOY_ALLOWED_SENDERS", "").split(",") if s.strip()]

    if not imap_user or not imap_pass or not allowed_senders:
        print("[EmailDeploy] Disabled — set DEPLOY_EMAIL, DEPLOY_EMAIL_PASS, DEPLOY_ALLOWED_SENDERS")
        return

    bot_dir = os.environ.get("BOT_DIR", "/opt/myapp")
    imap_host = os.environ.get("DEPLOY_IMAP_HOST", "imap.gmail.com")

    def deploy_files(attachments):
        """Deploy .py attachments. Returns (uploaded, errors)."""
        backup_dir = os.path.join(bot_dir, "_backup")
        os.makedirs(backup_dir, exist_ok=True)
        uploaded, errors = [], []
        for fname, content in attachments:
            if not fname.endswith('.py'):
                errors.append(f"{fname}: not .py, skipped")
                continue
            try:
                compile(content, fname, 'exec')
            except SyntaxError as e:
                errors.append(f"{fname}: syntax error line {e.lineno}: {e.msg}")
                continue
            dest = os.path.join(bot_dir, fname)
            if os.path.exists(dest):
                shutil.copy2(dest, os.path.join(backup_dir, fname))
            with open(dest, 'wb') as out:
                out.write(content)
            uploaded.append(fname)
        return uploaded, errors

    def restart_services(services=None):
        # Customize these service names for your project
        if services is None:
            services = ["myapp-worker", "myapp-dashboard"]
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
        """Extract pip: lines from email body, install packages."""
        import re, sys as _sys
        results = []
        for line in body_text.splitlines():
            line = line.strip()
            if line.lower().startswith('pip:'):
                pkg = line[4:].strip()
                if not pkg: continue
                if not re.match(r'^[a-zA-Z0-9_.>=<!\-\[\],\s]+$', pkg):
                    results.append(f"✗ pip: {pkg} — invalid characters")
                    continue
                try:
                    r = subprocess.run(
                        [_sys.executable, "-m", "pip", "install", pkg, "--break-system-packages"],
                        capture_output=True, text=True, timeout=120)
                    if r.returncode == 0:
                        results.append(f"✓ pip install {pkg}")
                    else:
                        results.append(f"✗ pip install {pkg} (exit {r.returncode})")
                except Exception as e:
                    results.append(f"✗ pip install {pkg}: {e}")
        return results

    def process_email(mail, num):
        """Process a single deploy email."""
        _, data = mail.fetch(num, '(RFC822)')
        msg = email_mod.message_from_bytes(data[0][1])

        from_raw = msg.get("From", "")
        from_addr = from_raw.split("<")[1].split(">")[0] if "<" in from_raw else from_raw
        from_addr = from_addr.strip().lower()

        if from_addr not in allowed_senders:
            return

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
            elif not fname and part.get_content_type() in ('text/plain', 'text/html'):
                payload = part.get_payload(decode=True)
                if payload:
                    decoded = payload.decode(errors='replace')
                    if '<' in decoded:
                        import re as _re
                        decoded = _re.sub(r'<br\s*/?>', '\n', decoded, flags=_re.IGNORECASE)
                        decoded = _re.sub(r'<[^>]+>', '', decoded)
                    body_text += decoded + "\n"

        pip_results = run_pip_installs(body_text)

        if attachments:
            uploaded, errors = deploy_files(attachments)
            if uploaded:
                # Send push notification BEFORE restart
                try:
                    from push import send_to_all
                    send_to_all("Deploy Complete", f"{len(uploaded)} files deployed", tag="deploy")
                except: pass
                time.sleep(0.5)

                # Dashboard-only files don't need worker restart
                dashboard_only = {'dashboard.py', 'game.py'}
                if all(f in dashboard_only for f in uploaded):
                    restart_services(["myapp-dashboard"])
                else:
                    restart_services()

    def idle_loop():
        global _email_deploy_conn
        while True:
            mail = None
            try:
                mail = imaplib.IMAP4_SSL(imap_host, timeout=30)
                mail.login(imap_user, imap_pass)
                mail.select("INBOX")
                _email_deploy_conn = mail

                while True:
                    _, nums = mail.search(None, 'UNSEEN')
                    for num in [n for n in nums[0].split() if n]:
                        try: process_email(mail, num)
                        except Exception as e: print(f"[EmailDeploy] Process error: {e}")

                    # IMAP IDLE — wait for new mail (29 min timeout, Gmail max)
                    tag = mail._new_tag().decode()
                    mail.send(f'{tag} IDLE\r\n'.encode())
                    mail.readline()  # + idling
                    # Wait for data or timeout
                    import select
                    readable, _, _ = select.select([mail.socket()], [], [], 1740)
                    mail.send(b'DONE\r\n')
                    mail.readline()

            except Exception as e:
                print(f"[EmailDeploy] Error: {e}")
                _email_deploy_conn = None
            finally:
                if mail:
                    try: mail.logout()
                    except: pass
            time.sleep(10)  # Reconnect delay

    thread = threading.Thread(target=idle_loop, daemon=True)
    thread.start()
```

### Start on Boot

```python
# At the bottom of dashboard.py, before app.run():
if __name__ == "__main__":
    init_db()
    _start_email_deploy()
    app.run(host=DASHBOARD_HOST, port=DASHBOARD_PORT)
```

---

## 12. Log System API

```python
@app.route("/api/logs")
@requires_auth
def api_logs():
    before = request.args.get("before", type=int)
    limit = min(int(request.args.get("limit", 100)), 500)
    return jsonify(get_logs(before_id=before, limit=limit))

@app.route("/api/logs/new")
@requires_auth
def api_logs_new():
    after = request.args.get("after", type=int, default=0)
    return jsonify(get_logs_after(after))
```

---

## 13. Time Helper

```python
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")

def to_central(iso_str: str) -> str:
    """Convert ISO UTC string to Central Time display."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        ct = dt.astimezone(CT)
        return ct.strftime("%m/%d %I:%M:%S %p CT")
    except Exception:
        return iso_str
```

---

## 14. Known Patterns & Gotchas

- **iOS PWA has no localStorage** — keep all state in JS variables or server-side
- **Font size 16px on inputs** — prevents iOS auto-zoom on focus
- **padding-bottom: 30px on tab bar** — accounts for iOS home indicator
- **env(safe-area-inset-top)** — respects the notch on toast positioning
- **Single-row state table with JSON columns** — simple but every update writes the whole row; fine for single-server
- **Command queue pattern** — dashboard never directly mutates worker state; enqueues commands that the worker picks up
- **Dashboard-only deploys** — when only `dashboard.py` changes, skip restarting the worker process to avoid interrupting background tasks
- **WAL checkpoint before backup** — ensures the backup captures all committed data
- **IMAP IDLE timeout** — Gmail caps at 29 minutes; reconnect loop handles this
- **`overscroll-behavior: contain`** — prevents iOS pull-to-refresh from interfering with in-app scrolling
- **Tri-state push returns** — `True` = sent, `False` = dead subscription (remove), `None` = temporary failure (keep)
- **Pull-to-refresh checks scroller, not window** — because the app scrolls inside `#contentWrap` (fixed position), you must check `scroller.scrollTop` not `window.scrollY`; guards needed for modals, charts, and per-tab disabling

---

## Quick Start Checklist for a New Project

1. Create DigitalOcean droplet
2. Install: `apt update && apt install -y python3 python3-pip nginx supervisor certbot python3-certbot-nginx`
3. Create project dir: `mkdir -p /opt/myapp`
4. Create `.env` with credentials
5. Create `config.py`, `db.py`, `push.py`, `dashboard.py`
6. Create supervisor config, reload
7. Create nginx config (HTTP only first), reload
8. Add Cloudflare A record for subdomain
9. Run certbot for SSL
10. Fix nginx config with proxy_pass and headers
11. Set up Cloudflare Email Routing for deploy address
12. Create Gmail + App Password for IMAP deploy
13. Generate VAPID keys for push notifications
14. Add to Home Screen on iOS for PWA
