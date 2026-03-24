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
    init_db, get_bot_state, update_bot_state, get_all_config, now_utc,
    enqueue_command, set_config, get_config,
    get_recent_trades, get_trade, get_cycle_trades,
    delete_trades, recompute_all_stats,
    get_trade_summary, get_round_stats, get_lifetime_stats,
    get_all_regime_stats, get_regime_risk, get_regime_worker_status,
    get_logs, get_logs_after,
    get_price_path, get_open_trade,
    get_live_prices,
    save_push_subscription, remove_push_subscription_by_endpoint,
    get_bankroll_chart_data, get_pnl_chart_data,
)

app = Flask(__name__)

# ═══════════════════════════════════════════════════════════════
#  AUTH
# ═══════════════════════════════════════════════════════════════

def check_auth(u, p):
    import hashlib
    if u != DASHBOARD_USER:
        return False
    # Check DB password first (if user changed it)
    try:
        stored_hash = get_config("dashboard_pass_hash")
        if stored_hash:
            return hashlib.sha256(p.encode()).hexdigest() == stored_hash
    except Exception:
        pass
    return p == DASHBOARD_PASS

def _auth_token():
    """Generate a signed auth token for cookie."""
    import hashlib
    # Use DB hash if available, else env password
    try:
        stored_hash = get_config("dashboard_pass_hash")
        if stored_hash:
            return hashlib.sha256(f"{DASHBOARD_USER}:{stored_hash}:botauth2".encode()).hexdigest()
    except Exception:
        pass
    return hashlib.sha256(f"{DASHBOARD_USER}:{DASHBOARD_PASS}:botauth2".encode()).hexdigest()

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
    headers: {'Content-Type': 'application/json'},
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
    headers: {'Content-Type': 'application/json'},
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
        token = request.cookies.get("bot_auth")
        if token == _auth_token():
            return f(*args, **kwargs)
        # Fall back to Basic Auth (for API clients)
        auth = request.authorization
        if auth and check_auth(auth.username, auth.password):
            resp = make_response(f(*args, **kwargs))
            resp.set_cookie("bot_auth", _auth_token(), max_age=90*86400,
                            httponly=True, samesite="Lax")
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
    data = request.get_json() or {}
    if check_auth(data.get("username", ""), data.get("password", "")):
        resp = jsonify({"ok": True})
        resp.set_cookie("bot_auth", _auth_token(), max_age=90*86400,
                        httponly=True, samesite="Lax")
        return resp
    return jsonify({"error": "Invalid credentials"}), 401


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

    return jsonify({"ok": True})


@app.route("/api/state")
@requires_auth
def api_state():
    state = get_bot_state()
    state["last_updated_ct"] = to_central(state.get("last_updated", ""))
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
    return jsonify(cfg)


@app.route("/api/config", methods=["POST"])
@requires_auth
def api_set_config():
    data = request.get_json() or {}
    for k, v in data.items():
        set_config(k, v)
    enqueue_command("update_config", data)
    return jsonify({"ok": True})


@app.route("/api/command", methods=["POST"])
@requires_auth
def api_command():
    data = request.get_json() or {}
    cmd = data.get("command", "")
    params = data.get("params", {})
    if cmd not in ("start", "stop", "cash_out", "reset_cycle",
                   "update_config", "lock_bankroll",
                   "manual_buy", "manual_set_sell", "manual_hold",
                   "dismiss_summary", "cancel_cash_out",
                   "cancel_pending", "preset_sell",
                   "stop_after_cycle", "cancel_stop_after_cycle"):
        return jsonify({"error": "Invalid command"}), 400

    # Lock/unlock: save config immediately so UI updates fast,
    # also queue command so bot resets cycle
    if cmd == "lock_bankroll":
        amount = float(params.get("amount", 0))
        current = float(get_all_config().get("locked_bankroll", 0))
        new_locked = max(0, current + amount)
        set_config("locked_bankroll", new_locked)

    # Dismiss summary: clear immediately, no need to queue
    if cmd == "dismiss_summary":
        update_bot_state({"last_completed_trade": None})
        return jsonify({"ok": True})

    # Start: set state for instant UI feedback
    if cmd == "start":
        state = get_bot_state()
        at = state.get("active_trade")
        has_manual = at and (at.get("is_manual") or at.get("is_ignored"))
        update_bot_state({
            "auto_trading": 1,
            "status": "trading" if has_manual else "searching",
            "status_detail": "Waiting for manual trade to finish..." if has_manual else "Starting...",
        })

    # Stop: set state for instant UI feedback
    if cmd == "stop":
        update_bot_state({
            "auto_trading": 0,
            "trades_remaining": 0,
            "stop_after_cycle": 0,
            "cycle_mode": 0,
            "status_detail": "Stopping...",
        })

    # Manual buy: set status immediately
    if cmd == "manual_buy":
        side = params.get("side", "yes")
        update_bot_state({
            "status": "trading",
            "status_detail": f"Placing {side.upper()} buy order...",
            "pending_trade": {
                "side": side,
                "shares_ordered": 0,
                "shares_filled": 0,
                "price_c": 0,
                "order_id": None,
                "ticker": "",
                "close_time": "",
                "is_manual": True,
                "sell_price_preset_c": 0,
                "placeholder": True,
            },
        })

    # Cash out: set status immediately for instant feedback
    if cmd == "cash_out":
        update_bot_state({
            "status": "trading",
            "status_detail": "CASHING OUT — selling aggressively...",
            "cashing_out": 1,
            "cancel_cash_out": 0,
        })

    # Cancel cash out: clear both flags immediately
    if cmd == "cancel_cash_out":
        update_bot_state({
            "cancel_cash_out": 1,
            "cashing_out": 0,
            "status_detail": "Cash out cancelled",
        })
        return jsonify({"ok": True})

    # Cancel pending: set flag immediately
    if cmd == "cancel_pending":
        update_bot_state({
            "status_detail": "Cancelling buy order...",
        })

    # Stop after cycle: set flag, don't queue — purely a flag check
    if cmd == "stop_after_cycle":
        update_bot_state({"stop_after_cycle": 1})
        return jsonify({"ok": True})

    # Cancel stop after cycle
    if cmd == "cancel_stop_after_cycle":
        update_bot_state({"stop_after_cycle": 0})
        return jsonify({"ok": True})

    cmd_id = enqueue_command(cmd, params)
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


@app.route("/api/trade/<int:trade_id>/cycle")
@requires_auth
def api_trade_cycle(trade_id):
    """Get all trades in the same martingale cycle."""
    cycle = get_cycle_trades(trade_id)
    for t in cycle:
        t["created_ct"] = to_central(t.get("created_at", ""))
    return jsonify(cycle)


@app.route("/api/trade/<int:trade_id>/delete", methods=["POST"])
@requires_auth
def api_delete_trade(trade_id):
    """Delete a single trade and recompute stats."""
    trade = get_trade(trade_id)
    if not trade:
        return jsonify({"error": "Trade not found"}), 404
    delete_trades([trade_id])
    recompute_all_stats()
    return jsonify({"ok": True, "deleted": 1})


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
    path = get_price_path(trade_id)
    return jsonify({"trade": t, "price_path": path})


@app.route("/api/trade/<int:trade_id>/delete_cycle", methods=["POST"])
@requires_auth
def api_delete_cycle(trade_id):
    """Delete all trades in the same martingale cycle and recompute stats."""
    cycle = get_cycle_trades(trade_id)
    if not cycle:
        return jsonify({"error": "No cycle found"}), 404
    ids = [t["id"] for t in cycle]
    delete_trades(ids)
    recompute_all_stats()
    return jsonify({"ok": True, "deleted": len(ids)})


@app.route("/api/rounds")
@requires_auth
def api_rounds():
    return jsonify(get_round_stats())


@app.route("/api/lifetime")
@requires_auth
def api_lifetime():
    return jsonify(get_lifetime_stats())


@app.route("/api/regimes")
@requires_auth
def api_regimes():
    return jsonify(get_all_regime_stats())


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

        # Per-round breakdown
        rounds = c.execute("""
            SELECT cycle_round, COUNT(*) as n,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                SUM(COALESCE(pnl,0)) as pnl
            FROM trades WHERE regime_label = ? AND outcome IN ('win','loss')
              AND COALESCE(is_ignored,0) = 0
            GROUP BY cycle_round ORDER BY cycle_round
        """, (label,)).fetchall()

        # Side breakdown
        sides = c.execute("""
            SELECT side, COUNT(*) as n,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                SUM(COALESCE(pnl,0)) as pnl
            FROM trades WHERE regime_label = ? AND outcome IN ('win','loss')
              AND COALESCE(is_ignored,0) = 0
            GROUP BY side
        """, (label,)).fetchall()

        # Recent trades
        recent = c.execute("""
            SELECT id, outcome, side, pnl, cycle_round,
                   avg_fill_price_c as entry_price_c, sell_price_c,
                   price_high_water_c, created_at
            FROM trades WHERE regime_label = ? AND outcome IN ('win','loss','cashed_out')
              AND COALESCE(is_ignored,0) = 0
            ORDER BY id DESC LIMIT 10
        """, (label,)).fetchall()

        # Avg entry price, avg HWM
        avgs = c.execute("""
            SELECT AVG(avg_fill_price_c) as avg_entry,
                   AVG(price_high_water_c) as avg_hwm,
                   AVG(sell_price_c) as avg_sell,
                   MAX(pnl) as best_pnl, MIN(pnl) as worst_pnl
            FROM trades WHERE regime_label = ? AND outcome IN ('win','loss')
              AND COALESCE(is_ignored,0) = 0
        """, (label,)).fetchone()

        recent_list = rows_to_list(recent)
        for r in recent_list:
            r["created_ct"] = to_central(r.get("created_at", ""))

        return jsonify({
            "stats": stats,
            "rounds": rows_to_list(rounds),
            "sides": rows_to_list(sides),
            "recent": recent_list,
            "averages": row_to_dict(avgs) if avgs else {},
        })


@app.route("/api/regime_status")
@requires_auth
def api_regime_status():
    s = get_regime_worker_status()
    if s.get("latest_snapshot"):
        s["latest_snapshot"]["captured_ct"] = to_central(
            s["latest_snapshot"].get("captured_at", ""))
    if s.get("latest_candle_ts"):
        s["latest_candle_ct"] = to_central(s["latest_candle_ts"])
    return jsonify(s)


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
    lines.append("# Kalshi BTC 15-min Martingale Bot — Status Report")
    lines.append(f"Generated: {to_central(now_utc())}")
    lines.append("")

    # Config
    lines.append("## Configuration")
    bet = f"${cfg.get('bet_size', 50)}" if cfg.get('bet_mode') == 'flat' else f"{cfg.get('bet_size', 5)}%"
    lines.append(f"- Bet: {bet} ({cfg.get('bet_mode', 'flat')} mode)")
    lines.append(f"- Max losses: {cfg.get('max_losses', 3)}")
    lines.append(f"- Entry range: {cfg.get('entry_price_min_c', 25)}-{cfg.get('entry_price_max_c', 42)}c")
    lines.append(f"- Locked bankroll: ${cfg.get('locked_bankroll', 0)}")
    lines.append(f"- Bankroll min/max: ${cfg.get('bankroll_min', 0)} / ${cfg.get('bankroll_max', 0)}")
    lines.append("")

    # State
    lines.append("## Current State")
    lines.append(f"- Balance: ${(state.get('bankroll_cents', 0) or 0) / 100:.2f}")
    lines.append(f"- Cycle: R{state.get('cycle_round', 1)}, streak={state.get('cycle_loss_streak', 0)}, hole=${state.get('cycle_hole', 0):.2f}")
    lines.append(f"- Session P&L: ${state.get('session_pnl', 0):.2f}")
    lines.append("")

    # Lifetime stats
    lines.append("## Lifetime Stats")
    w = stats.get('wins') or 0
    l = stats.get('losses') or 0
    lines.append(f"- Record: {w}W-{l}L ({stats.get('win_rate_pct', 0)}% win rate)")
    lines.append(f"- Total P&L: ${stats.get('total_pnl', 0):.2f}")
    lines.append(f"- Total wagered: ${stats.get('total_wagered', 0):.2f}")
    lines.append(f"- Total fees: ${stats.get('total_fees', 0):.2f}")
    lines.append(f"- ROI: {stats.get('roi_pct', 0)}%")
    lines.append(f"- Profit factor: {stats.get('profit_factor', 0)}")
    lines.append(f"- Best win streak: {stats.get('best_win_streak', 0)}")
    lines.append(f"- Worst loss streak: {stats.get('worst_loss_streak', 0)}")
    lines.append(f"- Max drawdown: ${stats.get('max_drawdown', 0):.2f}")
    lines.append(f"- Peak P&L: ${stats.get('peak_pnl', 0):.2f}")
    lines.append(f"- Cycles won: {stats.get('cycles_won', 0)}, max-loss resets: {stats.get('cycles_lost', 0)}")
    lines.append("")

    # Round breakdown
    rb = stats.get('round_breakdown', [])
    if rb:
        lines.append("## Round Breakdown")
        for r in rb:
            rt = (r.get('wins', 0) or 0) + (r.get('losses', 0) or 0)
            wr = round((r.get('wins', 0) or 0) / rt * 100) if rt > 0 else 0
            lines.append(f"- R{r['round']}: {r.get('wins',0)}W/{r.get('losses',0)}L ({wr}%), net ${r.get('net_pnl',0):.2f}")
        lines.append("")

    # Regime stats
    if regimes:
        lines.append("## Regime Stats")
        for r in sorted(regimes, key=lambda x: -(x.get('total_trades', 0) or 0)):
            n = r.get('total_trades', 0) or 0
            if n == 0:
                continue
            wr = round((r.get('win_rate', 0) or 0) * 100)
            lines.append(f"- {r.get('regime_label', '?')} [{r.get('risk_level', '?')}]: "
                        f"{wr}% win (n={n}), P&L ${r.get('total_pnl', 0):.2f}")
        lines.append("")

    # Daily P&L
    dp = stats.get('daily_pnl', [])
    if dp:
        lines.append("## Daily P&L (last 14 days)")
        for d in dp:
            lines.append(f"- {d['day']}: {d.get('wins',0)}W/{d.get('losses',0)}L, ${d.get('pnl',0):.2f}")
        lines.append("")

    # Entry delay breakdown
    db = stats.get('delay_breakdown', [])
    if db:
        lines.append("## Entry Delay Breakdown")
        for d in db:
            dt = (d.get('wins', 0) or 0) + (d.get('losses', 0) or 0)
            dwr = round((d.get('wins', 0) or 0) / dt * 100) if dt > 0 else 0
            lines.append(f"- {d['delay_min']}min delay: {d.get('wins',0)}W/{d.get('losses',0)}L ({dwr}%), net ${d.get('net_pnl',0):.2f}")
        lines.append("")

    # Price stability breakdown
    sb = stats.get('stability_breakdown', [])
    if sb:
        lines.append("## Price Stability Breakdown (price range during polling)")
        for s_row in sb:
            st2 = (s_row.get('wins', 0) or 0) + (s_row.get('losses', 0) or 0)
            swr = round((s_row.get('wins', 0) or 0) / st2 * 100) if st2 > 0 else 0
            lines.append(f"- {s_row.get('stability_bucket','?')}: {s_row.get('wins',0)}W/{s_row.get('losses',0)}L ({swr}%), net ${s_row.get('net_pnl',0):.2f}")
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
            rnd = t.get('cycle_round', 1)
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

            lines.append(f"- {ts} | {outcome.upper()} ${pnl:+.2f} | "
                        f"R{rnd} {side}@{entry}c→{sell}c | "
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
      renotify: true,
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



# ═══════════════════════════════════════════════════════════════
#  AI CHAT
# ═══════════════════════════════════════════════════════════════

def _gather_chat_context():
    """Gather all relevant bot data as a context string for Claude."""
    from db import get_conn, rows_to_list, row_to_dict
    parts = []

    # Bot state
    state = get_bot_state()
    parts.append("## Bot State")
    for k in ['status', 'status_detail', 'auto_trading', 'cycle_round', 'cycle_hole',
              'cycle_profit_target', 'cycle_loss_streak', 'session_wins', 'session_losses',
              'session_pnl', 'session_skips', 'lifetime_wins', 'lifetime_losses',
              'lifetime_pnl', 'bankroll_cents', 'last_ticker', 'cooldown_remaining']:
        parts.append(f"  {k}: {state.get(k, '—')}")

    # Config
    cfg = get_all_config()
    parts.append("\n## Config")
    for k, v in sorted(cfg.items()):
        parts.append(f"  {k}: {v}")

    # Regime stats
    regimes = get_all_regime_stats()
    parts.append(f"\n## Regime Stats ({len(regimes)} regimes)")
    for r in regimes:
        wr = f"{(r.get('win_rate',0)*100):.1f}%"
        parts.append(f"  {r['regime_label']}: {r.get('risk_level','?')} risk, "
                     f"{wr} win rate, {r.get('total_trades',0)} trades, "
                     f"P&L ${r.get('total_pnl',0):.2f}, "
                     f"CI [{(r.get('ci_lower',0)*100):.0f}-{(r.get('ci_upper',1)*100):.0f}%]")

    # Recent trades (last 30)
    trades = get_recent_trades(30)
    parts.append(f"\n## Recent Trades (last {len(trades)})")
    for t in trades:
        side = (t.get('side') or '').upper()
        entry = t.get('avg_fill_price_c') or t.get('entry_price_c') or 0
        pnl = t.get('pnl', 0)
        parts.append(f"  #{t['id']} {t.get('outcome','?')} {side}@{entry}c "
                     f"R{t.get('cycle_round',1)} PnL=${pnl:.2f} "
                     f"regime={t.get('regime_label','?')} "
                     f"{t.get('created_at','')[:16]}")

    # Lifetime stats
    ls = get_lifetime_stats()
    if ls:
        parts.append("\n## Lifetime Stats")
        for k, v in ls.items():
            if isinstance(v, float):
                parts.append(f"  {k}: {v:.2f}")
            else:
                parts.append(f"  {k}: {v}")

    # Round stats
    rs = get_round_stats()
    if rs:
        parts.append("\n## Round Stats")
        for r in rs:
            parts.append(f"  R{r.get('cycle_round',1)}: {r.get('wins',0)}W/{r.get('losses',0)}L "
                         f"PnL=${r.get('net_pnl',0):.2f}")

    return "\n".join(parts)


@app.route("/api/chat", methods=["POST"])
@requires_auth
def api_chat():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not set in environment"}), 500

    data = request.get_json() or {}
    user_msg = data.get("message", "").strip()
    if not user_msg:
        return jsonify({"error": "No message"}), 400

    context = _gather_chat_context()
    system = (
        "You are an AI assistant for a Kalshi BTC 15-minute binary options trading bot. "
        "You have full access to the bot's current data below. Answer questions concisely "
        "and helpfully. Use the data to give specific, data-driven answers. "
        "When discussing risk, reference actual win rates and confidence intervals. "
        "Format dollar amounts and percentages clearly.\n\n"
        "STRATEGY CONTEXT:\n"
        "This bot uses a martingale recovery strategy. After a loss, the next bet increases "
        "to recover prior losses plus the original profit target. A single win at any round "
        "recovers the entire cycle. This means:\n"
        "- Win rate is the most important metric, not raw P&L per regime.\n"
        "- A regime with 75%+ win rate is strong even if some individual trades show losses, "
        "because the martingale math recovers those losses on the next win.\n"
        "- Negative P&L in a regime usually means a max-loss cycle occurred (lost all rounds), "
        "which is rare but costly. This doesn't make the regime bad if win rate is high.\n"
        "- Regime risk levels are based on win rate thresholds, not P&L.\n"
        "- Recovery rounds (R2+) are expected and healthy — they mean the system is working.\n"
        "- The real danger is low win rate regimes where max-loss cycles happen frequently.\n"
        "Be encouraging about high win rate regimes even if P&L is temporarily negative. "
        "Focus on win rate, confidence intervals, and sample size when evaluating regimes.\n\n"
        f"CURRENT BOT DATA:\n{context}"
    )

    try:
        resp = http_requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1024,
                "system": system,
                "messages": [{"role": "user", "content": user_msg}],
            },
            timeout=30,
        )
        if resp.status_code != 200:
            try:
                err = resp.json().get("error", {}).get("message", resp.text[:200])
            except Exception:
                err = resp.text[:200]
            return jsonify({"error": f"API {resp.status_code}: {err}"}), 500
        result = resp.json()
        text = "".join(b.get("text", "") for b in result.get("content", []))
        return jsonify({"response": text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════
#  PAGES
# ═══════════════════════════════════════════════════════════════

@app.route("/logout")
def logout():
    resp = make_response(render_template_string(LOGIN_HTML))
    resp.delete_cookie("bot_auth")
    return resp


@app.route("/api/deploy/upload", methods=["POST"])
@requires_auth
def api_deploy_upload():
    """Upload .py files to the bot directory. Backs up existing files first."""
    import subprocess, shutil
    bot_dir = os.environ.get("BOT_DIR", "/opt/15-min-btc-bot")
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

    return jsonify({"uploaded": uploaded, "errors": errors, "backed_up": backed_up})


@app.route("/api/deploy/rollback", methods=["POST"])
@requires_auth
def api_deploy_rollback():
    """Restore backed up files and restart services."""
    import subprocess, shutil
    bot_dir = os.environ.get("BOT_DIR", "/opt/15-min-btc-bot")
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
    for svc in ["kalshi-bot", "kalshi-dashboard"]:
        try:
            subprocess.run(["supervisorctl", "restart", svc],
                           capture_output=True, text=True, timeout=10)
        except Exception:
            pass

    return jsonify({"restored": restored, "errors": errors})


@app.route("/api/deploy/backup_info")
@requires_auth
def api_deploy_backup_info():
    bot_dir = os.environ.get("BOT_DIR", "/opt/15-min-btc-bot")
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
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
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
    const r = await fetch('/api/deploy/rollback', {method:'POST'});
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
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({services:['kalshi-bot','kalshi-dashboard']})});
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
    bot_dir = os.environ.get("BOT_DIR", "/opt/15-min-btc-bot")
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
    """Restart bot and/or dashboard services."""
    import subprocess
    data = request.get_json() or {}
    services = data.get("services", ["kalshi-bot", "kalshi-dashboard"])
    results = {}
    for svc in services:
        try:
            r = subprocess.run(["supervisorctl", "restart", svc],
                               capture_output=True, text=True, timeout=10)
            results[svc] = r.stdout.strip() or r.stderr.strip()
        except Exception as e:
            results[svc] = str(e)
    return jsonify(results)


