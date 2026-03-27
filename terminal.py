"""
terminal.py — Web terminal for the trading platform.
Flask + flask-socketio app serving a browser-based shell via PTY.
"""

import eventlet
eventlet.monkey_patch()
import eventlet.tpool

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
        cur = c.execute("""
            INSERT INTO terminal_sessions (claude_session_id, created_at)
            VALUES (?, ?)
        """, (claude_session_id, now_utc()))
        return cur.lastrowid, claude_session_id


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


@app.route("/terminal/api/model", methods=["GET", "POST"])
def api_model():
    if not _is_authenticated():
        return jsonify({"error": "unauthorized"}), 401
    if request.method == "POST":
        data = request.get_json() or {}
        new_model = data.get("model") or None
        old_model = _claude_model["model"]
        _claude_model["model"] = new_model
        # When model changes, reset Claude session so next prompt starts fresh
        # (avoids loading Opus-sized history into Sonnet's smaller context)
        if new_model != old_model:
            s = _claude_session.get("session")
            if s and not s.busy:
                s.claude_session_id = None
                # Clear from DB too so reconnect doesn't reload it
                if s.db_session_id:
                    with get_conn() as c:
                        c.execute("UPDATE terminal_sessions SET claude_session_id = NULL WHERE id = ?",
                                  (s.db_session_id,))
                s.db_session_id = None
        return jsonify({"model": _claude_model["model"]})
    return jsonify({"model": _claude_model["model"]})


@app.route("/terminal/api/session/current")
def api_session_current():
    if not _is_authenticated():
        return jsonify({"error": "unauthorized"}), 401
    limit = request.args.get("limit", type=int)
    before_id = request.args.get("before_id", type=int)
    with get_conn() as c:
        row = c.execute("""
            SELECT id, claude_session_id, created_at FROM terminal_sessions
            WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1
        """).fetchone()
        if not row:
            return jsonify({"session": None, "messages": [], "total_count": 0})
        session = {
            "id": row["id"],
            "claude_session_id": row["claude_session_id"],
            "created_at": row["created_at"],
        }
        total_count = c.execute(
            "SELECT COUNT(*) FROM terminal_messages WHERE session_id = ?",
            (row["id"],)).fetchone()[0]
        if limit:
            if before_id:
                msgs = c.execute("""
                    SELECT id, role, content, activity_log, created_at FROM terminal_messages
                    WHERE session_id = ? AND id < ? ORDER BY id DESC LIMIT ?
                """, (row["id"], before_id, limit)).fetchall()
                msgs = list(reversed(msgs))
            else:
                # Latest N messages
                msgs = c.execute("""
                    SELECT id, role, content, activity_log, created_at FROM terminal_messages
                    WHERE session_id = ? ORDER BY id DESC LIMIT ?
                """, (row["id"], limit)).fetchall()
                msgs = list(reversed(msgs))
        else:
            msgs = c.execute("""
                SELECT id, role, content, activity_log, created_at FROM terminal_messages
                WHERE session_id = ? ORDER BY id ASC
            """, (row["id"],)).fetchall()
        messages = [
            {"id": m["id"], "role": m["role"], "content": m["content"],
             "activity_log": m["activity_log"], "created_at": m["created_at"]}
            for m in msgs
        ]
    # Check if Claude is currently working
    s = _claude_session.get("session")
    busy = bool(s and s.busy and s.alive)
    return jsonify({"session": session, "messages": messages,
                    "total_count": total_count, "busy": busy})


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

    # Re-attach to running Claude session if one exists
    s = _claude_session.get("session")
    if s and s.alive:
        s.sid = request.sid  # Update to new socket so emits reach this client

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
    # Do NOT cleanup claude — let it finish and save results to DB

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

