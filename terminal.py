"""
terminal.py — Web terminal for the trading platform.
Flask + flask-socketio app serving a browser-based shell via PTY.
"""

import eventlet
eventlet.monkey_patch()

import os, sys, pty, json, errno, signal, struct, fcntl, termios, hashlib, secrets, shutil, subprocess
import pwd
from flask import Flask, request, redirect, jsonify
from flask_socketio import SocketIO, emit, disconnect

# ── Platform imports ──────────────────────────────────────────
sys.path.insert(0, "/opt/trading-platform")
from config import DASHBOARD_USER, DASHBOARD_PASS
from db import get_config, set_config, get_conn, now_utc

# ── App setup ─────────────────────────────────────────────────
app = Flask(__name__)
app.config["SECRET_KEY"] = secrets.token_hex(32)

socketio = SocketIO(app, async_mode="eventlet", cors_allowed_origins=None,
                    path="/terminal/ws/socket.io")

# ── Auth helpers (mirrors dashboard.py logic) ─────────────────

def _get_session_salt():
    try:
        salt = get_config("_session_salt")
        if salt:
            return salt
    except Exception:
        pass
    salt = secrets.token_hex(16)
    set_config("_session_salt", salt)
    return salt

def _auth_token():
    salt = _get_session_salt()
    try:
        stored_hash = get_config("dashboard_pass_hash")
        if stored_hash:
            return hashlib.sha256(f"{DASHBOARD_USER}:{stored_hash}:{salt}".encode()).hexdigest()
    except Exception:
        pass
    return hashlib.sha256(f"{DASHBOARD_USER}:{DASHBOARD_PASS}:{salt}".encode()).hexdigest()

def _is_authenticated():
    token = request.cookies.get("platform_auth")
    if token and secrets.compare_digest(token, _auth_token()):
        return True
    return False

# ── Terminal session persistence ──────────────────────────────

def _init_terminal_db():
    with get_conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS terminal_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                claude_session_id TEXT,
                created_at TEXT NOT NULL,
                ended_at TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS terminal_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER REFERENCES terminal_sessions(id),
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                activity_log TEXT,
                created_at TEXT NOT NULL
            )
        """)


def _save_message(db_session_id, role, content, activity_log=None):
    if not db_session_id:
        return
    with get_conn() as c:
        c.execute("""
            INSERT INTO terminal_messages (session_id, role, content, activity_log, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (db_session_id, role, content,
              json.dumps(activity_log) if activity_log else None,
              now_utc()))


def _get_or_create_db_session(claude_session_id=None):
    """Get current active session or create one. Returns (db_session_id, claude_session_id)."""
    with get_conn() as c:
        row = c.execute("""
            SELECT id, claude_session_id FROM terminal_sessions
            WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1
        """).fetchone()
        if row:
            return row["id"], row["claude_session_id"]
        # Create new session
        c.execute("""
            INSERT INTO terminal_sessions (claude_session_id, created_at)
            VALUES (?, ?)
        """, (claude_session_id, now_utc()))
        return c.lastrowid, claude_session_id


# ── Active sessions ───────────────────────────────────────────
_active_session = {"fd": None, "pid": None, "sid": None}
_claude_session = {"session": None}   # forward declaration for disconnect handler

def _cleanup_shell():
    fd = _active_session["fd"]
    pid = _active_session["pid"]
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except (OSError, ChildProcessError):
            pass
    _active_session["fd"] = None
    _active_session["pid"] = None
    _active_session["sid"] = None

# ── HTTP routes ───────────────────────────────────────────────

@app.route("/terminal")
def terminal_page():
    if not _is_authenticated():
        return redirect("https://bot.bbrooks.dev/login")
    return TERMINAL_HTML

@app.route("/terminal/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/terminal/api/session/current")
def api_session_current():
    if not _is_authenticated():
        return jsonify({"error": "unauthorized"}), 401
    with get_conn() as c:
        row = c.execute("""
            SELECT id, claude_session_id, created_at FROM terminal_sessions
            WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1
        """).fetchone()
        if not row:
            return jsonify({"session": None, "messages": []})
        session = {
            "id": row["id"],
            "claude_session_id": row["claude_session_id"],
            "created_at": row["created_at"],
        }
        msgs = c.execute("""
            SELECT role, content, activity_log, created_at FROM terminal_messages
            WHERE session_id = ? ORDER BY id ASC
        """, (row["id"],)).fetchall()
        messages = [
            {"role": m["role"], "content": m["content"],
             "activity_log": m["activity_log"], "created_at": m["created_at"]}
            for m in msgs
        ]
    return jsonify({"session": session, "messages": messages})


@app.route("/terminal/api/session/new", methods=["POST"])
def api_session_new():
    if not _is_authenticated():
        return jsonify({"error": "unauthorized"}), 401
    with get_conn() as c:
        # End all active sessions
        c.execute("UPDATE terminal_sessions SET ended_at = ? WHERE ended_at IS NULL", (now_utc(),))
        # Create new session
        c.execute("""
            INSERT INTO terminal_sessions (created_at) VALUES (?)
        """, (now_utc(),))
        new_id = c.lastrowid
    return jsonify({"session": {"id": new_id, "claude_session_id": None, "created_at": now_utc()}})


# ── WebSocket handlers ────────────────────────────────────────

@socketio.on("connect", namespace="/terminal/ws")
def ws_connect():
    # Auth check on WebSocket
    token = request.cookies.get("platform_auth")
    if not token or not secrets.compare_digest(token, _auth_token()):
        disconnect()
        return

