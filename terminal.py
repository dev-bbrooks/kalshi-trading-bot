"""
terminal.py — Web terminal for the trading platform.
Flask + flask-socketio app serving a browser-based shell via PTY.
"""

import eventlet
eventlet.monkey_patch()
import eventlet.tpool

import os, sys, pty, json, errno, signal, struct, fcntl, termios, hashlib, secrets, shutil, subprocess, re
import pwd
from flask import Flask, request, jsonify
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

@app.route("/terminal/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/terminal/api/model", methods=["GET", "POST"])
def api_model():
    if not _is_authenticated():
        return jsonify({"error": "unauthorized"}), 401
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        new_model = data.get("model") or "sonnet"
        if new_model not in ("sonnet", "opus"):
            return jsonify({"error": "invalid model"}), 400
        _claude_model["model"] = new_model
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
    try:
        with get_conn() as c:
            c.execute("UPDATE terminal_sessions SET ended_at = ? WHERE ended_at IS NULL", (now_utc(),))
            cur = c.execute("INSERT INTO terminal_sessions (created_at) VALUES (?)", (now_utc(),))
            new_id = cur.lastrowid
            print(f"[terminal] Created new session id={new_id}", flush=True)
            return jsonify({"session": {"id": new_id, "claude_session_id": None}})
    except Exception as e:
        print(f"[terminal] api_session_new ERROR: {e}", flush=True)
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/terminal/api/enhancer", methods=["GET", "POST"])
def api_enhancer():
    if not _is_authenticated():
        return jsonify({"error": "unauthorized"}), 401
    if request.method == "POST":
        data = request.get_json() or {}
        if "enabled" in data:
            _enhancer_config["enabled"] = bool(data["enabled"])
    return jsonify({"enabled": _enhancer_config["enabled"]})


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

# Model config — hardcoded to opus
_claude_model = {"model": "opus"}

# Prompt enhancer config
_enhancer_config = {
    "enabled": True,
    "timeout": 30,  # seconds — kill enhancer if it takes too long
    "max_context_messages": 5,  # recent user messages to include
}

_CLAUDE_MD_PATH = "/opt/trading-platform/CLAUDE.md"

# ── Message router ───────────────────────────────────────────

_DIRECT_PATTERNS = {
    "git_status": [
        re.compile(r"^(git\s+)?status$", re.I),
        re.compile(r"what('s| has| hasn't| is).*\b(committed|pushed|changed|uncommitted|unpushed)\b", re.I),
        re.compile(r"show\s+(me\s+)?(uncommitted|unpushed|changed|dirty|git)", re.I),
        re.compile(r"any(thing)?\s+(to\s+)?(commit|push)", re.I),
    ],
    "git_push": [
        re.compile(r"^(git\s+)?push$", re.I),
        re.compile(r"^push\s+(it|to\s+github|everything|changes)$", re.I),
        re.compile(r"^commit\s+and\s+push$", re.I),
    ],
    "git_diff": [
        re.compile(r"^(git\s+)?diff$", re.I),
        re.compile(r"what('s| did).*change", re.I),
        re.compile(r"show\s+(me\s+)?(the\s+)?diff", re.I),
    ],
    "shell": [
        re.compile(r"^supervisorctl\s+", re.I),
        re.compile(r"^df\s+-h$", re.I),
        re.compile(r"^free\s+-m$", re.I),
        re.compile(r"^uptime$", re.I),
        re.compile(r"^pip3?\s+(list|show|install)\b", re.I),
        re.compile(r"^python3?\s+--version$", re.I),
        re.compile(r"^cat\s+/etc/supervisor", re.I),
    ],
}

def _route_message(text):
    """Route a message to direct handler, enhancer, or passthrough.
    Returns (action, handler_name, payload) tuple.
    action: 'direct', 'enhance', or 'passthrough'
    """
    stripped = text.strip()
    # Empty or multi-line input is never a direct command
    if not stripped or '\n' in stripped:
        return ("enhance" if _enhancer_config["enabled"] else "passthrough", None, None)
    for handler_name, patterns in _DIRECT_PATTERNS.items():
        for pattern in patterns:
            if pattern.search(stripped):
                return ("direct", handler_name, {"raw_text": stripped})
    # Not a direct action — let enhancer/passthrough logic decide
    return ("enhance" if _enhancer_config["enabled"] else "passthrough", None, None)