# Configurable model — can be changed at runtime via API
_claude_model = {"model": None}  # None = use default

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
        self.services_to_restart = set()
        self.current_activities = []  # Accumulates during a task

    def send_prompt(self, text):
        if self.busy:
            self._safe_emit("claude_error", {"text": "Still processing"})
            return

        claude_path = _find_claude()
        if not claude_path:
            self._safe_emit("claude_error",
                            {"text": "Claude Code not found", "detail": "Binary not in PATH"})
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

        if _claude_model["model"]:
            cmd.extend(["--model", _claude_model["model"]])

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
            self._safe_emit("claude_error",
                            {"text": "Failed to start", "detail": str(e)})
            self.busy = False
            self._emit_state("ready")
            return

        print(f"[terminal] Spawned claude subprocess pid={self.process.pid} cmd={cmd}", flush=True)
        self._resume_cmd = cmd  # save for retry logic
        self.sio.start_background_task(self._read_output)

    def _read_output(self):
        """Read stream-json output line by line."""
        response_parts = []
        self.current_activities = []
        _dbg_text_blocks = 0
        _dbg_result_count = 0

        def _read_lines():
            """Read stdout in a thread-safe way (eventlet + subprocess)."""
            def _blocking_readline():
                """Read a single line in the tpool thread."""
                raw_line = self.process.stdout.readline()
                if not raw_line:
                    return None
                return raw_line.decode("utf-8", errors="replace").strip()
            while self.alive:
                line = eventlet.tpool.execute(_blocking_readline)
                if line is None:
                    break
                if line:
                    yield line

        try:
            for line in _read_lines():
                # Emit raw for the log tab
                self._safe_emit("claude_raw", {"data": line + "\n"})
                eventlet.sleep(0)  # yield to let socketio flush

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
                            _dbg_text_blocks += 1
                            print(f"[debug] TEXT BLOCK #{_dbg_text_blocks}: {block['text'][:100]}", flush=True)
                            # Only keep the latest text — earlier text is thinking/planning
                            response_parts = [block["text"]]
                            # Scan text for restart phrases as they stream in
                            _blk_lower = block["text"].lower()
                            for _svc, _phrases in {
                                "platform-terminal": ["restart platform-terminal", "restart the terminal", "supervisorctl restart platform-terminal"],
                                "platform-dashboard": ["restart platform-dashboard", "restart the dashboard", "supervisorctl restart platform-dashboard"],
                                "plugin-btc-15m": ["restart plugin-btc-15m", "restart the bot", "supervisorctl restart plugin-btc-15m"],
                            }.items():
                                for _phrase in _phrases:
                                    if _phrase in _blk_lower:
                                        self.services_to_restart.add(_svc)
                                        break
                        elif block.get("type") == "tool_use":
                            name = block.get("name", "")
                            inp = block.get("input", {})
                            print(f"[debug] TOOL_USE: {name} {inp.get('command','')[:80] if name=='Bash' else ''}", flush=True)
                            # Detect service restart commands
                            if name == "Bash":
                                cmd_str = str(inp.get("command", ""))
                                for _svc in ("platform-terminal", "platform-dashboard", "plugin-btc-15m"):
                                    if f"restart {_svc}" in cmd_str:
                                        self.services_to_restart.add(_svc)
                                # Auto-detect: py_compile implies restart needed
                                if "py_compile" in cmd_str:
                                    if "dashboard.py" in cmd_str:
                                        self.services_to_restart.add("platform-dashboard")
                                    if "terminal.py" in cmd_str:
                                        self.services_to_restart.add("platform-terminal")
                                    if "bot.py" in cmd_str or "plugin.py" in cmd_str or "strategy.py" in cmd_str:
                                        self.services_to_restart.add("plugin-btc-15m")
                            # Auto-detect: Edit/Write on key files implies restart needed
                            if name in ("Edit", "Write"):
                                fp = inp.get("file_path", "")
                                if fp.endswith("dashboard.py"):
                                    self.services_to_restart.add("platform-dashboard")
                                elif fp.endswith("terminal.py"):
                                    self.services_to_restart.add("platform-terminal")
                                elif fp.endswith(("bot.py", "plugin.py", "strategy.py", "market_db.py", "notifications.py")):
                                    self.services_to_restart.add("plugin-btc-15m")
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
                            self.current_activities.append(activity)
                            self._safe_emit("claude_status",
                                            {"text": activity, "type": "activity"})
                            eventlet.sleep(0)  # yield to let socketio flush the emit

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
                    _dbg_result_count += 1
                    print(f"[debug] RESULT: has_text={bool(result_text)}, len={len(result_text)}, session_id={sid}", flush=True)
                    print(f"[debug] RESULT TEXT: {result_text[:100]}", flush=True)
                    if result_text:
                        response_parts = [result_text]

        except Exception as e:
            print(f"[debug] READ EXCEPTION: {e}", flush=True)
            self._safe_emit("claude_error",
                            {"text": "Read error", "detail": str(e)})

        print(f"[debug] READ LOOP ENDED, alive={self.alive}, poll={self.process.poll()}", flush=True)
        # Process finished
        self.process.wait()
        print(f"[debug] PROCESS WAIT DONE, returncode={self.process.returncode}", flush=True)

        # Capture stderr for logging
        stderr_output = ""
        try:
            stderr_output = self.process.stderr.read().decode("utf-8", errors="replace").strip()
        except Exception:
            pass
        if stderr_output:
            self._safe_emit("claude_raw", {"data": "STDERR: " + stderr_output})

        response = "\n".join(response_parts).strip() if response_parts else ""

        print(f"[debug] TOTALS: {_dbg_text_blocks} text blocks, {_dbg_result_count} results, {len(self.current_activities)} activities", flush=True)
        print(f"[debug] SERVICES TO RESTART: {self.services_to_restart}", flush=True)
        if response:
            print(f"[debug] EMITTING RESPONSE: len={len(response)}, first100={response[:100]}", flush=True)
        else:
            print(f"[debug] EMPTY RESPONSE, returncode={self.process.returncode}", flush=True)

        # If process failed with no output and we used --resume, retry without it
        if not response and self.process.returncode != 0 and self.claude_session_id and hasattr(self, '_resume_cmd'):
            retry_cmd = [a for a in self._resume_cmd if a not in ("--resume", self.claude_session_id)]
            print(f"[terminal] Retrying without --resume (exit code {self.process.returncode})", flush=True)
            self.claude_session_id = None
            try:
                env = os.environ.copy()
                env.pop("ANTHROPIC_API_KEY", None)
                if _CW:
                    env["HOME"] = _CW.pw_dir
                    env["USER"] = "claude-worker"
                def _preexec():
                    if _CW:
                        os.setgid(_CW.pw_gid)
                        os.setuid(_CW.pw_uid)
                self.process = subprocess.Popen(
                    retry_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    cwd="/opt/trading-platform",
                    env=env,
                    preexec_fn=_preexec,
                )
                print(f"[terminal] Retry spawned pid={self.process.pid}", flush=True)
                response_parts = []
                self.current_activities = []
                for line in _read_lines():
                    self._safe_emit("claude_raw", {"data": line + "\n"})
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg_type = msg.get("type", "")
                    if msg_type == "assistant":
                        content = msg.get("message", {}).get("content", [])
                        for block in content:
                            if block.get("type") == "text":
                                response_parts.append(block["text"])
                    elif msg_type == "result":
                        sid = msg.get("session_id")
                        if sid:
                            self.claude_session_id = sid
                            if self.db_session_id:
                                try:
                                    with get_conn() as c:
                                        c.execute("UPDATE terminal_sessions SET claude_session_id = ? WHERE id = ?",
                                                  (sid, self.db_session_id))
                                except Exception:
                                    pass
                        result_text = msg.get("result", "")
                        if result_text and not response_parts:
                            response_parts = [result_text]
                self.process.wait()
                response = "\n".join(response_parts).strip() if response_parts else ""
            except Exception as e:
                print(f"[terminal] Retry failed: {e}", flush=True)

        # Check response text for restart indicators
        _restart_phrases = {
            "platform-terminal": ["restart platform-terminal", "restart the terminal"],
            "platform-dashboard": ["restart platform-dashboard", "restart the dashboard"],
            "plugin-btc-15m": ["restart plugin-btc-15m", "restart the bot"],
        }
        response_lower = response.lower()
        for _svc, phrases in _restart_phrases.items():
            for phrase in phrases:
                if phrase in response_lower:
                    self.services_to_restart.add(_svc)
                    break

        # Strip lines containing supervisorctl restart from visible response
        if self.services_to_restart and response:
            lines = response.split("\n")
            lines = [l for l in lines if "supervisorctl restart" not in l.lower()]
            cleaned = "\n".join(lines).strip()
            if cleaned:
                response = cleaned


        if response:
            self._safe_emit("claude_response",
                            {"text": response, "id": self.claude_session_id or ""})
            # Save assistant message with activity log
            _save_message(self.db_session_id, "assistant", response, self.current_activities)
        elif self.process.returncode != 0:
            error_text = "Claude exited with error" + (": " + stderr_output[:500] if stderr_output else "")
            self._safe_emit("claude_error",
                            {"text": "Claude exited with error",
                             "detail": stderr_output[:500]})
            _save_message(self.db_session_id, "error", error_text)

        # Restart detected services
        if self.services_to_restart:
            import subprocess as sp
            # Process platform-terminal last (it kills this process)
            ordered = sorted(self.services_to_restart, key=lambda s: s == "platform-terminal")
            for svc in ordered:
                if svc == "platform-terminal":
                    restart_msg = "Restarting platform-terminal..."
                    self._safe_emit("claude_status", {"text": restart_msg, "type": "restart"})
                    _save_message(self.db_session_id, "system", restart_msg)
                    eventlet.sleep(2)  # Let the response reach the client first
                    sp.Popen(["supervisorctl", "restart", "platform-terminal"])
                else:
                    result = sp.run(["supervisorctl", "restart", svc],
                                    capture_output=True, text=True, timeout=10)
                    if result.returncode == 0:
                        restart_msg = f"Restarted {svc}"
                    else:
                        restart_msg = f"Failed to restart {svc}: {result.stderr.strip()}"
                    self._safe_emit("claude_status", {"text": restart_msg, "type": "restart"})
                    _save_message(self.db_session_id, "system", restart_msg)
            self.services_to_restart = set()

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

    def _safe_emit(self, event, data):
        """Emit to client, silently ignoring errors if client disconnected."""
        try:
            self.sio.emit(event, data, namespace="/terminal/ws", to=self.sid)
        except Exception as e:
            print(f"[terminal] _safe_emit failed: event={event} sid={self.sid} err={e}", flush=True)

    def _emit_state(self, state):
        self._safe_emit("claude_state",
                        {"state": state, "session_id": self.claude_session_id or ""})


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
<meta name="theme-color" content="#161b22">
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
  html, body { height: 100%; overflow: hidden; background: var(--card); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    touch-action: manipulation; overscroll-behavior: none; }

  #app { width: 100%; height: 100dvh; display: flex; flex-direction: column; }

  /* ── Dashboard-style header ── */
  #stickyHeader {
    background: var(--card); border-bottom: 1px solid var(--border);
    padding: 10px 14px; box-shadow: 0 4px 12px rgba(0,0,0,0.5); flex-shrink: 0; z-index: 50;
  }
  .hdr-row { display: flex; justify-content: space-between; align-items: center; }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
  .dot-green { background: var(--green); box-shadow: 0 0 4px var(--green); animation: live-pulse-green 2s ease-in-out infinite; }
  .dot-red { background: var(--red); box-shadow: 0 0 4px var(--red); animation: live-pulse-red 2s ease-in-out infinite; }
  .dot-yellow { background: var(--yellow); box-shadow: 0 0 4px var(--yellow); animation: live-pulse-yellow 2s ease-in-out infinite; }
  .dot-purple { background: #a371f7; box-shadow: 0 0 4px #a371f7; animation: live-pulse-purple 2s ease-in-out infinite; }
  .dot-blue { background: var(--blue); box-shadow: 0 0 4px var(--blue); animation: live-pulse-blue 2s ease-in-out infinite; }
  .dot-teal { background: #2dd4bf; box-shadow: 0 0 4px #2dd4bf; animation: live-pulse-teal 2s ease-in-out infinite; }
  @keyframes live-pulse-green { 0%,100% { box-shadow: 0 0 4px var(--green); opacity:1; } 50% { box-shadow: 0 0 10px var(--green); opacity:0.75; } }
  @keyframes live-pulse-red { 0%,100% { box-shadow: 0 0 4px var(--red); opacity:1; } 50% { box-shadow: 0 0 10px var(--red); opacity:0.75; } }
  @keyframes live-pulse-yellow { 0%,100% { box-shadow: 0 0 4px var(--yellow); opacity:1; } 50% { box-shadow: 0 0 10px var(--yellow); opacity:0.75; } }
  @keyframes live-pulse-purple { 0%,100% { box-shadow: 0 0 4px #a371f7; opacity:1; } 50% { box-shadow: 0 0 10px #a371f7; opacity:0.75; } }
  @keyframes live-pulse-blue { 0%,100% { box-shadow: 0 0 4px var(--blue); opacity:1; } 50% { box-shadow: 0 0 10px var(--blue); opacity:0.75; } }
  @keyframes live-pulse-teal { 0%,100% { box-shadow: 0 0 4px #2dd4bf; opacity:1; } 50% { box-shadow: 0 0 10px #2dd4bf; opacity:0.75; } }
  .mode-strip { display: flex; gap: 4px; margin-top: 8px; }
  .mode-btn { flex: 1; padding: 6px 2px 5px; border: 1px solid var(--border); border-radius: 6px;
    background: var(--card); cursor: pointer; text-align: center; font-size: 10px;
    font-weight: 600; color: var(--dim); letter-spacing: 0.3px; transition: all 0.15s;
    -webkit-tap-highlight-color: transparent; line-height: 1.2; min-height: 44px;
    display: flex; flex-direction: column; align-items: center; justify-content: center; }
  .mode-btn:active { filter: brightness(1.2); }
  .mode-btn .mode-icon { display: block; margin-bottom: 2px; opacity: 0.7; line-height: 0; }
  .mode-btn.m-active-observe { background: rgba(88,166,255,0.12); color: var(--blue); border-color: rgba(88,166,255,0.4); }
  .mode-btn.m-active-shadow { background: rgba(163,113,247,0.12); color: #a371f7; border-color: rgba(163,113,247,0.4); }
  .mode-btn.m-active-hybrid { background: rgba(45,212,191,0.12); color: #2dd4bf; border-color: rgba(45,212,191,0.4); }
  .mode-btn.m-active-auto { background: rgba(63,185,80,0.12); color: var(--green); border-color: rgba(63,185,80,0.4); }
  .mode-btn.m-active-manual { background: rgba(248,81,73,0.08); color: var(--text); border-color: rgba(248,81,73,0.3); }
  .mode-btn.m-staged { opacity: 0.6; border-style: dashed; background: transparent; }
  .bot-offline-banner {
    display: none; background: rgba(248,81,73,0.12); border-bottom: 1px solid var(--red);
    padding: 6px 12px; text-align: center; font-size: 12px; font-weight: 600;
    color: var(--red); letter-spacing: 0.3px; flex-shrink: 0;
  }
  .bot-offline-banner .offline-dot {
    display: inline-block; width: 6px; height: 6px; border-radius: 50%;
    background: var(--red); margin-right: 6px; vertical-align: middle;
  }

  /* ── Terminal sub-tabs ── */
  #sub-tabs {
    display: flex; background: var(--card); border-bottom: 1px solid var(--border); flex-shrink: 0;
  }
  .sub-tab {
    flex: 1; padding: 8px 0; text-align: center; font-size: 12px; font-weight: 500;
    color: var(--dim); cursor: pointer; border-bottom: 2px solid transparent;
    transition: color 0.15s, border-color 0.15s; -webkit-tap-highlight-color: transparent;
    display: flex; align-items: center; justify-content: center; gap: 6px;
  }
  .sub-tab.active { color: var(--blue); border-bottom-color: var(--blue); }
  .sub-tab .claude-dot {
    width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0;
  }
  @keyframes pulse { 50% { opacity: 0.4; } }
  .cdot-ready { background: var(--green); }
  .cdot-busy { background: var(--yellow); animation: pulse 1s infinite; }
  .cdot-dead { background: var(--red); }
  .cdot-none { background: var(--dim); }
  #new-session-btn {
    background: none; border: 1px solid var(--border); color: var(--dim); font-size: 11px;
    padding: 3px 10px; border-radius: 6px; cursor: pointer; font-family: inherit;
    -webkit-tap-highlight-color: transparent;
  }
  #new-session-btn:active { background: var(--bg); }

  /* ── Bottom tab bar (navigation) ── */
  .tab-bar {
    position: fixed; bottom: 0; left: 0; right: 0; z-index: 100;
    display: flex; align-items: stretch; justify-content: space-around;
    padding-top: 8px; padding-bottom: 30px;
    background: var(--card); border-top: 1px solid var(--border);
  }
  .tab-bar .tab-link { text-decoration: none; flex: 1; display: flex; }
  .tab-btn { background: none; border: none; cursor: pointer;
    display: flex; flex-direction: column; align-items: center; justify-content: flex-start;
    gap: 3px; padding: 8px 0 6px; -webkit-tap-highlight-color: transparent;
    flex: 1; font-size: 10px; color: var(--dim); }
  .tab-btn:active { opacity: 0.7; }
  .tab-btn.tab-active { color: var(--blue); }
  .tab-btn.tab-active svg { stroke: var(--blue); filter: drop-shadow(0 0 6px rgba(88,166,255,0.5)); }
  .tab-btn.tab-active span { text-shadow: 0 0 8px rgba(88,166,255,0.4); }
  .tab-btn svg { width: 26px; height: 26px; }

  /* ── Panels ── */
  #panels { flex: 1; min-height: 0; position: relative; }
  .panel { position: absolute; inset: 0; display: none; flex-direction: column; }
  .panel.active { display: flex; }

  /* ── Claude panel ── */
  #claude-panel { background: var(--bg); }
  #conversation {
    flex: 1; overflow-y: auto; padding: 12px; -webkit-overflow-scrolling: touch;
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
    position: absolute; bottom: 6px; right: 6px; background: rgba(255,255,255,0.08);
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

  /* ── System messages ── */
  .msg-system {
    align-self: center; background: rgba(88, 166, 255, 0.06);
    border: 1px solid rgba(88, 166, 255, 0.15);
    color: var(--blue); border-radius: 8px; padding: 6px 14px;
    font-size: 12px; text-align: center;
  }

  /* ── Session divider ── */
  .session-divider {
    text-align: center; color: var(--dim); font-size: 11px; padding: 8px 0;
    border-top: 1px solid var(--border); margin-top: 4px;
  }

  /* ── Input area ── */
  #input-area {
    padding: 8px 12px 104px;
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
  #quick-actions {
    flex-shrink: 0; border-bottom: 1px solid var(--border); background: var(--card);
    overflow: hidden; transition: max-height 0.2s ease;
  }
  #quick-actions.collapsed { max-height: 21px; }
  #quick-actions.expanded { max-height: 120px; }
  #qa-toggle {
    display: flex; align-items: center; gap: 6px; padding: 5px 12px;
    font-size: 11px; color: var(--dim); cursor: pointer; user-select: none;
    -webkit-tap-highlight-color: transparent;
  }
  #qa-toggle svg { transition: transform 0.2s; }
  #quick-actions.expanded #qa-toggle svg { transform: rotate(90deg); }
  #qa-buttons {
    display: flex; flex-wrap: wrap; gap: 6px; padding: 0 12px 8px;
  }
  .qa-btn {
    background: var(--bg); border: 1px solid var(--border); color: var(--dim);
    font-size: 12px; padding: 4px 10px; border-radius: 12px; cursor: pointer;
    font-family: inherit; white-space: nowrap; -webkit-tap-highlight-color: transparent;
  }
  .qa-btn:active { background: var(--card); color: var(--text); }
  #shell-wrap { flex: 1; min-height: 0; position: relative; padding-bottom: 74px; }
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
    flex: 1; overflow-y: auto; padding: 8px 12px 74px;
    font-family: 'SF Mono', Menlo, Monaco, monospace;
    font-size: 12px; color: var(--dim); white-space: pre-wrap; word-wrap: break-word;
    overscroll-behavior: none; background: var(--bg);
  }
</style>
</head>
<body>
<div id="app">
  <!-- Dashboard Header -->
  <div id="stickyHeader">
    <div class="hdr-row">
      <div style="display:flex;align-items:center;gap:6px;flex:1;min-width:0;overflow:hidden">
        <span class="status-dot" id="statusDot"></span>
        <strong id="statusText" style="font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">Loading...</strong>
      </div>
      <div id="hdrBankroll" style="display:flex;align-items:center;gap:6px;font-family:monospace;font-size:13px;color:var(--text);background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:4px 10px;flex-shrink:0;margin-left:8px">
        <span id="hdrBal" style="font-weight:700;font-size:15px">--</span>
      </div>
    </div>
    <div class="mode-strip" id="modeStrip">
      <div class="mode-btn" data-mode="observe"><span class="mode-icon"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8S1 12 1 12z"/><circle cx="12" cy="12" r="3"/></svg></span>Observe</div>
      <div class="mode-btn" data-mode="shadow"><span class="mode-icon"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M16 10.5C16 11.3284 15.5523 12 15 12C14.4477 12 14 11.3284 14 10.5C14 9.67157 14.4477 9 15 9C15.5523 9 16 9.67157 16 10.5Z" fill="currentColor"/><ellipse cx="9" cy="10.5" rx="1" ry="1.5" fill="currentColor"/><path d="M22 19.723V12.3006C22 6.61173 17.5228 2 12 2C6.47715 2 2 6.61173 2 12.3006V19.723C2 21.0453 3.35098 21.9054 4.4992 21.314C5.42726 20.836 6.5328 20.9069 7.39614 21.4998C8.36736 22.1667 9.63264 22.1667 10.6039 21.4998L10.9565 21.2576C11.5884 20.8237 12.4116 20.8237 13.0435 21.2576L13.3961 21.4998C14.3674 22.1667 15.6326 22.1667 16.6039 21.4998C17.4672 20.9069 18.5727 20.836 19.5008 21.314C20.649 21.9054 22 21.0453 22 19.723Z"/></svg></span>Shadow</div>
      <div class="mode-btn" data-mode="hybrid"><span class="mode-icon"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M9.75 3.104v5.714a2.25 2.25 0 0 1-.659 1.591L5 14.5M9.75 3.104c-.251.023-.501.05-.75.082m.75-.082a24.301 24.301 0 0 1 4.5 0m0 0v5.714c0 .597.237 1.17.659 1.591L19.8 15.3M14.25 3.104c.251.023.501.05.75.082M19.8 15.3l-1.57.393A9.065 9.065 0 0 1 12 15a9.065 9.065 0 0 0-6.23-.693L5 14.5m14.8.8 1.402 1.402c1.232 1.232.65 3.318-1.067 3.611A48.309 48.309 0 0 1 12 21c-2.773 0-5.491-.235-8.135-.687-1.718-.293-2.3-2.379-1.067-3.61L5 14.5"/></svg></span>Hybrid</div>
      <div class="mode-btn" data-mode="auto"><span class="mode-icon"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0 3.181 3.183a8.25 8.25 0 0 0 13.803-3.7M4.031 9.865a8.25 8.25 0 0 1 13.803-3.7l3.181 3.182m0-4.991v4.99"/></svg></span>Auto</div>
      <div class="mode-btn" data-mode="manual"><span class="mode-icon"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M10.05 4.575a1.575 1.575 0 1 0-3.15 0v3m3.15-3v-1.5a1.575 1.575 0 0 1 3.15 0v1.5m-3.15 0 .075 5.925m3.075.75V4.575m0 0a1.575 1.575 0 0 1 3.15 0V15M6.9 7.575a1.575 1.575 0 1 0-3.15 0v8.175a6.75 6.75 0 0 0 6.75 6.75h2.018a5.25 5.25 0 0 0 3.712-1.538l1.732-1.732a5.25 5.25 0 0 0 1.538-3.712l.003-2.024a.668.668 0 0 1 .198-.471 1.575 1.575 0 1 0-2.228-2.228 3.818 3.818 0 0 0-1.12 2.687M6.9 7.575V12m6.27 4.318A4.49 4.49 0 0 1 16.35 15m.002 0h-.002"/></svg></span>Manual</div>
    </div>
  </div>
  <div class="bot-offline-banner" id="offlineBanner">
    <span class="offline-dot"></span><span id="offlineText">Bot Offline</span>
  </div>

  <!-- Terminal sub-tabs -->
  <div id="sub-tabs">
    <div class="sub-tab active" data-tab="claude"><span id="claude-dot" class="claude-dot cdot-none"></span>Claude</div>
    <div class="sub-tab" data-tab="shell">Shell</div>
    <div class="sub-tab" data-tab="log">Log</div>
    <button id="new-session-btn">New</button>
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
      <div id="quick-actions" class="collapsed">
        <div id="qa-toggle"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>Quick Actions</div>
        <div id="qa-buttons">
          <button class="qa-btn" data-cmd="supervisorctl restart plugin-btc-15m">Restart Bot</button>
          <button class="qa-btn" data-cmd="supervisorctl restart platform-dashboard">Restart Dashboard</button>
          <button class="qa-btn" data-cmd="tail -50 /var/log/plugin-btc-15m.err.log">Bot Logs</button>
          <button class="qa-btn" data-cmd="tail -50 /var/log/platform-dashboard.err.log">Dash Logs</button>
          <button class="qa-btn" data-cmd="supervisorctl status">Status</button>
          <button class="qa-btn" data-cmd="df -h /">Disk</button>
        </div>
      </div>
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

<!-- Bottom Tab Bar (navigation between apps) -->
<div class="tab-bar">
  <a href="/" class="tab-link"><div class="tab-btn">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>
    <span>Home</span>
  </div></a>
  <a href="/#Trades" class="tab-link"><div class="tab-btn">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7.5 7.5 3m0 0L12 7.5M7.5 3v13.5m13.5 0L16.5 21m0 0L12 16.5m4.5 4.5V7.5"/></svg>
    <span>Trades</span>
  </div></a>
  <a href="/#Regimes" class="tab-link"><div class="tab-btn">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>
    <span>Regimes</span>
  </div></a>
  <a href="/#Stats" class="tab-link"><div class="tab-btn">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="12" width="4" height="9" rx="1"/><rect x="10" y="7" width="4" height="14" rx="1"/><rect x="17" y="3" width="4" height="18" rx="1"/></svg>
    <span>Stats</span>
  </div></a>
  <div class="tab-btn tab-active">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>
    <span>Terminal</span>
  </div>
  <a href="/#Settings" class="tab-link"><div class="tab-btn">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.325.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 0 1 1.37.49l1.296 2.247a1.125 1.125 0 0 1-.26 1.431l-1.003.827c-.293.241-.438.613-.43.992a7.723 7.723 0 0 1 0 .255c-.008.378.137.75.43.991l1.004.827c.424.35.534.955.26 1.43l-1.298 2.247a1.125 1.125 0 0 1-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.47 6.47 0 0 1-.22.128c-.331.183-.581.495-.644.869l-.213 1.281c-.09.543-.56.94-1.11.94h-2.594c-.55 0-1.019-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 0 1-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 0 1-1.369-.49l-1.297-2.247a1.125 1.125 0 0 1 .26-1.431l1.004-.827c.292-.24.437-.613.43-.991a6.932 6.932 0 0 1 0-.255c.007-.38-.138-.751-.43-.992l-1.004-.827a1.125 1.125 0 0 1-.26-1.43l1.297-2.247a1.125 1.125 0 0 1 1.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.086.22-.128.332-.183.582-.495.644-.869l.214-1.28Z"/><circle cx="12" cy="12" r="3"/></svg>
    <span>Settings</span>
  </div></a>
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
  var subTabs = document.querySelectorAll('.sub-tab');
  var panels = document.querySelectorAll('.panel');
  var currentTab = 'claude';
  var shellInitialized = false;

  function switchTab(name) {
    currentTab = name;
    subTabs.forEach(function(t) { t.classList.toggle('active', t.dataset.tab === name); });
    panels.forEach(function(p) { p.classList.toggle('active', p.id === name + '-panel'); });
    if (name === 'shell' && !shellInitialized) initShell();
    if (name === 'shell' && shellInitialized) { fitAddon.fit(); term.focus(); }
  }
  subTabs.forEach(function(t) { t.addEventListener('click', function() { switchTab(t.dataset.tab); }); });

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
      // Don't reload — just let the connect handler re-sync below
    }
    if (shellInitialized) {
      // Re-spawn shell on reconnect
      socket.emit('shell_start');
      var d = fitAddon.proposeDimensions();
      if (d) socket.emit('resize', {rows: d.rows, cols: d.cols});
    }
    // Sync Claude status indicator and catch up on missed messages
    fetch('/terminal/api/session/current?limit=' + _PAGE_SIZE).then(function(r) { return r.json(); }).then(function(data) {
      if (!data.session) { setClaudeState('none'); return; }
      currentDbSessionId = data.session.id;
      currentClaudeSessionId = data.session.claude_session_id;
      var newTotal = data.total_count || 0;
      // Render any messages that arrived while disconnected
      if (newTotal > renderedMsgCount) {
        collapseActivityCard();
        // Only the newest messages we haven't seen — they're at the end of the response
        var skip = Math.max(0, data.messages.length - (newTotal - renderedMsgCount));
        var newMsgs = data.messages.slice(skip);
        newMsgs.forEach(function(msg) { renderDbMessage(msg); });
        renderedMsgCount = newTotal;
        scrollToBottom();
      }
      if (data.busy) {
        backendSessionAlive = true;
        setClaudeState('busy');
        if (!activityCard) showActivityCard();
      } else if (currentClaudeSessionId) {
        setClaudeState('ready');
      } else {
        setClaudeState('none');
      }
    }).catch(function() {});
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

  // Quick actions bar
  var qaBar = document.getElementById('quick-actions');
  document.getElementById('qa-toggle').addEventListener('click', function() {
    qaBar.classList.toggle('collapsed');
    qaBar.classList.toggle('expanded');
    // Re-fit terminal after transition
    setTimeout(function() { if (fitAddon) fitAddon.fit(); }, 250);
  });
  document.querySelectorAll('.qa-btn').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var cmd = btn.dataset.cmd;
      if (!cmd) return;
      // Ensure shell is initialized
      if (!shellInitialized) { switchTab('shell'); }
      // Write command + enter to PTY
      socket.emit('input', cmd + '\n');
    });
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
  var logContent = document.getElementById('log-content');

  var claudeState = 'none'; // none, ready, busy, dead
  var claudeSessionStarting = false;
  var activityCard = null;
  var activityLog = [];
  var autoScroll = true;
  var currentDbSessionId = null;
  var currentClaudeSessionId = null;
  var backendSessionAlive = false;  // true when ws claude_start has been called this connection
  var renderedMsgCount = 0;  // tracks DB messages already rendered, for reconnect catch-up
  var _oldestMsgId = null;   // oldest message id currently rendered (for loading older)
  var _totalMsgCount = 0;    // total messages in DB for this session
  var _loadingOlder = false; // prevents concurrent fetches
  var _PAGE_SIZE = 50;       // messages per page

  function setClaudeState(state) {
    claudeState = state;
    claudeDot.className = 'claude-dot cdot-' + state;
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
    renderedMsgCount++;  // track user message
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
      var el = document.createElement('div');
      el.className = 'msg-system';
      el.textContent = d.text;
      conv.appendChild(el);
      scrollToBottom();
      renderedMsgCount++;  // track system message
      if (d.text.indexOf('platform-terminal') !== -1) pendingRestart = true;
    }
    updateActivityCard(d.text);
  });

  socket.on('claude_response', function(d) {
    collapseActivityCard();
    if (d.id) currentClaudeSessionId = d.id;
    addAssistantMsg(d.text, d.text);
    renderedMsgCount++;  // track so reconnect doesn't duplicate
  });

  socket.on('claude_error', function(d) {
    collapseActivityCard();
    addError(d.text + (d.detail ? ': ' + d.detail : ''), true);
    renderedMsgCount++;  // track so reconnect doesn't duplicate
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
    setClaudeState('dead');
  });

  // 'reconnect' fires after 'connect' — connect handler already syncs state.
  // Nothing extra needed here.

  // Log clear button
  document.getElementById('log-clear-btn').addEventListener('click', function() {
    logContent.textContent = '';
  });

  // ── Session persistence ──
  function renderDbMessage(msg, prepend) {
    if (msg.role === 'user') {
      if (prepend) {
        var el = document.createElement('div');
        el.className = 'msg msg-user';
        el.textContent = msg.content;
        conv.insertBefore(el, conv.firstChild);
      } else {
        addUserMsg(msg.content);
      }
    } else if (msg.role === 'assistant') {
      var activity = [];
      try { activity = JSON.parse(msg.activity_log || '[]'); } catch(e) {}
      if (prepend) {
        // Build assistant message element for prepend
        var el = document.createElement('div');
        el.className = 'msg msg-assistant';
        el.innerHTML = renderMd(msg.content);
        // Activity log (collapsed) if present
        if (activity && activity.length > 0) {
          var logEl = document.createElement('div');
          logEl.className = 'activity-log';
          logEl.textContent = activity.join('\n');
          var toggle = document.createElement('div');
          toggle.className = 'activity-collapsed';
          toggle.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:middle;margin-right:4px;"><polyline points="9 18 15 12 9 6"/></svg>View activity (' + activity.length + ' steps)';
          toggle.addEventListener('click', function() {
            var open = logEl.style.display === 'block';
            logEl.style.display = open ? 'none' : 'block';
          });
          conv.insertBefore(logEl, conv.firstChild);
          conv.insertBefore(toggle, conv.firstChild);
        }
        conv.insertBefore(el, conv.firstChild);
      } else {
        addAssistantMsg(msg.content, msg.content, activity);
      }
    } else if (msg.role === 'error') {
      if (prepend) {
        var el = document.createElement('div');
        el.className = 'msg-error';
        el.textContent = msg.content;
        conv.insertBefore(el, conv.firstChild);
      } else {
        addError(msg.content, false);
      }
    } else if (msg.role === 'system') {
      var el = document.createElement('div');
      el.className = 'msg-system';
      el.textContent = msg.content;
      if (prepend) conv.insertBefore(el, conv.firstChild);
      else conv.appendChild(el);
    }
  }

  async function loadSession() {
    try {
      var r = await fetch('/terminal/api/session/current?limit=' + _PAGE_SIZE);
      var data = await r.json();
      if (data.session) {
        currentDbSessionId = data.session.id;
        currentClaudeSessionId = data.session.claude_session_id;
        _totalMsgCount = data.total_count || 0;
        data.messages.forEach(function(msg) {
          renderDbMessage(msg);
        });
        renderedMsgCount = _totalMsgCount;  // track total for reconnect catch-up
        if (data.messages.length > 0) {
          _oldestMsgId = data.messages[0].id;
        }
        if (data.busy) {
          // Claude is still working — re-attach
          backendSessionAlive = true;
          setClaudeState('busy');
          showActivityCard();
          // The ws_connect handler already re-attached our sid,
          // so live events will flow to us
        } else if (currentClaudeSessionId) {
          // Show ready state but need startSession on next prompt to create backend session
          setClaudeState('ready');
        }
        // Scroll to bottom after loading history
        conv.scrollTop = conv.scrollHeight;
        // Preload next batch so it's ready before user scrolls
        if (_oldestMsgId) loadOlderMessages();
      }
    } catch(e) {
      console.error('Failed to load session:', e);
    }
  }

  // ── Infinite scroll — load older messages ──
  async function loadOlderMessages() {
    if (_loadingOlder || !_oldestMsgId || !currentDbSessionId) return;
    _loadingOlder = true;
    try {
      var r = await fetch('/terminal/api/session/current?limit=' + _PAGE_SIZE + '&before_id=' + _oldestMsgId);
      var data = await r.json();
      if (!data.messages || data.messages.length === 0) {
        _oldestMsgId = null;  // no more messages
        _loadingOlder = false;
        return;
      }
      // Build all elements in a fragment first (no reflows)
      var frag = document.createDocumentFragment();
      for (var i = 0; i < data.messages.length; i++) {
        var msg = data.messages[i];
        var el;
        if (msg.role === 'user') {
          el = document.createElement('div');
          el.className = 'msg msg-user';
          el.textContent = msg.content;
        } else if (msg.role === 'assistant') {
          el = document.createElement('div');
          el.className = 'msg msg-assistant';
          el.innerHTML = renderMd(msg.content);
        } else if (msg.role === 'error') {
          el = document.createElement('div');
          el.className = 'msg-error';
          el.textContent = msg.content;
        } else if (msg.role === 'system') {
          el = document.createElement('div');
          el.className = 'msg-system';
          el.textContent = msg.content;
        }
        if (el) frag.appendChild(el);
      }
      _oldestMsgId = data.messages[0].id;
      var prevHeight = conv.scrollHeight;
      conv.insertBefore(frag, conv.firstChild);
      conv.scrollTop += conv.scrollHeight - prevHeight;
    } catch(e) {
      console.error('Failed to load older messages:', e);
    }
    _loadingOlder = false;
  }

  conv.addEventListener('scroll', function() {
    autoScroll = conv.scrollTop + conv.clientHeight >= conv.scrollHeight - 30;
    // Load more when within 3 screens of top — very aggressive preload
    if (conv.scrollTop < conv.clientHeight * 3 && _oldestMsgId && !_loadingOlder) {
      loadOlderMessages();
    }
  });

  // ── iOS PWA resume handler ──
  // When the user leaves and comes back, iOS freezes the JS context.
  // The socket may be stale. Force reconnect and catch up.
  document.addEventListener('visibilitychange', function() {
    if (document.visibilityState === 'visible') {
      // Force socket reconnect if it's disconnected
      if (!socket.connected) {
        socket.connect();
      } else {
        // Already connected but state might be stale — re-sync
        fetch('/terminal/api/session/current?limit=' + _PAGE_SIZE).then(function(r) { return r.json(); }).then(function(data) {
          if (!data.session) { setClaudeState('none'); return; }
          currentDbSessionId = data.session.id;
          currentClaudeSessionId = data.session.claude_session_id;
          var newTotal = data.total_count || 0;
          if (newTotal > renderedMsgCount) {
            collapseActivityCard();
            var skip = Math.max(0, data.messages.length - (newTotal - renderedMsgCount));
            var newMsgs = data.messages.slice(skip);
            newMsgs.forEach(function(msg) { renderDbMessage(msg); });
            renderedMsgCount = newTotal;
            scrollToBottom();
          }
          if (data.busy) {
            backendSessionAlive = true;
            setClaudeState('busy');
            if (!activityCard) showActivityCard();
          } else if (currentClaudeSessionId) {
            setClaudeState('ready');
          } else {
            setClaudeState('none');
          }
        }).catch(function() {});
      }
    }
  });

  // Hide tab bar when keyboard is open, restore when closed
  var tabBar = document.querySelector('.tab-bar');
  var inputArea = document.getElementById('input-area');
  promptInput.addEventListener('focus', function() {
    tabBar.style.display = 'none';
    inputArea.style.paddingBottom = '8px';
  });
  promptInput.addEventListener('blur', function() {
    tabBar.style.display = '';
    inputArea.style.paddingBottom = '';
  });

  // Dismiss keyboard: tap on conversation, or drag down from input area (instant, like iOS)
  var _touchStartY = 0;
  var _touchStartEl = null;
  var _dragDismissed = false;
  document.addEventListener('touchstart', function(e) {
    _touchStartY = e.touches[0].clientY;
    _touchStartEl = e.target;
    _dragDismissed = false;
  }, {passive: true});
  document.addEventListener('touchmove', function(e) {
    // Instant dismiss when dragging down from input area (not the textarea itself)
    if (_dragDismissed || document.activeElement !== promptInput) return;
    if (!_touchStartEl || !_touchStartEl.closest('#input-area')) return;
    // Allow scroll inside textarea only if it actually has overflow (content taller than visible area + buffer)
    if (_touchStartEl.closest('#prompt-input') && promptInput.scrollHeight > promptInput.clientHeight + 5) return;
    var dy = e.touches[0].clientY - _touchStartY;
    if (dy > 10) {
      _dragDismissed = true;
      promptInput.blur();
    }
  }, {passive: true});
  document.addEventListener('touchend', function(e) {
    if (_dragDismissed || document.activeElement !== promptInput) return;
    var absDy = Math.abs(e.changedTouches[0].clientY - _touchStartY);
    // Tap on conversation (not scroll, not on buttons/links)
    if (absDy < 10 && _touchStartEl && _touchStartEl.closest('#conversation')
        && !_touchStartEl.closest('button') && !_touchStartEl.closest('a')) {
      promptInput.blur();
    }
  });

  // ═══════════════════════════════════════════════════
  //  DASHBOARD STATE POLLING
  // ═══════════════════════════════════════════════════
  function updateDashboardState(s) {
    if (!s) return;
    // Status dot
    var dotEl = document.getElementById('statusDot');
    var statusEl = document.getElementById('statusText');
    var dotClass = 'status-dot';
    var statusText = s.status_detail || s.status || 'Unknown';
    if (s.status === 'stopped' || s.status === 'error') {
      dotClass += ' dot-red';
    } else if (s.active_trade) {
      dotClass += ' dot-green';
    } else if (s.shadow_trade) {
      dotClass += ' dot-purple';
    } else if (s.pending_trade) {
      dotClass += ' dot-yellow';
    } else {
      dotClass += ' dot-blue';
    }
    dotEl.className = dotClass;
    statusEl.textContent = statusText;
    // Bankroll
    var bal = (s.bankroll_cents || 0) / 100;
    document.getElementById('hdrBal').textContent = '$' + bal.toFixed(2);
    // Mode strip
    var mode = s.trading_mode || 'observe';
    document.querySelectorAll('#modeStrip .mode-btn').forEach(function(b) {
      b.className = b.dataset.mode === mode ? 'mode-btn m-active-' + mode : 'mode-btn';
    });
    // Offline banner
    var banner = document.getElementById('offlineBanner');
    if (s.last_updated) {
      var lu = new Date(s.last_updated.replace(' ', 'T').replace('Z', '+00:00'));
      var staleSec = (Date.now() - lu.getTime()) / 1000;
      if (staleSec > 90) {
        var staleMin = Math.floor(staleSec / 60);
        var label = staleMin >= 60 ? Math.floor(staleMin/60) + 'h ' + (staleMin%60) + 'm' : staleMin + 'm';
        document.getElementById('offlineText').textContent = 'Bot Offline — no heartbeat for ' + label;
        banner.style.display = '';
      } else {
        banner.style.display = 'none';
      }
    }
  }

  function pollDashboardState() {
    fetch('/api/state').then(function(r) { return r.json(); }).then(function(s) {
      updateDashboardState(s);
    }).catch(function() {});
  }
  pollDashboardState();
  setInterval(pollDashboardState, 15000);

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