@socketio.on("shell_start", namespace="/terminal/ws")
def ws_shell_start():
    """Spawn shell PTY on demand (when user switches to Shell tab)."""
    # Only one session at a time
    if _active_session["fd"] is not None:
        _cleanup_shell()

    pid, fd = pty.fork()
    if pid == 0:
        # Child process
        os.chdir("/opt/trading-platform")
        os.environ["TERM"] = "xterm-256color"
        os.environ["LANG"] = "en_US.UTF-8"
        os.execvp("/bin/bash", ["/bin/bash", "--login"])
    else:
        _active_session["fd"] = fd
        _active_session["pid"] = pid
        _active_session["sid"] = request.sid

        # Non-blocking reads
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        socketio.start_background_task(_read_pty, fd, request.sid)

@socketio.on("input", namespace="/terminal/ws")
def ws_input(data):
    fd = _active_session["fd"]
    if fd is not None and request.sid == _active_session["sid"]:
        try:
            os.write(fd, data.encode("utf-8") if isinstance(data, str) else data)
        except OSError:
            pass

@socketio.on("resize", namespace="/terminal/ws")
def ws_resize(data):
    fd = _active_session["fd"]
    if fd is not None and request.sid == _active_session["sid"]:
        try:
            rows = int(data.get("rows") or 24)
            cols = int(data.get("cols") or 80)
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
        except (OSError, ValueError, TypeError):
            pass

@socketio.on("disconnect", namespace="/terminal/ws")
def ws_disconnect():
    if request.sid == _active_session["sid"]:
        _cleanup_shell()
    # Also clean up Claude session if owned by this client
    s = _claude_session.get("session")
    if s and s.sid == request.sid:
        _cleanup_claude()

def _read_pty(fd, sid):
    """Background task: read PTY output and emit to client."""
    while True:
        try:
            eventlet.sleep(0.01)
            if _active_session["fd"] != fd:
                break
            try:
                data = os.read(fd, 4096)
                if not data:
                    break
                socketio.emit("output", data.decode("utf-8", errors="replace"),
                              namespace="/terminal/ws", to=sid)
            except (OSError, IOError) as e:
                if getattr(e, 'errno', None) in (errno.EAGAIN, errno.EWOULDBLOCK):
                    continue
                break
        except Exception:
            break

    # Shell exited — notify client
    try:
        socketio.emit("exit", namespace="/terminal/ws", to=sid)
    except Exception:
        pass
    if _active_session["fd"] == fd:
        _cleanup_shell()

# ── Claude Code session (stream-json mode) ───────────────────

_CLAUDE_SEARCH_PATHS = [
    "/usr/local/bin/claude",
    "/root/.local/bin/claude",
    "/root/.npm-global/bin/claude",
    "/usr/bin/claude",
]

def _find_claude():
    p = shutil.which("claude")
    if p:
        return p
    for p in _CLAUDE_SEARCH_PATHS:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None

# Pre-resolve claude-worker UID/GID at import time
try:
    _CW = pwd.getpwnam("claude-worker")
except KeyError:
    _CW = None