# ── Git helpers ──────────────────────────────────────────────

_GIT_DIR = "/opt/trading-platform"

def _run_git(args):
    """Run a git command and return (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(
            ["git"] + args,
            cwd=_GIT_DIR,
            capture_output=True, text=True, timeout=15
        )
        return r.returncode, r.stdout.rstrip(), r.stderr.strip()
    except Exception as e:
        return 1, "", str(e)

def _git_status_data():
    """Gather git status, diff stats, and unpushed commits."""
    # Branch
    _, branch, _ = _run_git(["branch", "--show-current"])

    # Uncommitted files
    _, porcelain, _ = _run_git(["status", "--porcelain"])
    uncommitted = []
    for line in porcelain.splitlines():
        if len(line) >= 4:
            status = line[:2].strip()
            filepath = line[3:]
            uncommitted.append({"status": status, "file": filepath})

    # Diff stat (for tracked modified files)
    _, diff_stat_raw, _ = _run_git(["diff", "--stat", "--stat-width=60"])
    diff_stat = []
    for line in diff_stat_raw.splitlines():
        if "|" in line and ("+" in line or "-" in line or "Bin" in line):
            parts = line.split("|")
            fname = parts[0].strip()
            changes = parts[1].strip() if len(parts) > 1 else ""
            diff_stat.append({"file": fname, "changes": changes})

    # Also get staged diff stat
    _, staged_stat_raw, _ = _run_git(["diff", "--cached", "--stat", "--stat-width=60"])
    for line in staged_stat_raw.splitlines():
        if "|" in line and ("+" in line or "-" in line or "Bin" in line):
            parts = line.split("|")
            fname = parts[0].strip()
            changes = parts[1].strip() if len(parts) > 1 else ""
            # Avoid duplicates
            if not any(d["file"] == fname for d in diff_stat):
                diff_stat.append({"file": fname, "changes": changes})

    # Unpushed commits
    _, unpushed_raw, _ = _run_git(["log", "origin/main..HEAD", "--oneline"])
    unpushed = []
    for line in unpushed_raw.splitlines():
        if line.strip():
            parts = line.split(" ", 1)
            unpushed.append({
                "hash": parts[0],
                "message": parts[1] if len(parts) > 1 else ""
            })

    clean = len(uncommitted) == 0 and len(unpushed) == 0
    return {
        "branch": branch or "main",
        "uncommitted": uncommitted,
        "diff_stat": diff_stat,
        "unpushed": unpushed,
        "clean": clean,
    }


def _read_claude_md():
    """Read CLAUDE.md from disk each time (always current)."""
    try:
        with open(_CLAUDE_MD_PATH, "r") as f:
            return f.read()
    except Exception:
        return "(CLAUDE.md not available)"


def _get_recent_user_messages(db_session_id, limit=5):
    """Get the last N user messages from the current terminal session for context."""
    if not db_session_id:
        return []
    try:
        with get_conn() as c:
            rows = c.execute(
                "SELECT content FROM terminal_messages WHERE session_id = ? AND role = 'user' ORDER BY id DESC LIMIT ?",
                (db_session_id, limit)
            ).fetchall()
            return [r[0] for r in reversed(rows)]  # chronological order
    except Exception:
        return []


def _build_enhancer_prompt(user_text, recent_messages, claude_md_content):
    """Build the full prompt for the enhancer instance."""

    recent_context = ""
    if recent_messages:
        recent_context = "RECENT USER MESSAGES (oldest to newest, for understanding references like 'it', 'that', 'more', etc.):\n"
        for i, msg in enumerate(recent_messages, 1):
            recent_context += f"{i}. {msg}\n"
        recent_context += "\n"

    return f"""[PROMPT ENHANCER MODE]

You are a prompt enhancer for a Claude Code terminal on a Kalshi BTC trading platform. A developer will send you their raw message. Your job is to decide what to do with it and respond with EXACTLY ONE of the following — nothing else, no extra text, no explanation:

OPTION 1 — ENHANCE: If the message is a substantial code request (new feature, bug fix, refactoring, adding/modifying endpoints, UI changes, config changes, debugging, etc.), create a detailed structured prompt and wrap it in tags:
<ENHANCED_PROMPT>
[Your detailed prompt here]
</ENHANCED_PROMPT>

