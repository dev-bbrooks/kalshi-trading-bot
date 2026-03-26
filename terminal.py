"""
terminal.py — Web terminal for the trading platform.
Flask + flask-socketio app serving a browser-based shell via PTY.
"""

import eventlet
eventlet.monkey_patch()

import os, sys, pty, errno, signal, struct, fcntl, termios, hashlib, secrets
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

# ── Active shell session ──────────────────────────────────────
_active_session = {"fd": None, "pid": None, "sid": None}

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
  html, body { height: 100%; overflow: hidden; background: #0a0a0a; }
  #terminal-container {
    width: 100%; height: 100dvh; padding: 0;
    display: flex; flex-direction: column;
  }
  #status-bar {
    height: 28px; line-height: 28px; padding: 0 10px;
    background: #1a1a2e; color: #888; font-size: 12px;
    font-family: -apple-system, system-ui, sans-serif;
    display: flex; justify-content: space-between; align-items: center;
    flex-shrink: 0;
  }
  #status-bar .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
  .dot-connected { background: #00d26a; }
  .dot-disconnected { background: #f44; }
  .dot-connecting { background: #f90; animation: pulse 1s infinite; }
  @keyframes pulse { 50% { opacity: 0.4; } }
  #terminal-wrap { flex: 1; min-height: 0; }
  .xterm { height: 100%; }
</style>
</head>
<body>
<div id="terminal-container">
  <div id="status-bar">
    <span><span id="status-dot" class="dot dot-connecting"></span><span id="status-text">Connecting…</span></span>
    <span id="status-info">trading-platform</span>
  </div>
  <div id="terminal-wrap"></div>
</div>

<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-web-links@0.9.0/lib/xterm-addon-web-links.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/socket.io-client@4.7.2/dist/socket.io.min.js"></script>
<script>
(function() {
  var term = new Terminal({
    cursorBlink: true,
    fontSize: 16,
    fontFamily: '"SF Mono", "Menlo", "Monaco", "Courier New", monospace',
    theme: {
      background: '#0a0a0a',
      foreground: '#c9d1d9',
      cursor: '#58a6ff',
      selectionBackground: '#264f78',
      black: '#0d1117', red: '#ff7b72', green: '#7ee787', yellow: '#d29922',
      blue: '#58a6ff', magenta: '#bc8cff', cyan: '#39c5cf', white: '#c9d1d9',
      brightBlack: '#484f58', brightRed: '#ffa198', brightGreen: '#56d364',
      brightYellow: '#e3b341', brightBlue: '#79c0ff', brightMagenta: '#d2a8ff',
      brightCyan: '#56d4dd', brightWhite: '#f0f6fc'
    },
    allowProposedApi: true,
    scrollback: 5000
  });

  var fitAddon = new FitAddon.FitAddon();
  var webLinksAddon = new WebLinksAddon.WebLinksAddon();
  term.loadAddon(fitAddon);
  term.loadAddon(webLinksAddon);
  term.open(document.getElementById('terminal-wrap'));
  fitAddon.fit();

  var dot = document.getElementById('status-dot');
  var stxt = document.getElementById('status-text');

  function setStatus(state, text) {
    dot.className = 'dot dot-' + state;
    stxt.textContent = text;
  }

  var socket = io('/terminal/ws', {
    path: '/terminal/ws/socket.io',
    transports: ['websocket'],
    reconnection: true,
    reconnectionDelay: 1000,
    reconnectionDelayMax: 5000
  });

  socket.on('connect', function() {
    setStatus('connected', 'Connected');
    var dims = fitAddon.proposeDimensions();
    if (dims) socket.emit('resize', {rows: dims.rows, cols: dims.cols});
  });

  socket.on('output', function(data) {
    term.write(data);
  });

  socket.on('exit', function() {
    term.write('\r\n\x1b[33m[shell exited — reconnecting…]\x1b[0m\r\n');
    setTimeout(function() { socket.disconnect(); socket.connect(); }, 1000);
  });

  socket.on('disconnect', function() {
    setStatus('disconnected', 'Disconnected');
  });

  socket.on('reconnecting', function() {
    setStatus('connecting', 'Reconnecting…');
  });

  socket.on('reconnect', function() {
    setStatus('connected', 'Connected');
    term.clear();
  });

  term.onData(function(data) {
    socket.emit('input', data);
  });

  window.addEventListener('resize', function() {
    fitAddon.fit();
    var dims = fitAddon.proposeDimensions();
    if (dims) socket.emit('resize', {rows: dims.rows, cols: dims.cols});
  });

  new ResizeObserver(function() {
    fitAddon.fit();
    var dims = fitAddon.proposeDimensions();
    if (dims) socket.emit('resize', {rows: dims.rows, cols: dims.cols});
  }).observe(document.getElementById('terminal-wrap'));
})();
</script>
</body>
</html>
"""

# ── Main ──────────────────────────────────────────────────────
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=8051, log_output=True)