@app.route("/")
@requires_auth
def index():
    return render_template_string(MAIN_HTML)


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
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: var(--bg); color: var(--text); padding: 12px; padding-top: 0;
         padding-bottom: 110px;
         font-size: 14px; max-width: 600px; margin: 0 auto; }
  #stickyHeader { position: sticky; top: 0; z-index: 50; background: var(--card);
                  border-bottom: 1px solid var(--border); padding: 10px 14px;
                  margin: 0 -12px; /* bleed to edges */
                  box-shadow: 0 4px 12px rgba(0,0,0,0.5); }
  .hdr-row { display: flex; justify-content: space-between; align-items: center; }
  #hdrBankroll:active { background: rgba(255,255,255,0.12); }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
          padding: 14px; margin-bottom: 12px; }
  .card h3 { color: var(--blue); font-size: 13px; text-transform: uppercase;
             letter-spacing: 0.5px; margin-bottom: 10px; }
  .status-bar { display: flex; justify-content: space-between; align-items: center;
                flex-wrap: wrap; gap: 8px; }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
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
  .big-num { font-size: 28px; font-weight: 700; font-family: 'SF Mono', monospace; }
  .pos { color: var(--green); }
  .neg { color: var(--red); }
  .dim { color: var(--dim); font-size: 12px; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .grid3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; }
  .stat { text-align: center; padding: 6px; }
  .stat .label { color: var(--dim); font-size: 11px; text-transform: uppercase; }
  .stat .val { font-size: 18px; font-weight: 600; font-family: monospace; margin-top: 2px; }
  .btn { padding: 10px 16px; border: none; border-radius: 6px; font-size: 14px;
         font-weight: 600; cursor: pointer; width: 100%; margin-top: 6px; }
  .btn-green { background: var(--green); color: #000; }
  .btn-red { background: var(--red); color: #fff; }
  .btn-yellow { background: var(--yellow); color: #000; }
  .btn-blue { background: var(--blue); color: #000; }
  .btn-dim { background: var(--border); color: var(--text); }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .trade-live { border-left: 3px solid var(--green); }
  .input-row { display: flex; gap: 8px; align-items: center; margin-top: 8px; }
  .input-row label { color: var(--dim); font-size: 12px; min-width: 90px; }
  .input-row input, .input-row select { background: var(--bg); border: 1px solid var(--border);
    color: var(--text); padding: 6px 8px; border-radius: 4px; font-size: 16px; flex: 1; }
  .toggle { display: flex; align-items: center; gap: 8px; margin-top: 6px; }
  .toggle input[type=checkbox] { width: 18px; height: 18px; }
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
    background: rgba(0,0,0,0.8); display: none; align-items: flex-start;
    justify-content: center; z-index: 100; overflow-y: auto; -webkit-overflow-scrolling: touch;
    overscroll-behavior: contain; padding: 24px 0; }
  .confirm-box { background: var(--card); border: 1px solid var(--red); border-radius: 10px;
    padding: 24px; max-width: 300px; text-align: center; margin: auto; }
  .modal-panel { background: var(--card); border-radius: 12px; padding: 16px; width: 95%;
    border: 1px solid var(--border); margin: auto; }
  .confirm-btns { display: flex; gap: 8px; margin-top: 16px; }
  .confirm-btns .btn { flex: 1; }
  .chat-chip { background: var(--bg); border: 1px solid var(--border); border-radius: 16px;
    padding: 6px 12px; font-size: 12px; color: var(--blue); cursor: pointer;
    -webkit-tap-highlight-color: transparent; }
  .chat-chip:active { background: rgba(88,166,255,0.1); }
  .chat-msg { margin-bottom:12px; }
  .chat-user { text-align: right; }
  .chat-user > div { display:inline-block; background:var(--blue); color:#000; padding:8px 14px;
    border-radius:18px 18px 4px 18px; max-width:85%; text-align:left; font-size:13px; line-height:1.4; }
  .chat-ai > div { display:inline-block; background:var(--bg); border:1px solid var(--border);
    padding:10px 14px; border-radius:18px 18px 18px 4px; max-width:92%; color:var(--text);
    font-size:13px; line-height:1.6; }
  .chat-ai > div strong { color: var(--blue); }
  .chat-err > div { display:inline-block; background:rgba(248,81,73,0.08); border:1px solid rgba(248,81,73,0.2);
    padding:8px 14px; border-radius:18px 18px 18px 4px; color:var(--red); font-size:13px; }
  .chat-thinking > div { display:inline-block; background:var(--bg); border:1px solid var(--border);
    padding:10px 14px; border-radius:18px 18px 18px 4px; color:var(--dim); font-size:13px; }
  @keyframes chat-dots { 0%,80%,100% { opacity:0.2 } 40% { opacity:1 } }
  .chat-dot { display:inline-block; width:5px; height:5px; border-radius:50%; background:var(--dim);
    margin:0 1px; animation: chat-dots 1.4s infinite; }
  .chat-dot:nth-child(2) { animation-delay: 0.2s; }
  .chat-dot:nth-child(3) { animation-delay: 0.4s; }
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
  .monitor-card { border-left: 3px solid var(--blue); }
  .detail-toggle { color: var(--blue); font-size: 12px; cursor: pointer; margin-top: 8px;
                   display: block; }
  .detail-section { display: none; margin-top: 8px; padding-top: 8px;
                    border-top: 1px solid var(--border); }
  .expo-result { background: var(--bg); border-radius: 4px; padding: 8px; margin-top: 8px;
                 font-family: monospace; font-size: 13px; }
  .regime-detail-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 4px 12px;
    font-size: 12px; color: var(--dim); }
  .regime-detail-grid .rdg-val { color: var(--text); font-weight: 600; }
  .trade-card { background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
                padding: 10px; margin-bottom: 8px; border-left: 3px solid var(--border); }
  .trade-card.tc-win { border-left-color: var(--green); }
  .trade-card.tc-loss { border-left-color: var(--red); }
  .trade-card.tc-skip { border-left-color: var(--dim); }
  .trade-card.tc-cashout { border-left-color: var(--orange); }
  .trade-card.tc-open { border-left-color: var(--blue); }
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
  .tc-tag.tag-cashout { background: #2a2a1a; color: var(--orange); }
  .tc-tag.tag-skip { background: #1a1a2a; color: var(--dim); }
  .tc-tag.tag-yes { background: #1a2a1a; color: var(--green); }
  .tc-tag.tag-no { background: #2a1a1a; color: var(--red); }
  .tc-tag.tag-recovery { background: #2a1a2a; color: #d2a8ff; }
  .tc-tag.tag-open { background: #1a2a3a; color: var(--blue); }
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
  .ctrl-play svg { fill: var(--green); filter: drop-shadow(0 0 6px rgba(63,185,80,0.5)); }
  .ctrl-play:disabled svg { fill: var(--dim); filter: none; }
  .ctrl-stop svg { fill: var(--red); filter: drop-shadow(0 0 6px rgba(248,81,73,0.5)); }
  .ctrl-stop:disabled svg { fill: var(--dim); filter: none; }
  .ctrl-stop.stop-pending svg { fill: var(--dim); filter: none; }

  .tab-bar { position: fixed; bottom: 0; left: 0; right: 0; z-index: 90;
    display: flex; align-items: flex-start; justify-content: space-around;
    padding-top: 8px; padding-bottom: 30px;
    background: var(--card); border-top: 1px solid var(--border); }
  .tab-btn { background: none; border: none; cursor: pointer;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    gap: 3px; padding: 4px 0;
    -webkit-tap-highlight-color: transparent;
    flex: 1; font-size: 10px; color: var(--dim); }
  .tab-btn:active { opacity: 0.7; }
  .tab-btn svg { width: 36px; height: 36px; }
  .ctrl-icon svg { width: 52px; height: 52px; transition: fill 0.2s, filter 0.2s; }
  .buy-side-btn { border: none; border-radius: 8px; padding: 12px 8px; text-align: center;
                  cursor: pointer; transition: filter 0.15s; -webkit-tap-highlight-color: transparent; }
  .buy-side-btn:active { filter: brightness(1.2); }
  .buy-side-btn.btn-disabled { opacity: 0.5; pointer-events: none; }
  .buy-side-btn.btn-disabled .side-yes, .buy-side-btn.btn-disabled .side-no { color: var(--dim); }
  .buy-yes { background: rgba(63,185,80,0.12); border: 1px solid rgba(63,185,80,0.3); }
  .buy-no { background: rgba(248,81,73,0.12); border: 1px solid rgba(248,81,73,0.3); }
  .delete-btn { background: none; border: none; cursor: pointer; padding: 2px;
                opacity: 0.35; transition: opacity 0.15s; }
  .delete-btn:hover { opacity: 1; }
  .delete-btn svg { width: 16px; height: 16px; stroke: var(--dim); }
  .delete-btn:hover svg { stroke: var(--red); }
  .mini-chart { width: 100%; height: 80px; margin: 8px 0; border-radius: 4px;
                background: var(--bg); border: 1px solid var(--border); }
</style>
</head>
<body>

<!-- Sticky Header -->
<div id="stickyHeader">
  <div class="hdr-row">
    <div style="display:flex;align-items:center;gap:6px">
      <span class="status-dot" id="statusDot"></span>
      <strong id="statusText" style="font-size:14px">Loading...</strong>
    </div>
    <div id="hdrBankroll" onclick="openBankrollModal()" style="font-family:monospace;font-size:20px;font-weight:700;cursor:pointer;color:#fff;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);border-radius:6px;padding:6px 16px;-webkit-tap-highlight-color:transparent">—</div>
  </div>
  <div id="statusSub" class="dim" style="margin-top:3px;font-size:12px;line-height:1.4"></div>
</div>
<div style="height:8px"></div>

<!-- Bankroll Modal -->
<div class="confirm-overlay" id="bankrollModal" style="display:none">
  <div class="modal-panel" style="max-width:420px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <h3 style="color:var(--blue);font-size:14px;margin:0">Bankroll</h3>
      <button onclick="closeModal('bankrollModal')" style="background:none;border:none;color:var(--dim);font-size:20px;cursor:pointer;padding:4px"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
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

    <!-- P&L -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px">
      <div style="padding:8px;background:var(--bg);border-radius:6px">
        <div class="dim" style="font-size:10px;margin-bottom:2px">Session P&L</div>
        <div style="font-size:16px;font-weight:600;font-family:monospace" id="bkmSessionPnl">$0.00</div>
        <div class="dim" style="font-size:10px" id="bkmSessionStats">0W–0L</div>
      </div>
      <div style="padding:8px;background:var(--bg);border-radius:6px">
        <div class="dim" style="font-size:10px;margin-bottom:2px">Lifetime P&L</div>
        <div style="font-size:16px;font-weight:600;font-family:monospace" id="bkmLifetimePnl">$0.00</div>
        <div class="dim" style="font-size:10px" id="bkmLifetimeStats">0W–0L</div>
      </div>
    </div>

    <!-- Cycle info -->
    <div style="padding:8px;background:var(--bg);border-radius:6px;margin-bottom:12px" id="bkmCycleInfo"></div>

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

    <!-- Lock/Unlock -->
    <div style="border-top:1px solid var(--border);padding-top:8px">
      <span class="detail-toggle" onclick="toggleDetail('bkmLockSection')">▸ Lock / Unlock Funds</span>
      <div class="detail-section" id="bkmLockSection">
        <div class="dim" style="font-size:11px;margin-bottom:8px">Locked funds excluded from trading. Cycle resets on change.</div>
        <div class="input-row">
          <label>Amount $</label>
          <input type="number" id="lockAmount" min="0" step="10" value="100">
        </div>
        <div style="display:flex;gap:8px;margin-top:6px">
          <button class="btn btn-dim" style="flex:1;margin:0;padding:8px"
                  onclick="lockFunds(parseFloat($('#lockAmount').value))">+ Lock</button>
          <button class="btn btn-dim" style="flex:1;margin:0;padding:8px"
                  onclick="lockFunds(-parseFloat($('#lockAmount').value))">− Unlock</button>
        </div>
        <div class="input-row" style="margin-top:8px">
          <label>Set Total</label>
          <input type="number" id="lockTotal" min="0" step="10">
          <button class="btn btn-dim" style="width:auto;margin:0;padding:6px 12px"
                  onclick="setLockedTotal()">Set</button>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Live Market Monitor (shown when NOT trading) -->
<div class="card monitor-card" id="monitorCard" style="display:none">
  <h3><span class="live-dot"></span> Live Market</h3>
  <div class="grid2">
    <div class="stat">
      <div class="label">Market</div>
      <div class="val" id="monMarket" style="font-size:16px">—</div>
    </div>
    <div class="stat">
      <div class="label">Time Left</div>
      <div class="val" id="monTime" style="font-family:monospace">—</div>
    </div>
  </div>
  <!-- Prices + Buy buttons (always visible) -->
  <div id="monManualMode" class="grid2" style="margin-top:8px">
    <button class="buy-side-btn buy-yes" id="btnBuyYes" onclick="manualBuy('yes')">
      <div class="dim" style="font-size:10px;margin-bottom:2px">YES</div>
      <div class="side-yes" style="font-size:22px;font-family:monospace" id="monYesAsk">—</div>
      <div class="dim" style="font-size:10px;margin-top:2px;font-family:monospace" id="monYesSpread"></div>
    </button>
    <button class="buy-side-btn buy-no" id="btnBuyNo" onclick="manualBuy('no')">
      <div class="dim" style="font-size:10px;margin-bottom:2px">NO</div>
      <div class="side-no" style="font-size:22px;font-family:monospace" id="monNoAsk">—</div>
      <div class="dim" style="font-size:10px;margin-top:2px;font-family:monospace" id="monNoSpread"></div>
    </button>
  </div>
  <!-- Live price chart -->
  <div style="position:relative">
    <canvas id="liveChart" class="mini-chart" width="600" height="80"></canvas>
    <div id="liveChartLabel" style="position:absolute;top:12px;left:8px;font-size:11px;font-family:monospace;color:var(--dim);pointer-events:none;background:rgba(13,17,23,0.8);padding:1px 4px;border-radius:3px"></div>
  </div>
  <!-- Regime -->
  <div style="display:flex;align-items:center;gap:8px;margin-top:10px">
    <span class="regime-tag" id="monRisk">—</span>
    <span style="font-size:13px;font-weight:600" id="monRegimeLabel">—</span>
  </div>
  <div class="regime-detail-grid" id="monRegimeGrid" style="margin-top:6px"></div>
</div>

<!-- Pending Trade (buy order waiting for fill) -->
<div class="card trade-live" id="pendingCard" style="display:none;border-left:3px solid var(--yellow)">
  <h3 style="margin-bottom:0"><span class="live-dot" style="background:var(--yellow)"></span> Pending Buy</h3>
  <div class="grid2" style="margin-top:8px">
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
    <div class="stat">
      <div class="label">Time Left</div>
      <div class="val" id="pendTime" style="font-family:monospace">—</div>
    </div>
  </div>
  <div class="progress-bar" style="margin-top:6px">
    <div class="progress-fill" id="pendProgress" style="width:0%;background:var(--yellow)"></div>
  </div>
  <!-- Pending price chart -->
  <div style="position:relative">
    <canvas id="pendChart" class="mini-chart" width="600" height="80"></canvas>
    <div id="pendChartLabel" style="position:absolute;top:12px;left:8px;font-size:11px;font-family:monospace;color:var(--dim);pointer-events:none;background:rgba(13,17,23,0.8);padding:1px 4px;border-radius:3px"></div>
  </div>
  <div style="margin-top:10px">
    <div class="input-row">
      <label>Preset Sell</label>
      <input type="number" id="pendSellPrice" min="2" max="99" placeholder="e.g. 85" style="width:60px">
      <span class="dim">¢</span>
      <button class="btn btn-dim" style="width:auto;margin:0;padding:6px 10px" onclick="presetSell()">Set</button>
    </div>
    <div class="dim" style="font-size:11px;margin-top:4px" id="pendSellInfo"></div>
  </div>
  <div style="margin-top:10px">
    <button class="btn btn-red" onclick="cancelPending()">Cancel Buy Order</button>
  </div>
</div>

<!-- Active Trade (shown when trading) -->
<div class="card trade-live" id="tradeCard" style="display:none">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <h3 style="margin-bottom:0"><span class="live-dot live-dot-green"></span> Active Trade</h3>
    <label style="font-size:10px;color:var(--dim);display:flex;align-items:center;gap:4px">
      <input type="checkbox" id="fastUpdates" style="width:14px;height:14px" checked> 1s updates
    </label>
  </div>
  <div style="display:flex;align-items:center;gap:8px;margin:8px 0">
    <span class="regime-tag" id="tradeRisk">—</span>
    <span style="font-size:13px;font-weight:600" id="tradeRegimeLabel">—</span>
    <span class="dim" id="tradeRegimeStats"></span>
  </div>
  <!-- Mini price chart -->
  <div style="position:relative">
    <canvas id="priceChart" class="mini-chart" width="600" height="80"></canvas>
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
      <div class="label">Time Left</div>
      <div class="val" id="tradeTime" style="font-family:monospace">—</div>
    </div>
    <div class="stat">
      <div class="label">Cost</div>
      <div class="val" id="tradeCost">—</div>
    </div>
    <div class="stat">
      <div class="label">HWM</div>
      <div class="val" id="tradeHwm">—</div>
    </div>
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
      <div>Cycle Round: <strong id="tdRound">1</strong></div>
      <div>Hole: <strong id="tdHole">$0</strong></div>
      <div>Target: <strong id="tdTarget">$0</strong></div>
      <div>Data Bet: <strong id="tdData">No</strong></div>
    </div>
  </div>

  <!-- Manual trade controls (shown only for manual trades) -->
  <div id="manualControls" style="display:none;margin-top:10px;padding-top:10px;border-top:1px solid var(--border)">
    <div class="dim" style="font-size:11px;margin-bottom:6px">Manual / Ignored — Set exit</div>
    <div class="input-row" style="margin-top:0">
      <label>Sell @</label>
      <input type="number" id="manualSellPrice" min="2" max="99" style="width:60px">
      <span class="dim">¢</span>
      <button class="btn btn-green" style="width:auto;margin:0;padding:6px 12px;font-size:12px"
              onclick="setManualSell()">Set Sell</button>
    </div>
    <button class="btn btn-dim" style="margin-top:6px;font-size:12px;padding:8px" onclick="manualHold()">Hold to Close</button>
  </div>

  <!-- Stopping banner -->
  <div id="stoppingBanner" style="display:none;margin-top:8px;padding:8px;border-radius:6px;background:rgba(248,81,73,0.08);border:1px solid rgba(248,81,73,0.3);text-align:center;font-size:12px;color:var(--red)">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px;margin-right:4px"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>Stopped — trade kept as ignored, sell order active
  </div>

  <!-- Cash out (shown for ALL trades) -->
  <div id="cashOutSection" style="margin-top:12px">
    <button class="btn btn-red" id="cashOutBtn" onclick="showCashOut()">
      <span class="icon-btn">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M15.75 9V5.25A2.25 2.25 0 0 0 13.5 3h-6a2.25 2.25 0 0 0-2.25 2.25v13.5A2.25 2.25 0 0 0 7.5 21h6a2.25 2.25 0 0 0 2.25-2.25V15m3-3h-9m0 0 3-3m-3 3 3 3"/></svg>
        Cash Out
      </span>
    </button>
    <button class="btn btn-dim" id="cancelCashOutBtn" onclick="cancelCashOut()" style="display:none;border-color:var(--orange);color:var(--orange)">
      <span class="icon-btn">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/></svg>
        Cancel Cash Out
      </span>
    </button>
  </div>
  </div>
</div>

<!-- Session Stats -->
<div class="card collapsible">
  <h3 onclick="toggleCard(this)">Session Stats <span class="card-arrow">▾</span></h3>
  <div class="card-subtitle" id="subSessionCycle"></div>
  <div class="card-body">

  <!-- Session stats -->
  <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:6px;margin-bottom:8px">
    <div class="stat"><div class="label">Wins</div><div class="val pos" id="statWins">0</div></div>
    <div class="stat"><div class="label">Losses</div><div class="val neg" id="statLosses">0</div></div>
    <div class="stat"><div class="label">Skips</div><div class="val" id="statSkips">0</div></div>
    <div class="stat"><div class="label">P&L</div><div class="val" id="statSessionPnl">$0</div></div>
  </div>
  <div class="dim" style="font-size:11px;text-align:center;margin-bottom:10px" id="statSessionDetail"></div>

  <!-- Cycle stats -->
  <div style="border-top:1px solid var(--border);padding-top:8px">
  <div class="dim" style="font-size:11px;font-weight:600;margin-bottom:4px">CURRENT CYCLE</div>
  <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:6px;margin-bottom:6px">
    <div class="stat"><div class="label">Round</div><div class="val" id="cycleRound">1</div></div>
    <div class="stat"><div class="label">Streak</div><div class="val" id="cycleStreak">0</div></div>
    <div class="stat"><div class="label">Hole</div><div class="val" id="cycleHole">$0</div></div>
    <div class="stat"><div class="label">Target</div><div class="val" id="cycleTarget">$0</div></div>
  </div>
  <div class="dim" style="font-size:11px;text-align:center" id="cycleDetail"></div>
  </div>

  <!-- Round projections -->
  <span class="detail-toggle" onclick="toggleDetail('projSection')" style="margin-top:6px">▸ Round projections</span>
  <div class="detail-section" id="projSection">
    <table class="proj-table">
      <thead><tr><th>Round</th><th>Bet</th><th>Est. Cost</th><th>Sell @</th><th>Cum. Loss</th></tr></thead>
      <tbody id="projBody"></tbody>
    </table>
  </div>

  </div>
</div>

<!-- Lifetime Stats Modal -->
<div class="confirm-overlay" id="lifetimeModal" style="display:none">
  <div class="modal-panel" style="max-width:480px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <h3 style="color:var(--blue);font-size:14px;margin:0">Lifetime Stats</h3>
      <button onclick="closeModal('lifetimeModal')" style="background:none;border:none;color:var(--dim);font-size:20px;cursor:pointer;padding:4px"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
    </div>
    <div id="lifetimeStats"><div class="dim">Loading...</div></div>
    <div>
      <span class="detail-toggle" onclick="toggleDetail('roundBreakdown')" style="display:block">▸ Round breakdown</span>
      <div class="detail-section" id="roundBreakdown"><div class="dim">No round data yet</div></div>
    </div>
    <div>
      <span class="detail-toggle" onclick="toggleDetail('delayBreakdown')" style="display:block">▸ Entry delay breakdown</span>
      <div class="detail-section" id="delayBreakdown"><div class="dim">No delay data yet</div></div>
    </div>
    <div>
      <span class="detail-toggle" onclick="toggleDetail('stabilityBreakdown')" style="display:block">▸ Price stability breakdown</span>
      <div class="detail-section" id="stabilityBreakdown"><div class="dim">No stability data yet</div></div>
    </div>
    <div>
      <span class="detail-toggle" onclick="toggleDetail('dailyPnl')" style="display:block">▸ Daily P&L</span>
      <div class="detail-section" id="dailyPnl"><div class="dim">No daily data yet</div></div>
    </div>
  </div>
</div>

<!-- Settings Modal -->
<div class="confirm-overlay" id="settingsModal" style="display:none">
  <div class="modal-panel" style="max-width:420px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <h3 style="color:var(--blue);font-size:14px;margin:0">Settings</h3>
      <button onclick="closeModal('settingsModal')" style="background:none;border:none;color:var(--dim);font-size:20px;cursor:pointer;padding:4px"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
    </div>

    <!-- Trade Mode -->
    <div class="dim" style="font-size:11px;font-weight:600;margin-bottom:4px">TRADE MODE</div>
    <div class="input-row">
      <label>Mode</label>
      <select id="tradeMode">
        <option value="continuous">Continuous</option>
        <option value="single">Single Trade</option>
        <option value="count">N Trades</option>
        <option value="cycle">One Cycle</option>
      </select>
      <input type="number" id="tradeCount" value="5" min="1" max="999"
             style="width:60px;display:none"
             onchange="saveSetting('trade_count',parseInt(this.value))">
    </div>

    <!-- Bet Settings -->
    <div style="border-top:1px solid var(--border);padding-top:10px;margin-top:10px">
    <div class="dim" style="font-size:11px;font-weight:600;margin-bottom:4px">BET SETTINGS</div>
    <div class="input-row">
      <label>Bet Mode</label>
      <select id="betMode" onchange="saveSetting('bet_mode',this.value);calcExposure()">
        <option value="flat">Flat $</option>
        <option value="percent">% Bankroll</option>
      </select>
    </div>
    <div class="input-row">
      <label>Bet Size</label>
      <input type="number" id="betSize" step="1" min="1"
             onchange="saveSetting('bet_size',parseFloat(this.value));calcExposure()">
    </div>
    <div class="input-row">
      <label>Max Losses</label>
      <input type="number" id="maxLosses" min="1" max="10"
             onchange="saveSetting('max_losses',parseInt(this.value));calcExposure()">
    </div>
    <div class="input-row">
      <label>Entry Range</label>
      <input type="number" id="entryPriceMin" min="1" max="50" style="width:55px"
             onchange="saveSetting('entry_price_min_c',parseInt(this.value))">
      <span class="dim">to</span>
      <input type="number" id="entryPriceMax" min="1" max="50" style="width:55px"
             onchange="saveSetting('entry_price_max_c',parseInt(this.value))">
      <span class="dim">cents</span>
    </div>
    <div class="input-row">
      <label>Entry Delay</label>
      <input type="number" id="entryDelay" min="0" max="12" value="0"
             onchange="saveSetting('entry_delay_minutes',parseInt(this.value))">
      <span class="dim">min (0 = ASAP)</span>
    </div>
    <div class="input-row">
      <label>Cooldown</label>
      <input type="number" id="cooldownML" min="0" max="20" value="0"
             onchange="saveSetting('cooldown_after_max_loss',parseInt(this.value))">
      <span class="dim">markets after max loss</span>
    </div>
    </div>

    <!-- Exposure Calculator -->
    <div style="border-top:1px solid var(--border);padding-top:10px;margin-top:10px">
    <span class="detail-toggle" onclick="toggleDetail('expoSection')">▸ Exposure Calculator</span>
    <div class="detail-section" id="expoSection">
      <div class="input-row">
        <label>Max Risk %</label>
        <input type="number" id="maxRiskPct" min="1" max="100" value="50" step="5"
               onchange="calcExposure()">
        <span class="dim">of bankroll</span>
      </div>
      <div class="expo-result" id="expoResult">—</div>
    </div>
    </div>

    <!-- Bankroll Guards -->
    <div style="border-top:1px solid var(--border);padding-top:10px;margin-top:10px">
    <span class="detail-toggle" onclick="toggleDetail('guardsSection')">▸ Bankroll Guards</span>
    <div class="detail-section" id="guardsSection">
      <div class="input-row">
        <label>Min Bankroll</label>
        <input type="number" id="bankrollMin" min="0" step="50"
               onchange="saveSetting('bankroll_min',parseFloat(this.value)||0)">
        <span class="dim">$ (0 = off)</span>
      </div>
      <div class="input-row">
        <label>Max Bankroll</label>
        <input type="number" id="bankrollMax" min="0" step="50"
               onchange="saveSetting('bankroll_max',parseFloat(this.value)||0)">
        <span class="dim">$ (0 = off)</span>
      </div>
      <div class="input-row">
        <label>Session Target</label>
        <input type="number" id="sessionTarget" min="0" step="10"
               onchange="saveSetting('session_profit_target',parseFloat(this.value)||0)">
        <span class="dim">$ profit to stop (0 = off)</span>
      </div>
    </div>
    </div>

    <!-- Auto-Lock -->
    <div style="border-top:1px solid var(--border);padding-top:10px;margin-top:10px">
    <span class="detail-toggle" onclick="toggleDetail('autoLockSection')">▸ Auto-Lock Profits</span>
    <div class="detail-section" id="autoLockSection">
      <div class="toggle">
        <input type="checkbox" id="autoLockEnabled"
               onchange="saveSetting('auto_lock_enabled',this.checked)">
        <label for="autoLockEnabled" class="dim">Enable auto-lock</label>
      </div>
      <div class="input-row">
        <label>When eff. ≥</label>
        <input type="number" id="autoLockThreshold" min="0" step="100"
               onchange="saveSetting('auto_lock_threshold',parseFloat(this.value)||0)">
        <span class="dim">$</span>
      </div>
      <div class="input-row">
        <label>Lock amount</label>
        <input type="number" id="autoLockAmount" min="0" step="50"
               onchange="saveSetting('auto_lock_amount',parseFloat(this.value)||0)">
        <span class="dim">$</span>
      </div>
    </div>
    </div>

    <!-- Risk & Regime Toggles -->
    <div style="border-top:1px solid var(--border);padding-top:10px;margin-top:10px">
    <div class="dim" style="font-size:11px;font-weight:600;margin-bottom:4px">RISK & REGIME</div>
    <div class="toggle">
      <input type="checkbox" id="ignoreMode"
             onchange="saveSetting('ignore_mode',this.checked)">
      <label for="ignoreMode" class="dim" style="color:var(--orange)">
        Ignore Mode — trades won't count in stats (resets cycle)
      </label>
    </div>
    <div class="toggle">
      <input type="checkbox" id="skipUnknown"
             onchange="saveSetting('skip_unknown_regimes',this.checked)">
      <label for="skipUnknown" class="dim">Skip unknown regimes (else $1 data bet)</label>
    </div>
    <div class="toggle">
      <input type="checkbox" id="skipHigh"
             onchange="saveSetting('skip_high_risk',this.checked)">
      <label for="skipHigh" class="dim">Skip high-risk regimes</label>
    </div>
    <div class="toggle">
      <input type="checkbox" id="skipTerrible" checked
             onchange="saveSetting('skip_terrible',this.checked)">
      <label for="skipTerrible" class="dim">Skip extreme risk regimes</label>
    </div>
    <div class="toggle">
      <input type="checkbox" id="skipModerate"
             onchange="saveSetting('skip_moderate',this.checked)">
      <label for="skipModerate" class="dim">Skip moderate-risk regimes</label>
    </div>
    <div class="toggle">
      <input type="checkbox" id="dataBetFullSize"
             onchange="saveSetting('data_bet_full_size',this.checked)">
      <label for="dataBetFullSize" class="dim" style="color:var(--yellow)">
        Full-size data bets (enables martingale in unknown regimes)
      </label>
    </div>
    <div class="toggle">
      <input type="checkbox" id="dataBetOnSkip"
             onchange="saveSetting('data_bet_on_skip',this.checked)">
      <label for="dataBetOnSkip" class="dim">
        Data bet instead of skip (collect data for skipped regimes)
      </label>
    </div>
    <div class="toggle">
      <input type="checkbox" id="disableRoundLimits"
             onchange="saveSetting('disable_round_limits',this.checked)">
      <label for="disableRoundLimits" class="dim" style="color:var(--orange)">
        Disable round-specific risk limits
      </label>
    </div>
    <div class="input-row" style="margin-top:8px">
      <label>Sell Override</label>
      <input type="number" id="customSellPrice" min="0" max="99" value="0" style="width:60px"
             onchange="saveSetting('custom_sell_price_c',parseInt(this.value))">
      <span class="dim">¢ (0 = auto)</span>
    </div>
    </div>

    <!-- Notifications -->
    <div style="border-top:1px solid var(--border);padding-top:10px;margin-top:10px">
    <div class="dim" style="font-size:11px;font-weight:600;margin-bottom:4px">NOTIFICATIONS</div>
    <div id="pushStatus" class="dim" style="margin-bottom:6px">Checking...</div>
    <button class="btn btn-blue" id="pushToggleBtn" onclick="togglePush()" style="display:none;margin-bottom:8px">
      Enable Notifications
    </button>
    <div class="dim" style="font-size:11px;margin-bottom:4px">Notify me for:</div>
    <div class="toggle"><input type="checkbox" id="notifyWins" checked onchange="saveSetting('push_notify_wins',this.checked)"><label for="notifyWins" class="dim">Wins</label></div>
    <div class="toggle"><input type="checkbox" id="notifyLosses" checked onchange="saveSetting('push_notify_losses',this.checked)"><label for="notifyLosses" class="dim">Losses</label></div>
    <div class="toggle"><input type="checkbox" id="notifyErrors" checked onchange="saveSetting('push_notify_errors',this.checked)"><label for="notifyErrors" class="dim">Errors & stops</label></div>
    <div class="toggle"><input type="checkbox" id="notifyBuys" onchange="saveSetting('push_notify_buys',this.checked)"><label for="notifyBuys" class="dim">Buys</label></div>
    <div class="toggle"><input type="checkbox" id="notifySkips" onchange="saveSetting('push_notify_skips',this.checked)"><label for="notifySkips" class="dim">Skips</label></div>
    <div class="toggle"><input type="checkbox" id="notifyCycles" onchange="saveSetting('push_notify_cycles',this.checked)" checked><label for="notifyCycles" class="dim">Cycle complete</label></div>
    <div class="input-row" style="margin-top:6px">
      <label>Quiet Hours</label>
      <input type="number" id="quietStart" min="0" max="23" value="0" style="width:50px"
             onchange="saveSetting('push_quiet_start',parseInt(this.value))">
      <span class="dim">to</span>
      <input type="number" id="quietEnd" min="0" max="23" value="0" style="width:50px"
             onchange="saveSetting('push_quiet_end',parseInt(this.value))">
      <span class="dim">CT</span>
    </div>
    </div>

    <!-- Links -->
    <div style="border-top:1px solid var(--border);padding-top:10px;margin-top:10px;text-align:center">
      <a href="/logs" id="logsLink" onclick="this.textContent='Loading logs...'">View Full Logs →</a>
    </div>

    <!-- Deploy -->
    <div style="border-top:1px solid var(--border);padding-top:10px;margin-top:10px">
    <div class="dim" style="font-size:11px;font-weight:600;margin-bottom:8px">DEPLOY</div>
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
      <button class="btn btn-dim" id="deployRestartBtn" onclick="doRestart()" style="flex:1">Restart Services</button>
    </div>
    <div id="deployStatus" style="margin-top:6px;font-size:11px"></div>
    <div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px">
      <span id="deployBackupInfo" class="dim" style="font-size:10px"></span>
      <a href="/rollback" style="color:var(--orange);font-size:10px">Rollback →</a>
    </div>
    </div>

    <div style="border-top:1px solid var(--border);padding-top:10px;margin-top:10px;text-align:center">
      <button onclick="doLogout()" style="background:none;border:1px solid rgba(248,81,73,0.3);border-radius:6px;padding:8px 24px;color:var(--red);cursor:pointer;font-size:13px;-webkit-tap-highlight-color:transparent">Log Out</button>
    </div>
  </div>
</div>

<!-- Recent Trades Modal -->
<div class="confirm-overlay" id="tradesModal" style="display:none">
  <div class="modal-panel" style="max-width:560px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <h3 style="color:var(--blue);font-size:14px;margin:0">Recent Trades</h3>
      <button onclick="closeModal('tradesModal')" style="background:none;border:none;color:var(--dim);font-size:20px;cursor:pointer;padding:4px"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
    </div>
    <div class="filter-chips" id="tradeFilters">
      <button class="chip active" data-filter="all" onclick="setTradeFilter('all',this)">All</button>
      <button class="chip" data-filter="win" onclick="setTradeFilter('win',this)">Wins</button>
      <button class="chip" data-filter="loss" onclick="setTradeFilter('loss',this)">Losses</button>
      <button class="chip" data-filter="cashed_out" onclick="setTradeFilter('cashed_out',this)">Cashouts</button>
      <button class="chip" data-filter="skipped" onclick="setTradeFilter('skipped',this)">Skips</button>
      <button class="chip" data-filter="data" onclick="setTradeFilter('data',this)">Data Bets</button>
      <button class="chip" data-filter="ignored" onclick="setTradeFilter('ignored',this)">Ignored</button>
      <button class="chip" data-filter="recovery" onclick="setTradeFilter('recovery',this)">Recovery</button>
      <button class="chip" data-filter="open" onclick="setTradeFilter('open',this)">Open</button>
      <button class="chip" data-filter="yes" onclick="setTradeFilter('yes',this)">YES Bets</button>
      <button class="chip" data-filter="no" onclick="setTradeFilter('no',this)">NO Bets</button>
    </div>
    <div id="tradeList"></div>
  </div>
</div>

<!-- Regime Risk Levels -->
<div class="card collapsible">
  <h3 onclick="toggleCard(this)">Regime Risk Levels <span class="card-arrow">▾</span></h3>
  <div class="card-subtitle" id="subRegimes"></div>
  <div class="card-body">
  <div class="filter-chips" id="regimeFilters">
    <button class="chip active" data-filter="all" onclick="setRegimeFilter('all',this)">All</button>
    <button class="chip" data-filter="low" onclick="setRegimeFilter('low',this)">Low</button>
    <button class="chip" data-filter="moderate" onclick="setRegimeFilter('moderate',this)">Moderate</button>
    <button class="chip" data-filter="high" onclick="setRegimeFilter('high',this)">High</button>
    <button class="chip" data-filter="terrible" onclick="setRegimeFilter('terrible',this)">Extreme</button>
    <button class="chip" data-filter="unknown" onclick="setRegimeFilter('unknown',this)">Unknown</button>
  </div>
  <div id="regimeList"></div>
  </div>
</div>

<!-- Regime Detail Modal -->
<div class="confirm-overlay" id="regimeDetailOverlay" style="display:none">
  <div class="modal-panel" style="max-width:480px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <h3 style="color:var(--blue);font-size:14px;margin:0" id="regimeDetailTitle">Regime</h3>
      <button onclick="closeModal('regimeDetailOverlay')" style="background:none;border:none;color:var(--dim);font-size:20px;cursor:pointer;padding:4px"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
    </div>
    <div id="regimeDetailContent"></div>
  </div>
</div>

<!-- Regime Worker Status -->
<div class="card collapsible">
  <h3 onclick="toggleCard(this)">Regime Engine <span class="card-arrow">▾</span></h3>
  <div class="card-subtitle" id="subEngine"></div>
  <div class="card-body">
  <div id="regimeWorkerStatus">
    <div class="dim">Loading...</div>
  </div>
  </div>
</div>

<!-- Cash Out Confirmation -->
<div class="confirm-overlay" id="cashOutOverlay">
  <div class="confirm-box">
    <h3><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--red)" stroke-width="2" style="vertical-align:-3px;margin-right:4px"><path d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/></svg>Cash Out?</h3>
    <p>This will aggressively sell all contracts at the best available price.</p>
    <p class="dim" style="margin-top:8px">This trade will not count as a martingale round.</p>
    <div class="confirm-btns">
      <button class="btn btn-dim" onclick="hideCashOut()">Cancel</button>
      <button class="btn btn-red" onclick="confirmCashOut()">CASH OUT</button>
    </div>
  </div>
</div>

<!-- Trade Detail Popup -->
<div class="confirm-overlay" id="tradeDetailOverlay" style="display:none">
  <div class="modal-panel" style="max-width:560px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <h3 style="color:var(--blue);font-size:14px;margin:0">Trade Detail</h3>
      <button onclick="closeModal('tradeDetailOverlay')" style="background:none;border:none;color:var(--dim);font-size:20px;cursor:pointer;padding:4px"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
    </div>
    <div id="tradeDetailContent"></div>
    <div style="position:relative">
      <canvas id="tradeDetailChart" style="width:100%;height:100px;border-radius:4px;background:var(--bg);border:1px solid var(--border);margin-top:8px"></canvas>
      <div id="tradeDetailChartLabel" style="position:absolute;top:12px;left:8px;font-size:11px;font-family:monospace;color:var(--dim);pointer-events:none;background:rgba(13,17,23,0.8);padding:1px 4px;border-radius:3px"></div>
    </div>
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

<!-- Manual Buy Confirmation -->
<div class="confirm-overlay" id="manualBuyOverlay" style="display:none">
  <div class="confirm-box" style="border-color:var(--blue);max-width:340px;text-align:left">
    <h3 style="color:var(--blue);text-align:center">Manual Buy</h3>
    <div id="manualBuyDetails" style="font-size:13px;margin:10px 0"></div>
    <div style="background:rgba(210,153,34,0.08);border:1px solid rgba(210,153,34,0.2);border-radius:6px;padding:8px;font-size:11px;color:var(--yellow);margin:10px 0">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-1px;margin-right:3px"><path d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/></svg>
      This will: stop auto-trading, reset the martingale cycle, and the trade will NOT count towards data or stats.
    </div>
    <div class="confirm-btns" style="justify-content:center">
      <button class="btn btn-dim" onclick="closeModal('manualBuyOverlay')">Cancel</button>
      <button class="btn btn-blue" id="confirmManualBuyBtn" onclick="confirmManualBuy()">Confirm Buy</button>
    </div>
  </div>
</div>

<!-- Stop Confirmation -->
<div class="confirm-overlay" id="stopOverlay" style="display:none">
  <div class="confirm-box" style="border-color:var(--red);max-width:340px;text-align:left">
    <h3 style="color:var(--red);text-align:center">Stop Bot?</h3>
    <div id="stopDetails" style="font-size:13px;margin:10px 0;color:var(--dim)"></div>
    <div style="display:flex;flex-direction:column;gap:8px;margin-top:12px">
      <button class="btn btn-red" onclick="confirmStop()" style="width:100%">Stop Now</button>
      <button class="btn btn-dim" id="stopAfterCycleBtn" onclick="stopAfterCycle()" style="width:100%;border-color:var(--orange);color:var(--orange)">Stop After This Cycle</button>
      <button class="btn btn-dim" onclick="closeModal('stopOverlay')" style="width:100%">Keep Running</button>
    </div>
  </div>
</div>

<!-- AI Chat Modal -->
<div class="confirm-overlay" id="chatModal" style="display:none;overflow:hidden;padding:0;overscroll-behavior:none">
  <div style="display:flex;flex-direction:column;width:100%;max-width:560px;margin:0 auto;height:100%;overflow:hidden">
    <!-- Header -->
    <div style="flex-shrink:0;padding:calc(10px + env(safe-area-inset-top, 0px)) 16px 10px;background:var(--card);border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center">
      <div style="display:flex;align-items:center;gap:8px">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="var(--blue)" stroke="none"><path d="M4.913 2.658c2.075-.27 4.19-.408 6.337-.408 2.147 0 4.262.139 6.337.408 1.922.25 3.291 1.861 3.405 3.727a4.403 4.403 0 0 0-1.032-.211 50.89 50.89 0 0 0-8.42 0c-2.358.196-4.04 2.19-4.04 4.434v4.286a4.47 4.47 0 0 0 2.433 3.984L7.28 21.53A.75.75 0 0 1 6 20.97v-1.95a49.99 49.99 0 0 1-1.087-.128C2.905 18.636 1.5 17.09 1.5 15.27V5.885c0-1.866 1.37-3.477 3.413-3.227ZM15.75 7.5c-1.376 0-2.739.057-4.086.169C10.124 7.797 9 9.103 9 10.609v4.285c0 1.507 1.128 2.814 2.67 2.94 1.243.102 2.5.157 3.768.165l2.782 2.781a.75.75 0 0 0 1.28-.53v-2.39l.33-.026c1.542-.125 2.67-1.433 2.67-2.94v-4.286c0-1.505-1.125-2.811-2.664-2.94A49.392 49.392 0 0 0 15.75 7.5Z"/></svg>
        <span style="font-size:15px;font-weight:600;color:var(--text)">Ask AI</span>
      </div>
      <div style="display:flex;align-items:center;gap:8px">
        <button onclick="clearChat()" style="background:none;border:none;color:var(--dim);cursor:pointer;padding:4px;font-size:11px;-webkit-tap-highlight-color:transparent">Clear</button>
        <button onclick="closeModal('chatModal')" style="background:none;border:none;color:var(--dim);cursor:pointer;padding:4px;-webkit-tap-highlight-color:transparent"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
      </div>
    </div>
    <!-- Messages -->
    <div id="chatMessages" style="flex:1;overflow-y:auto;padding:16px;overscroll-behavior:contain;-webkit-overflow-scrolling:touch">
      <div id="chatWelcome" style="text-align:center;padding:30px 10px">
        <svg width="40" height="40" viewBox="0 0 24 24" fill="var(--border)" stroke="none" style="margin-bottom:12px"><path d="M4.913 2.658c2.075-.27 4.19-.408 6.337-.408 2.147 0 4.262.139 6.337.408 1.922.25 3.291 1.861 3.405 3.727a4.403 4.403 0 0 0-1.032-.211 50.89 50.89 0 0 0-8.42 0c-2.358.196-4.04 2.19-4.04 4.434v4.286a4.47 4.47 0 0 0 2.433 3.984L7.28 21.53A.75.75 0 0 1 6 20.97v-1.95a49.99 49.99 0 0 1-1.087-.128C2.905 18.636 1.5 17.09 1.5 15.27V5.885c0-1.866 1.37-3.477 3.413-3.227ZM15.75 7.5c-1.376 0-2.739.057-4.086.169C10.124 7.797 9 9.103 9 10.609v4.285c0 1.507 1.128 2.814 2.67 2.94 1.243.102 2.5.157 3.768.165l2.782 2.781a.75.75 0 0 0 1.28-.53v-2.39l.33-.026c1.542-.125 2.67-1.433 2.67-2.94v-4.286c0-1.505-1.125-2.811-2.664-2.94A49.392 49.392 0 0 0 15.75 7.5Z"/></svg>
        <div style="color:var(--text);font-size:14px;font-weight:600;margin-bottom:6px">Ask anything about your bot</div>
        <div class="dim" style="font-size:12px;line-height:1.5">Trades, regimes, strategy, settings — I have access to all your live data.</div>
        <div id="chatSuggestions" style="display:flex;flex-wrap:wrap;gap:6px;justify-content:center;margin-top:16px">
          <button class="chat-chip" onclick="askSuggestion(this)">What's my best regime?</button>
          <button class="chat-chip" onclick="askSuggestion(this)">Summarize today's performance</button>
          <button class="chat-chip" onclick="askSuggestion(this)">Am I profitable overall?</button>
          <button class="chat-chip" onclick="askSuggestion(this)">Which regimes should I skip?</button>
        </div>
      </div>
    </div>
    <!-- Input -->
    <div style="flex-shrink:0;padding:10px 16px calc(14px + env(safe-area-inset-bottom, 0px));background:var(--card);border-top:1px solid var(--border)">
      <div style="display:flex;gap:8px;align-items:flex-end">
        <input type="text" id="chatInput" placeholder="Ask a question..." style="flex:1;background:var(--bg);border:1px solid var(--border);border-radius:20px;padding:10px 16px;color:var(--text);font-size:14px;outline:none" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendChat()}">
        <button onclick="sendChat()" id="chatSendBtn" style="background:var(--blue);border:none;border-radius:50%;width:38px;height:38px;display:flex;align-items:center;justify-content:center;cursor:pointer;flex-shrink:0;-webkit-tap-highlight-color:transparent">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#000" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2L11 13"/><path d="M22 2L15 22L11 13L2 9L22 2Z"/></svg>
        </button>
      </div>
    </div>
  </div>
</div>

<!-- Bottom Tab Bar -->
<div class="tab-bar">
  <button class="tab-btn" onclick="openLifetimeModal()">
    <svg viewBox="0 0 24 24" fill="white" stroke="none"><path d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 0 1 3 19.875v-6.75ZM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 0 1-1.125-1.125V8.625ZM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 0 1-1.125-1.125V4.125Z"/></svg>
    <span>Stats</span>
  </button>
  <button class="tab-btn" onclick="openTradesModal()">
    <svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M3 7.5 7.5 3m0 0L12 7.5M7.5 3v13.5m13.5 0L16.5 21m0 0L12 16.5m4.5 4.5V7.5"/></svg>
    <span>Trades</span>
  </button>
  <button class="tab-btn" onclick="toggleBot()">
    <div class="ctrl-icon ctrl-play" id="btnMain">
      <svg id="btnMainSvg" viewBox="0 0 24 24"><path d="M5.25 5.653c0-.856.917-1.398 1.667-.986l11.54 6.347a1.125 1.125 0 0 1 0 1.972l-11.54 6.347a1.125 1.125 0 0 1-1.667-.986V5.653Z"/></svg>
    </div>
  </button>
  <button class="tab-btn" onclick="openChatModal()">
    <svg viewBox="0 0 24 24" fill="white" stroke="none"><path d="M4.913 2.658c2.075-.27 4.19-.408 6.337-.408 2.147 0 4.262.139 6.337.408 1.922.25 3.291 1.861 3.405 3.727a4.403 4.403 0 0 0-1.032-.211 50.89 50.89 0 0 0-8.42 0c-2.358.196-4.04 2.19-4.04 4.434v4.286a4.47 4.47 0 0 0 2.433 3.984L7.28 21.53A.75.75 0 0 1 6 20.97v-1.95a49.99 49.99 0 0 1-1.087-.128C2.905 18.636 1.5 17.09 1.5 15.27V5.885c0-1.866 1.37-3.477 3.413-3.227ZM15.75 7.5c-1.376 0-2.739.057-4.086.169C10.124 7.797 9 9.103 9 10.609v4.285c0 1.507 1.128 2.814 2.67 2.94 1.243.102 2.5.157 3.768.165l2.782 2.781a.75.75 0 0 0 1.28-.53v-2.39l.33-.026c1.542-.125 2.67-1.433 2.67-2.94v-4.286c0-1.505-1.125-2.811-2.664-2.94A49.392 49.392 0 0 0 15.75 7.5Z"/></svg>
    <span>AI</span>
  </button>
  <button class="tab-btn" onclick="openModal('settingsModal');loadConfig();loadBackupInfo()">
    <svg viewBox="0 0 24 24" fill="white" stroke="white" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.325.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 0 1 1.37.49l1.296 2.247a1.125 1.125 0 0 1-.26 1.431l-1.003.827c-.293.241-.438.613-.43.992a7.723 7.723 0 0 1 0 .255c-.008.378.137.75.43.991l1.004.827c.424.35.534.955.26 1.43l-1.298 2.247a1.125 1.125 0 0 1-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.47 6.47 0 0 1-.22.128c-.331.183-.581.495-.644.869l-.213 1.281c-.09.543-.56.94-1.11.94h-2.594c-.55 0-1.019-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 0 1-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 0 1-1.369-.49l-1.297-2.247a1.125 1.125 0 0 1 .26-1.431l1.004-.827c.292-.24.437-.613.43-.991a6.932 6.932 0 0 1 0-.255c.007-.38-.138-.751-.43-.992l-1.004-.827a1.125 1.125 0 0 1-.26-1.43l1.297-2.247a1.125 1.125 0 0 1 1.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.086.22-.128.332-.183.582-.495.644-.869l.214-1.28Z"/><path fill="var(--card)" stroke="white" stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z"/></svg>
    <span>Settings</span>
  </button>
</div>

<script>
const $ = s => document.querySelector(s);

let _modalScrollY = 0;
let _modalCount = 0;

function openModal(id) {
  const el = document.getElementById(id);
  el.style.display = 'flex';
  el.scrollTop = 0;
  _modalCount++;
  if (_modalCount === 1) {
    _modalScrollY = window.scrollY;
    document.body.style.position = 'fixed';
    document.body.style.top = `-${_modalScrollY}px`;
    document.body.style.left = '0';
    document.body.style.right = '0';
    document.body.style.width = '100%';
    document.body.style.overflow = 'hidden';
    // Hide tab bar behind modal
    const tb = document.querySelector('.tab-bar');
    if (tb) tb.style.zIndex = '0';
  }
}
function closeModal(id) {
  document.getElementById(id).style.display = 'none';
  _modalCount = Math.max(0, _modalCount - 1);
  if (_modalCount === 0) {
    document.body.style.position = '';
    document.body.style.top = '';
    document.body.style.left = '';
    document.body.style.right = '';
    document.body.style.width = '';
    document.body.style.overflow = '';
    window.scrollTo(0, _modalScrollY);
    // Restore tab bar
    const tb = document.querySelector('.tab-bar');
    if (tb) tb.style.zIndex = '90';
  }
}

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
  bar.style.cssText = 'position:fixed;top:0;left:0;right:0;height:3px;background:var(--blue);' +
    'z-index:999;transform:scaleX(0);transform-origin:left;transition:transform 0.15s;display:none';
  document.body.appendChild(bar);

  function isModalOpen() {
    return _modalCount > 0;
  }

  document.addEventListener('touchstart', e => {
    if (window.scrollY <= 0 && e.touches.length === 1 && !isModalOpen() && !_chartTouchActive) {
      startY = e.touches[0].clientY;
      pulling = true;
      triggered = false;
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
    const dy = Math.max(0, (e.changedTouches[0] || {}).clientY - startY);
    if (dy >= threshold && window.scrollY <= 0) {
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
let currentLocked = 0;  // From config, not from input
let lastStateData = {};  // Store for ticking countdown
let chartData = [];      // [{ts: Date, bid: number}] for timeline chart
let chartTradeId = null; // Reset chart on new trade
let chartStartMs = 0;    // Market open time (ms)
let chartEndMs = 0;      // Market close time (ms)
let cachedLifetimePnl = 0;

function openBankrollModal() {
  const s = _uiState;
  const rawBal = (s.bankroll_cents || 0) / 100;
  const effBal = Math.max(rawBal - currentLocked, 0);
  const at = s.active_trade;
  const inTrade = at ? (at.actual_cost || 0) : 0;

  // Main balances
  const effEl = $('#bkmEffective');
  effEl.textContent = '$' + effBal.toFixed(2);
  effEl.style.color = '#fff';
  $('#bkmTotal').textContent = '$' + rawBal.toFixed(2);
  $('#bkmLocked').textContent = '$' + currentLocked.toFixed(2);
  $('#bkmLocked').style.color = currentLocked > 0 ? 'var(--yellow)' : 'var(--dim)';
  $('#bkmInTrade').textContent = inTrade > 0 ? '$' + inTrade.toFixed(2) : '—';
  $('#bkmInTrade').style.color = inTrade > 0 ? 'var(--blue)' : 'var(--dim)';

  // Session P&L
  const spnl = s.session_pnl || 0;
  const sw = s.session_wins || 0, sl = s.session_losses || 0;
  const sTotal = sw + sl;
  const sWr = sTotal > 0 ? (sw / sTotal * 100).toFixed(0) : '—';
  const bkmSp = $('#bkmSessionPnl');
  bkmSp.textContent = (spnl >= 0 ? '+' : '') + '$' + spnl.toFixed(2);
  bkmSp.className = spnl > 0 ? 'pos' : spnl < 0 ? 'neg' : '';
  $('#bkmSessionStats').textContent = `${sw}W–${sl}L · ${sTotal} trades · ${sWr}% WR`;

  // Lifetime P&L
  const lpnl = cachedLifetimePnl;
  const bkmLp = $('#bkmLifetimePnl');
  bkmLp.textContent = (lpnl >= 0 ? '+' : '') + '$' + lpnl.toFixed(2);
  bkmLp.className = lpnl > 0 ? 'pos' : lpnl < 0 ? 'neg' : '';
  const lw = s.lifetime_wins || 0, ll = s.lifetime_losses || 0;
  const lTotal = lw + ll;
  const lWr = lTotal > 0 ? (lw / lTotal * 100).toFixed(0) : '—';
  $('#bkmLifetimeStats').textContent = `${lw}W–${ll}L · ${lTotal} trades · ${lWr}% WR`;

  // Cycle info
  const cr = s.cycle_round || 1;
  const ch = s.cycle_hole || 0;
  const ct = s.cycle_profit_target || 0;
  const cs = s.cycle_loss_streak || 0;
  let cycleHtml = `<div class="dim" style="font-size:10px;margin-bottom:4px;font-weight:600">CURRENT CYCLE</div>`;
  cycleHtml += `<div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;font-size:12px">`;
  cycleHtml += `<div>Round: <strong>R${cr}</strong></div>`;
  cycleHtml += `<div>Streak: <strong>${cs}</strong></div>`;
  cycleHtml += `<div>Hole: <strong class="${ch > 0 ? 'neg' : ''}">$${ch.toFixed(2)}</strong></div>`;
  cycleHtml += `<div>Target: <strong>$${ct.toFixed(2)}</strong></div>`;
  cycleHtml += `</div>`;
  const proj = s.cycle_projection;
  if (proj && proj.rounds) {
    const nextR = proj.rounds.find(r => r.round === cr);
    cycleHtml += `<div style="margin-top:6px;font-size:11px;color:var(--dim)">`;
    if (nextR) cycleHtml += `Next bet: <strong>$${nextR.bet.toFixed(2)}</strong>`;
    if (proj.total_max_exposure) cycleHtml += ` · Max exposure: $${proj.total_max_exposure.toFixed(2)}`;
    cycleHtml += `</div>`;
  }
  // Cooldown
  const cd = s.cooldown_remaining || 0;
  if (cd > 0) cycleHtml += `<div style="margin-top:4px;color:var(--orange);font-size:12px">Cooldown: ${cd} market(s)</div>`;
  $('#bkmCycleInfo').innerHTML = cycleHtml;

  // Warning
  const warnEl = $('#bkmWarning');
  if (proj && proj.warning) {
    warnEl.textContent = proj.warning;
    warnEl.style.display = '';
  } else {
    warnEl.style.display = 'none';
  }

  // Lock total
  if (currentLocked > 0) $('#lockTotal').value = currentLocked.toFixed(2);

  // Show modal then load charts
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
    // Show "Resolving..." then poll aggressively for result
    patchUI({status_detail: 'Market closed — resolving trade...'});
    // Rapid polls to catch the resolution
    pollState();
    setTimeout(pollState, 1000);
    setTimeout(pollState, 2500);
    setTimeout(pollState, 5000);
  } else if (which === 'monitor') {
    // Live market ended → transition to "between markets"
    if (autoOn) {
      patchUI({status_detail: 'Market closed — waiting for next market...'});
    } else {
      // Show between-markets state
      $('#monMarket').textContent = '—';
      $('#monTime').textContent = '—';
      $('#monYesSpread').textContent = '';
      $('#monNoSpread').textContent = '';
    }
    // Clear stale close time so we don't fire again
    lastStateData._monCloseTime = null;
    // Poll to pick up new market
    pollState();
    setTimeout(pollState, 2000);
    setTimeout(pollState, 5000);
    setTimeout(pollState, 10000);
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
  for (let i = 0; i < data.length; i++) {
    const x = toX(data[i].ts);
    const y = toY(data[i].bid);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.stroke();

  // Fill under
  const lastX = toX(data[data.length - 1].ts);
  const lastY = toY(lastBid);
  ctx.lineTo(lastX, H - pad.b);
  ctx.lineTo(toX(data[0].ts), H - pad.b);
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
  canvas._chartMap = {
    data: data.map(d => ({...d, x: d.ts, val: d.bid})),
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
  if (ticker !== _livePriceBuf.ticker) {
    _livePriceBuf = {ticker, data: [], closeTime};
  }
  _livePriceBuf.closeTime = closeTime;
  const now = Date.now();
  // Dedupe: skip if same prices within 500ms
  const last = _livePriceBuf.data[_livePriceBuf.data.length - 1];
  if (last && now - last.ts < 500 && last.ya === yesAsk && last.na === noAsk) return;
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
  if (yMax - yMin < 6) { yMin -= 3; yMax += 3; }

  // Time range: use market close time if available
  let startMs = data[0].ts;
  let endMs = buf.closeTime ? new Date(buf.closeTime).getTime() : data[data.length - 1].ts;
  if (endMs <= startMs) endMs = data[data.length - 1].ts;
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

    // Current price dot + label
    ctx.beginPath();
    ctx.arc(lastX, lastY, 3, 0, Math.PI * 2);
    ctx.fillStyle = lineColor;
    ctx.fill();
    ctx.font = '10px monospace';
    ctx.fillStyle = lineColor;
    const labelX = lastX > W - 40 ? lastX - 30 : lastX + 5;
    ctx.fillText(lastBid + '¢', labelX, lastY - 6);
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
    if (cm && cm.redraw) cm.redraw();
    const labelId = canvas.id + 'Label';
    const label = document.getElementById(labelId);
    if (label) label.innerHTML = '';
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
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (r.status === 401) {
    location.reload();  // Will show login form
    throw new Error('Unauthorized');
  }
  return r.json();
}
// Rapid poll burst after user actions to catch state transitions fast
async function cmd(command, params={}) {
  try {
    await api('/api/command', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
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
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({[key]: val})
  });
  // Refresh state after bot processes the config change
  setTimeout(pollState, 800);
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

// ── Controls ─────────────────────────────────────────────
async function toggleBot() {
  const btn = $('#btnMain');
  const isStop = btn.classList.contains('ctrl-stop');
  if (isStop) {
    const s = _uiState;

    // If stop_after_cycle is pending, tap again to cancel it
    if (s.stop_after_cycle) {
      patchUI({stop_after_cycle: 0});
      api('/api/command', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({command:'cancel_stop_after_cycle', params:{}})});
      showToast('Cancelled — continuing', 'green');
      return;
    }

    const hasActive = !!s.active_trade;
    const at = s.active_trade || {};
    const detail = s.status_detail || '';
    const isSkipping = detail.includes('Skipped') || detail.includes('Cooldown');
    const tradesLeft = s.trades_remaining || 0;
    const cycleRound = s.cycle_round || 1;
    const cycleHole = s.cycle_hole || 0;
    const inCycle = cycleRound > 1 || cycleHole > 0;
    const isManualTrade = hasActive && (at.is_manual || at.is_ignored);
    const needsConfirm = (hasActive && !isManualTrade) || isSkipping || (inCycle && !isManualTrade);

    if (needsConfirm) {
      let html = '';
      if (hasActive && !isManualTrade) {
        const sideCls = at.side === 'yes' ? 'side-yes' : 'side-no';
        html += `<div style="margin-bottom:8px">Active trade: <strong><span class="${sideCls}">${(at.side||'').toUpperCase()}</span> @ ${at.avg_price_c||0}¢</strong> (${at.fill_count||0} shares)</div>`;
        html += `<div>The trade will be <strong>kept</strong> with its current sell order, but marked as <strong>ignored</strong> in stats. Martingale cycle will reset.</div>`;
      } else if (inCycle && !hasActive) {
        html += `<div>Currently in a martingale cycle (Round ${cycleRound}, hole: $${cycleHole.toFixed(2)}).</div>`;
        html += `<div style="margin-top:6px">Stopping will <strong>reset the cycle</strong> back to Round 1. Any recovery progress will be lost.</div>`;
      } else if (isSkipping) {
        html += `<div>Bot is waiting through a skipped market.</div>`;
        html += `<div style="margin-top:6px">Stopping will reset the martingale cycle.</div>`;
      }
      if (tradesLeft > 1) {
        html += `<div style="margin-top:6px;color:var(--orange)">${tradesLeft} remaining trade(s) will be cancelled.</div>`;
      }
      $('#stopDetails').innerHTML = html;
      const showAfterCycle = !s.stop_after_cycle && !isManualTrade;
      $('#stopAfterCycleBtn').style.display = showAfterCycle ? '' : 'none';
      openModal('stopOverlay');
    } else {
      // Optimistic → render → then fire API
      patchUI({auto_trading: 0, status: 'stopped', status_detail: 'Stopped',
               stop_after_cycle: 0, cycle_round: 1, cycle_hole: 0, cycle_profit_target: 0});
      cmd('stop');
      showToast('Stopped', 'red');
    }
  } else {
    // Optimistic → render → then fire API
    patchUI({auto_trading: 1, status: 'searching', status_detail: 'Starting...'});
    const mode = $('#tradeMode').value;
    const count = parseInt($('#tradeCount').value) || 5;
    cmd('start', {mode, count});
    showToast('Starting bot...', 'green');
  }
}

async function confirmStop() {
  closeModal('stopOverlay');
  const at = _uiState.active_trade;
  if (at) {
    at.is_manual = true;
    at.is_ignored = true;
    patchUI({auto_trading: 0, status: 'trading', status_detail: 'Stopped — trade kept as ignored',
             active_trade: at, stop_after_cycle: 0, cycle_round: 1, cycle_hole: 0, cycle_profit_target: 0});
  } else {
    patchUI({auto_trading: 0, status: 'stopped', status_detail: 'Stopped',
             stop_after_cycle: 0, cycle_round: 1, cycle_hole: 0, cycle_profit_target: 0});
  }
  cmd('stop');
  showToast('Stopping...', 'red');
}

async function stopAfterCycle() {
  closeModal('stopOverlay');
  patchUI({stop_after_cycle: 1});
  api('/api/command', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({command:'stop_after_cycle', params:{}})});
  showToast('Will stop after this cycle', 'orange');
}

async function cancelStopAfterCycle() {
  patchUI({stop_after_cycle: 0});
  api('/api/command', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({command:'cancel_stop_after_cycle', params:{}})});
  showToast('Cancelled — continuing', 'green');
}
function showCashOut() { openModal('cashOutOverlay'); }
function hideCashOut() { closeModal('cashOutOverlay'); }
async function confirmCashOut() {
  hideCashOut();
  patchUI({cashing_out: 1, status_detail: 'CASHING OUT — selling aggressively...'});
  api('/api/command', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({command:'cash_out', params:{}})});
  showToast('Selling all contracts...', 'orange');
}

async function cancelCashOut() {
  patchUI({cashing_out: 0, status_detail: 'Cash out cancelled'});
  api('/api/command', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({command:'cancel_cash_out', params:{}})});
  showToast('Cancelling cash out...', 'yellow');
}

let pendingManualSide = null;
function manualBuy(side) {
  pendingManualSide = side;
  const sideCls = side === 'yes' ? 'side-yes' : 'side-no';
  const sideLabel = side.toUpperCase();

  // Get current prices from live market state
  const lm = lastStateData._liveMarket;
  const ask = lm ? (side === 'yes' ? lm.yes_ask : lm.no_ask) : null;
  const bid = lm ? (side === 'yes' ? lm.yes_bid : lm.no_bid) : null;

  // Estimate cost
  const bankroll = currentBankroll;
  const mode = $('#betMode').value;
  const size = parseFloat($('#betSize').value) || 0;
  let r1Bet = mode === 'percent' ? (size / 100) * bankroll : size;
  const shares = ask > 0 ? Math.floor(r1Bet / (ask / 100)) : 0;
  const estCost = shares > 0 ? (shares * ask / 100).toFixed(2) : '—';

  let html = `<div style="text-align:center;margin-bottom:8px">
    <span class="${sideCls}" style="font-size:22px;font-weight:700">${sideLabel}</span>
    <span class="dim" style="font-size:14px"> @ ${ask || '—'}¢</span>
  </div>`;
  html += `<div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 12px;font-size:12px;color:var(--dim)">
    <div>Shares: <strong>${shares}</strong></div>
    <div>Est. Cost: <strong>$${estCost}</strong></div>
    <div>Bid: <strong>${bid || '—'}¢</strong></div>
    <div>Bankroll: <strong>$${bankroll.toFixed(2)}</strong></div>
  </div>`;

  const isRunning = !!_uiState.auto_trading;
  if (isRunning) {
    html += `<div style="color:var(--orange);font-size:11px;margin-top:6px">Auto-trading is active and will be stopped.</div>`;
  }

  $('#manualBuyDetails').innerHTML = html;
  $('#confirmManualBuyBtn').textContent = `Buy ${sideLabel}`;
  $('#confirmManualBuyBtn').className = side === 'yes' ? 'btn buy-yes' : 'btn buy-no';
  $('#confirmManualBuyBtn').style.cssText = side === 'yes'
    ? 'background:rgba(63,185,80,0.3);color:#3fb950;border:1px solid rgba(63,185,80,0.5)'
    : 'background:rgba(248,81,73,0.3);color:#f85149;border:1px solid rgba(248,81,73,0.5)';
  openModal('manualBuyOverlay');
}
async function confirmManualBuy() {
  closeModal('manualBuyOverlay');
  if (!pendingManualSide) return;
  const side = pendingManualSide;
  pendingManualSide = null;
  // Optimistic: show pending card immediately, hide live
  patchUI({
    pending_trade: {side, shares_ordered: 0, shares_filled: 0, price_c: 0,
                    order_id: null, ticker: '', close_time: '', is_manual: true,
                    sell_price_preset_c: 0, placeholder: true},
    status: 'trading',
    status_detail: `Placing ${side.toUpperCase()} buy order...`,
  });
  cmd('manual_buy', {side});
  showToast(`Placing ${side.toUpperCase()} buy order...`, 'yellow');
}

async function cancelPending() {
  // Optimistic: clear pending, show live
  patchUI({pending_trade: null, status_detail: 'Cancelling buy order...'});
  cmd('cancel_pending');
  showToast('Cancelling buy order...', 'yellow');
}

async function presetSell() {
  const price = parseInt($('#pendSellPrice').value);
  if (!price || price < 2 || price > 99) { showToast('Price must be 2-99¢', 'red'); return; }
  await cmd('preset_sell', {price_c: price});
  showToast(`Sell preset: ${price}¢`, 'green');
}
function setManualSell() {
  const price = parseInt($('#manualSellPrice').value);
  if (!price || price < 2 || price > 99) { showToast('Price must be 2-99¢', 'red'); return; }
  cmd('manual_set_sell', {price_c: price});
  showToast(`Sell limit set: ${price}¢`, 'green');
}
function manualHold() {
  cmd('manual_hold');
  showToast('Holding to close', 'yellow');
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
  const effBal = Math.max(rawBal - currentLocked, 0);
  const el = $('#bkmEffective');
  if (el) {
    el.textContent = '$' + effBal.toFixed(2);
    $('#bkmTotal').textContent = '$' + rawBal.toFixed(2);
    $('#bkmLocked').textContent = '$' + currentLocked.toFixed(2);
    $('#bkmLocked').style.color = currentLocked > 0 ? 'var(--yellow)' : 'var(--dim)';
  }
}
function lockFunds(amount) {
  currentLocked = Math.max(0, currentLocked + amount);
  $('#lockTotal').value = currentLocked.toFixed(2);
  renderUI(_uiState);
  _refreshModalBalances();
  cmd('lock_bankroll', {amount});
  if (amount > 0) {
    showToast(`Locked $${amount.toFixed(2)}`, 'green');
  } else {
    showToast(`Unlocked $${Math.abs(amount).toFixed(2)}`, 'red');
  }
}
function setLockedTotal() {
  const total = parseFloat($('#lockTotal').value) || 0;
  const delta = total - currentLocked;
  currentLocked = Math.max(0, total);
  renderUI(_uiState);
  _refreshModalBalances();
  cmd('lock_bankroll', {amount: delta});
  showToast(`Locked set to $${currentLocked.toFixed(2)}`, 'green');
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
      'padding:6px 14px;border-radius:6px;' +
      'font-size:13px;font-weight:600;opacity:0;transition:opacity 0.3s;z-index:200;pointer-events:none;';
    document.body.appendChild(t);
  }
  const colors = {
    green: {bg: 'var(--green)', fg: '#000'},
    red: {bg: 'var(--red)', fg: '#fff'},
    yellow: {bg: 'var(--yellow)', fg: '#000'},
    blue: {bg: 'var(--blue)', fg: '#000'},
    orange: {bg: 'var(--orange)', fg: '#000'},
  };
  const c = colors[color] || colors.green;
  t.style.background = c.bg;
  t.style.color = c.fg;
  t.textContent = msg;
  t.style.opacity = '1';
  setTimeout(() => t.style.opacity = '0', 2000);
}

$('#tradeMode').onchange = function() {
  $('#tradeCount').style.display = this.value === 'count' ? '' : 'none';
  saveSetting('trade_mode', this.value);
};

// ── Exposure Calculator ──────────────────────────────────
function calcExposure() {
  const mode = $('#betMode').value;
  const size = parseFloat($('#betSize').value) || 0;
  const maxL = parseInt($('#maxLosses').value) || 3;
  const riskPct = parseFloat($('#maxRiskPct').value) || 50;
  const bankroll = currentBankroll;
  const maxRisk$ = bankroll * riskPct / 100;

  let r1Bet = mode === 'percent' ? (size / 100) * bankroll : size;
  if (r1Bet <= 0) { $('#expoResult').innerHTML = '—'; return; }

  // Use server projection if available (accounts for fees, rounding, recovery math)
  const proj = (_uiState.cycle_projection);
  let totalExpo, affordable;

  if (proj && proj.rounds && proj.rounds.length) {
    // Use the last round's cumulative_loss as total exposure
    totalExpo = proj.rounds[proj.rounds.length - 1].cumulative_loss || 0;
    // Count how many rounds fit within risk budget
    affordable = 0;
    for (const rd of proj.rounds) {
      if (rd.cumulative_loss <= maxRisk$) affordable++;
      else break;
    }
    affordable = Math.max(1, affordable);
  } else {
    // Fallback: geometric estimate
    totalExpo = (Math.pow(2, maxL) - 1) * r1Bet;
    affordable = Math.max(1, Math.floor(Math.log2(maxRisk$ / r1Bet + 1)));
  }

  const suggestedBet = maxRisk$ / (Math.pow(2, maxL) - 1);
  const suggestedPct = (suggestedBet / bankroll * 100);

  let html = `<div>Total max exposure: <strong class="${totalExpo > bankroll ? 'neg' : ''}">\$${totalExpo.toFixed(2)}</strong></div>`;
  if (mode === 'percent') {
    html += `<div>R1 bet: \$${r1Bet.toFixed(2)} (${size}% of \$${bankroll.toFixed(0)})</div>`;
  }
  html += `<div>Bankroll: \$${bankroll.toFixed(2)} · Risk budget: \$${maxRisk$.toFixed(2)} (${riskPct}%)</div>`;

  if (totalExpo > maxRisk$) {
    html += `<div style="margin-top:4px;color:var(--orange)">`;
    if (mode === 'percent') {
      html += `At ${size}%: can afford <strong>${affordable}</strong> max losses within ${riskPct}% risk<br>`;
      html += `At ${maxL} max losses: use <strong>${suggestedPct.toFixed(1)}%</strong> (\$${suggestedBet.toFixed(2)})`;
    } else {
      html += `At \$${r1Bet.toFixed(0)} bet: can afford <strong>${affordable}</strong> max losses within ${riskPct}% risk<br>`;
      html += `At ${maxL} max losses: bet should be <strong>\$${suggestedBet.toFixed(2)}</strong>`;
    }
    html += `</div>`;
  } else {
    html += `<div style="color:var(--green);margin-top:4px"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px;margin-right:4px"><path d="M20 6L9 17l-5-5"/></svg>Within ${riskPct}% risk budget</div>`;
  }
  $('#expoResult').innerHTML = html;
}

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
  if (partial.pending_trade !== undefined) _uiState.pending_trade = partial.pending_trade;
  if (partial.auto_trading !== undefined) _uiState.auto_trading = partial.auto_trading;
  if (partial.cashing_out !== undefined) _uiState.cashing_out = partial.cashing_out;
  if (partial.status !== undefined) _uiState.status = partial.status;
  if (partial.status_detail !== undefined) _uiState.status_detail = partial.status_detail;
  if (partial.stop_after_cycle !== undefined) _uiState.stop_after_cycle = partial.stop_after_cycle;
  if (partial.cycle_round !== undefined) _uiState.cycle_round = partial.cycle_round;
  if (partial.cycle_hole !== undefined) _uiState.cycle_hole = partial.cycle_hole;
  if (partial.cycle_profit_target !== undefined) _uiState.cycle_profit_target = partial.cycle_profit_target;
  lastStateData._lastState = _uiState;
  renderUI(_uiState);
}

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

    // Status bar
    const st = s.status || 'stopped';

    // Enable/disable start and stop buttons
    const autoOn = s.auto_trading;
    const isRunning = !!autoOn;
    const hasActiveTrade = !!s.active_trade;
    const hasPendingTrade = !!s.pending_trade;

    const btn = $('#btnMain');
    if (isRunning) {
      btn.className = 'ctrl-icon ctrl-stop';
      btn.querySelector('svg').innerHTML = '<path d="M5.25 7.5A2.25 2.25 0 0 1 7.5 5.25h9a2.25 2.25 0 0 1 2.25 2.25v9a2.25 2.25 0 0 1-2.25 2.25h-9a2.25 2.25 0 0 1-2.25-2.25v-9Z"/>';
    } else {
      btn.className = 'ctrl-icon ctrl-play';
      btn.querySelector('svg').innerHTML = '<path d="M5.25 5.653c0-.856.917-1.398 1.667-.986l11.54 6.347a1.125 1.125 0 0 1 0 1.972l-11.54 6.347a1.125 1.125 0 0 1-1.667-.986V5.653Z"/>';
    }
    if (isRunning && s.stop_after_cycle) btn.classList.add('stop-pending');

    // ── Build rich status text ──
    const detail = s.status_detail || '';
    const cr = s.cycle_round || 1;
    const hole = s.cycle_hole || 0;
    const target = s.cycle_profit_target || 0;
    const at0 = s.active_trade;
    const stopAfter = s.stop_after_cycle;
    lastStateData._delayEndISO = s._delay_end_iso || null;

    let statusMain = '';
    let statusColor = '';
    let dotClass = 'dot-red';
    let subParts = [];

    if (!isRunning) {
      // ── STOPPED ──
      statusMain = 'Stopped';
      if (hasActiveTrade) statusMain = 'Stopped · trade active';

      // Bet config
      const betMode = $('#betMode')?.value || 'flat';
      const betSize = parseFloat($('#betSize')?.value) || 0;
      const maxL = parseInt($('#maxLosses')?.value) || 3;
      let betLabel = betMode === 'percent' ? `${betSize}% ($${((betSize/100)*currentBankroll).toFixed(0)})` : `$${betSize.toFixed(0)}`;
      subParts.push(`Bet: ${betLabel} · ${maxL} max losses`);
      const entryDelay = parseInt($('#entryDelay')?.value) || 0;
      if (entryDelay > 0) subParts[0] += ` · ${entryDelay}m delay`;

    } else if (s.cashing_out) {
      // ── CASHING OUT ──
      statusMain = 'CASHING OUT';
      dotClass = 'dot-yellow';
      statusColor = 'var(--orange)';
      subParts.push('Aggressively selling all contracts...');

    } else if (hasPendingTrade) {
      // ── PENDING BUY ──
      const pt = s.pending_trade;
      const pSide = (pt.side || '').toUpperCase();
      statusMain = `Buying ${pSide}`;
      dotClass = 'dot-yellow';
      statusColor = 'var(--yellow)';
      const pFilled = pt.fill_count || 0;
      const pOrdered = pt.shares_ordered || 0;
      subParts.push(`${pFilled}/${pOrdered} filled @ ${pt.price_c||0}¢`);

    } else if (hasActiveTrade) {
      // ── ACTIVE TRADE ──
      const tSide = (at0.side || 'yes').toUpperCase();
      const tBid = at0.current_bid || 0;
      const tEntry = at0.avg_price_c || 0;
      const tPnlEst = ((tBid - tEntry) * (at0.fill_count || 0) / 100);
      const bidUp = tBid >= tEntry;
      statusMain = `Trading · ${tSide}@${tEntry}¢ → ${tBid}¢`;
      statusColor = bidUp ? 'var(--green)' : 'var(--red)';
      dotClass = 'dot-green';

      const pnlSign = tPnlEst >= 0 ? '+' : '';
      const pnlColor = tPnlEst >= 0 ? 'var(--green)' : 'var(--red)';
      subParts.push(`<span style="color:${pnlColor}">${pnlSign}$${tPnlEst.toFixed(2)}</span> est P&L · Sell target: ${at0.sell_price_c||0}¢`);

    } else {
      // ── RUNNING but no trade ──
      dotClass = 'dot-yellow';

      if (detail.includes('Skipped')) {
        statusMain = 'Skipped';
        statusColor = 'var(--yellow)';
        const reason = detail.replace('Skipped: ', '');
        subParts.push(reason);
      } else if (detail.includes('Cooldown')) {
        statusMain = 'Cooldown';
        statusColor = 'var(--yellow)';
        subParts.push(detail);
      } else if (detail.includes('Watching')) {
        statusMain = 'Watching prices';
        statusColor = 'var(--blue)';
        const watchDetail = detail.replace('Watching ', '').replace(' — waiting for price', ' · need');
        subParts.push(watchDetail);
      } else if (detail.includes('Entry delay')) {
        const delayEnd = s._delay_end_iso;
        if (delayEnd) {
          const diff = Math.max(0, (new Date(delayEnd) - new Date()) / 1000);
          if (diff > 0) {
            statusMain = `Entry delay · ${fmtMmSs(diff)}`;
          } else {
            statusMain = 'Entering...';
          }
        } else {
          statusMain = 'Entry delay';
        }
        statusColor = 'var(--yellow)';
      } else if (detail.includes('Market closed')) {
        statusMain = 'Resolving trade...';
        statusColor = 'var(--blue)';
      } else if (detail.includes('WIN') || detail.includes('+$')) {
        statusMain = detail;
        dotClass = 'dot-green';
        statusColor = 'var(--green)';
      } else if (detail.includes('LOSS') || detail.includes('MAX LOSS')) {
        statusMain = detail;
        statusColor = 'var(--red)';
      } else if (detail.includes('Starting')) {
        statusMain = 'Starting...';
        statusColor = 'var(--blue)';
      } else if (detail.includes('Between') || detail.includes('waiting for next') || detail.includes('Next market') || detail.includes('Awaiting') || !detail || detail === 'Awaiting start command' || detail === 'Bot ready') {
        const nmNext = lastStateData._nextMarketOpen;
        if (nmNext) {
          const diff = Math.max(0, (new Date(nmNext) - new Date()) / 1000);
          if (diff > 0 && diff < 1200) {
            statusMain = `Next market in ${fmtMmSs(diff)}`;
          } else if (diff <= 0) {
            statusMain = 'Market starting...';
          } else {
            statusMain = 'Waiting for next market';
          }
        } else {
          statusMain = 'Waiting for next market';
        }
        statusColor = 'var(--dim)';
      } else if (detail.includes('Stopped')) {
        statusMain = detail;
        statusColor = 'var(--red)';
      } else if (detail) {
        statusMain = detail;
      } else {
        // No detail — same as waiting for market
        const nmNext2 = lastStateData._nextMarketOpen;
        if (nmNext2) {
          const diff2 = Math.max(0, (new Date(nmNext2) - new Date()) / 1000);
          if (diff2 > 0 && diff2 < 1200) {
            statusMain = `Next market in ${fmtMmSs(diff2)}`;
          } else {
            statusMain = 'Searching';
          }
        } else {
          statusMain = 'Searching';
        }
      }
    }

    // Append cycle info when mid-cycle
    if (cr > 1 || hole > 0) {
      const cycleStr = `R${cr}` + (hole > 0 ? ` · $${hole.toFixed(2)} hole` : '');
      subParts.push(cycleStr);
    }

    // Append stop after cycle
    if (isRunning && stopAfter) {
      subParts.push('<span style="color:var(--orange)">Will stop after this cycle</span>');
    }

    // Append mode + bet info when running and sub is sparse
    if (isRunning && subParts.length < 2) {
      const tradeMode = $('#tradeMode')?.value || 'continuous';
      const rem = s.trades_remaining;
      const betMode2 = $('#betMode')?.value || 'flat';
      const betSize2 = parseFloat($('#betSize')?.value) || 0;
      const maxL2 = parseInt($('#maxLosses')?.value) || 3;
      let betLabel2 = betMode2 === 'percent' ? `${betSize2}%` : `$${betSize2.toFixed(0)}`;
      let modeStr = '';
      if (tradeMode === 'single') modeStr = 'Single trade';
      else if (tradeMode === 'count' && rem) modeStr = `${rem} trade${rem>1?'s':''} left`;
      else if (tradeMode === 'cycle') modeStr = 'One cycle';
      const infoParts = [`${betLabel2} bet · ${maxL2} max`];
      if (modeStr) infoParts.push(modeStr);
      subParts.push(infoParts.join(' · '));
    }

    // Render
    $('#statusDot').className = 'status-dot ' + dotClass;
    const stEl = $('#statusText');
    stEl.textContent = statusMain;
    stEl.style.color = statusColor || '';

    const subEl = $('#statusSub');
    if (subParts.length) {
      subEl.innerHTML = subParts.join(' · ');
      subEl.style.display = '';
    } else {
      subEl.style.display = 'none';
    }

    // Bankroll
    const rawBal = (s.bankroll_cents || 0) / 100;
    const effBal = Math.max(rawBal - currentLocked, 0);
    currentBankroll = effBal;
    $('#hdrBankroll').textContent = '$' + effBal.toFixed(2);

    // Cycle
    const proj = s.cycle_projection;
    const cs = s.cycle_loss_streak || 0;
    $('#cycleRound').textContent = cr;
    $('#cycleStreak').textContent = cs;
    const holeEl = $('#cycleHole');
    holeEl.textContent = '$' + hole.toFixed(2);
    holeEl.className = 'val' + (hole > 0 ? ' neg' : '');
    $('#cycleTarget').textContent = '$' + target.toFixed(2);

    // Cycle detail line
    let cycleDetailParts = [];
    if (proj) {
      const nextR = proj.rounds ? proj.rounds.find(r => r.round === cr) : null;
      if (nextR) cycleDetailParts.push(`Next bet: $${nextR.bet.toFixed(2)}`);
      if (proj.total_max_exposure) cycleDetailParts.push(`Max exposure: $${proj.total_max_exposure.toFixed(2)}`);
    }
    $('#cycleDetail').textContent = cycleDetailParts.join(' · ');

    // Projection table
    if (proj && proj.rounds) {
      const tbody = $('#projBody');
      tbody.innerHTML = proj.rounds.map(r =>
        `<tr class="${r.round === cr ? 'current-round' : ''}">
          <td>R${r.round}${r.round === cr ? ' ←' : ''}</td>
          <td>\$${r.bet.toFixed(0)}</td>
          <td>\$${r.est_cost.toFixed(0)}</td>
          <td>${r.est_sell_c}¢</td>
          <td class="neg">\$${r.cumulative_loss.toFixed(0)}</td>
        </tr>`
      ).join('');
    }

    // Session stats
    const sw = s.session_wins || 0;
    const sl = s.session_losses || 0;
    const spnl2 = s.session_pnl || 0;
    const sSkips = s.session_skips || 0;
    $('#statWins').textContent = sw;
    $('#statLosses').textContent = sl;
    $('#statSkips').textContent = sSkips;
    const spEl = $('#statSessionPnl');
    spEl.textContent = (spnl2>=0?'+':'') + '$' + spnl2.toFixed(2);
    spEl.className = 'val ' + (spnl2 > 0 ? 'pos' : spnl2 < 0 ? 'neg' : '');
    const sTotal = sw + sl;
    const sWR = sTotal > 0 ? (sw / sTotal * 100).toFixed(0) : '—';
    $('#statSessionDetail').textContent = `${sWR}% win rate · ${sTotal} trades placed`;

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
      loadTrades(); loadRegimes(); loadLifetimeStats();
      // Fire-and-forget dismiss (don't re-poll from inside poll)
      api('/api/command', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({command: 'dismiss_summary', params: {}})
      });
    }

    // Exactly ONE card visible: active > pending > live
    if (at) {
      tc.style.display = '';
      mc.style.display = 'none';
      pc.style.display = 'none';
      lastStateData._monCloseTime = null;
      lastStateData._nextMarketOpen = at.close_time;

      // Resolving state — dim the card
      const isResolving = at.resolving;
      tc.style.opacity = isResolving ? '0.6' : '1';

      // Show manual controls for manual/ignored trades (hide during resolve)
      const isManual = at.is_manual || at.is_ignored;
      $('#manualControls').style.display = (isManual && !isResolving) ? '' : 'none';
      $('#cashOutSection').style.display = isResolving ? 'none' : '';
      const isIgnoredStop = at.is_ignored && !autoOn;
      $('#stoppingBanner').style.display = isIgnoredStop ? '' : 'none';

      // Toggle cash out vs cancel button
      const isCashingOut = s.cashing_out;
      $('#cashOutBtn').style.display = isCashingOut ? 'none' : '';
      $('#cancelCashOutBtn').style.display = isCashingOut ? '' : 'none';

      // Regime bar
      $('#tradeRisk').outerHTML = riskTag(at.risk_level);
      const newRisk = document.querySelector('#tradeCard .regime-tag');
      if (newRisk) newRisk.id = 'tradeRisk';
      $('#tradeRegimeLabel').textContent = (at.regime_label || 'unknown').replace(/_/g, ' ');
      const wr = ((at.regime_win_rate||0)*100).toFixed(0);
      $('#tradeRegimeStats').textContent = at.regime_trades ?
        `${wr}% win (n=${at.regime_trades})` : '';

      // Main stats
      const tradeSide = (at.side||'').toUpperCase();
      const tradeSideCls = at.side === 'yes' ? 'side-yes' : 'side-no';
      $('#tradeSide').innerHTML = `<span class="${tradeSideCls}">${tradeSide}</span> @ ${at.avg_price_c}¢`;
      const bid = at.current_bid || at.avg_price_c || 0;
      const bidEl = $('#tradeBid');
      bidEl.textContent = bid + '¢';
      bidEl.className = 'val price-display ' + (bid >= (at.sell_price_c||999) ? 'pos' :
        bid > at.avg_price_c ? 'pos' : bid < at.avg_price_c ? 'neg' : '');
      $('#tradeSell').textContent = at.sell_price_c ? at.sell_price_c + '¢' : (at.hold_to_close ? 'Hold' : '—');

      lastStateData._tradeCloseTime = at.close_time;
      lastStateData._tradeEndFired = false;
      tickCountdown('tradeTime', at.close_time);

      $('#tradeCost').textContent = '$' + (at.actual_cost || 0).toFixed(2);
      $('#tradeHwm').textContent = (at.high_water_c || 0) + '¢';

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
      pnlEl.textContent = (estPnl>=0?'+':'') + '$' + estPnl.toFixed(2);
      pnlEl.className = estPnl >= 0 ? 'pos' : 'neg';

      // Detail section
      $('#tradeRegimeGrid').innerHTML = buildRegimeGrid(at);
      $('#tdRound').textContent = isManual ? 'Manual' : (at.cycle_round || 1);
      $('#tdHole').textContent = '$' + (at.cycle_hole||0).toFixed(2);
      $('#tdTarget').textContent = '$' + (at.cycle_target||0).toFixed(2);
      $('#tdData').textContent = at.is_data_bet ? (at.actual_cost > 2 ? 'Yes (full-size)' : 'Yes ($1)') : 'No';

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
      const cashBal = rawBal;
      const bkInfo = `In trade: $${atCost.toFixed(2)} · If sell fills: <span class="pos">+$${atWinPnl.toFixed(2)}</span> · If resolves: <span class="pos">+$${atResolvePnl.toFixed(2)}</span>`;
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
        // Load historical price path
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
              // Prepend historical data before live data
              chartData = hist.concat(chartData);
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

      // Store live market for manual buy popup
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
        lastStateData._nextMarketOpen = lm.close_time;
        $('#monMarket').textContent = marketStartTime(lm.close_time);
        lastStateData._monCloseTime = lm.close_time;
        lastStateData._monEndFired = false;
        tickCountdown('monTime', lm.close_time);

        const ya = lm.yes_ask, na = lm.no_ask;
        const yb = lm.yes_bid, nb = lm.no_bid;
        const yaStr = (ya || '—') + (ya ? '¢' : '');
        const naStr = (na || '—') + (na ? '¢' : '');
        $('#monYesAsk').textContent = yaStr;
        $('#monNoAsk').textContent = naStr;
        $('#monYesSpread').textContent = (ya && yb) ? (ya - yb) + '¢ spread' : '';
        $('#monNoSpread').textContent = (na && nb) ? (na - nb) + '¢ spread' : '';

        // Disable buttons at extreme prices
        const yesBtn = $('#btnBuyYes');
        const noBtn = $('#btnBuyNo');
        const yesOff = !ya || ya >= 99 || ya <= 1;
        const noOff = !na || na >= 99 || na <= 1;
        yesBtn.className = 'buy-side-btn buy-yes' + (yesOff ? ' btn-disabled' : '');
        noBtn.className = 'buy-side-btn buy-no' + (noOff ? ' btn-disabled' : '');
        if (yesOff) $('#monYesSpread').textContent = ya >= 99 ? 'Unavailable' : ya <= 1 ? 'No offers' : '';
        if (noOff) $('#monNoSpread').textContent = na >= 99 ? 'Unavailable' : na <= 1 ? 'No offers' : '';

        // Accumulate live prices and draw chart
        pushLivePrice(lm.ticker, lm.close_time, ya, na, yb, nb);
        drawLiveMarketChart('liveChart');

        const riskHtml = riskTag(lm.risk_level);
        const monRiskEl = $('#monRisk');
        monRiskEl.outerHTML = riskHtml;
        const newEl = document.querySelector('#monitorCard .regime-tag');
        if (newEl) newEl.id = 'monRisk';
        $('#monRegimeLabel').textContent = (lm.regime_label || 'unknown').replace(/_/g, ' ');
        $('#monRegimeGrid').innerHTML = buildRegimeGrid(lm);
      } else {
        mc.style.display = '';
        $('#monMarket').textContent = '—';
        $('#monTime').textContent = '—';
        $('#monYesAsk').textContent = '—';
        $('#monNoAsk').textContent = '—';
        $('#monYesSpread').textContent = '';
        $('#monNoSpread').textContent = '';
        $('#btnBuyYes').className = 'buy-side-btn buy-yes btn-disabled';
        $('#btnBuyNo').className = 'buy-side-btn buy-no btn-disabled';
        $('#monRegimeLabel').textContent = '—';
        $('#monRegimeGrid').innerHTML = '';
        lastStateData._monCloseTime = null;
        lastStateData._nextMarketOpen = null;
        const lc = document.getElementById('liveChart');
        if (lc) lc.style.display = 'none';
      }
    }

    // ── Collapsed card subtitles ───────────────────────────
    const _cr = s.cycle_round||1, _ch = s.cycle_hole||0;
    let subText = '';
    if (_cr > 1 || _ch > 0) subText = `R${_cr} · $${_ch.toFixed(2)} hole`;
    $('#subSessionCycle').textContent = subText;
  } catch(e) { console.error('Render error:', e); }
}

// ── Config loading ──────────────────────────────────────
async function loadConfig() {
  const cfg = await api('/api/config');
  if (cfg.trade_mode) {
    $('#tradeMode').value = cfg.trade_mode;
    $('#tradeCount').style.display = cfg.trade_mode === 'count' ? '' : 'none';
  }
  if (cfg.trade_count) $('#tradeCount').value = cfg.trade_count;
  if (cfg.bet_mode) $('#betMode').value = cfg.bet_mode;
  if (cfg.bet_size) $('#betSize').value = cfg.bet_size;
  if (cfg.max_losses) $('#maxLosses').value = cfg.max_losses;
  if (cfg.entry_price_min_c) $('#entryPriceMin').value = cfg.entry_price_min_c;
  if (cfg.entry_price_max_c) $('#entryPriceMax').value = cfg.entry_price_max_c;
  if (cfg.entry_delay_minutes !== undefined) $('#entryDelay').value = cfg.entry_delay_minutes;
  if (cfg.cooldown_after_max_loss !== undefined) $('#cooldownML').value = cfg.cooldown_after_max_loss;
  if (cfg.locked_bankroll !== undefined) {
    currentLocked = parseFloat(cfg.locked_bankroll) || 0;
    $('#lockTotal').value = currentLocked;
  }
  if (cfg.bankroll_min) $('#bankrollMin').value = cfg.bankroll_min;
  if (cfg.bankroll_max) $('#bankrollMax').value = cfg.bankroll_max;
  if (cfg.session_profit_target) $('#sessionTarget').value = cfg.session_profit_target;
  if (cfg.auto_lock_enabled !== undefined) $('#autoLockEnabled').checked = cfg.auto_lock_enabled;
  if (cfg.auto_lock_threshold) $('#autoLockThreshold').value = cfg.auto_lock_threshold;
  if (cfg.auto_lock_amount) $('#autoLockAmount').value = cfg.auto_lock_amount;
  if (cfg.ignore_mode !== undefined) $('#ignoreMode').checked = cfg.ignore_mode;
  if (cfg.skip_unknown_regimes !== undefined) $('#skipUnknown').checked = cfg.skip_unknown_regimes;
  if (cfg.skip_high_risk !== undefined) $('#skipHigh').checked = cfg.skip_high_risk;
  if (cfg.skip_terrible !== undefined) $('#skipTerrible').checked = cfg.skip_terrible;
  if (cfg.skip_moderate !== undefined) $('#skipModerate').checked = cfg.skip_moderate;
  if (cfg.data_bet_full_size !== undefined) $('#dataBetFullSize').checked = cfg.data_bet_full_size;
  if (cfg.data_bet_on_skip !== undefined) $('#dataBetOnSkip').checked = cfg.data_bet_on_skip;
  if (cfg.disable_round_limits !== undefined) $('#disableRoundLimits').checked = cfg.disable_round_limits;
  if (cfg.custom_sell_price_c !== undefined) $('#customSellPrice').value = cfg.custom_sell_price_c;
  if (cfg.push_notify_wins !== undefined) $('#notifyWins').checked = cfg.push_notify_wins;
  if (cfg.push_notify_losses !== undefined) $('#notifyLosses').checked = cfg.push_notify_losses;
  if (cfg.push_notify_errors !== undefined) $('#notifyErrors').checked = cfg.push_notify_errors;
  if (cfg.push_notify_buys !== undefined) $('#notifyBuys').checked = cfg.push_notify_buys;
  if (cfg.push_notify_skips !== undefined) $('#notifySkips').checked = cfg.push_notify_skips;
  if (cfg.push_notify_cycles !== undefined) $('#notifyCycles').checked = cfg.push_notify_cycles;
  if (cfg.push_quiet_start) $('#quietStart').value = cfg.push_quiet_start;
  if (cfg.push_quiet_end) $('#quietEnd').value = cfg.push_quiet_end;
  setTimeout(calcExposure, 500);
}

// ── Trades + Regimes ────────────────────────────────────
let currentTradeFilter = 'all';

function setTradeFilter(filter, btn) {
  currentTradeFilter = filter;
  document.querySelectorAll('#tradeFilters .chip').forEach(c => {
    c.className = 'chip';
    if (c.dataset.filter === filter) {
      const colorMap = {win:'active-green', loss:'active-red', cashed_out:'active-orange',
                        skipped:'active', data:'active', ignored:'active-yellow', recovery:'active',
                        open:'active', yes:'active-green', no:'active-red'};
      c.className = 'chip ' + (colorMap[filter] || 'active');
    }
  });
  loadTrades();
}

async function loadTrades() {
  let trades = await api('/api/trades?limit=50');
  const el = $('#tradeList');
  if (!trades.length) { el.innerHTML = '<div class="dim">No trades yet</div>'; return; }

  // Apply filter
  const f = currentTradeFilter;
  if (f !== 'all') {
    trades = trades.filter(t => {
      if (f === 'win') return t.outcome === 'win';
      if (f === 'loss') return t.outcome === 'loss';
      if (f === 'cashed_out') return t.outcome === 'cashed_out';
      if (f === 'skipped') return ['skipped','no_fill'].includes(t.outcome);
      if (f === 'data') return t.is_data_collection;
      if (f === 'ignored') return t.is_ignored;
      if (f === 'recovery') return (t.cycle_round || 1) > 1;
      if (f === 'open') return t.outcome === 'open';
      if (f === 'yes') return t.side === 'yes';
      if (f === 'no') return t.side === 'no';
      return true;
    });
  }
  el.innerHTML = trades.map(t => {
    const o = t.outcome || 'unknown';
    const pnl = t.pnl || 0;
    const cardCls = o === 'win' ? 'tc-win' : o === 'loss' ? 'tc-loss' :
                    o === 'cashed_out' ? 'tc-cashout' : o === 'open' ? 'tc-open' : 'tc-skip';
    const pnlCls = pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : 'dim';

    // Outcome label
    const outLabel = {
      win: 'WIN', loss: 'LOSS', cashed_out: 'CASHED OUT',
      skipped: 'SKIP', no_fill: 'NO FILL', open: 'OPEN'
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
    else if (o === 'cashed_out') tags += tag('CASHED OUT', 'tag-cashout', 'cashed_out');
    else if (o === 'skipped' || o === 'no_fill') tags += tag(o === 'no_fill' ? 'NO FILL' : 'SKIP', 'tag-skip', 'skipped');
    else if (o === 'open') tags += tag('OPEN', 'tag-open', 'open');

    // Side tag
    if (t.side === 'yes') tags += tag('YES', 'tag-yes', 'yes');
    else if (t.side === 'no') tags += tag('NO', 'tag-no', 'no');

    // Special tags
    if (t.is_data_collection) tags += tag('DATA BET', 'data', 'data');
    if (t.is_ignored) tags += tag('IGNORED', 'ignored', 'ignored');
    if ((t.cycle_round || 1) > 1) tags += tag('R' + t.cycle_round, 'tag-recovery', 'recovery');

    // Mode tag
    const mode = t.trade_mode || '';
    if (mode === 'manual') tags += tag('MANUAL', 'ignored', 'ignored');

    // Skip reason
    const skipLine = (o === 'skipped' || o === 'no_fill') && t.skip_reason ?
      `<div style="font-size:11px;color:var(--dim);margin-top:4px">${escHtml(t.skip_reason)}</div>` : '';

    // Market time
    const marketTime = t.market_ct || '';

    // Only show full detail for real trades
    const isReal = ['win', 'loss', 'cashed_out', 'open'].includes(o);

    let detailHtml = '';
    if (isReal) {
      detailHtml = `
        <div class="tc-details">
          <div>Side: <strong><span class="${t.side==='yes'?'side-yes':'side-no'}">${side}</span> @ ${entry}¢</strong></div>
          <div>Sell target: <strong>${sell}¢</strong></div>
          <div>Shares: <strong>${filled}</strong></div>
          <div>Cost: <strong>$${cost.toFixed(2)}</strong>${fees > 0 ? ` <span style="color:var(--dim)">(+$${fees.toFixed(2)} fees)</span>` : ''}</div>
          <div>HWM: <strong>${hwm}¢</strong></div>
          <div>Progress: <strong>${progress.toFixed(0)}%</strong></div>
          <div>Round: <strong>${t.cycle_round || 1}</strong></div>
          <div>Stability: <strong>${t.price_stability_c != null ? t.price_stability_c + '¢' : '—'}</strong></div>
          <div>Hole: <strong>$${(t.cycle_hole || 0).toFixed(2)}</strong></div>
        </div>`;
    }

    return `<div class="trade-card ${cardCls}" onclick="showTradeDetail(${t.id})" style="cursor:pointer">
      <div class="tc-header">
        <div>
          <span class="tc-outcome ${pnlCls}">${outLabel}</span>
          ${riskTag(riskLvl)}
        </div>
        <span class="tc-pnl ${pnlCls}">${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}</span>
      </div>
      <div style="display:flex;justify-content:space-between;margin-top:4px">
        <span class="dim" style="font-size:12px">${regLabel}</span>
        <span class="dim" style="font-size:11px">${marketTime ? marketTime + ' · ' : ''}${t.created_ct || ''}</span>
      </div>
      ${detailHtml}
      ${skipLine}
      <div class="tc-tags">${tags}</div>
      <div style="text-align:right;margin-top:4px">
        <button class="delete-btn" title="Delete trade"
              onclick="event.stopPropagation();showDeleteTrade(${t.id}, '${escHtml(outLabel)}', '${(pnl>=0?'+':'') + '$' + pnl.toFixed(2)}', ${t.cycle_round||1})">
          <svg viewBox="0 0 24 24" fill="none" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="m14.74 9-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 0 1-2.244 2.077H8.084a2.25 2.25 0 0 1-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 0 0-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 0 1 3.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 0 0-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 0 0-7.5 0"/></svg>
        </button>
      </div>
    </div>`;
  }).join('');

  function escHtml(s) { return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/'/g,'&#39;'); }
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
      <span class="tc-outcome ${pCls}" style="font-size:16px">${o.toUpperCase()}</span>
      <span class="tc-pnl ${pCls}" style="font-size:18px">${pnl>=0?'+':''}$${pnl.toFixed(2)}</span>
    </div>`;

    html += `<div style="display:grid;grid-template-columns:1fr 1fr;gap:3px 12px;font-size:12px;color:var(--dim)">
      <div>Side: <strong><span class="${sideCls}">${(t.side||'').toUpperCase()}</span> @ ${entry}¢</strong></div>
      <div>Sell Target: <strong>${sell}¢</strong></div>
      <div>Shares: <strong>${t.shares_filled||0}</strong></div>
      <div>Sold: <strong>${t.sell_filled||0}</strong></div>
      <div>Cost: <strong>$${(t.actual_cost||0).toFixed(2)}</strong></div>
      <div>Gross: <strong>$${(t.gross_proceeds||0).toFixed(2)}</strong></div>
      <div>Fees: <strong>$${(t.fees_paid||0).toFixed(2)}</strong></div>
      <div>Market Result: <strong>${t.market_result||'N/A'}</strong></div>
      <div>HWM: <strong>${hwm}¢</strong></div>
      <div>LWM: <strong>${lwm}¢</strong></div>
      <div>Oscillations: <strong>${osc}</strong></div>
      <div>Progress: <strong>${prog.toFixed(0)}%</strong></div>
      <div>Stability: <strong>${stab != null ? stab+'¢' : '—'}</strong></div>
      <div>Entry Delay: <strong>${delay}m</strong></div>
    </div>`;

    html += `<div style="margin-top:8px;padding-top:8px;border-top:1px solid var(--border);font-size:12px;color:var(--dim)">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:3px 12px">
        <div>Cycle Round: <strong>R${t.cycle_round||1}</strong></div>
        <div>Hole: <strong>$${(t.cycle_hole||0).toFixed(2)}</strong></div>
        <div>Target: <strong>$${(t.cycle_profit_target||0).toFixed(2)}</strong></div>
        <div>Streak: <strong>${t.cycle_loss_streak||0}</strong></div>
      </div>
    </div>`;

    html += `<div style="margin-top:6px;display:flex;align-items:center;gap:6px">
      ${riskTag(riskLvl)} <span style="font-size:12px">${regime}</span>
    </div>`;

    let tags = [];
    if (t.is_data_collection) tags.push('DATA BET');
    if (t.is_ignored) tags.push('IGNORED');
    if (t.trade_mode) tags.push(t.trade_mode.toUpperCase());
    if (tags.length) {
      html += `<div style="margin-top:4px">${tags.map(tg => `<span class="tc-tag">${tg}</span>`).join(' ')}</div>`;
    }

    html += `<div style="margin-top:6px;font-size:11px;color:var(--dim)">
      Market: ${t.market_ct || '—'} · Traded: ${t.created_ct || '—'}
      ${t.notes ? '<br>Notes: ' + t.notes : ''}
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
  } catch(e) { console.error('Trade detail error:', e); }
}

// ── Delete trade ─────────────────────────────────────────
let pendingDeleteId = null;

async function showDeleteTrade(tradeId, label, pnlStr, cycleRound) {
  pendingDeleteId = tradeId;
  const info = $('#deleteInfo');
  const btns = $('#deleteBtns');

  // Fetch cycle info
  let cycleInfo = '';
  let cycleBtn = '';

  try {
    const cycle = await api(`/api/trade/${tradeId}/cycle`);
    if (cycle.length > 1) {
      const totalPnl = cycle.reduce((s, t) => s + (t.pnl || 0), 0);
      cycleInfo = `<div class="warning-box" style="margin-top:8px">
        This trade is part of a ${cycle.length}-trade martingale cycle
        (total P&L: ${totalPnl >= 0 ? '+' : ''}$${totalPnl.toFixed(2)}).
        Deleting mid-cycle trades will leave gaps in the data.
      </div>`;
      cycleBtn = `<button class="btn btn-yellow" style="margin-top:4px"
        onclick="doDeleteCycle(${tradeId}, ${cycle.length})">
        Delete Entire Cycle (${cycle.length} trades)
      </button>`;
    }
  } catch(e) {}

  info.innerHTML = `
    <div style="font-size:13px;color:var(--text)">
      <strong>${label}</strong> ${pnlStr} (Round ${cycleRound})
    </div>
    ${cycleInfo}
  `;

  btns.innerHTML = `
    <div class="confirm-btns">
      <button class="btn btn-dim" onclick="hideDelete()">Cancel</button>
      <button class="btn btn-red" onclick="doDeleteSingle(${tradeId})">Delete This Trade</button>
    </div>
    ${cycleBtn}
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

async function doDeleteCycle(tradeId, count) {
  hideDelete();
  showToast(`Deleting ${count} trades...`, 'yellow');
  try {
    const r = await api(`/api/trade/${tradeId}/delete_cycle`, {method: 'POST'});
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

async function loadRegimes() {
  let regimes = await api('/api/regimes');
  const el = $('#regimeList');
  if (!regimes.length) { el.innerHTML = '<div class="dim">No data yet — trades build this</div>'; return; }

  if (currentRegimeFilter !== 'all') {
    regimes = regimes.filter(r => r.risk_level === currentRegimeFilter);
  }

  el.innerHTML = regimes.map(r => {
    const wr = ((r.win_rate||0)*100).toFixed(0);
    const n = r.total_trades || 0;
    const w = r.wins || 0;
    const l = r.losses || 0;
    const pnl = r.total_pnl || 0;
    const pnlCls = pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : '';
    const label = r.regime_label || '';
    const borderColor = {low:'var(--green)',moderate:'var(--yellow)',high:'var(--orange)',terrible:'var(--red)'}[r.risk_level] || 'var(--border)';
    return `<div style="background:var(--bg);border-radius:6px;padding:8px;margin-bottom:6px;border-left:3px solid ${borderColor};cursor:pointer" onclick="showRegimeDetail('${label.replace(/'/g,"\\'")}')">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div style="display:flex;align-items:center;gap:6px">
          ${riskTag(r.risk_level)}
          <span style="font-size:12px;font-weight:600">${label.replace(/_/g,' ')}</span>
        </div>
        <div style="text-align:right">
          <span style="font-family:monospace;font-weight:600">${wr}%</span>
          <span class="dim" style="font-size:11px;margin-left:4px">(${n})</span>
        </div>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--dim);margin-top:3px">
        <span>${w}W / ${l}L</span>
        <span class="${pnlCls}">${pnl>=0?'+':''}$${pnl.toFixed(2)}</span>
      </div>
    </div>`;
  }).join('');
}

async function showRegimeDetail(label) {
  try {
    const d = await api('/api/regime/' + encodeURIComponent(label) + '/detail');
    const s = d.stats || {};
    const avgs = d.averages || {};
    const wr = ((s.win_rate||0)*100).toFixed(1);
    const ciL = ((s.ci_lower||0)*100).toFixed(0);
    const ciU = ((s.ci_upper||1)*100).toFixed(0);
    const pnl = s.total_pnl || 0;
    const pnlCls = pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : '';

    $('#regimeDetailTitle').innerHTML = riskTag(s.risk_level) + ' ' + label.replace(/_/g, ' ');

    let html = '';

    // Overview stats
    html += `<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:12px">
      <div class="stat"><div class="label">Win Rate</div><div class="val" style="font-size:20px">${wr}%</div></div>
      <div class="stat"><div class="label">Trades</div><div class="val">${s.total_trades||0}</div></div>
      <div class="stat"><div class="label">P&L</div><div class="val ${pnlCls}">${pnl>=0?'+':''}$${pnl.toFixed(2)}</div></div>
    </div>`;

    // Detail grid
    html += `<div style="display:grid;grid-template-columns:1fr 1fr;gap:3px 12px;font-size:12px;color:var(--dim);margin-bottom:12px">
      <div>Wins: <strong class="pos">${s.wins||0}</strong></div>
      <div>Losses: <strong class="neg">${s.losses||0}</strong></div>
      <div>CI: <strong>${ciL}–${ciU}%</strong></div>
      <div>Avg P&L: <strong>${(s.avg_pnl||0)>=0?'+':''}$${(s.avg_pnl||0).toFixed(2)}</strong></div>
      <div>Avg Entry: <strong>${avgs.avg_entry ? Math.round(avgs.avg_entry)+'¢' : '—'}</strong></div>
      <div>Avg Sell: <strong>${avgs.avg_sell ? Math.round(avgs.avg_sell)+'¢' : '—'}</strong></div>
      <div>Avg HWM: <strong>${avgs.avg_hwm ? Math.round(avgs.avg_hwm)+'¢' : '—'}</strong></div>
      <div>Best: <strong class="pos">${avgs.best_pnl!=null ? '+$'+avgs.best_pnl.toFixed(2) : '—'}</strong></div>
      <div>Worst: <strong class="neg">${avgs.worst_pnl!=null ? '$'+avgs.worst_pnl.toFixed(2) : '—'}</strong></div>
    </div>`;

    // Side breakdown
    if (d.sides && d.sides.length) {
      html += `<div style="border-top:1px solid var(--border);padding-top:8px;margin-bottom:10px">
        <div class="dim" style="font-size:11px;font-weight:600;margin-bottom:4px">BY SIDE</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">`;
      for (const sd of d.sides) {
        const swr = sd.n > 0 ? ((sd.wins/sd.n)*100).toFixed(0) : '—';
        const sCls = sd.side === 'yes' ? 'side-yes' : 'side-no';
        const sPnlCls = (sd.pnl||0) > 0 ? 'pos' : (sd.pnl||0) < 0 ? 'neg' : '';
        html += `<div style="background:var(--card);padding:6px;border-radius:4px">
          <span class="${sCls}" style="font-weight:600">${(sd.side||'').toUpperCase()}</span>
          <span class="dim" style="font-size:11px"> ${sd.n} trades · ${swr}% · <span class="${sPnlCls}">${(sd.pnl||0)>=0?'+':''}$${(sd.pnl||0).toFixed(2)}</span></span>
        </div>`;
      }
      html += `</div></div>`;
    }

    // Round breakdown
    if (d.rounds && d.rounds.length) {
      html += `<div style="border-top:1px solid var(--border);padding-top:8px;margin-bottom:10px">
        <div class="dim" style="font-size:11px;font-weight:600;margin-bottom:4px">BY ROUND</div>
        <table class="proj-table" style="width:100%">
          <thead><tr><th>Round</th><th>Trades</th><th>Wins</th><th>Win%</th><th>P&L</th></tr></thead>
          <tbody>`;
      for (const rd of d.rounds) {
        const rwr = rd.n > 0 ? ((rd.wins/rd.n)*100).toFixed(0) : '0';
        const rPnlCls = (rd.pnl||0) > 0 ? 'pos' : (rd.pnl||0) < 0 ? 'neg' : '';
        html += `<tr>
          <td>R${rd.cycle_round||1}</td>
          <td>${rd.n}</td>
          <td>${rd.wins||0}</td>
          <td>${rwr}%</td>
          <td class="${rPnlCls}">${(rd.pnl||0)>=0?'+':''}$${(rd.pnl||0).toFixed(2)}</td>
        </tr>`;
      }
      html += `</tbody></table></div>`;
    }

    // Recent trades
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
            <span class="dim"> R${t.cycle_round||1}</span>
          </div>
          <div>
            <span class="${tCls}">${tPnl>=0?'+':''}$${tPnl.toFixed(2)}</span>
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