OPTION 2 — CLARIFY: If the message is a short iterative follow-up that references something ambiguous ("it", "that", "the color", "still wrong", "make it bigger", etc.), resolve the ambiguous references using the recent message history, then output the clarified version in tags:
<ENHANCED_PROMPT>
[Original message with ambiguous references replaced by specific names/descriptions]
</ENHANCED_PROMPT>

This is NOT a full enhancement — do not add file paths, numbered plans, or verification steps. Just replace vague references with concrete ones so the resumed session knows exactly what "it" refers to. Keep the casual tone and brevity of the original message.

Examples:
- "increase it more" → "increase the header font size more" (if recent messages were about header font size)
- "it's still the wrong color" → "the terminal keyboard background is still the wrong color"
- "add a border too" → "add a border to the settings card too"
- "try 20px" → "try 20px for the sidebar width"

OPTION 3 — PASSTHROUGH: If the message is any of these, output ONLY this tag:
<PASSTHROUGH/>
- Conversational (greetings, "how's it going", status questions, "what did you change")
- A direct instruction that's already specific enough ("change X to Y in file Z")
- A short follow-up where the reference is already clear ("make the font bigger" after just discussing fonts)
- Widget or action requests — anything where the user wants to see, inspect, or trigger something rather than write code (styling widgets, git status, system info, data summaries, "show me the header styles", "what's the git status", "anything to push")
- Messages starting with [DESIGN_APPLY] — auto-generated from the design widget
- Anything you're uncertain about — when in doubt, passthrough

RULES FOR ENHANCED PROMPTS:
- Start with a clear one-line summary of the task
- List which files to read first (use full paths under /opt/trading-platform/)
- Write a numbered step-by-step plan
- Include verification steps: `python3 -m py_compile <file>` for every modified .py file
- If the change affects dashboard.py, terminal.py, bot.py, or strategy.py, mention which services need restarting and include the supervisorctl restart line (the auto-restart system handles execution)
- Reference relevant project conventions from the CLAUDE.md context below
- Do NOT include the CLAUDE.md content itself in the enhanced prompt — Claude Code already has it. Just reference specific rules when relevant.
- Keep the enhanced prompt focused and actionable — not a lecture, a work order

DEVELOPER META-INSTRUCTIONS:
If the developer's message contains instructions addressed to you (prefixed with "enhancer," or "enhancer:" or similar), ALWAYS treat the message as ENHANCE or CLARIFY — never passthrough. Incorporate the meta-instruction into your enhanced output, but strip the "enhancer, ..." text itself from the final prompt so the working session never sees it.

Exception: if the meta-instruction explicitly asks to pass through (e.g. "enhancer, pass this through", "enhancer, send as-is", "enhancer, don't change this"), strip the "enhancer, ..." text and passthrough the remaining message unchanged using <PASSTHROUGH/>.

PROJECT CONTEXT (CLAUDE.md):
---
{claude_md_content}
---