class ClaudeCodeSession:
    def __init__(self, sio, sid, claude_session_id=None, db_session_id=None):
        self.sio = sio
        self.sid = sid
        self.claude_session_id = claude_session_id  # Claude's session ID for --resume
        self.db_session_id = db_session_id          # terminal_sessions.id
        self.process = None      # subprocess.Popen
        self.alive = True
        self.busy = False
        self.prompt_count = 0
        self.needs_restart = False

    def send_prompt(self, text):
        if self.busy:
            self.sio.emit("claude_error", {"text": "Still processing"},
                          namespace="/terminal/ws", to=self.sid)
            return

        claude_path = _find_claude()
        if not claude_path:
            self.sio.emit("claude_error",
                          {"text": "Claude Code not found", "detail": "Binary not in PATH"},
                          namespace="/terminal/ws", to=self.sid)
            return

        self.busy = True
        self._emit_state("busy")
        self.prompt_count += 1

        # Ensure we have a db session (create on first prompt)
        if not self.db_session_id:
            self.db_session_id, stored_claude_id = _get_or_create_db_session(self.claude_session_id)
            if not self.claude_session_id and stored_claude_id:
                self.claude_session_id = stored_claude_id

        # Save user message
        _save_message(self.db_session_id, "user", text)

        cmd = [claude_path, "-p", text,
               "--dangerously-skip-permissions",
               "--output-format", "stream-json", "--verbose"]

        if self.claude_session_id:
            cmd.extend(["--resume", self.claude_session_id])

        env = os.environ.copy()
        env.pop("ANTHROPIC_API_KEY", None)
        if _CW:
            env["HOME"] = _CW.pw_dir
            env["USER"] = "claude-worker"

        def _preexec():
            if _CW:
                os.setgid(_CW.pw_gid)
                os.setuid(_CW.pw_uid)

        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                cwd="/opt/trading-platform",
                env=env,
                preexec_fn=_preexec,
            )
        except Exception as e:
            self.sio.emit("claude_error",
                          {"text": "Failed to start", "detail": str(e)},
                          namespace="/terminal/ws", to=self.sid)
            self.busy = False
            self._emit_state("ready")
            return

        self.sio.start_background_task(self._read_output)

    def _read_output(self):
        """Read stream-json output line by line."""
        response_parts = []
        activityLog = []

        def _read_lines():
            """Read stdout in a thread-safe way (eventlet + subprocess)."""
            for raw_line in self.process.stdout:
                if not self.alive:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                yield line

        try:
            for line in _read_lines():
                # Emit raw for the log tab
                self.sio.emit("claude_raw", {"data": line + "\n"},
                              namespace="/terminal/ws", to=self.sid)

                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type", "")

                # Assistant message — may contain text or tool_use
                if msg_type == "assistant":
                    content = msg.get("message", {}).get("content", [])
                    for block in content:
                        if block.get("type") == "text":
                            response_parts.append(block["text"])
                        elif block.get("type") == "tool_use":
                            name = block.get("name", "")
                            inp = block.get("input", {})
                            # Detect terminal restart commands
                            if name == "Bash":
                                cmd_str = str(inp.get("command", ""))
                                if "restart platform-terminal" in cmd_str or "restart platform_terminal" in cmd_str:
                                    self.needs_restart = True
                            if name == "Read":
                                activity = f"Reading {inp.get('file_path', '...')}"
                            elif name == "Write":
                                activity = f"Writing {inp.get('file_path', '...')}"
                            elif name == "Bash":
                                activity = f"Running: {inp.get('command', '...')[:80]}"
                            elif name == "Edit":
                                activity = f"Editing {inp.get('file_path', '...')}"
                            elif name in ("Glob", "Grep"):
                                activity = f"{name}: {inp.get('pattern', '...')}"
                            else:
                                activity = f"{name}"
                            activityLog.append(activity)
                            self.sio.emit("claude_status",
                                          {"text": activity, "type": "activity"},
                                          namespace="/terminal/ws", to=self.sid)

                # Final result
                elif msg_type == "result":
                    sid = msg.get("session_id")
                    if sid:
                        self.claude_session_id = sid
                        # Persist claude_session_id to database
                        if self.db_session_id:
                            try:
                                with get_conn() as c:
                                    c.execute("UPDATE terminal_sessions SET claude_session_id = ? WHERE id = ?",
                                              (sid, self.db_session_id))
                            except Exception:
                                pass
                    result_text = msg.get("result", "")
                    if result_text:
                        response_parts = [result_text]

        except Exception as e:
            self.sio.emit("claude_error",
                          {"text": "Read error", "detail": str(e)},
                          namespace="/terminal/ws", to=self.sid)

        # Process finished
        self.process.wait()

        response = "\n".join(response_parts).strip() if response_parts else ""

        if response:
            self.sio.emit("claude_response",
                          {"text": response, "id": self.claude_session_id or ""},
                          namespace="/terminal/ws", to=self.sid)
            # Save assistant message with activity log
            _save_message(self.db_session_id, "assistant", response, activityLog)
        elif self.process.returncode != 0:
            stderr_out = self.process.stderr.read().decode("utf-8", errors="replace").strip()
            error_text = "Claude exited with error" + (": " + stderr_out[:500] if stderr_out else "")
            self.sio.emit("claude_error",
                          {"text": "Claude exited with error",
                           "detail": stderr_out[:500]},
                          namespace="/terminal/ws", to=self.sid)
            _save_message(self.db_session_id, "error", error_text)

        # Check response text for restart indicators
        restart_phrases = [
            "restart platform-terminal",
            "restart the terminal",
            "supervisorctl restart platform-terminal",
            "need to restart the terminal",
        ]
        response_lower = response.lower()
        for phrase in restart_phrases:
            if phrase in response_lower:
                self.needs_restart = True
                break

        # If restart needed, emit event and trigger delayed restart
        if self.needs_restart:
            self.needs_restart = False
            self.sio.emit("claude_status",
                          {"text": "Restarting terminal service...", "type": "restart"},
                          namespace="/terminal/ws", to=self.sid)
            eventlet.sleep(2)  # Let the client render the response first
            import subprocess as sp
            sp.Popen(["supervisorctl", "restart", "platform-terminal"])

        self.busy = False
        self.process = None
        self._emit_state("ready" if self.alive else "dead")

    def stop(self):
        self.alive = False
        proc = self.process
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        self._emit_state("dead")

    def _emit_state(self, state):
        self.sio.emit("claude_state",
                      {"state": state, "session_id": self.claude_session_id or ""},
                      namespace="/terminal/ws", to=self.sid)


def _cleanup_claude():
    s = _claude_session["session"]
    if s is not None:
        s.stop()
    _claude_session["session"] = None

# ── Claude Code WebSocket handlers ────────────────────────────

@socketio.on("claude_start", namespace="/terminal/ws")
def ws_claude_start(data=None):
    token = request.cookies.get("platform_auth")
    if not token or not secrets.compare_digest(token, _auth_token()):
        disconnect()
        return

    _cleanup_claude()
    claude_session_id = None
    db_session_id = None
    if isinstance(data, dict):
        claude_session_id = data.get("claude_session_id")
        db_session_id = data.get("db_session_id")
    session = ClaudeCodeSession(socketio, request.sid,
                                claude_session_id=claude_session_id,
                                db_session_id=db_session_id)
    _claude_session["session"] = session
    session._emit_state("ready")

@socketio.on("claude_prompt", namespace="/terminal/ws")
def ws_claude_prompt(data):
    s = _claude_session["session"]
    if s and s.sid == request.sid:
        text = data if isinstance(data, str) else data.get("text", "")
        if text:
            s.send_prompt(text)
    else:
        emit("claude_error", {"text": "No active Claude Code session"})

@socketio.on("claude_stop", namespace="/terminal/ws")
def ws_claude_stop():
    s = _claude_session["session"]
    if s and s.sid == request.sid:
        _cleanup_claude()