async function loadRegimeWorkerStatus() {
  try {
    const s = await api('/api/regime_status');
    const el = $('#regimeWorkerStatus');

    function row(label, val, cls) {
      return `<div class="stat-row"><span class="sr-label">${label}</span><span class="sr-val ${cls||''}">${val}</span></div>`;
    }

    let html = '';
    html += row('Regime Snapshots', s.snapshot_count || 0);

    if (s.avg_snapshot_interval_s) {
      html += row('Snapshot Interval', `~${Math.round(s.avg_snapshot_interval_s/60)}m`);
    }

    html += row('Baselines Computed', s.baseline_count || 0);
    html += row('Regime Labels Tracked', s.regime_labels_tracked || 0);

    const snap = s.latest_snapshot;
    if (snap) {
      html += '<div class="stat-section-title" style="margin-top:8px">Latest Snapshot</div>';
      html += row('Time', snap.captured_ct || '—');
      html += row('BTC', snap.btc_price ? '$' + Math.round(snap.btc_price).toLocaleString() : '—');
      html += row('Regime', (snap.composite_label || '—').replace(/_/g,' '));
      html += row('Confidence', ((snap.regime_confidence||0)*100).toFixed(0) + '%');
      html += `<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:2px 8px;font-size:11px;color:var(--dim);margin-top:4px">
        <div>Vol: <strong style="color:var(--text)">${snap.vol_regime||'?'}/5</strong></div>
        <div>Trend: <strong style="color:var(--text)">${trendLabel(snap.trend_regime)}</strong></div>
        <div>Volume: <strong style="color:var(--text)">${snap.volume_regime||'?'}/5</strong></div>
      </div>`;
      if (snap.btc_return_15m != null) {
        const r15 = snap.btc_return_15m;
        const r1h = snap.btc_return_1h;
        html += `<div style="font-size:11px;color:var(--dim);margin-top:4px">
          15m: <span class="${r15>=0?'pos':'neg'}">${r15>=0?'+':''}${r15.toFixed(3)}%</span>
          ${r1h != null ? ` · 1h: <span class="${r1h>=0?'pos':'neg'}">${r1h>=0?'+':''}${r1h.toFixed(3)}%</span>` : ''}
        </div>`;
      }
    }

    el.innerHTML = html;
  } catch(e) { console.error('Regime status error:', e); }
}