{recent_context}DEVELOPER'S CURRENT MESSAGE:
{user_text}"""

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

    def _run_enhancer(self, user_text):
        """Run the enhancer instance. Returns (enhanced_text, was_enhanced) or (original_text, False) on any failure."""
        import time as _time
        try:
            recent_msgs = _get_recent_user_messages(self.db_session_id, _enhancer_config["max_context_messages"])
            claude_md = _read_claude_md()
            enhancer_prompt = _build_enhancer_prompt(user_text, recent_msgs, claude_md)

            claude_path = _find_claude()
            if not claude_path:
                return user_text, False

            cmd = [claude_path, "-p", enhancer_prompt,
                   "--dangerously-skip-permissions",
                   "--output-format", "stream-json", "--verbose"]

            if _claude_model["model"]:
                cmd.extend(["--model", _claude_model["model"]])

            # NO --resume — always a fresh throwaway instance
            env = os.environ.copy()
            env.pop("ANTHROPIC_API_KEY", None)
            if _CW:
                env["HOME"] = _CW.pw_dir
                env["USER"] = "claude-worker"

            def _preexec():
                if _CW:
                    os.setgid(_CW.pw_gid)
                    os.setuid(_CW.pw_uid)

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                cwd="/opt/trading-platform",
                env=env,
                preexec_fn=_preexec,
            )

            print(f"[enhancer] Spawned pid={proc.pid}", flush=True)

            # Read output with timeout
            response_text = ""
            start_time = _time.time()

            def _blocking_readline():
                return proc.stdout.readline()

            while True:
                if _time.time() - start_time > _enhancer_config["timeout"]:
                    print("[enhancer] Timeout — killing", flush=True)
                    proc.kill()
                    proc.wait()
                    return user_text, False

                try:
                    raw_line = eventlet.tpool.execute(_blocking_readline)
                except Exception:
                    break

                if not raw_line:
                    break

                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type", "")

                if msg_type == "assistant":
                    content = msg.get("message", {}).get("content", [])
                    for block in content:
                        if block.get("type") == "text":
                            response_text = block["text"]

                elif msg_type == "result":
                    result_text = msg.get("result", "")
                    if result_text:
                        response_text = result_text

            proc.wait()

            # Parse the response
            if "<PASSTHROUGH/>" in response_text:
                print(f"[enhancer] Passthrough", flush=True)
                return user_text, False

            if "<ENHANCED_PROMPT>" in response_text and "</ENHANCED_PROMPT>" in response_text:
                start = response_text.index("<ENHANCED_PROMPT>") + len("<ENHANCED_PROMPT>")
                end = response_text.index("</ENHANCED_PROMPT>")
                enhanced = response_text[start:end].strip()
                if enhanced:
                    print(f"[enhancer] Enhanced ({len(enhanced)} chars)", flush=True)
                    return enhanced, True

            # Unexpected output — fallback to passthrough
            print(f"[enhancer] Unexpected output, falling back. Response: {response_text[:200]}", flush=True)
            return user_text, False

        except Exception as e:
            print(f"[enhancer] Error: {e}", flush=True)
            return user_text, False

    def _emit_widget(self, widget_type, data):
        """Emit a widget response using delimiter-based encoding (like design widgets)."""
        widget_json = json.dumps({"type": widget_type, "data": data})
        text = f"__WIDGET__{widget_json}__/WIDGET__"
        print(f"[router] emitting widget text len={len(text)}: {text[:120]}", flush=True)
        self._safe_emit("claude_response", {"text": text})

    def _handle_direct(self, handler_name, payload):
        """Dispatch to the appropriate direct handler."""
        handlers = {
            "git_status": self._direct_git_status,
            "git_push": self._direct_git_push,
            "git_diff": self._direct_git_diff,
            "shell": self._direct_shell,
        }
        fn = handlers.get(handler_name)
        if fn:
            fn(payload)
        else:
            self._safe_emit("claude_error", {"text": f"Unknown handler: {handler_name}"})

    def _direct_git_status(self, payload):
        """Handle git status request — emit git widget."""
        self._safe_emit("claude_status", {"type": "direct", "text": "Checking git status..."})
        data = _git_status_data()
        self._emit_widget("git_status", data)

    def _direct_git_push(self, payload):
        """Handle git push — commit all + push."""
        self._safe_emit("claude_status", {"type": "direct", "text": "Pushing to GitHub..."})

        # Check if there's anything to commit
        status_data = _git_status_data()

        if status_data["clean"]:
            self._emit_widget("git_push_result", {
                "success": True, "already_clean": True,
                "message": "Nothing to commit or push — working tree clean."
            })
            return

        # Stage all
        rc, _, err = _run_git(["add", "-A"])
        if rc != 0:
            self._emit_widget("git_push_result", {
                "success": False, "message": f"git add failed: {err}"
            })
            return

        # Generate commit message from changed files
        changed_files = [f["file"] for f in status_data["uncommitted"]]
        if len(changed_files) <= 5:
            commit_msg = "Update " + ", ".join(changed_files)
        else:
            commit_msg = f"Update {len(changed_files)} files"

        # Commit (only if there are uncommitted changes)
        committed = False
        if status_data["uncommitted"]:
            rc, out, err = _run_git(["commit", "-m", commit_msg])
            if rc != 0 and "nothing to commit" not in err.lower():
                self._emit_widget("git_push_result", {
                    "success": False, "message": f"git commit failed: {err}"
                })
                return
            committed = True

        # Push
        rc, out, err = _run_git(["push"])
        if rc != 0:
            self._emit_widget("git_push_result", {
                "success": False, "message": f"git push failed: {err}"
            })
            return

        # Get the new commit hash
        _, new_hash, _ = _run_git(["rev-parse", "--short", "HEAD"])

        self._emit_widget("git_push_result", {
            "success": True,
            "committed": committed,
            "commit_hash": new_hash,
            "commit_message": commit_msg if committed else None,
            "files": changed_files,
            "pushed_to": "origin/main",
        })

    def _direct_git_diff(self, payload):
        """Show compact diff."""
        self._safe_emit("claude_status", {"type": "direct", "text": "Getting diff..."})
        _, diff_output, _ = _run_git(["diff", "--stat"])
        _, staged_output, _ = _run_git(["diff", "--cached", "--stat"])
        combined = (diff_output + "\n" + staged_output).strip()
        if not combined:
            combined = "No changes."
        self._safe_emit("claude_response", {"text": combined})

    def _direct_shell(self, payload):
        """Run an allowed shell command directly."""
        cmd = payload.get("raw_text", "")
        self._safe_emit("claude_status", {"type": "direct", "text": f"Running: {cmd}"})
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30, cwd=_GIT_DIR)
            output = (r.stdout + r.stderr).strip() or "(no output)"
            self._safe_emit("claude_response", {"text": output})
        except Exception as e:
            self._safe_emit("claude_error", {"text": f"Command failed: {e}"})

    def send_prompt(self, text, enhance=False):
        if self.busy:
            self._safe_emit("claude_error", {"text": "Still processing"})
            return

        self.busy = True
        self._emit_state("busy")
        self.prompt_count += 1

        # Run everything else in a background task so it survives disconnects
        self.sio.start_background_task(self._execute_prompt, text, enhance)

    @staticmethod
    def _is_discussion(text):
        """Return True if the message is asking about a feature rather than requesting an action."""
        patterns = [
            r'\btell me about\b',
            r'\bexplain\b',
            r'\bhow does\b',
            r'\bdescribe\b',
            r'\bwhat is the\b',
            r'\bhow do\b',
            r'\bwhy does\b',
            r'\bhow did\b',
            r'\bwhen does\b',
            r'\babout the\b',
        ]
        lower = text.lower()
        return any(re.search(p, lower) for p in patterns)

    def _execute_prompt(self, text, enhance=False):
        """Background task: router, enhancer, process spawn, output reading."""
        # Ensure we have a db session (create on first prompt)
        if not self.db_session_id:
            self.db_session_id, stored_claude_id = _get_or_create_db_session(self.claude_session_id)
            if not self.claude_session_id and stored_claude_id:
                self.claude_session_id = stored_claude_id

        # Save original user message
        _save_message(self.db_session_id, "user", text)

        # ── Router (always runs, both send modes) ──
        action, handler_name, route_payload = _route_message(text)

        if action == "direct":
            self._handle_direct(handler_name, route_payload)
            self.busy = False
            self._emit_state("ready")
            return

        # ── Claude Code needed from here on ──
        claude_path = _find_claude()
        if not claude_path:
            self._safe_emit("claude_error",
                            {"text": "Claude Code not found", "detail": "Binary not in PATH"})
            self.busy = False
            self._emit_state("ready")
            return

        # ── Prompt enhancer (only if enhance=True AND enabled) ──
        actual_prompt = text
        if enhance and _enhancer_config["enabled"]:
            self._safe_emit("claude_status", {"type": "enhancer", "text": "Enhancing prompt..."})
            enhanced_text, was_enhanced = self._run_enhancer(text)
            if was_enhanced:
                actual_prompt = enhanced_text
                self._safe_emit("claude_status", {"type": "enhancer", "text": "Prompt enhanced — executing..."})
            else:
                self._safe_emit("claude_status", {"type": "enhancer", "text": ""})

        # Prepend discussion guard if user is asking about a feature, not requesting an action
        if self._is_discussion(text):
            actual_prompt = (
                "[DO NOT emit DIRECT_ACTION markers for this message — "
                "the user is asking about a feature, not requesting an action.]\n\n"
                + actual_prompt
            )

        cmd = [claude_path, "-p", actual_prompt,
               "--dangerously-skip-permissions",
               "--output-format", "stream-json", "--verbose"]

        if _claude_model["model"]:
            cmd.extend(["--model", _claude_model["model"]])

        will_resume = bool(self.claude_session_id)
        print(f"[debug] send_prompt: claude_session_id={self.claude_session_id!r} (type={type(self.claude_session_id).__name__}), will_resume={will_resume}, db_session_id={self.db_session_id}", flush=True)
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
        self._read_output()

    def _read_output(self):
        """Read stream-json output line by line."""
        response_parts = []
        self.current_activities = []
        _dbg_text_blocks = 0
        _dbg_result_count = 0
        _had_tool_use = False

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

                # System init — capture model name
                if msg_type == "system":
                    model_id = msg.get("model")
                    if model_id:
                        self._safe_emit("claude_model", {"model": model_id})

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
                            _had_tool_use = True
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
                        # Preserve text block if it contains a design widget payload
                        # (result_text is often a plain summary that strips the widget JSON)
                        current_text = response_parts[0] if response_parts else ""
                        if "<!--DESIGN_WIDGET-->" in current_text and "<!--DESIGN_WIDGET-->" not in result_text:
                            print(f"[debug] PRESERVING text block with DESIGN_WIDGET (result would overwrite)", flush=True)
                        else:
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

        # Check for DIRECT_ACTION marker from Claude Code
        # Only honor if it appeared in the first text block with no tool_use before it.
        # A real direct action intent means Claude emits ONLY the marker immediately.
        da_marker = '<!--DIRECT_ACTION:'
        if da_marker in response:
            da_match = re.search(r'<!--DIRECT_ACTION:(\w+)-->', response)
            if da_match:
                action_name = da_match.group(1)
                if _had_tool_use or _dbg_text_blocks > 1:
                    print(f"[debug] DIRECT_ACTION '{action_name}' ignored: tool_use={_had_tool_use}, text_blocks={_dbg_text_blocks}", flush=True)
                    # Strip the marker from the response and show it as normal text
                    response = re.sub(r'<!--DIRECT_ACTION:\w+-->\s*', '', response).strip()
                else:
                    print(f"[debug] DIRECT_ACTION detected: {action_name}", flush=True)
                    self._handle_direct(action_name, {"raw_text": ""})
                    _save_message(self.db_session_id, "assistant", f"[direct action: {action_name}]", self.current_activities)
                    self.busy = False
                    self.process = None
                    self._emit_state("ready")
                    return

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
        claude_session_id = data.get("claude_session_id") or None  # coerce falsy to None
        db_session_id = data.get("db_session_id")
    print(f"[debug] ws_claude_start: claude_session_id={claude_session_id!r}, db_session_id={db_session_id!r}", flush=True)
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
        enhance = data.get("enhance", False) if isinstance(data, dict) else False
        if text:
            s.send_prompt(text, enhance=enhance)
    else:
        emit("claude_error", {"text": "No active Claude Code session"})

@socketio.on("claude_stop", namespace="/terminal/ws")
def ws_claude_stop():
    s = _claude_session["session"]
    if s and s.sid == request.sid:
        _cleanup_claude()


@socketio.on("direct_action", namespace="/terminal/ws")
def ws_direct_action(data):
    """Handle direct actions triggered by widget buttons (e.g., push from git status widget)."""
    token = request.cookies.get("platform_auth")
    if not token or not secrets.compare_digest(token, _auth_token()):
        disconnect()
        return

    s = _claude_session.get("session")
    if not s:
        # Create a temporary session just for this action
        s = ClaudeCodeSession(socketio, request.sid)
        _claude_session["session"] = s

    action = data.get("action", "") if isinstance(data, dict) else ""
    s.sid = request.sid  # ensure emits go to this client

    if action == "git_push":
        s._safe_emit("claude_status", {"type": "direct", "text": "Pushing..."})
        s._direct_git_push(data.get("args", {}))
    else:
        emit("claude_error", {"text": f"Unknown action: {action}"})


@socketio.on("style_override", namespace="/terminal/ws")
def ws_style_override(data):
    emit("style_override", data, broadcast=True)

@socketio.on("style_override_clear", namespace="/terminal/ws")
def ws_style_override_clear(data):
    emit("style_override_clear", data, broadcast=True)


# ── Main ──────────────────────────────────────────────────────
if __name__ == "__main__":
    _init_terminal_db()
    socketio.run(app, host="0.0.0.0", port=8051, log_output=True)