# ── HTML template ─────────────────────────────────────────────

TERMINAL_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>Terminal — Trading Platform</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css">
<style>
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
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; overflow: hidden; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }

  #app { width: 100%; height: 100dvh; display: flex; flex-direction: column; }

  /* ── Header ── */
  #header {
    height: 48px; padding: 0 14px; background: var(--card); border-bottom: 1px solid var(--border);
    box-shadow: 0 4px 12px rgba(0,0,0,0.5);
    display: flex; align-items: center; justify-content: space-between; flex-shrink: 0;
  }
  #header .title { font-size: 15px; font-weight: 600; color: var(--text); }
  #header .status { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--dim); }
  #header .status .dot {
    width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
  }
  .dot-ready { background: var(--green); }
  .dot-busy { background: var(--yellow); animation: pulse 1s infinite; }
  .dot-dead, .dot-disconnected { background: var(--red); }
  .dot-none { background: var(--dim); }
  @keyframes pulse { 50% { opacity: 0.4; } }
  #new-session-btn {
    background: none; border: 1px solid var(--border); color: var(--dim); font-size: 12px;
    padding: 5px 12px; border-radius: 6px; cursor: pointer; font-family: inherit;
  }
  #new-session-btn:active { background: var(--bg); }

  /* ── Tab bar ── */
  #tabs {
    display: flex; background: var(--card); border-bottom: 1px solid var(--border); flex-shrink: 0;
  }
  .tab {
    flex: 1; padding: 10px 0; text-align: center; font-size: 13px; font-weight: 500;
    color: var(--dim); cursor: pointer; border-bottom: 2px solid transparent;
    transition: color 0.15s, border-color 0.15s; -webkit-tap-highlight-color: transparent;
  }
  .tab.active { color: var(--blue); border-bottom-color: var(--blue); }

  /* ── Panels ── */
  #panels { flex: 1; min-height: 0; position: relative; }
  .panel { position: absolute; inset: 0; display: none; flex-direction: column; }
  .panel.active { display: flex; }

  /* ── Claude panel ── */
  #claude-panel { background: var(--bg); }
  #conversation {
    flex: 1; overflow-y: auto; padding: 12px; overscroll-behavior: none;
    display: flex; flex-direction: column; gap: 12px;
  }
  .msg { max-width: 88%; padding: 10px 14px; border-radius: 16px; font-size: 15px;
    line-height: 1.5; word-wrap: break-word; position: relative; }
  .msg-user {
    align-self: flex-end; background: rgba(88, 166, 255, 0.08);
    border: 1px solid rgba(88, 166, 255, 0.15); border-bottom-right-radius: 4px;
    white-space: pre-wrap;
  }
  .msg-assistant {
    align-self: flex-start; background: var(--card); border: 1px solid var(--border);
    border-bottom-left-radius: 4px;
  }
  .msg-assistant pre {
    background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
    padding: 10px; margin: 8px 0; overflow-x: auto; font-size: 13px;
    font-family: 'SF Mono', Menlo, Monaco, monospace; white-space: pre-wrap;
    word-wrap: break-word;
  }
  .msg-assistant code {
    font-family: 'SF Mono', Menlo, Monaco, monospace; font-size: 13px;
    background: var(--bg); padding: 1px 5px; border-radius: 4px;
  }
  .msg-assistant pre code { background: none; padding: 0; }
  .msg-assistant strong { color: #f0f0f0; }
  .copy-btn {
    position: absolute; top: 6px; right: 6px; background: rgba(255,255,255,0.08);
    border: none; color: var(--dim); font-size: 11px; padding: 3px 8px; border-radius: 6px;
    cursor: pointer; opacity: 0; transition: opacity 0.15s;
    display: flex; align-items: center; justify-content: center;
  }
  .msg-assistant:hover .copy-btn, .msg-assistant:active .copy-btn { opacity: 1; }
  .copy-btn.copied { color: var(--green); }

  /* ── Activity card ── */
  .activity-card {
    align-self: flex-start; max-width: 88%; padding: 8px 14px; border-radius: 8px;
    background: var(--card); border: 1px solid var(--border); font-size: 13px; color: var(--dim);
    display: flex; align-items: center; gap: 8px;
  }
  .activity-card .spinner {
    width: 8px; height: 8px; border-radius: 50%; background: var(--yellow);
    animation: pulse 1s infinite; flex-shrink: 0;
  }
  .activity-collapsed {
    font-size: 12px; color: var(--dim); cursor: pointer; align-self: flex-start;
    padding: 2px 0; margin-top: -8px;
  }
  .activity-collapsed:hover { color: var(--text); }
  .activity-log {
    display: none; align-self: flex-start; max-width: 88%; padding: 8px 12px;
    background: var(--card); border-radius: 8px; font-size: 12px; color: var(--dim);
    font-family: monospace; white-space: pre-wrap; margin-top: -8px;
  }

  /* ── Error / restart ── */
  .msg-error {
    align-self: center; background: rgba(248, 81, 73, 0.08);
    border: 1px solid rgba(248, 81, 73, 0.3);
    color: var(--red); border-radius: 8px; padding: 10px 16px; font-size: 13px;
    text-align: center;
  }
  .msg-error button {
    background: var(--red); color: #fff; border: none; padding: 6px 14px;
    border-radius: 6px; margin-top: 8px; cursor: pointer; font-size: 13px;
    font-family: inherit;
  }

  /* ── Session divider ── */
  .session-divider {
    text-align: center; color: var(--dim); font-size: 11px; padding: 8px 0;
    border-top: 1px solid var(--border); margin-top: 4px;
  }

  /* ── Input area ── */
  #input-area {
    padding: 8px 12px; padding-bottom: calc(8px + env(safe-area-inset-bottom, 0px));
    background: var(--card); border-top: 1px solid var(--border); display: flex; gap: 8px;
    align-items: flex-end; flex-shrink: 0;
  }
  #claude-paste-btn {
    width: 44px; height: 44px; border-radius: 8px; border: 1px solid var(--border);
    background: var(--bg); color: var(--dim); cursor: pointer;
    flex-shrink: 0; display: flex; align-items: center; justify-content: center;
  }
  #claude-paste-btn:active { background: var(--card); }
  #prompt-input {
    flex: 1; background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
    color: var(--text); font-size: 16px; padding: 10px 14px; resize: none;
    font-family: inherit; line-height: 1.4;
    min-height: 44px; max-height: 45vh; overflow-y: auto;
  }
  #prompt-input::placeholder { color: var(--dim); }
  #prompt-input:focus { outline: none; border-color: var(--blue); }
  #send-btn {
    width: 44px; height: 44px; border-radius: 8px; border: none;
    background: var(--blue); color: #fff; cursor: pointer;
    flex-shrink: 0; display: flex; align-items: center; justify-content: center;
    transition: background 0.15s;
  }
  #send-btn:disabled { background: var(--border); color: var(--dim); cursor: default; }
  #send-btn.stop-btn { background: var(--red); }

  /* ── Shell panel ── */
  #shell-panel { background: var(--bg); }
  #shell-wrap { flex: 1; min-height: 0; position: relative; }
  .xterm { height: 100%; }
  #shell-paste-btn {
    position: absolute; bottom: 16px; right: 16px; z-index: 10;
    width: 44px; height: 44px; border-radius: 8px; border: 1px solid var(--border);
    background: var(--bg); color: var(--dim); cursor: pointer;
    display: flex; align-items: center; justify-content: center;
  }
  #shell-paste-btn:hover { background: var(--card); }

  /* ── Log panel ── */
  #log-panel { background: var(--bg); }
  #log-header {
    padding: 8px 12px; display: flex; justify-content: space-between; align-items: center;
    border-bottom: 1px solid var(--border); flex-shrink: 0;
  }
  #log-header span { font-size: 12px; color: var(--dim); }
  #log-clear-btn {
    background: none; border: 1px solid var(--border); color: var(--dim); font-size: 11px;
    padding: 3px 10px; border-radius: 4px; cursor: pointer;
  }
  #log-content {
    flex: 1; overflow-y: auto; padding: 8px 12px; font-family: 'SF Mono', Menlo, Monaco, monospace;
    font-size: 12px; color: var(--dim); white-space: pre-wrap; word-wrap: break-word;
    overscroll-behavior: none; background: var(--bg);
  }