async function loadLifetimeStats() {
  // If the modal is open, refresh will show; if not, data is cached for next open
  await _loadLifetimeStatsInner();
}
function openLifetimeModal() {
  openModal('lifetimeModal');
  _loadLifetimeStatsInner();
}
async function _loadLifetimeStatsInner() {
  try {
    const s = await api('/api/lifetime');
    const el = $('#lifetimeStats');

    const w = s.wins || 0, l = s.losses || 0, total = w + l;
    const wr = total > 0 ? (w/total*100).toFixed(1) : '0';
    const pnl = s.total_pnl || 0;
    cachedLifetimePnl = pnl;
    const pnlCls = pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : '';

    function row(label, val, cls) {
      return `<div class="stat-row"><span class="sr-label">${label}</span><span class="sr-val ${cls||''}">${val}</span></div>`;
    }

    let html = '';

    // Record
    html += '<div class="stat-section-title">Record</div>';
    html += row('Record', `${w}W – ${l}L` + (s.cashouts ? ` – ${s.cashouts} cashout` : ''));
    html += row('Win Rate', wr + '%', w > l ? 'pos' : l > w ? 'neg' : '');
    html += row('Trades Placed', total + (s.skips ? ` (+${s.skips} skipped)` : ''));
    if (s.data_bets) html += row('Data Bets', s.data_bets);

    // Streaks
    html += '<div class="stat-section-title">Streaks</div>';
    html += row('Best Win Streak', s.best_win_streak || 0, 'pos');
    html += row('Worst Loss Streak', s.worst_loss_streak || 0, 'neg');
    if (s.current_streak_type) {
      const stCls = s.current_streak_type === 'win' ? 'pos' : 'neg';
      const stLabel = s.current_streak_type === 'win' ? 'W' : 'L';
      html += row('Current Streak', s.current_streak_len + stLabel, stCls);
    }

    // Money
    html += '<div class="stat-section-title">Money</div>';
    html += row('Total P&L', (pnl>=0?'+':'') + '$' + pnl.toFixed(2), pnlCls);
    html += row('Total Wagered', '$' + (s.total_wagered||0).toFixed(2));
    html += row('Total Fees Paid', '$' + (s.total_fees||0).toFixed(2), 'neg');
    html += row('ROI', (s.roi_pct||0) + '%', s.roi_pct > 0 ? 'pos' : s.roi_pct < 0 ? 'neg' : '');
    html += row('Profit Factor', s.profit_factor || '—', s.profit_factor > 1 ? 'pos' : 'neg');

    // Best/worst
    html += '<div class="stat-section-title">Extremes</div>';
    const fmtPnl = (v) => {
      if (v >= 0) return '+$' + v.toFixed(2);
      return '-$' + Math.abs(v).toFixed(2);
    };
    const bt = s.best_trade_pnl || 0;
    const wt = s.worst_trade_pnl || 0;
    html += row('Best Trade', fmtPnl(bt), bt >= 0 ? 'pos' : 'neg');
    html += row('Worst Trade', fmtPnl(wt), wt >= 0 ? 'pos' : 'neg');
    html += row('Avg Win', fmtPnl(s.avg_win_pnl||0), 'pos');
    html += row('Avg Loss', fmtPnl(s.avg_loss_pnl||0), 'neg');
    html += row('Peak P&L', fmtPnl(s.peak_pnl||0), (s.peak_pnl||0) >= 0 ? 'pos' : 'neg');
    html += row('Max Drawdown', '-$' + (s.max_drawdown||0).toFixed(2), 'neg');

    // Cycles
    html += '<div class="stat-section-title">Cycles</div>';
    html += row('Cycles Won', s.cycles_won || 0, 'pos');
    html += row('Max-Loss Resets', s.cycles_lost || 0, 'neg');

    el.innerHTML = html;

    // Round breakdown
    const rb = s.round_breakdown || [];
    if (rb.length > 0) {
      const rbEl = $('#roundBreakdown');
      rbEl.innerHTML = '<table class="proj-table"><thead><tr>' +
        '<th>Round</th><th>W</th><th>L</th><th>Win%</th><th>Net</th></tr></thead><tbody>' +
        rb.map(r => {
          const rt = (r.wins||0) + (r.losses||0);
          const rwr = rt > 0 ? ((r.wins||0)/rt*100).toFixed(0) : '—';
          const rpnl = r.net_pnl || 0;
          return `<tr>
            <td>R${r.round}</td>
            <td class="pos">${r.wins||0}</td>
            <td class="neg">${r.losses||0}</td>
            <td>${rwr}%</td>
            <td class="${rpnl>=0?'pos':'neg'}">${rpnl>=0?'+':''}$${rpnl.toFixed(2)}</td>
          </tr>`;
        }).join('') + '</tbody></table>';
    }

    // Daily P&L
    const dp = s.daily_pnl || [];
    if (dp.length > 0) {
      const dpEl = $('#dailyPnl');
      dpEl.innerHTML = dp.map(d => {
        const dpnl = d.pnl || 0;
        const cls = dpnl > 0 ? 'pos' : dpnl < 0 ? 'neg' : '';
        return `<div class="stat-row">
          <span class="sr-label">${d.day} · ${d.wins||0}W/${d.losses||0}L</span>
          <span class="sr-val ${cls}">${dpnl>=0?'+':''}$${dpnl.toFixed(2)}</span>
        </div>`;
      }).join('');
    }

    // Entry delay breakdown
    const db2 = s.delay_breakdown || [];
    if (db2.length > 0) {
      const dbEl = $('#delayBreakdown');
      dbEl.innerHTML = '<table class="proj-table"><thead><tr>' +
        '<th>Delay</th><th>W</th><th>L</th><th>Win%</th><th>Net</th></tr></thead><tbody>' +
        db2.map(r => {
          const rt = (r.wins||0) + (r.losses||0);
          const rwr = rt > 0 ? ((r.wins||0)/rt*100).toFixed(0) : '—';
          const rpnl = r.net_pnl || 0;
          return `<tr>
            <td>${r.delay_min}m</td>
            <td class="pos">${r.wins||0}</td>
            <td class="neg">${r.losses||0}</td>
            <td>${rwr}%</td>
            <td class="${rpnl>=0?'pos':'neg'}">${rpnl>=0?'+':''}$${rpnl.toFixed(2)}</td>
          </tr>`;
        }).join('') + '</tbody></table>';
    }

    // Price stability breakdown
    const sb = s.stability_breakdown || [];
    if (sb.length > 0) {
      const sbEl = $('#stabilityBreakdown');
      sbEl.innerHTML = '<table class="proj-table"><thead><tr>' +
        '<th>Stability</th><th>W</th><th>L</th><th>Win%</th><th>Net</th></tr></thead><tbody>' +
        sb.filter(r => r.stability_bucket !== 'N/A').map(r => {
          const rt = (r.wins||0) + (r.losses||0);
          const rwr = rt > 0 ? ((r.wins||0)/rt*100).toFixed(0) : '—';
          const rpnl = r.net_pnl || 0;
          return `<tr>
            <td>${r.stability_bucket}</td>
            <td class="pos">${r.wins||0}</td>
            <td class="neg">${r.losses||0}</td>
            <td>${rwr}%</td>
            <td class="${rpnl>=0?'pos':'neg'}">${rpnl>=0?'+':''}$${rpnl.toFixed(2)}</td>
          </tr>`;
        }).join('') + '</tbody></table>';
    }

  } catch(e) { console.error('Lifetime stats error:', e); }
}

