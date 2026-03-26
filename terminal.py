"""
terminal.py — Web terminal for the trading platform.
Flask + flask-socketio app serving a browser-based shell via PTY.
"""

import eventlet
eventlet.monkey_patch()

import os, sys, pty, re, errno, signal, struct, fcntl, termios, hashlib, secrets, shutil
from uuid import uuid4
from flask import Flask, request, redirect, jsonify
from flask_socketio import SocketIO, emit, disconnect

# ── Platform imports ──────────────────────────────────────────
sys.path.insert(0, "/opt/trading-platform")
from config import DASHBOARD_USER, DASHBOARD_PASS
from db import get_config, set_config

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

# ── WebSocket handlers ────────────────────────────────────────

@socketio.on("connect", namespace="/terminal/ws")
def ws_connect():
    # Auth check on WebSocket
    token = request.cookies.get("platform_auth")
    if not token or not secrets.compare_digest(token, _auth_token()):
        disconnect()
        return

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
            rows = int(data.get("rows", 24))
            cols = int(data.get("cols", 80))
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
        except (OSError, ValueError):
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

# ── Claude Code session ───────────────────────────────────────

# ANSI escape stripper
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][A-B012]|\x1b\[[\?]?[0-9;]*[hlm]|\x1b\[[0-9]*[ABCDJKHGP]|\x1b=|\x1b>|\r')

def _strip_ansi(text):
    return _ANSI_RE.sub('', text)

# Activity patterns (⏺ prefix lines from Claude Code)
_ACTIVITY_RE = re.compile(r'[⏺●]\s+(.+)')

# Claude Code "ready for input" prompt patterns
# Matches the ">" or "❯" prompt at start of line that signals input ready
_PROMPT_RE = re.compile(r'(?:^|\n)\s*[>❯]\s*$')

# Also detect the initial welcome/ready state
_WELCOME_RE = re.compile(r'(?:Type your prompt|How can I help|What would you like)')

_CLAUDE_SEARCH_PATHS = [
    "/root/.local/bin/claude",
    "/usr/local/bin/claude",
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


class ClaudeCodeSession:
    def __init__(self, sio, sid):
        self.sio = sio
        self.sid = sid
        self.process = None      # child PID
        self.master_fd = None    # PTY master fd
        self.alive = False
        self.busy = False
        self.session_id = str(uuid4())[:8]
        self.raw_buffer = ""
        self.response_buf = ""   # accumulates response text between prompt sent and ready

    def start(self):
        claude_path = _find_claude()
        if not claude_path:
            self.sio.emit("claude_error",
                          {"text": "Claude Code not found", "detail": "Binary not in PATH or known locations"},
                          namespace="/terminal/ws", to=self.sid)
            return False

        pid, fd = pty.fork()
        if pid == 0:
            # Child
            os.chdir("/opt/trading-platform")
            os.environ["TERM"] = "xterm-256color"
            os.environ["LANG"] = "en_US.UTF-8"
            os.execvp(claude_path, [claude_path])
        else:
            self.process = pid
            self.master_fd = fd
            self.alive = True

            # Non-blocking
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

            self._emit_state("ready")
            self.sio.start_background_task(self._read_loop)
            return True

    def send_prompt(self, text):
        if not self.alive or self.master_fd is None:
            self.sio.emit("claude_error",
                          {"text": "No active session"},
                          namespace="/terminal/ws", to=self.sid)
            return
        if self.busy:
            self.sio.emit("claude_error",
                          {"text": "Still processing"},
                          namespace="/terminal/ws", to=self.sid)
            return

        self.busy = True
        self.response_buf = ""
        self._emit_state("busy")

        # Write prompt + newline to PTY
        data = text.strip() + "\n"
        try:
            os.write(self.master_fd, data.encode("utf-8"))
        except OSError as e:
            self.sio.emit("claude_error",
                          {"text": "Write failed", "detail": str(e)},
                          namespace="/terminal/ws", to=self.sid)
            self.busy = False
            self._emit_state("ready")

    def resize(self, rows, cols):
        if self.master_fd is not None:
            try:
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
            except (OSError, ValueError):
                pass

    def stop(self):
        self.alive = False
        fd = self.master_fd
        pid = self.process
        self.master_fd = None
        self.process = None
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
        self._emit_state("dead")

    def _emit_state(self, state):
        self.sio.emit("claude_state",
                      {"state": state, "session_id": self.session_id},
                      namespace="/terminal/ws", to=self.sid)

    def _read_loop(self):
        while self.alive:
            try:
                eventlet.sleep(0.01)
                if not self.alive or self.master_fd is None:
                    break
                try:
                    data = os.read(self.master_fd, 4096)
                    if not data:
                        break
                    text = data.decode("utf-8", errors="replace")

                    # Always emit raw output
                    self.sio.emit("claude_raw", {"data": text},
                                  namespace="/terminal/ws", to=self.sid)

                    # Parse clean text
                    clean = _strip_ansi(text)
                    self._parse_output(clean)

                except (OSError, IOError) as e:
                    if getattr(e, 'errno', None) in (errno.EAGAIN, errno.EWOULDBLOCK):
                        continue
                    break
            except Exception:
                break

        # Session ended
        if self.alive:
            self.alive = False
            self.sio.emit("claude_error",
                          {"text": "Session ended unexpectedly"},
                          namespace="/terminal/ws", to=self.sid)
            self._emit_state("dead")

    def _parse_output(self, clean):
        self.raw_buffer += clean

        # Check for activity lines
        for m in _ACTIVITY_RE.finditer(clean):
            self.sio.emit("claude_status",
                          {"text": m.group(1).strip(), "type": "activity"},
                          namespace="/terminal/ws", to=self.sid)

        if self.busy:
            # Accumulate response text (skip activity lines)
            for line in clean.splitlines():
                stripped = line.strip()
                if stripped and not _ACTIVITY_RE.match(stripped):
                    self.response_buf += line + "\n"

            # Check if Claude returned to input prompt
            if _PROMPT_RE.search(self.raw_buffer):
                response = self.response_buf.strip()
                if response:
                    self.sio.emit("claude_response",
                                  {"text": response, "id": self.session_id},
                                  namespace="/terminal/ws", to=self.sid)
                self.busy = False
                self.response_buf = ""
                self.raw_buffer = ""
                self._emit_state("ready")
        else:
            # Not busy — check for welcome/ready prompt to clear buffer
            if _PROMPT_RE.search(self.raw_buffer) or _WELCOME_RE.search(self.raw_buffer):
                self.raw_buffer = ""


def _cleanup_claude():
    s = _claude_session["session"]
    if s is not None:
        s.stop()
    _claude_session["session"] = None

# ── Claude Code WebSocket handlers ────────────────────────────

@socketio.on("claude_start", namespace="/terminal/ws")
def ws_claude_start():
    token = request.cookies.get("platform_auth")
    if not token or not secrets.compare_digest(token, _auth_token()):
        disconnect()
        return

    _cleanup_claude()
    session = ClaudeCodeSession(socketio, request.sid)
    if session.start():
        _claude_session["session"] = session

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

@socketio.on("claude_resize", namespace="/terminal/ws")
def ws_claude_resize(data):
    s = _claude_session["session"]
    if s and s.sid == request.sid:
        try:
            s.resize(int(data.get("rows", 24)), int(data.get("cols", 80)))
        except (ValueError, AttributeError):
            pass


# ── HTML template ─────────────────────────────────────────────

TERMINAL_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>Terminal — Trading Platform</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; overflow: hidden; background: #0a0a0a; color: #e0e0e0;
    font-family: system-ui, -apple-system, sans-serif; }

  #app { width: 100%; height: 100dvh; display: flex; flex-direction: column; }

  /* ── Header ── */
  #header {
    height: 44px; padding: 0 12px; background: #111; border-bottom: 1px solid #333;
    display: flex; align-items: center; justify-content: space-between; flex-shrink: 0;
  }
  #header .title { font-size: 14px; font-weight: 600; color: #e0e0e0; }
  #header .status { display: flex; align-items: center; gap: 6px; font-size: 12px; color: #888; }
  #header .status .dot {
    width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
  }
  .dot-ready { background: #22c55e; }
  .dot-busy { background: #f59e0b; animation: pulse 1s infinite; }
  .dot-dead, .dot-disconnected { background: #ef4444; }
  .dot-none { background: #555; }
  @keyframes pulse { 50% { opacity: 0.4; } }
  #new-session-btn {
    background: none; border: 1px solid #444; color: #888; font-size: 12px;
    padding: 4px 10px; border-radius: 6px; cursor: pointer;
  }
  #new-session-btn:active { background: #222; }

  /* ── Tab bar ── */
  #tabs {
    display: flex; background: #111; border-bottom: 1px solid #333; flex-shrink: 0;
  }
  .tab {
    flex: 1; padding: 10px 0; text-align: center; font-size: 13px; font-weight: 500;
    color: #666; cursor: pointer; border-bottom: 2px solid transparent;
    transition: color 0.15s, border-color 0.15s; -webkit-tap-highlight-color: transparent;
  }
  .tab.active { color: #4a9eff; border-bottom-color: #4a9eff; }

  /* ── Panels ── */
  #panels { flex: 1; min-height: 0; position: relative; }
  .panel { position: absolute; inset: 0; display: none; flex-direction: column; }
  .panel.active { display: flex; }

  /* ── Claude panel ── */
  #claude-panel { background: #0a0a0a; }
  #conversation {
    flex: 1; overflow-y: auto; padding: 12px; overscroll-behavior: none;
    display: flex; flex-direction: column; gap: 12px;
  }
  .msg { max-width: 88%; padding: 10px 14px; border-radius: 16px; font-size: 15px;
    line-height: 1.5; word-wrap: break-word; position: relative; }
  .msg-user {
    align-self: flex-end; background: #1a1a2e; border-bottom-right-radius: 4px;
    white-space: pre-wrap;
  }
  .msg-assistant {
    align-self: flex-start; background: #1a1a1a; border-bottom-left-radius: 4px;
  }
  .msg-assistant pre {
    background: #0d0d0d; border: 1px solid #333; border-radius: 8px;
    padding: 10px; margin: 8px 0; overflow-x: auto; font-size: 13px;
    font-family: 'SF Mono', Menlo, Monaco, monospace; white-space: pre-wrap;
    word-wrap: break-word;
  }
  .msg-assistant code {
    font-family: 'SF Mono', Menlo, Monaco, monospace; font-size: 13px;
    background: #1e1e1e; padding: 1px 5px; border-radius: 4px;
  }
  .msg-assistant pre code { background: none; padding: 0; }
  .msg-assistant strong { color: #f0f0f0; }
  .copy-btn {
    position: absolute; top: 6px; right: 6px; background: rgba(255,255,255,0.08);
    border: none; color: #888; font-size: 11px; padding: 3px 8px; border-radius: 6px;
    cursor: pointer; opacity: 0; transition: opacity 0.15s;
  }
  .msg-assistant:hover .copy-btn, .msg-assistant:active .copy-btn { opacity: 1; }
  .copy-btn.copied { color: #22c55e; }

  /* ── Activity card ── */
  .activity-card {
    align-self: flex-start; max-width: 88%; padding: 8px 14px; border-radius: 12px;
    background: #1a1a1a; border: 1px solid #2a2a2a; font-size: 13px; color: #aaa;
    display: flex; align-items: center; gap: 8px;
  }
  .activity-card .spinner {
    width: 8px; height: 8px; border-radius: 50%; background: #f59e0b;
    animation: pulse 1s infinite; flex-shrink: 0;
  }
  .activity-collapsed {
    font-size: 12px; color: #555; cursor: pointer; align-self: flex-start;
    padding: 2px 0; margin-top: -8px;
  }
  .activity-collapsed:hover { color: #888; }
  .activity-log {
    display: none; align-self: flex-start; max-width: 88%; padding: 8px 12px;
    background: #111; border-radius: 8px; font-size: 12px; color: #777;
    font-family: monospace; white-space: pre-wrap; margin-top: -8px;
  }

  /* ── Error / restart ── */
  .msg-error {
    align-self: center; background: #2a1515; border: 1px solid #4a2020;
    color: #ef4444; border-radius: 8px; padding: 10px 16px; font-size: 13px;
    text-align: center;
  }
  .msg-error button {
    background: #ef4444; color: #fff; border: none; padding: 6px 14px;
    border-radius: 6px; margin-top: 8px; cursor: pointer; font-size: 13px;
  }

  /* ── Session divider ── */
  .session-divider {
    text-align: center; color: #444; font-size: 11px; padding: 8px 0;
    border-top: 1px solid #222; margin-top: 4px;
  }

  /* ── Input area ── */
  #input-area {
    padding: 8px 12px; padding-bottom: calc(8px + env(safe-area-inset-bottom, 0px));
    background: #111; border-top: 1px solid #333; display: flex; gap: 8px;
    align-items: flex-end; flex-shrink: 0;
  }
  #claude-paste-btn {
    width: 44px; height: 44px; border-radius: 10px; border: none;
    background: #1a1a1a; color: #888; font-size: 20px; cursor: pointer;
    flex-shrink: 0; display: flex; align-items: center; justify-content: center;
  }
  #claude-paste-btn:active { background: #2a2a2a; }
  #prompt-input {
    flex: 1; background: #1a1a1a; border: 1px solid #333; border-radius: 12px;
    color: #e0e0e0; font-size: 16px; padding: 10px 14px; resize: none;
    font-family: system-ui, -apple-system, sans-serif; line-height: 1.4;
    min-height: 44px; max-height: 45vh; overflow-y: auto;
  }
  #prompt-input::placeholder { color: #555; }
  #prompt-input:focus { outline: none; border-color: #4a9eff; }
  #send-btn {
    width: 44px; height: 44px; border-radius: 10px; border: none;
    background: #4a9eff; color: #fff; font-size: 20px; cursor: pointer;
    flex-shrink: 0; display: flex; align-items: center; justify-content: center;
    transition: background 0.15s;
  }
  #send-btn:disabled { background: #333; color: #666; cursor: default; }
  #send-btn.stop-btn { background: #ef4444; }

  /* ── Shell panel ── */
  #shell-panel { background: #0a0a0a; }
  #shell-wrap { flex: 1; min-height: 0; position: relative; }
  .xterm { height: 100%; }
  #shell-paste-btn {
    position: absolute; bottom: 16px; right: 16px; z-index: 10;
    width: 40px; height: 40px; border-radius: 50%; border: none;
    background: rgba(255,255,255,0.1); color: #c9d1d9; font-size: 18px;
    cursor: pointer; display: flex; align-items: center; justify-content: center;
    backdrop-filter: blur(4px); transition: background 0.2s;
  }
  #shell-paste-btn:hover { background: rgba(255,255,255,0.2); }

  /* ── Log panel ── */
  #log-panel { background: #0a0a0a; }
  #log-header {
    padding: 8px 12px; display: flex; justify-content: space-between; align-items: center;
    border-bottom: 1px solid #222; flex-shrink: 0;
  }
  #log-header span { font-size: 12px; color: #666; }
  #log-clear-btn {
    background: none; border: 1px solid #333; color: #666; font-size: 11px;
    padding: 3px 10px; border-radius: 4px; cursor: pointer;
  }
  #log-content {
    flex: 1; overflow-y: auto; padding: 8px 12px; font-family: 'SF Mono', Menlo, Monaco, monospace;
    font-size: 12px; color: #888; white-space: pre-wrap; word-wrap: break-word;
    overscroll-behavior: none;
  }