</style>
</head>
<body>
<div id="app">
  <!-- Header -->
  <div id="header">
    <span style="display:flex;align-items:center;"><a href="/" style="color: var(--dim); text-decoration: none; font-size: 12px; margin-right: 8px;">&larr; Dashboard</a><span class="title">Claude Code</span></span>
    <span class="status">
      <span id="claude-dot" class="dot dot-none"></span>
      <span id="claude-status-text">Idle</span>
    </span>

    <button id="new-session-btn">New Session</button>
  </div>

  <!-- Tabs -->
  <div id="tabs">
    <div class="tab active" data-tab="claude">Claude</div>
    <div class="tab" data-tab="shell">Shell</div>
    <div class="tab" data-tab="log">Log</div>
  </div>

  <!-- Panels -->
  <div id="panels">
    <!-- Claude panel -->
    <div id="claude-panel" class="panel active">
      <div id="conversation"></div>
      <div id="input-area">
        <button id="claude-paste-btn" title="Paste"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 4h2a2 2 0 012 2v14a2 2 0 01-2 2H6a2 2 0 01-2-2V6a2 2 0 012-2h2"/><rect x="8" y="2" width="8" height="4" rx="1" ry="1"/><path d="M12 11v6"/><path d="M9 14l3 3 3-3"/></svg></button>
        <textarea id="prompt-input" rows="1" placeholder="Send a prompt to Claude Code..."></textarea>
        <button id="send-btn" title="Send"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg></button>
      </div>
    </div>

    <!-- Shell panel -->
    <div id="shell-panel" class="panel">
      <div id="shell-wrap">
        <button id="shell-paste-btn" title="Paste from clipboard"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 4h2a2 2 0 012 2v14a2 2 0 01-2 2H6a2 2 0 01-2-2V6a2 2 0 012-2h2"/><rect x="8" y="2" width="8" height="4" rx="1" ry="1"/><path d="M12 11v6"/><path d="M9 14l3 3 3-3"/></svg></button>
      </div>
    </div>

    <!-- Log panel -->
    <div id="log-panel" class="panel">
      <div id="log-header">
        <span>Raw Claude Code Output</span>
        <button id="log-clear-btn">Clear</button>
      </div>
      <div id="log-content"></div>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-web-links@0.9.0/lib/xterm-addon-web-links.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/socket.io-client@4.7.2/dist/socket.io.min.js"></script>