// ── AI Chat ──────────────────────────────────────────────
function openChatModal() {
  openModal('chatModal');
  setTimeout(() => $('#chatInput').focus(), 100);
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

    const resp = await fetch('/api/deploy/upload', {method: 'POST', body: form});
    const data = await resp.json();

    let html = '';
    if (data.uploaded && data.uploaded.length) {
      html += `<div style="color:var(--green)">Uploaded: ${data.uploaded.join(', ')}</div>`;
    }
    if (data.errors && data.errors.length) {
      html += data.errors.map(e => `<div style="color:var(--red)">${e}</div>`).join('');
    }

    if (data.uploaded && data.uploaded.length && (!data.errors || !data.errors.length)) {
      html += '<span style="color:var(--blue)">Restarting services...</span>';
      status.innerHTML = html;

      // Auto-restart — dashboard restarts last so we see the bot restart
      const restartResp = await api('/api/deploy/restart', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({services: ['kalshi-bot', 'kalshi-dashboard']})
      });
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
  status.innerHTML = '<span style="color:var(--blue)">Restarting services...</span>';

  try {
    const resp = await api('/api/deploy/restart', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({services: ['kalshi-bot', 'kalshi-dashboard']})
    });
    status.innerHTML = '<div style="color:var(--green)">Restarted. Page will reload...</div>';
    setTimeout(() => location.reload(), 3000);
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

// Paste code char counter
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
        headers: {'Content-Type': 'application/json'},
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
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({services: ['kalshi-bot', 'kalshi-dashboard']})
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

function clearChat() {
  const container = $('#chatMessages');
  const welcome = document.getElementById('chatWelcome');
  container.innerHTML = '';
  // Recreate welcome
  container.innerHTML = `<div id="chatWelcome" style="text-align:center;padding:30px 10px">
    <svg width="40" height="40" viewBox="0 0 24 24" fill="var(--border)" stroke="none" style="margin-bottom:12px"><path d="M4.913 2.658c2.075-.27 4.19-.408 6.337-.408 2.147 0 4.262.139 6.337.408 1.922.25 3.291 1.861 3.405 3.727a4.403 4.403 0 0 0-1.032-.211 50.89 50.89 0 0 0-8.42 0c-2.358.196-4.04 2.19-4.04 4.434v4.286a4.47 4.47 0 0 0 2.433 3.984L7.28 21.53A.75.75 0 0 1 6 20.97v-1.95a49.99 49.99 0 0 1-1.087-.128C2.905 18.636 1.5 17.09 1.5 15.27V5.885c0-1.866 1.37-3.477 3.413-3.227ZM15.75 7.5c-1.376 0-2.739.057-4.086.169C10.124 7.797 9 9.103 9 10.609v4.285c0 1.507 1.128 2.814 2.67 2.94 1.243.102 2.5.157 3.768.165l2.782 2.781a.75.75 0 0 0 1.28-.53v-2.39l.33-.026c1.542-.125 2.67-1.433 2.67-2.94v-4.286c0-1.505-1.125-2.811-2.664-2.94A49.392 49.392 0 0 0 15.75 7.5Z"/></svg>
    <div style="color:var(--text);font-size:14px;font-weight:600;margin-bottom:6px">Ask anything about your bot</div>
    <div class="dim" style="font-size:12px;line-height:1.5">Trades, regimes, strategy, settings — I have access to all your live data.</div>
    <div style="display:flex;flex-wrap:wrap;gap:6px;justify-content:center;margin-top:16px">
      <button class="chat-chip" onclick="askSuggestion(this)">What's my best regime?</button>
      <button class="chat-chip" onclick="askSuggestion(this)">Summarize today's performance</button>
      <button class="chat-chip" onclick="askSuggestion(this)">Am I profitable overall?</button>
      <button class="chat-chip" onclick="askSuggestion(this)">Which regimes should I skip?</button>
    </div>
  </div>`;
}

function askSuggestion(btn) {
  $('#chatInput').value = btn.textContent;
  sendChat();
}

function formatChatResponse(text) {
  let html = escHtml(text);
  // Bold
  html = html.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
  // Bullet lists: lines starting with - or •
  html = html.replace(/^[-•]\s+(.+)$/gm, '<div style="padding-left:12px;margin:2px 0">• $1</div>');
  // Numbered lists
  html = html.replace(/^(\d+)\.\s+(.+)$/gm, '<div style="padding-left:12px;margin:2px 0">$1. $2</div>');
  // Paragraphs (double newline)
  html = html.replace(/\n\n/g, '</p><p style="margin:8px 0">');
  // Single newlines
  html = html.replace(/\n/g, '<br>');
  // Inline code
  html = html.replace(/`([^`]+)`/g, '<code style="background:rgba(255,255,255,0.06);padding:1px 4px;border-radius:3px;font-size:12px">$1</code>');
  return '<p style="margin:0">' + html + '</p>';
}

let _chatSending = false;

async function sendChat() {
  if (_chatSending) return;
  const input = $('#chatInput');
  const msg = input.value.trim();
  if (!msg) return;

  _chatSending = true;
  const container = $('#chatMessages');
  const sendBtn = $('#chatSendBtn');

  // Hide welcome
  const welcome = document.getElementById('chatWelcome');
  if (welcome) welcome.remove();

  // User bubble
  container.innerHTML += `<div class="chat-msg chat-user"><div>${escHtml(msg)}</div></div>`;
  input.value = '';

  // Disable send
  sendBtn.style.opacity = '0.4';
  sendBtn.style.pointerEvents = 'none';

  // Thinking bubble with animated dots
  const thinkId = 'think_' + Date.now();
  container.innerHTML += `<div class="chat-msg chat-thinking" id="${thinkId}"><div><span class="chat-dot"></span><span class="chat-dot"></span><span class="chat-dot"></span></div></div>`;
  container.scrollTop = container.scrollHeight;

  try {
    const resp = await api('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: msg}),
    });

    const thinkEl = document.getElementById(thinkId);
    if (resp.error) {
      if (thinkEl) { thinkEl.className = 'chat-msg chat-err'; thinkEl.innerHTML = `<div>${escHtml(resp.error)}</div>`; }
    } else {
      const html = formatChatResponse(resp.response);
      if (thinkEl) { thinkEl.className = 'chat-msg chat-ai'; thinkEl.innerHTML = `<div>${html}</div>`; }
    }
  } catch(e) {
    const thinkEl = document.getElementById(thinkId);
    if (thinkEl) { thinkEl.className = 'chat-msg chat-err'; thinkEl.innerHTML = `<div>${escHtml(e.toString())}</div>`; }
  }

  sendBtn.style.opacity = '';
  sendBtn.style.pointerEvents = '';
  _chatSending = false;
  container.scrollTop = container.scrollHeight;
}

function escHtml(s) { return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/'/g,'&#39;'); }

// ── Init ─────────────────────────────────────────────────
function openTradesModal() {
  openModal('tradesModal');
  loadTrades();
}

// Backfill live price chart from server history
async function loadLivePriceHistory() {
  try {
    const prices = await api('/api/live_prices');
    if (!prices || !prices.length) return;
    const currentTicker = prices[prices.length - 1].ticker;
    // Only backfill if buffer is empty or different ticker
    if (_livePriceBuf.data.length > 5 && _livePriceBuf.ticker === currentTicker) return;
    const backfill = [];
    for (const p of prices) {
      if (p.ticker !== currentTicker) continue;
      const ts = new Date(p.ts).getTime();
      backfill.push({ts, ya: p.yes_ask || 0, na: p.no_ask || 0,
        yb: p.yes_bid || 0, nb: p.no_bid || 0});
    }
    // Merge: backfill first, then any existing poll data
    const existing = _livePriceBuf.ticker === currentTicker ? _livePriceBuf.data : [];
    const lastBackfillTs = backfill.length ? backfill[backfill.length - 1].ts : 0;
    const newPollData = existing.filter(d => d.ts > lastBackfillTs);
    _livePriceBuf = {ticker: currentTicker, data: [...backfill, ...newPollData], closeTime: _livePriceBuf.closeTime};
  } catch(e) { console.error('Live price backfill error:', e); }
}

loadLivePriceHistory();
loadConfig();
loadTrades();
loadRegimes();
loadLifetimeStats();
loadRegimeWorkerStatus();
pollState();

// Dynamic poll rate using setTimeout chains (not setInterval — avoids race conditions)
let _pollRate = 1000;
function schedulePoll() {
  setTimeout(async () => {
    await pollState();
    // Recalculate rate after each poll
    const fast = document.getElementById('fastUpdates');
    const isFast = fast && fast.checked;
    const s = _uiState;
    const cashingOut = s.cashing_out;
    const autoOn = s.auto_trading;
    const hasActive = !!s.active_trade;
    const hasPending = !!s.pending_trade;
    if (cashingOut) _pollRate = 500;
    else if (autoOn || hasPending) _pollRate = 1000;
    else if (hasActive && isFast) _pollRate = 1000;
    else if (hasActive) _pollRate = 2000;
    else _pollRate = 1000;
    schedulePoll();
  }, _pollRate);
}
schedulePoll();

setInterval(loadTrades, 15000);
setInterval(loadRegimes, 30000);
setInterval(loadLifetimeStats, 30000);
setInterval(loadRegimeWorkerStatus, 30000);
// Light config sync — just update derived values like currentLocked without touching inputs
setInterval(async () => {
  try {
    const cfg = await api('/api/config');
    currentLocked = parseFloat(cfg.locked_bankroll) || 0;
  } catch(e) {}
}, 10000);

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
      headers: {'Content-Type': 'application/json'},
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
        headers: {'Content-Type': 'application/json'},
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
  body { font-family: 'SF Mono', 'Menlo', monospace; background: var(--bg);
         color: var(--text); font-size: 12px; }
  .header { position: sticky; top: 0; background: var(--card); border-bottom: 1px solid var(--border);
            padding: 10px 14px; display: flex; justify-content: space-between; align-items: center;
            z-index: 10; gap: 8px; flex-wrap: wrap; }
  .header a { color: var(--blue); text-decoration: none; font-family: sans-serif; font-size: 14px; }
  .header .count { color: var(--dim); font-family: sans-serif; font-size: 12px; }
  .header-btn { background: var(--border); color: var(--text); border: none; padding: 4px 10px;
                border-radius: 4px; cursor: pointer; font-size: 12px; font-family: sans-serif; }
  .header-btn:active { background: var(--blue); color: #000; }
  #logContainer { padding: 8px; }
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
  .toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
           background: var(--green); color: #000; padding: 8px 16px; border-radius: 6px;
           font-family: sans-serif; font-size: 14px; font-weight: 600;
           opacity: 0; transition: opacity 0.3s; z-index: 100; }
  .toast.show { opacity: 1; }
  .filter-bar { position: sticky; top: 44px; background: var(--bg); padding: 6px 14px;
                border-bottom: 1px solid var(--border); z-index: 9; display: flex;
                gap: 8px; align-items: center; font-family: sans-serif; font-size: 12px; }
  .filter-btn { background: none; border: 1px solid var(--border); color: var(--dim);
                padding: 2px 8px; border-radius: 3px; cursor: pointer; font-size: 11px; }
  .filter-btn.active { border-color: var(--blue); color: var(--blue); }
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
</div>

<div id="loadMore">
  <button onclick="loadOlder()">Load Older</button>
</div>
<div id="logContainer"></div>

<div class="toast" id="toast"></div>

<script>
let oldestId = null;
let newestId = 0;
const container = document.getElementById('logContainer');
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
  } else if (filter === 'trade') {
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
        msg.includes('Sell placed') || msg.includes('Skipped') ||
        msg.includes('Cash out')) ? '' : 'none';
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
    const scrollBefore = document.body.scrollHeight;
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
    // Leave visible for manual copy
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
  window.scrollTo(0, document.body.scrollHeight);
}

async function loadOlder() {
  if (!oldestId) return;
  const levelParam = (currentFilter === 'ERROR' || currentFilter === 'WARNING') ? `&level=${currentFilter}` : '';
  const logs = await (await fetch(`/api/logs?before=${oldestId}&limit=200${levelParam}`)).json();
  logs.reverse();
  const scrollBefore = document.body.scrollHeight;
  for (const l of logs) {
    container.insertBefore(renderLine(l), container.firstChild);
    allLogs.unshift(l);
    if (l.id < oldestId) oldestId = l.id;
  }
  totalCount += logs.length;
  countEl.textContent = totalCount;
  window.scrollTo(0, document.body.scrollHeight - scrollBefore + window.scrollY);
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
      const atBottom = (window.innerHeight + window.scrollY) >= document.body.scrollHeight - 100;
      if (atBottom) window.scrollTo(0, document.body.scrollHeight);
    }
  } catch(e) {}
}

loadInitial();
setInterval(pollNew, 2000);
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def main():
    init_db()
    print(f"Dashboard starting on {DASHBOARD_HOST}:{DASHBOARD_PORT}")
    app.run(host=DASHBOARD_HOST, port=DASHBOARD_PORT, debug=False)


if __name__ == "__main__":
    main()