</style>
</head>
<body>
<div id="app">
  <!-- Header -->
  <div id="header">
    <span class="title">Claude Code</span>
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
        <button id="claude-paste-btn" title="Paste">&#x1F4CB;</button>
        <textarea id="prompt-input" rows="1" placeholder="Send a prompt to Claude Code..."></textarea>
        <button id="send-btn" title="Send">&#x2191;</button>
      </div>
    </div>

    <!-- Shell panel -->
    <div id="shell-panel" class="panel">
      <div id="shell-wrap">
        <button id="shell-paste-btn" title="Paste from clipboard">&#x1F4CB;</button>
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

  // ═══════════════════════════════════════════════════
  //  SHELL MODE (lazy init)
  // ═══════════════════════════════════════════════════
  var term, fitAddon;

  function initShell() {
    if (shellInitialized) return;
    shellInitialized = true;

    term = new Terminal({
      cursorBlink: true, fontSize: 16,
      fontFamily: '"SF Mono", Menlo, Monaco, "Courier New", monospace',
      theme: {
        background: '#0a0a0a', foreground: '#c9d1d9', cursor: '#58a6ff',
        selectionBackground: '#264f78',
        black: '#0d1117', red: '#ff7b72', green: '#7ee787', yellow: '#d29922',
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
    if (shellInitialized) {
      var d = fitAddon.proposeDimensions();
      if (d) socket.emit('resize', {rows: d.rows, cols: d.cols});
    }
  });
  socket.on('output', function(data) { if (term) term.write(data); });
  socket.on('exit', function() {
    if (term) term.write('\r\n\x1b[33m[shell exited — reconnecting...]\x1b[0m\r\n');
    setTimeout(function() { socket.disconnect(); socket.connect(); }, 1000);
  });
  socket.on('reconnect', function() { if (term) term.clear(); });

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

  function setClaudeState(state) {
    claudeState = state;
    claudeDot.className = 'dot dot-' + state;
    var labels = {none:'Idle', ready:'Ready', busy:'Working...', dead:'Disconnected'};
    claudeStatusText.textContent = labels[state] || state;
    updateSendBtn();
  }

  function updateSendBtn() {
    if (claudeState === 'busy') {
      sendBtn.textContent = '\u25A0'; // stop square
      sendBtn.classList.add('stop-btn');
      sendBtn.disabled = false;
    } else {
      sendBtn.textContent = '\u2191'; // up arrow
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

  function addAssistantMsg(text, rawText) {
    var el = document.createElement('div');
    el.className = 'msg msg-assistant';
    el.innerHTML = renderMd(text);
    // Copy button
    var btn = document.createElement('button');
    btn.className = 'copy-btn';
    btn.textContent = 'Copy';
    btn.addEventListener('click', function() {
      navigator.clipboard.writeText(rawText || text).then(function() {
        btn.textContent = 'Copied!';
        btn.classList.add('copied');
        setTimeout(function() { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 1500);
      }).catch(function(){});
    });
    el.appendChild(btn);
    conv.appendChild(el);
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
      toggle.textContent = '\u25B6 View activity (' + activityLog.length + ' steps)';
      var logEl = document.createElement('div');
      logEl.className = 'activity-log';
      logEl.textContent = activityLog.join('\n');
      toggle.addEventListener('click', function() {
        var open = logEl.style.display === 'block';
        logEl.style.display = open ? 'none' : 'block';
        toggle.textContent = (open ? '\u25B6' : '\u25BC') + ' View activity (' + activityLog.length + ' steps)';
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
    socket.emit('claude_start');
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
    if (claudeState === 'none' || claudeState === 'dead') {
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
  document.getElementById('new-session-btn').addEventListener('click', function() {
    if (claudeState === 'busy' || claudeState === 'ready') {
      socket.emit('claude_stop');
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
    updateActivityCard(d.text);
  });

  socket.on('claude_response', function(d) {
    collapseActivityCard();
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
    if (claudeState !== 'none') setClaudeState('dead');
  });

  socket.on('reconnect', function() {
    // Session is gone after reconnect
    if (claudeState !== 'none') {
      setClaudeState('none');
      addError('Connection lost. Session ended.', true);
    }
  });

  // Log clear button
  document.getElementById('log-clear-btn').addEventListener('click', function() {
    logContent.textContent = '';
  });

  // Init
  updateSendBtn();
})();
</script>
</body>
</html>
"""

# ── Main ──────────────────────────────────────────────────────
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=8051, log_output=True)