<script>
(function() {
  // ═══════════════════════════════════════════════════
  //  TAB SWITCHING
  // ═══════════════════════════════════════════════════
  var tabs = document.querySelectorAll('.tab');
  var panels = document.querySelectorAll('.panel');
  var currentTab = 'claude';
  var shellInitialized = false;

  function switchTab(name) {
    currentTab = name;
    tabs.forEach(function(t) { t.classList.toggle('active', t.dataset.tab === name); });
    panels.forEach(function(p) { p.classList.toggle('active', p.id === name + '-panel'); });
    if (name === 'shell' && !shellInitialized) initShell();
    if (name === 'shell' && shellInitialized) { fitAddon.fit(); term.focus(); }
  }
  tabs.forEach(function(t) { t.addEventListener('click', function() { switchTab(t.dataset.tab); }); });

  // ═══════════════════════════════════════════════════
  //  WEBSOCKET
  // ═══════════════════════════════════════════════════
  var socket = io('/terminal/ws', {
    path: '/terminal/ws/socket.io',
    transports: ['websocket'],
    reconnection: true,
    reconnectionDelay: 1000,
    reconnectionDelayMax: 5000
  });
  var pendingRestart = false;

  // ═══════════════════════════════════════════════════
  //  SHELL MODE (lazy init)
  // ═══════════════════════════════════════════════════
  var term, fitAddon;

  function initShell() {
    if (shellInitialized) return;
    shellInitialized = true;

    socket.emit('shell_start');

    term = new Terminal({
      cursorBlink: true, fontSize: 16,
      fontFamily: '"SF Mono", Menlo, Monaco, "Courier New", monospace',
      theme: {
        background: '#0d1117', foreground: '#c9d1d9', cursor: '#58a6ff',
        selectionBackground: '#264f78',
        black: '#0d1117', red: '#f85149', green: '#3fb950', yellow: '#d29922',
        blue: '#58a6ff', magenta: '#bc8cff', cyan: '#39c5cf', white: '#c9d1d9',
        brightBlack: '#484f58', brightRed: '#ffa198', brightGreen: '#56d364',
        brightYellow: '#e3b341', brightBlue: '#79c0ff', brightMagenta: '#d2a8ff',
        brightCyan: '#56d4dd', brightWhite: '#f0f6fc'
      },
      allowProposedApi: true, scrollback: 5000
    });

    fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    term.loadAddon(new WebLinksAddon.WebLinksAddon());
    term.open(document.getElementById('shell-wrap'));
    fitAddon.fit();

    term.onData(function(data) { socket.emit('input', data); });

    var ro = new ResizeObserver(function() {
      if (currentTab === 'shell') {
        fitAddon.fit();
        var d = fitAddon.proposeDimensions();
        if (d) socket.emit('resize', {rows: d.rows, cols: d.cols});
      }
    });
    ro.observe(document.getElementById('shell-wrap'));
  }

  // Shell socket events
  socket.on('connect', function() {
    if (pendingRestart) {
      pendingRestart = false;
      setTimeout(function() { window.location.reload(); }, 500);
      return;
    }
    if (shellInitialized) {
      // Re-spawn shell on reconnect
      socket.emit('shell_start');
      var d = fitAddon.proposeDimensions();
      if (d) socket.emit('resize', {rows: d.rows, cols: d.cols});
    }
  });
  socket.on('output', function(data) { if (term) term.write(data); });
  socket.on('exit', function() {
    if (term) {
      term.write('\r\n\x1b[33m[shell exited — restarting...]\x1b[0m\r\n');
      setTimeout(function() { socket.emit('shell_start'); }, 1000);
    }
  });

  window.addEventListener('resize', function() {
    if (shellInitialized && currentTab === 'shell') {
      fitAddon.fit();
      var d = fitAddon.proposeDimensions();
      if (d) socket.emit('resize', {rows: d.rows, cols: d.cols});
    }
  });

  // Shell paste button
  document.getElementById('shell-paste-btn').addEventListener('click', function() {
    if (navigator.clipboard && navigator.clipboard.readText) {
      navigator.clipboard.readText().then(function(t) { if (t && term) term.paste(t); }).catch(function(){});
    }
  });

  // ═══════════════════════════════════════════════════
  //  MARKDOWN RENDERER (lightweight)
  // ═══════════════════════════════════════════════════
  function renderMd(text) {
    // Escape HTML first
    var h = text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    // Code blocks: ```lang\n...\n```
    h = h.replace(/```(\w*)\n([\s\S]*?)```/g, function(_, lang, code) {
      return '<pre><code>' + code.replace(/\n$/, '') + '</code></pre>';
    });
    // Inline code
    h = h.replace(/`([^`]+)`/g, '<code>$1</code>');
    // Bold
    h = h.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    // Line breaks
    h = h.replace(/\n/g, '<br>');
    // Fix: remove <br> inside <pre>
    h = h.replace(/<pre><code>([\s\S]*?)<\/code><\/pre>/g, function(_, code) {
      return '<pre><code>' + code.replace(/<br>/g, '\n') + '</code></pre>';
    });
    return h;
  }

  // ═══════════════════════════════════════════════════
  //  CLAUDE MODE
  // ═══════════════════════════════════════════════════
  var conv = document.getElementById('conversation');
  var promptInput = document.getElementById('prompt-input');
  var sendBtn = document.getElementById('send-btn');
  var claudeDot = document.getElementById('claude-dot');
  var claudeStatusText = document.getElementById('claude-status-text');
  var logContent = document.getElementById('log-content');

  var claudeState = 'none'; // none, ready, busy, dead
  var claudeSessionStarting = false;
  var activityCard = null;
  var activityLog = [];
  var autoScroll = true;
  var currentDbSessionId = null;
  var currentClaudeSessionId = null;
  var backendSessionAlive = false;  // true when ws claude_start has been called this connection

  function setClaudeState(state) {
    claudeState = state;
    claudeDot.className = 'dot dot-' + state;
    var labels = {none:'Idle', ready:'Ready', busy:'Working...', dead:'Disconnected'};
    claudeStatusText.textContent = labels[state] || state;
    updateSendBtn();
  }

  function updateSendBtn() {
    if (claudeState === 'busy') {
      sendBtn.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><rect x="4" y="4" width="16" height="16" rx="2"/></svg>';
      sendBtn.classList.add('stop-btn');
      sendBtn.disabled = false;
    } else {
      sendBtn.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>';
      sendBtn.classList.remove('stop-btn');
      sendBtn.disabled = !promptInput.value.trim();
    }
  }

  // Auto-scroll detection
  conv.addEventListener('scroll', function() {
    autoScroll = conv.scrollTop + conv.clientHeight >= conv.scrollHeight - 30;
  });
  function scrollToBottom() {
    if (autoScroll) conv.scrollTop = conv.scrollHeight;
  }

  // Add message bubble
  function addUserMsg(text) {
    var el = document.createElement('div');
    el.className = 'msg msg-user';
    el.textContent = text;
    conv.appendChild(el);
    scrollToBottom();
  }

  function addAssistantMsg(text, rawText, restoredActivity) {
    var el = document.createElement('div');
    el.className = 'msg msg-assistant';
    el.innerHTML = renderMd(text);
    // Copy button
    var btn = document.createElement('button');
    btn.className = 'copy-btn';
    btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>';
    btn.addEventListener('click', function() {
      navigator.clipboard.writeText(rawText || text).then(function() {
        btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--green)" stroke-width="2.5"><path d="M20 6L9 17l-5-5"/></svg>';
        btn.classList.add('copied');
        setTimeout(function() { btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>'; btn.classList.remove('copied'); }, 1500);
      }).catch(function(){});
    });
    el.appendChild(btn);
    conv.appendChild(el);
    // Render restored activity log (collapsed) for history messages
    if (restoredActivity && restoredActivity.length > 0) {
      var toggle = document.createElement('div');
      toggle.className = 'activity-collapsed';
      toggle.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:middle;margin-right:4px;"><polyline points="9 18 15 12 9 6"/></svg>View activity (' + restoredActivity.length + ' steps)';
      var logEl = document.createElement('div');
      logEl.className = 'activity-log';
      logEl.textContent = restoredActivity.join('\n');
      toggle.addEventListener('click', function() {
        var open = logEl.style.display === 'block';
        logEl.style.display = open ? 'none' : 'block';
        toggle.innerHTML = (open ? '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:middle;margin-right:4px;"><polyline points="9 18 15 12 9 6"/></svg>' : '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:middle;margin-right:4px;"><polyline points="6 9 12 15 18 9"/></svg>') + 'View activity (' + restoredActivity.length + ' steps)';
      });
      conv.appendChild(toggle);
      conv.appendChild(logEl);
    }
    scrollToBottom();
  }

  function addError(text, showRestart) {
    var el = document.createElement('div');
    el.className = 'msg-error';
    el.textContent = text;
    if (showRestart) {
      var btn = document.createElement('button');
      btn.textContent = 'Restart Session';
      btn.addEventListener('click', function() { startSession(); });
      el.appendChild(document.createElement('br'));
      el.appendChild(btn);
    }
    conv.appendChild(el);
    scrollToBottom();
  }

  function showActivityCard() {
    activityLog = [];
    activityCard = document.createElement('div');
    activityCard.className = 'activity-card';
    activityCard.innerHTML = '<span class="spinner"></span><span class="activity-text">Thinking...</span>';
    conv.appendChild(activityCard);
    scrollToBottom();
  }

  function updateActivityCard(text) {
    if (!activityCard) return;
    activityLog.push(text);
    var t = activityCard.querySelector('.activity-text');
    if (t) t.textContent = text;
    scrollToBottom();
  }

  function collapseActivityCard() {
    if (!activityCard) return;
    var card = activityCard;
    activityCard = null;
    card.remove();
    if (activityLog.length > 0) {
      var toggle = document.createElement('div');
      toggle.className = 'activity-collapsed';
      toggle.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:middle;margin-right:4px;"><polyline points="9 18 15 12 9 6"/></svg>View activity (' + activityLog.length + ' steps)';
      var logEl = document.createElement('div');
      logEl.className = 'activity-log';
      logEl.textContent = activityLog.join('\n');
      toggle.addEventListener('click', function() {
        var open = logEl.style.display === 'block';
        logEl.style.display = open ? 'none' : 'block';
        toggle.innerHTML = (open ? '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:middle;margin-right:4px;"><polyline points="9 18 15 12 9 6"/></svg>' : '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:middle;margin-right:4px;"><polyline points="6 9 12 15 18 9"/></svg>') + 'View activity (' + activityLog.length + ' steps)';
      });
      conv.appendChild(toggle);
      conv.appendChild(logEl);
    }
    scrollToBottom();
  }

  function addSessionDivider() {
    var el = document.createElement('div');
    el.className = 'session-divider';
    el.textContent = '— New Session —';
    conv.appendChild(el);
    scrollToBottom();
  }

  // ── Session management ──
  function startSession(thenSend) {
    claudeSessionStarting = true;
    backendSessionAlive = true;
    socket.emit('claude_start', {claude_session_id: currentClaudeSessionId, db_session_id: currentDbSessionId});
    // Wait for ready, then optionally send
    if (thenSend) {
      var handler = function(d) {
        if (d.state === 'ready') {
          socket.off('claude_state', handler);
          claudeSessionStarting = false;
          sendPrompt(thenSend);
        }
      };
      socket.on('claude_state', handler);
      // Timeout fallback
      setTimeout(function() { socket.off('claude_state', handler); claudeSessionStarting = false; }, 15000);
    }
  }

  function sendPrompt(text) {
    addUserMsg(text);
    showActivityCard();
    socket.emit('claude_prompt', {text: text});
  }

  // ── Send / stop button ──
  function handleSend() {
    if (claudeState === 'busy') {
      socket.emit('claude_stop');
      return;
    }
    var text = promptInput.value.trim();
    if (!text) return;
    promptInput.value = '';
    autoResizeInput();
    updateSendBtn();
    if (claudeState === 'none' || claudeState === 'dead' || !backendSessionAlive) {
      startSession(text);
    } else {
      sendPrompt(text);
    }
  }

  sendBtn.addEventListener('click', handleSend);
  promptInput.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  });
  promptInput.addEventListener('input', function() {
    autoResizeInput();
    updateSendBtn();
  });

  function autoResizeInput() {
    promptInput.style.height = 'auto';
    promptInput.style.height = Math.min(promptInput.scrollHeight, window.innerHeight * 0.45) + 'px';
  }

  // New session button
  document.getElementById('new-session-btn').addEventListener('click', async function() {
    if (claudeState === 'busy' || claudeState === 'ready') {
      socket.emit('claude_stop');
    }
    try {
      var r = await fetch('/terminal/api/session/new', {method: 'POST'});
      var data = await r.json();
      currentDbSessionId = data.session.id;
      currentClaudeSessionId = null;
    } catch(e) {
      console.error('Failed to create new session:', e);
    }
    setClaudeState('none');
    addSessionDivider();
  });


  // Claude paste button
  document.getElementById('claude-paste-btn').addEventListener('click', function() {
    if (navigator.clipboard && navigator.clipboard.readText) {
      navigator.clipboard.readText().then(function(t) {
        if (t) {
          promptInput.value += t;
          autoResizeInput();
          updateSendBtn();
          promptInput.focus();
        }
      }).catch(function(){});
    }
  });

  // ── Claude WebSocket events ──
  socket.on('claude_state', function(d) {
    if (d.state === 'ready') {
      if (claudeState === 'busy') {
        collapseActivityCard();
      }
      setClaudeState('ready');
    } else if (d.state === 'busy') {
      setClaudeState('busy');
    } else if (d.state === 'dead') {
      collapseActivityCard();
      setClaudeState('dead');
    }
  });

  socket.on('claude_status', function(d) {
    if (d.type === 'restart') {
      pendingRestart = true;
      var el = document.createElement('div');
      el.style.cssText = 'text-align:center;color:var(--yellow);font-size:12px;padding:8px 0;';
      el.textContent = 'Restarting terminal service...';
      conv.appendChild(el);
      scrollToBottom();
    }
    updateActivityCard(d.text);
  });

  socket.on('claude_response', function(d) {
    collapseActivityCard();
    if (d.id) currentClaudeSessionId = d.id;
    addAssistantMsg(d.text, d.text);
  });

  socket.on('claude_error', function(d) {
    collapseActivityCard();
    addError(d.text + (d.detail ? ': ' + d.detail : ''), true);
    if (claudeState === 'busy') setClaudeState('dead');
  });

  socket.on('claude_raw', function(d) {
    // Append to log panel
    logContent.textContent += d.data;
    // Auto-scroll log
    logContent.scrollTop = logContent.scrollHeight;
  });

  socket.on('disconnect', function() {
    backendSessionAlive = false;
    if (claudeState !== 'none') setClaudeState('dead');
  });

  socket.on('reconnect', function() {
    // WebSocket session is gone, but db session persists
    if (claudeState !== 'none') {
      // Session can be resumed on next prompt via --resume
      setClaudeState(currentClaudeSessionId ? 'ready' : 'none');
      if (!currentClaudeSessionId) {
        addError('Connection lost. Session ended.', true);
      }
    }
  });

  // Log clear button
  document.getElementById('log-clear-btn').addEventListener('click', function() {
    logContent.textContent = '';
  });

  // ── Session persistence ──
  async function loadSession() {
    try {
      var r = await fetch('/terminal/api/session/current');
      var data = await r.json();
      if (data.session) {
        currentDbSessionId = data.session.id;
        currentClaudeSessionId = data.session.claude_session_id;
        data.messages.forEach(function(msg) {
          if (msg.role === 'user') {
            addUserMsg(msg.content);
          } else if (msg.role === 'assistant') {
            var activity = [];
            try { activity = JSON.parse(msg.activity_log || '[]'); } catch(e) {}
            addAssistantMsg(msg.content, msg.content, activity);
          } else if (msg.role === 'error') {
            addError(msg.content, false);
          }
        });
        if (currentClaudeSessionId) {
          // Show ready state but need startSession on next prompt to create backend session
          setClaudeState('ready');
        }
        // Scroll to bottom after loading history
        conv.scrollTop = conv.scrollHeight;
      }
    } catch(e) {
      console.error('Failed to load session:', e);
    }
  }

  // Init
  loadSession();
  updateSendBtn();
})();
</script>
</body>
</html>
"""

# ── Main ──────────────────────────────────────────────────────
if __name__ == "__main__":
    _init_terminal_db()
    socketio.run(app, host="0.0.0.0", port=8051, log_output=True)
