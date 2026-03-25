"""
dashboard.py — BTC 15-minute plugin dashboard components.
Implements render methods and API routes for the BTC 15m plugin.

Ported from legacy/dashboard.py — faithfully preserving all HTML/CSS/JS.
"""
import json
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from flask import request, jsonify

# ── Imports from platform ──
import sys
import os

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
from db import (
    get_conn,
    get_config,
    set_config,
    get_all_config,
    get_plugin_state,
    rows_to_list,
    row_to_dict,
    now_utc,
)
from config import CT, ET

# ── Imports from plugin ──
from plugins.btc_15m import market_db


# ═══════════════════════════════════════════════════════════════
#  ROUTE REGISTRATION
# ═══════════════════════════════════════════════════════════════


def register_routes(app):
    """Register all /api/btc_15m/* routes."""

    # Import auth decorator and helpers from dashboard
    from dashboard import requires_auth, to_central, fpnl

    # ── Plugin state ──────────────────────────────────────

    @app.route("/api/btc_15m/state")
    @requires_auth
    def btc15m_state():
        state = get_plugin_state("btc_15m")
        # Inject trading_mode from config into state dict for UI
        s = state.get("state", {})
        if "trading_mode" not in s:
            s["trading_mode"] = get_config("btc_15m.trading_mode", "observe")
            state["state"] = s
        return jsonify(state)

    # ── Trades (server-side filtered, paginated) ──────────

    @app.route("/api/btc_15m/trades")
    @requires_auth
    def btc15m_trades():
        """Server-side filtered, paginated trades with aggregate stats.
        Ported from legacy /api/trades_v2."""
        filters = request.args.get("filters", "all")
        offset = request.args.get("offset", 0, type=int)
        limit = request.args.get("limit", 30, type=int)
        regime = request.args.get("regime", "")

        where_parts = []
        params = []

        filter_list = [f.strip() for f in filters.split(",") if f.strip()]
        if "all" not in filter_list and filter_list:
            conditions = []
            for f in filter_list:
                if f == "win":
                    conditions.append("outcome = 'win'")
                elif f == "loss":
                    conditions.append("outcome = 'loss'")
                elif f == "cashed_out":
                    conditions.append("outcome = 'cashed_out'")
                elif f == "skipped":
                    conditions.append(
                        "outcome IN ('skipped', 'no_fill')"
                    )
                elif f == "error":
                    conditions.append("outcome = 'error'")
                elif f == "incomplete":
                    conditions.append(
                        "outcome = 'skipped' AND market_result IS NULL"
                    )
                elif f == "ignored":
                    conditions.append("COALESCE(is_data_collection, 0) = 1")
                elif f == "shadow":
                    conditions.append("COALESCE(is_shadow, 0) = 1")
                elif f == "yes":
                    conditions.append(
                        "side = 'yes' AND outcome IN ('win','loss','cashed_out','open')"
                    )
                elif f == "no":
                    conditions.append(
                        "side = 'no' AND outcome IN ('win','loss','cashed_out','open')"
                    )
            if conditions:
                where_parts.append("(" + " OR ".join(conditions) + ")")

        if regime:
            where_parts.append("regime_label = ?")
            params.append(regime)

        where_parts.append("outcome != 'open'")
        where_sql = " AND ".join(where_parts) if where_parts else "1=1"

        with get_conn() as c:
            # Stats
            stats_row = c.execute(
                f"""
                SELECT COUNT(*) as total,
                    SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                    SUM(CASE WHEN outcome IN ('skipped','no_fill') THEN 1 ELSE 0 END) as skips,
                    SUM(CASE WHEN outcome='cashed_out' THEN 1 ELSE 0 END) as cashouts,
                    SUM(CASE WHEN outcome='error' THEN 1 ELSE 0 END) as errors,
                    SUM(CASE WHEN outcome IN ('win','loss','cashed_out') THEN COALESCE(pnl,0) ELSE 0 END) as pnl,
                    MAX(CASE WHEN outcome IN ('win','loss','cashed_out') THEN pnl END) as best,
                    MIN(CASE WHEN outcome IN ('win','loss','cashed_out') THEN pnl END) as worst
                FROM btc15m_trades WHERE {where_sql}
            """,
                params,
            ).fetchone()
            stats = {
                "total": stats_row["total"] or 0,
                "wins": stats_row["wins"] or 0,
                "losses": stats_row["losses"] or 0,
                "skips": stats_row["skips"] or 0,
                "cashouts": stats_row["cashouts"] or 0,
                "errors": stats_row["errors"] or 0,
                "pnl": round(stats_row["pnl"] or 0, 2),
                "best": round(stats_row["best"] or 0, 2),
                "worst": round(stats_row["worst"] or 0, 2),
            }
            real = stats["wins"] + stats["losses"]
            stats["win_rate"] = (
                round(stats["wins"] / real * 100, 1) if real > 0 else 0
            )

            # Paginated trades
            rows = c.execute(
                f"""
                SELECT * FROM btc15m_trades WHERE {where_sql}
                ORDER BY created_at DESC LIMIT ? OFFSET ?
            """,
                params + [limit, offset],
            ).fetchall()
            trades = rows_to_list(rows)

            # Distinct regimes
            regimes = c.execute(
                """
                SELECT DISTINCT regime_label FROM btc15m_trades
                WHERE regime_label IS NOT NULL AND outcome != 'open'
                ORDER BY regime_label
            """
            ).fetchall()
            regime_list = [r["regime_label"] for r in regimes]

        for t in trades:
            t["created_ct"] = to_central(t.get("created_at", ""))
            if t.get("entry_time_utc"):
                t["entry_ct"] = to_central(t["entry_time_utc"])
            if t.get("exit_time_utc"):
                t["exit_ct"] = to_central(t["exit_time_utc"])

        return jsonify(
            {
                "trades": trades,
                "stats": stats,
                "regimes": regime_list,
                "has_more": len(trades) == limit,
            }
        )

    # ── Single trade detail ───────────────────────────────

    @app.route("/api/btc_15m/trade/<int:trade_id>/detail")
    @requires_auth
    def btc15m_trade_detail(trade_id):
        trade = market_db.get_trade(trade_id)
        if not trade:
            return jsonify({"error": "Not found"}), 404
        trade["created_ct"] = to_central(trade.get("created_at", ""))
        if trade.get("entry_time_utc"):
            trade["entry_ct"] = to_central(trade["entry_time_utc"])
        if trade.get("exit_time_utc"):
            trade["exit_ct"] = to_central(trade["exit_time_utc"])
        price_path = market_db.get_price_path(trade_id)
        return jsonify({"trade": trade, "price_path": price_path})

    # ── Trade delete ──────────────────────────────────────

    @app.route("/api/btc_15m/trade/<int:trade_id>/delete", methods=["POST"])
    @requires_auth
    def btc15m_trade_delete(trade_id):
        try:
            deleted = market_db.delete_trades([trade_id])
            market_db.recompute_all_stats()
            return jsonify({"ok": True, "deleted": deleted})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    # ── Delete incomplete trades ──────────────────────────

    @app.route("/api/btc_15m/trades/delete_incomplete", methods=["POST"])
    @requires_auth
    def btc15m_delete_incomplete():
        try:
            with get_conn() as c:
                rows = c.execute(
                    """
                    SELECT id FROM btc15m_trades
                    WHERE outcome = 'skipped' AND market_result IS NULL
                """
                ).fetchall()
                ids = [r["id"] for r in rows]
            if ids:
                deleted = market_db.delete_trades(ids)
            else:
                deleted = 0
            return jsonify({"ok": True, "deleted": deleted})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    # ── Trade summary ─────────────────────────────────────

    @app.route("/api/btc_15m/trade_summary")
    @requires_auth
    def btc15m_trade_summary():
        return jsonify(market_db.get_trade_summary())

    # ── Trades CSV export ─────────────────────────────────

    @app.route("/api/btc_15m/trades/csv")
    @requires_auth
    def btc15m_trades_csv():
        """Export all trades as CSV."""
        import csv
        import io

        with get_conn() as c:
            rows = c.execute(
                """
                SELECT * FROM btc15m_trades
                WHERE outcome != 'open'
                ORDER BY created_at DESC
            """
            ).fetchall()
            trades = rows_to_list(rows)

        if not trades:
            return "No trades", 200

        output = io.StringIO()
        if trades:
            writer = csv.DictWriter(output, fieldnames=trades[0].keys())
            writer.writeheader()
            for t in trades:
                writer.writerow(t)

        from flask import Response as FlaskResponse

        return FlaskResponse(
            output.getvalue(),
            mimetype="text/csv",
            headers={
                "Content-Disposition": "attachment; filename=btc15m_trades.csv"
            },
        )

    # ── Observations ──────────────────────────────────────

    @app.route("/api/btc_15m/observations")
    @requires_auth
    def btc15m_observations():
        limit = request.args.get("limit", 50, type=int)
        with get_conn() as c:
            rows = c.execute(
                """
                SELECT * FROM btc15m_observations
                ORDER BY created_at DESC LIMIT ?
            """,
                (limit,),
            ).fetchall()
        return jsonify(rows_to_list(rows))

    @app.route("/api/btc_15m/observation_count")
    @requires_auth
    def btc15m_observation_count():
        return jsonify(market_db.get_observation_count())

    # ── Lifetime stats ────────────────────────────────────

    @app.route("/api/btc_15m/lifetime_stats")
    @requires_auth
    def btc15m_lifetime_stats():
        return jsonify(market_db.get_lifetime_stats())

    # ── Regime stats ──────────────────────────────────────

    @app.route("/api/btc_15m/regime_stats")
    @requires_auth
    def btc15m_regime_stats():
        return jsonify(market_db.get_all_regime_stats())

    # ── Hourly stats ──────────────────────────────────────

    @app.route("/api/btc_15m/hourly_stats")
    @requires_auth
    def btc15m_hourly_stats():
        return jsonify(market_db.get_all_hourly_stats())

    # ── Strategy results ──────────────────────────────────

    @app.route("/api/btc_15m/strategy_results")
    @requires_auth
    def btc15m_strategy_results():
        setup_type = request.args.get("setup_type", "global")
        setup_value = request.args.get("setup_value", "")
        with get_conn() as c:
            if setup_value:
                rows = c.execute(
                    """
                    SELECT * FROM btc15m_strategy_results
                    WHERE setup_type = ? AND setup_value = ?
                    ORDER BY tw_ev_c DESC
                """,
                    (setup_type, setup_value),
                ).fetchall()
            else:
                rows = c.execute(
                    """
                    SELECT * FROM btc15m_strategy_results
                    WHERE setup_type = ?
                    ORDER BY tw_ev_c DESC
                """,
                    (setup_type,),
                ).fetchall()
        return jsonify(rows_to_list(rows))

    # ── Recommendation ────────────────────────────────────

    @app.route("/api/btc_15m/recommendation")
    @requires_auth
    def btc15m_recommendation():
        try:
            from plugins.btc_15m.strategy import get_recommendation

            rec = get_recommendation()
            return jsonify(rec or {})
        except Exception as e:
            return jsonify({"error": str(e)})

    # ── Fair value edge ───────────────────────────────────

    @app.route("/api/btc_15m/fv_edge")
    @requires_auth
    def btc15m_fv_edge():
        try:
            from plugins.btc_15m.strategy import BtcFairValueModel

            model = BtcFairValueModel()
            state = get_plugin_state("btc_15m")
            s = state.get("state", {})
            live = s.get("live_market", {})
            if live and live.get("yes_ask") and live.get("close_time"):
                edge = model.compute_edge(
                    yes_ask=live["yes_ask"],
                    no_ask=live["no_ask"],
                    btc_price=live.get("btc_price", 0),
                    open_price=live.get("open_price", 0),
                    close_time=live["close_time"],
                )
                return jsonify(edge or {})
            return jsonify({})
        except Exception as e:
            return jsonify({"error": str(e)})

    # ── Price path for a trade ────────────────────────────

    @app.route("/api/btc_15m/price_path/<int:trade_id>")
    @requires_auth
    def btc15m_price_path(trade_id):
        data = market_db.get_price_path(trade_id)
        return jsonify(data)

    # ── Live prices ───────────────────────────────────────

    @app.route("/api/btc_15m/live_prices")
    @requires_auth
    def btc15m_live_prices():
        limit = request.args.get("limit", 500, type=int)
        data = market_db.get_live_prices(limit=limit)
        return jsonify(data)

    # ── Skip analysis ─────────────────────────────────────

    @app.route("/api/btc_15m/skip_analysis")
    @requires_auth
    def btc15m_skip_analysis():
        try:
            data = market_db.get_skipped_trades_needing_result()
            return jsonify(data)
        except Exception as e:
            return jsonify({"error": str(e)})

    # ── Shadow stats ──────────────────────────────────────

    @app.route("/api/btc_15m/shadow_stats")
    @requires_auth
    def btc15m_shadow_stats():
        with get_conn() as c:
            row = c.execute(
                """
                SELECT COUNT(*) as total,
                    SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                    SUM(CASE WHEN outcome IN ('win','loss') THEN COALESCE(pnl,0) ELSE 0 END) as pnl,
                    AVG(CASE WHEN spread_at_entry_c IS NOT NULL THEN spread_at_entry_c END) as avg_spread,
                    AVG(CASE WHEN shadow_fill_latency_ms IS NOT NULL THEN shadow_fill_latency_ms END) as avg_latency
                FROM btc15m_trades WHERE COALESCE(is_shadow, 0) = 1
            """
            ).fetchone()
        return jsonify(row_to_dict(row) or {})

    # ── Combined performance ──────────────────────────────

    @app.route("/api/btc_15m/performance")
    @requires_auth
    def btc15m_performance():
        stats = market_db.get_lifetime_stats()
        hourly = market_db.get_all_hourly_stats()
        return jsonify({"lifetime": stats, "hourly": hourly})

    # ── Feature importance ────────────────────────────────

    @app.route("/api/btc_15m/feature_importance")
    @requires_auth
    def btc15m_feature_importance():
        data = market_db.get_feature_importance()
        return jsonify(data)

    # ── BTC surface data ──────────────────────────────────

    @app.route("/api/btc_15m/surface_data")
    @requires_auth
    def btc15m_surface_data():
        data = market_db.get_btc_surface_data()
        return jsonify(data)

    # ── Regime worker status ──────────────────────────────

    @app.route("/api/btc_15m/regime_worker_status")
    @requires_auth
    def btc15m_regime_worker_status():
        from db import get_regime_heartbeat, is_regime_worker_running

        beat = get_regime_heartbeat("BTC")
        running = is_regime_worker_running("BTC")
        return jsonify({"heartbeat": beat, "running": running})

    # ── Walk-forward selection ────────────────────────────

    @app.route("/api/btc_15m/walkforward_selection")
    @requires_auth
    def btc15m_walkforward():
        with get_conn() as c:
            rows = c.execute(
                """
                SELECT * FROM btc15m_strategy_results
                WHERE oos_validated = 1 AND setup_type = 'global'
                ORDER BY tw_ev_c DESC LIMIT 20
            """
            ).fetchall()
        return jsonify(rows_to_list(rows))

    # ── Validation summary ────────────────────────────────

    @app.route("/api/btc_15m/validation_summary")
    @requires_auth
    def btc15m_validation_summary():
        with get_conn() as c:
            total = c.execute(
                "SELECT COUNT(*) as n FROM btc15m_strategy_results WHERE setup_type='global'"
            ).fetchone()["n"]
            validated = c.execute(
                "SELECT COUNT(*) as n FROM btc15m_strategy_results WHERE setup_type='global' AND oos_validated=1"
            ).fetchone()["n"]
            positive = c.execute(
                "SELECT COUNT(*) as n FROM btc15m_strategy_results WHERE setup_type='global' AND tw_ev_c > 0"
            ).fetchone()["n"]
        return jsonify(
            {"total": total, "validated": validated, "positive_ev": positive}
        )

    # ── Export analysis ───────────────────────────────────

    @app.route("/api/btc_15m/export_ai_analysis", methods=["POST"])
    @requires_auth
    def btc15m_export_analysis():
        """Export markdown analysis summary."""
        stats = market_db.get_lifetime_stats()
        regime_stats = market_db.get_all_regime_stats()
        obs_count = market_db.get_observation_count()
        md = "# BTC 15m Trading Analysis\n\n"
        md += "## Lifetime Stats\n"
        md += f"- Trades: {stats.get('trades_placed', 0)}\n"
        md += f"- Win Rate: {stats.get('win_rate_pct', 0):.1f}%\n"
        md += f"- Total P&L: ${stats.get('total_pnl', 0):.2f}\n"
        obs_n = obs_count.get("total", 0) if isinstance(obs_count, dict) else 0
        md += f"- Observations: {obs_n}\n\n"
        md += "## Regime Stats\n"
        for r in regime_stats:
            md += (
                f"- {r.get('regime_label', '?')}: "
                f"{r.get('total_trades', 0)} trades, "
                f"{r.get('win_rate_pct', 0):.0f}% WR, "
                f"${r.get('total_pnl', 0):.2f} P&L\n"
            )
        return jsonify({"markdown": md})

    # ── Regimes list (for Bitcoin tab) ────────────────────

    @app.route("/api/btc_15m/regimes")
    @requires_auth
    def btc15m_regimes():
        """Full regime list with trade stats, obs counts, best EV.
        Mirrors legacy /api/regimes."""
        data = market_db.get_all_regime_stats()
        return jsonify(data)

    # ── Regime CSV export ─────────────────────────────────

    @app.route("/api/btc_15m/regimes/csv")
    @requires_auth
    def btc15m_regimes_csv():
        import csv
        import io

        data = market_db.get_all_regime_stats()
        if not data:
            return "No regime data", 200
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=data[0].keys())
        writer.writeheader()
        for r in data:
            writer.writerow(r)

        from flask import Response as FlaskResponse

        return FlaskResponse(
            output.getvalue(),
            mimetype="text/csv",
            headers={
                "Content-Disposition": "attachment; filename=btc15m_regimes.csv"
            },
        )


# ═══════════════════════════════════════════════════════════════
#  RENDER METHODS — HTML/JS components injected into dashboard
# ═══════════════════════════════════════════════════════════════


def render_header_html():
    """Mode strip for this plugin in the sticky header.
    Ported from legacy dashboard header mode strip."""
    return r"""
<!-- BTC 15m Mode Strip -->
<div class="mode-strip" id="modeStrip">
  <div class="mode-btn" data-mode="observe" onclick="setTradingMode('observe')">
    <span class="mode-icon">&#9673;</span>Observe</div>
  <div class="mode-btn" data-mode="shadow" onclick="setTradingMode('shadow')">
    <span class="mode-icon">&#9672;</span>Shadow</div>
  <div class="mode-btn" data-mode="hybrid" onclick="setTradingMode('hybrid')">
    <span class="mode-icon">&#11041;</span>Hybrid</div>
  <div class="mode-btn" data-mode="auto" onclick="setTradingMode('auto')">
    <span class="mode-icon">&#9678;</span>Auto</div>
  <div class="mode-btn" data-mode="manual" onclick="setTradingMode('manual')">
    <span class="mode-icon">&#9635;</span>Manual</div>
</div>
"""


def render_home_card_html():
    """Home tab content: live market monitor, active trade, session stats.
    Ported from legacy pageHome (lines 5807-6093)."""
    return r"""
<!-- BTC 15m Home Card -->

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
  <div style="padding:0 16px">
    <div class="skel skel-line-sm" style="width:60px;margin-bottom:10px"></div>
    <div class="stat-grid" style="opacity:0.5">
      <div style="text-align:center"><div class="skel skel-line" style="width:80%;margin:0 auto"></div><div class="skel skel-line-sm" style="width:60%;margin:4px auto 0"></div></div>
      <div style="text-align:center"><div class="skel skel-line" style="width:80%;margin:0 auto"></div><div class="skel skel-line-sm" style="width:60%;margin:4px auto 0"></div></div>
      <div style="text-align:center"><div class="skel skel-line" style="width:80%;margin:0 auto"></div><div class="skel skel-line-sm" style="width:60%;margin:4px auto 0"></div></div>
      <div style="text-align:center"><div class="skel skel-line" style="width:80%;margin:0 auto"></div><div class="skel skel-line-sm" style="width:60%;margin:4px auto 0"></div></div>
    </div>
  </div>
</div>

<!-- Live Market Monitor (shown when NOT in active trade) -->
<div id="monitorCard" class="card" style="display:none;border-left:3px solid var(--blue)">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
    <div class="stat" style="flex:1;text-align:left">
      <div class="label">Market</div>
      <div class="val" id="monMarket" style="font-size:16px">&mdash;</div>
    </div>
    <div class="stat" style="text-align:right">
      <div class="label">Time Left</div>
      <div class="val" id="monTime" style="font-family:monospace">&mdash;</div>
    </div>
  </div>
  <!-- Prices -->
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
    <div style="text-align:center;padding:10px 8px;border-radius:8px;background:rgba(63,185,80,0.12);border:1px solid rgba(63,185,80,0.3)">
      <div class="dim" style="font-size:10px;margin-bottom:2px">YES</div>
      <div class="side-yes" style="font-size:22px;font-family:monospace" id="monYesAsk">&mdash;</div>
      <div class="dim" style="font-size:10px;margin-top:2px;font-family:monospace" id="monYesSpread"></div>
      <div style="font-size:9px;margin-top:1px;font-family:monospace;color:rgba(136,132,216,0.8);display:none" id="monYesFV"></div>
    </div>
    <div style="text-align:center;padding:10px 8px;border-radius:8px;background:rgba(248,81,73,0.12);border:1px solid rgba(248,81,73,0.3)">
      <div class="dim" style="font-size:10px;margin-bottom:2px">NO</div>
      <div class="side-no" style="font-size:22px;font-family:monospace" id="monNoAsk">&mdash;</div>
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
    <span class="regime-tag" id="monRisk">&mdash;</span>
    <span style="font-size:13px;font-weight:600" id="monRegimeLabel">&mdash;</span>
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
        <span style="font-size:9px;color:var(--dim)">(1 contract &middot; execution data)</span>
      </div>
    </div>
    <div class="grid2" style="gap:6px">
      <div class="stat" style="padding:4px"><div class="label">Side</div><div class="val" id="shadowSide" style="font-size:16px">&mdash;</div></div>
      <div class="stat" style="padding:4px"><div class="label">Fill</div><div class="val" id="shadowFill" style="font-size:16px">&mdash;</div></div>
    </div>
    <div class="grid2" style="gap:6px;margin-top:4px">
      <div class="stat" style="padding:4px"><div class="label">Slippage</div><div class="val" id="shadowSlip" style="font-size:16px">&mdash;</div></div>
      <div class="stat" style="padding:4px"><div class="label">Est P&amp;L</div><div class="val" id="shadowPnl" style="font-size:16px">&mdash;</div></div>
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
        <div class="stat" style="padding:4px"><div class="label">Side</div><div class="val" id="simSide" style="font-size:16px">&mdash;</div></div>
        <div class="stat" style="padding:4px"><div class="label">Entry</div><div class="val" id="simEntry" style="font-size:16px">&mdash;</div></div>
      </div>
      <div class="grid2" style="gap:6px;margin-top:4px">
        <div class="stat" style="padding:4px"><div class="label">Sell Target</div><div class="val" id="simSell" style="font-size:16px">&mdash;</div></div>
        <div class="stat" style="padding:4px"><div class="label">Est P&amp;L</div><div class="val" id="simPnl" style="font-size:16px">&mdash;</div></div>
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
      <div class="val" id="pendMarket" style="font-size:16px">&mdash;</div>
    </div>
    <div class="stat" style="text-align:right">
      <div class="label">Time Left</div>
      <div class="val" id="pendTime" style="font-family:monospace">&mdash;</div>
    </div>
  </div>
  <div class="grid2">
    <div class="stat">
      <div class="label">Side / Price</div>
      <div class="val" id="pendSide">&mdash;</div>
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
      <span class="dim">&cent;</span>
    </div>
    <div class="dim" style="font-size:11px;margin-top:4px" id="pendSellInfo"></div>
  </div>
</div>

<!-- Active Trade (shown when trading) -->
<div class="card trade-live" id="tradeCard" style="display:none">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
    <div class="stat" style="flex:1;text-align:left">
      <div class="label">Market</div>
      <div class="val" id="tradeMarket" style="font-size:16px">&mdash;</div>
    </div>
    <div class="stat" style="text-align:right">
      <div class="label">Time Left</div>
      <div class="val" id="tradeTime" style="font-family:monospace">&mdash;</div>
    </div>
  </div>
  <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
    <span class="regime-tag" id="tradeRisk">&mdash;</span>
    <span style="font-size:13px;font-weight:600" id="tradeRegimeLabel">&mdash;</span>
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
      <div class="val" id="tradeSide">&mdash;</div>
    </div>
    <div class="stat">
      <div class="label">Current Bid</div>
      <div class="val price-display" id="tradeBid">&mdash;</div>
    </div>
    <div class="stat">
      <div class="label">Sell Target</div>
      <div class="val" id="tradeSell">&mdash;</div>
    </div>
    <div class="stat">
      <div class="label">Cost</div>
      <div class="val" id="tradeCost">&mdash;</div>
    </div>
    <div class="stat">
      <div class="label">HWM</div>
      <div class="val" id="tradeHwm">&mdash;</div>
    </div>
    <div class="stat">
      <div class="label">Shares</div>
      <div class="val" id="tradeShares">&mdash;</div>
    </div>
  </div>
  <div style="margin:8px 0 4px;display:flex;align-items:center;gap:8px">
    <span class="dim" style="font-size:11px;white-space:nowrap">Win est.</span>
    <div style="flex:1;height:6px;background:var(--border);border-radius:3px;overflow:hidden;position:relative">
      <div id="winProbBar" style="height:100%;border-radius:3px;transition:width 0.5s,background 0.3s;width:0%"></div>
    </div>
    <span id="winProbPct" style="font-size:13px;font-weight:700;font-family:monospace;min-width:38px;text-align:right">&mdash;</span>
  </div>
  <div class="progress-bar">
    <div class="progress-fill" id="tradeProgress" style="width:0%;background:var(--blue)"></div>
  </div>
  <div class="dim" style="margin-top:4px">
    <span id="sellProgress">0/0 sold</span> &middot;
    Est. P&amp;L: <span id="tradeEstPnl">$0.00</span> &middot;
    <span class="dim" id="tradeSpread"></span>
  </div>
  <div class="dim" style="margin-top:2px;font-size:11px">
    <span id="tradeBankInfo"></span>
  </div>

  <span class="detail-toggle" onclick="toggleDetail('tradeDetail')">&#9656; More details</span>
  <div class="detail-section" id="tradeDetail">
    <div class="regime-detail-grid" id="tradeRegimeGrid"></div>
    <div class="grid2" style="font-size:12px;margin-top:8px;padding-top:8px;border-top:1px solid var(--border)">
    </div>
  </div>

  <!-- Stopping banner -->
  <div id="stoppingBanner" style="display:none;margin-top:8px;padding:8px;border-radius:6px;background:rgba(248,81,73,0.08);border:1px solid rgba(248,81,73,0.3);text-align:center;font-size:12px;color:var(--red)">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px;margin-right:4px"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>Stopped &mdash; trade kept as ignored, sell order active
  </div>

  <!-- Cash out (shown for ALL trades) -->
  <div id="cashOutSection" style="margin-top:12px">
    <button class="act-btn act-btn-red" id="cashOutBtn" onclick="showCashOut()">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M15.75 9V5.25A2.25 2.25 0 0 0 13.5 3h-6a2.25 2.25 0 0 0-2.25 2.25v13.5A2.25 2.25 0 0 0 7.5 21h6a2.25 2.25 0 0 0 2.25-2.25V15m3-3h-9m0 0 3-3m-3 3 3 3"/></svg>
      <span>Cash Out</span>
      <span id="cashOutEstimate" style="font-family:monospace;font-size:12px;margin-left:auto"></span>
    </button>
    <button class="act-btn act-btn-dim" id="cancelCashOutBtn" onclick="cancelCashOut()" style="display:none;margin-top:6px">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/></svg>
      Cancel Cash Out
    </button>
  </div>
</div>

<!-- Session Stats -->
<div style="padding:0 16px;margin-top:12px" id="sessionStatsSection">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
    <div class="dim" style="font-size:10px;font-weight:600;letter-spacing:0.5px">SESSION</div>
    <div style="display:flex;gap:6px">
      <button id="recoverSessionBtn" onclick="recoverSession()" style="display:none;background:none;border:1px solid rgba(88,166,255,0.3);border-radius:4px;color:var(--blue);font-size:9px;padding:2px 8px;cursor:pointer;-webkit-tap-highlight-color:transparent">Recover</button>
      <button onclick="resetSession()" style="background:none;border:1px solid var(--border);border-radius:4px;color:var(--dim);font-size:9px;padding:2px 8px;cursor:pointer;-webkit-tap-highlight-color:transparent">Reset</button>
    </div>
  </div>

  <div class="stat-grid">
    <div class="stat"><div class="label">Wins</div><div class="val pos" id="statWins">0</div></div>
    <div class="stat"><div class="label">Losses</div><div class="val neg" id="statLosses">0</div></div>
    <div class="stat"><div class="label">Win Rate</div><div class="val" id="statWinRate">&mdash;</div></div>
    <div class="stat"><div class="label">P&amp;L</div><div class="val" id="statSessionPnl">$0</div></div>
  </div>
  <div class="stat-grid">
    <div class="stat"><div class="label">W Streak</div><div class="val pos" id="statWinStreak">0</div></div>
    <div class="stat"><div class="label">L Streak</div><div class="val neg" id="statLossStreak">0</div></div>
    <div class="stat"><div class="label">Peak P&amp;L</div><div class="val" id="statPeakPnl">&mdash;</div></div>
    <div class="stat"><div class="label">Drawdown</div><div class="val" id="statDrawdown">&mdash;</div></div>
  </div>
  <div class="stat-grid">
    <div class="stat"><div class="label">Trades</div><div class="val" id="statTrades">0</div></div>
    <div class="stat"><div class="label">Observed</div><div class="val" id="statSkips">0</div></div>
    <div class="stat"><div class="label">Avg P&amp;L</div><div class="val" id="statAvgPnl">&mdash;</div></div>
  </div>
</div>
"""


def render_trade_card_template():
    """Trade list with filter system.
    Ported from legacy pageTrades (lines 6740-6796)."""
    return r"""
<!-- BTC 15m Trade List -->
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
  <div class="dim" style="font-size:10px;font-weight:600;letter-spacing:0.5px">TRADES</div>
  <button onclick="exportBtc15mCSV()" style="background:none;border:1px solid var(--border);border-radius:6px;padding:3px 8px;font-size:10px;color:var(--dim);cursor:pointer;-webkit-tap-highlight-color:transparent">
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
  <button class="chip" data-filter="win" onclick="setTradeFilter('win')">Wins</button>
  <button class="chip" data-filter="loss" onclick="setTradeFilter('loss')">Losses</button>
  <button class="chip" data-filter="cashed_out" onclick="setTradeFilter('cashed_out')">Cashouts</button>
  <button class="chip" data-filter="skipped" onclick="setTradeFilter('skipped')">Observed</button>
  <button class="chip" data-filter="error" onclick="setTradeFilter('error')">Errors</button>
  <button class="chip" data-filter="incomplete" onclick="setTradeFilter('incomplete')">Incomplete</button>
  <button class="chip" data-filter="ignored" onclick="setTradeFilter('ignored')">Ignored</button>
  <button class="chip" data-filter="shadow" onclick="setTradeFilter('shadow')">Shadow</button>
  <button class="chip" data-filter="yes" onclick="setTradeFilter('yes')">YES</button>
  <button class="chip" data-filter="no" onclick="setTradeFilter('no')">NO</button>
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

<!-- Trade Detail Modal -->
<div class="confirm-overlay" id="tradeDetailOverlay" style="display:none">
  <div class="modal-panel" style="max-width:480px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <h3 style="color:var(--blue);font-size:14px;margin:0">Trade Detail</h3>
      <button onclick="closeModal('tradeDetailOverlay')" style="background:none;border:none;color:var(--dim);font-size:20px;cursor:pointer;padding:10px;margin:-6px;-webkit-tap-highlight-color:transparent"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
    </div>
    <canvas id="tradeDetailChart" style="width:100%;height:100px;display:none"></canvas>
    <div id="tradeDetailContent"></div>
  </div>
</div>

<!-- Delete Trade Confirmation -->
<div class="confirm-overlay" id="deleteOverlay" style="display:none">
  <div class="modal-panel" style="max-width:340px">
    <div style="font-size:12px;font-weight:600;color:var(--red);margin-bottom:10px">Delete Trade</div>
    <div id="deleteInfo"></div>
    <div id="deleteBtns" style="margin-top:12px"></div>
  </div>
</div>
"""


def render_regime_config_html():
    """Per-plugin regime settings: BTC price, chart, current regime, engine status, regime list.
    Ported from legacy pageBitcoin (lines 6799-6871)."""
    return r"""
<!-- BTC 15m Regime Config -->

<!-- BTC Price Header -->
<div style="display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:8px">
  <div>
    <div class="dim" style="font-size:10px;font-weight:600;letter-spacing:0.5px">BITCOIN</div>
    <div id="btcPriceMain" style="font-size:24px;font-weight:700;font-family:monospace;color:var(--text)">&mdash;</div>
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
  <span class="detail-toggle" onclick="toggleDetail('regimeEngineSection')" style="margin:0;padding:0;line-height:1">&#9656; Engine Status</span>
  <div class="detail-section" id="regimeEngineSection" style="margin-top:8px">
    <div id="regimeEngineContent"><div class="dim">Loading...</div></div>
  </div>
</div>

<!-- Regime List -->
<div>
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
    <div class="dim" style="font-size:10px;font-weight:600;letter-spacing:0.5px">REGIMES</div>
    <button onclick="shareFile('/api/btc_15m/regimes/csv','btc15m_regimes_'+new Date().toISOString().slice(0,10)+'.csv')" style="background:none;border:1px solid var(--border);border-radius:6px;padding:3px 8px;font-size:10px;color:var(--dim);cursor:pointer;-webkit-tap-highlight-color:transparent">
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
"""


def render_stats_section_html():
    """Stats hub: summary cards + navigation to sub-pages.
    Ported from legacy pageStats (lines 6096-6180)."""
    return r"""
<!-- BTC 15m Stats -->

<!-- Stats Hub (main view) -->
<div id="statsHub">
  <!-- Summary Cards -->
  <div id="statsSummaryCards" class="stat-summary-grid">
    <div class="stat-summary-card"><div class="ssc-val" id="ssWinRate">&mdash;</div><div class="ssc-label">Win Rate</div></div>
    <div class="stat-summary-card"><div class="ssc-val" id="ssTotalPnl">&mdash;</div><div class="ssc-label">Total P&amp;L</div></div>
    <div class="stat-summary-card"><div class="ssc-val" id="ssROI">&mdash;</div><div class="ssc-label">ROI</div></div>
    <div class="stat-summary-card"><div class="ssc-val" id="ssProfitFactor">&mdash;</div><div class="ssc-label">Profit Factor</div></div>
  </div>

  <!-- Navigation Grid -->
  <div class="stats-nav-grid">
    <div class="stats-nav-card" onclick="statsNavTo('performance')">
      <div class="snc-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg></div>
      <div class="snc-title">Performance</div>
      <div class="snc-desc">Record, streaks, P&amp;L, daily &amp; hourly stats</div>
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
    <div class="stats-nav-card" onclick="statsNavTo('observatory')">
      <div class="snc-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg></div>
      <div class="snc-title">Observatory</div>
      <div class="snc-desc">Strategy lab, net edge, hold vs sell, fees</div>
      <div class="snc-preview" id="hubObsPreview"></div>
    </div>
    <div class="stats-nav-card" onclick="statsNavTo('models')">
      <div class="snc-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20V10M18 20V4M6 20v-4"/></svg></div>
      <div class="snc-title">Models &amp; Calibration</div>
      <div class="snc-desc">Fair value, BTC surface, confidence, features</div>
      <div class="snc-preview" id="hubModelPreview"></div>
    </div>
    <div class="stats-nav-card" onclick="statsNavTo('validation')">
      <div class="snc-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 11l3 3L22 4M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/></svg></div>
      <div class="snc-title">Validation &amp; Execution</div>
      <div class="snc-desc">Edge gaps, P&amp;L attribution, persistence, shadow trades</div>
      <div class="snc-preview" id="hubValPreview"></div>
    </div>
    <div class="stats-nav-card" onclick="statsNavTo('shadow')">
      <div class="snc-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10"/></svg></div>
      <div class="snc-title">Shadow Trading</div>
      <div class="snc-desc">Real execution data, fills, slippage, outcomes</div>
      <div class="snc-preview" id="hubShadowPreview"></div>
    </div>
    <div class="stats-nav-card" onclick="statsNavTo('convergence')">
      <div class="snc-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg></div>
      <div class="snc-title">Data Convergence</div>
      <div class="snc-desc">How much the numbers are still changing</div>
      <div class="snc-preview" id="hubConvPreview"></div>
    </div>
  </div>
  <div style="height:20px"></div>
</div>

<!-- Stats Sub-page (shown when navigated into a section) -->
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
"""


def render_settings_html():
    """Plugin settings: strategy picker, bet sizing, execution, risk, automation, notifications.
    Ported from legacy pageSettings (lines 6183-6554)."""
    return r"""
<!-- BTC 15m Settings -->

<!-- TRADING -->
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
      <div class="dim" style="font-size:10px;margin-bottom:2px" id="entryLabel">Buy &le;</div>
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
  <div id="modelSideWarning" style="display:none;font-size:10px;padding:4px 6px;margin-bottom:4px;border-radius:4px;background:rgba(210,153,34,0.08);border:1px solid rgba(210,153,34,0.15);color:var(--yellow)">Model side is not validated by the Strategy Observatory. Strategy risk data will show as unknown.</div>
  <div id="modelEdgeRow" class="input-row" style="display:none">
    <label>Min Model Edge %</label>
    <input type="number" id="minModelEdge" step="0.5" min="0" max="25" value="3" style="width:60px"
           onchange="saveSetting('btc_15m.min_model_edge_pct',parseFloat(this.value))">
    <span class="dim" style="font-size:10px">Only enter when model edge &ge; this</span>
  </div>

  <!-- Per-Regime Breakdown (from Observatory sims) -->
  <div id="regimePreviewCard" style="margin-bottom:8px">
    <div onclick="document.getElementById('regimePreviewBody').style.display=document.getElementById('regimePreviewBody').style.display==='none'?'':'none';this.querySelector('.chevron').textContent=document.getElementById('regimePreviewBody').style.display==='none'?'\u25b8':'\u25be'" style="cursor:pointer;font-size:11px;font-weight:600;color:var(--dim);padding:4px 0;display:flex;align-items:center;gap:4px">
      <span class="chevron">&#9656;</span> Per-Regime Breakdown
      <span id="regimePreviewCount" style="font-weight:400;font-size:10px;color:var(--dim)"></span>
    </div>
    <div id="regimePreviewBody" style="display:none">
      <div id="regimePreviewContent" style="font-size:11px">
        <div class="dim" style="font-size:10px;padding:4px 0">Change strategy parameters above to see per-regime data from Observatory simulations and real trades. Needs &ge;10 samples to classify risk.</div>
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
           onchange="saveSetting('btc_15m.bet_size',parseFloat(this.value))">
    <span class="dim" id="betSizeHint" style="font-size:10px">$ per trade</span>
  </div>

  <!-- Edge Scaled settings (shown when bet mode = edge_scaled) -->
  <div id="edgeScaledSettings" style="display:none;margin-top:4px;padding:8px;background:rgba(48,54,61,0.3);border-radius:6px">
    <div class="dim" style="font-size:10px;margin-bottom:6px">Scale bet size by FV model edge. Base bet &times; tier multiplier.</div>
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
           style="width:60px" onchange="saveSetting('btc_15m.max_consecutive_losses',parseInt(this.value))">
    <span class="dim">consec. losses (0=off)</span>
  </div>
  <div class="input-row">
    <label>Cooldown</label>
    <input type="number" id="cooldownAfterLoss" min="0" max="20" value="0"
           style="width:60px" onchange="saveSetting('btc_15m.cooldown_after_loss_stop',parseInt(this.value))">
    <span class="dim">markets to skip after stop</span>
  </div>

  <div class="sc-sub">EXECUTION</div>
  <div class="toggle" style="margin-top:0">
    <label class="tog"><input type="checkbox" id="adaptiveEntry"
           onchange="saveSetting('btc_15m.adaptive_entry',this.checked)"><span class="tpill"></span></label>
    <span class="dim">Adaptive Entry &mdash; start below ask, walk up on retries</span>
  </div>
  <div class="toggle">
    <label class="tog"><input type="checkbox" id="dynamicSellEnabled"
           onchange="saveSetting('btc_15m.dynamic_sell_enabled',this.checked);document.getElementById('dynamicSellFloor').closest('.input-row').style.display=this.checked?'':'none'"><span class="tpill"></span></label>
    <span class="dim">Dynamic Sell &mdash; model adjusts sell target during trade</span>
  </div>
  <div class="input-row" id="dynamicSellFloorRow" style="display:none">
    <label>Min Move &cent;</label>
    <input type="number" id="dynamicSellFloor" min="1" max="15" value="3" style="width:60px"
           onchange="saveSetting('btc_15m.dynamic_sell_floor_c',parseInt(this.value))">
    <span class="dim">min change to replace sell order</span>
  </div>
  <div class="toggle">
    <label class="tog"><input type="checkbox" id="earlyExitEv"
           onchange="saveSetting('btc_15m.early_exit_ev',this.checked)"><span class="tpill"></span></label>
    <span class="dim">Early Exit &mdash; sell losing trades when holding is -EV</span>
  </div>
  <div class="input-row">
    <label>Trailing Stop</label>
    <input type="number" id="trailingStopPct" min="0" max="100" value="0" style="width:60px"
           onchange="saveSetting('btc_15m.trailing_stop_pct',parseInt(this.value))">
    <span class="dim">% of target progress (0=off)</span>
  </div>
</div>

<!-- RISK & REGIME -->
<div class="settings-card">
  <div class="sc-title">RISK &amp; REGIME</div>

  <div class="toggle" style="margin-top:0">
    <label class="tog"><input type="checkbox" id="ignoreMode"
           onchange="saveSetting('btc_15m.ignore_mode',this.checked)"><span class="tpill"></span></label>
    <span class="dim" style="color:var(--orange)">Ignore Mode &mdash; trades won't count in stats</span>
  </div>

  <div class="sc-sub">ACTION PER RISK LEVEL</div>
  <div class="sc-hint" style="margin-bottom:8px">Risk is a composite score based on EV, confidence, OOS validation, downside, and robustness for the strategy being played. Override per-regime in Bitcoin tab.</div>
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

  <div class="sc-hint" style="margin-top:6px">Per-regime filters (volatility, hour, day, side, round, stability) are set on individual regime cards in the Bitcoin tab.</div>
</div>

<!-- AUTOMATION -->
<div class="settings-card">
  <div class="sc-title">AUTOMATION</div>

  <div class="sc-sub" style="margin-top:12px">AUTO-STRATEGY</div>
  <div class="sc-hint" style="margin-bottom:6px;font-size:10px">Strategy Observatory picks the highest EV strategy per regime. Active in Hybrid, Auto, and Shadow modes. These parameters control when a full-size trade is placed vs a shadow fallback.</div>
  <div id="autoStratParams">
    <div class="input-row">
      <label>Min observations</label>
      <input type="number" id="autoStrategyMinN" min="5" max="200" value="20"
             onchange="saveSetting('btc_15m.auto_strategy_min_samples',parseInt(this.value))">
    </div>
    <div class="input-row">
      <label>Min EV/trade</label>
      <input type="number" id="autoStrategyMinEv" min="0" max="50" step="0.5" value="0"
             onchange="saveSetting('btc_15m.auto_strategy_min_ev_c',parseFloat(this.value))">
      <span class="dim">&cent; (0 = any positive)</span>
    </div>
    <div class="input-row">
      <label>Fee buffer</label>
      <input type="number" id="feeBuffer" min="0" max="0.1" step="0.01" value="0.03" style="width:60px"
             onchange="saveSetting('btc_15m.min_breakeven_fee_buffer',parseFloat(this.value))">
      <span class="dim">strategy must survive fees + this</span>
    </div>
    <div class="toggle" style="margin-top:6px">
      <label class="tog"><input type="checkbox" id="autoStratTradeAll"
             onchange="_onAutoStratTradeAllToggle(this.checked)"><span class="tpill"></span></label>
      <span class="dim">Trade all regimes &mdash; bypass risk levels and per-regime filters</span>
    </div>
    <div id="autoStratTradeAllBanner" style="display:none;font-size:10px;color:var(--blue);padding:6px 8px;margin-top:4px;background:rgba(88,166,255,0.06);border:1px solid rgba(88,166,255,0.15);border-radius:6px">All regime filters, overrides, and risk levels are bypassed. Auto-strategy's own filters (min obs, min EV, fee buffer) still apply.</div>
  </div>

  <div class="sc-sub" style="margin-top:12px">POLLING</div>
  <div class="input-row" style="margin-top:0">
    <label>Price Poll</label>
    <input type="number" id="pricePollInterval" min="1" max="10" value="2" style="width:60px"
           onchange="saveSetting('btc_15m.price_poll_interval',parseInt(this.value))">
    <span class="dim">sec between price checks</span>
  </div>
  <div class="input-row">
    <label>Order Poll</label>
    <input type="number" id="orderPollInterval" min="1" max="15" value="3" style="width:60px"
           onchange="saveSetting('btc_15m.order_poll_interval',parseInt(this.value))">
    <span class="dim">sec between fill checks</span>
  </div>

  <div class="sc-sub" style="margin-top:12px">TRADING MODE</div>
  <div class="sc-hint" style="margin-bottom:6px;font-size:10px">Use the mode selector at the top of the Home tab to switch between Observe, Shadow, Hybrid, Auto, and Manual modes. Auto-strategy parameters above apply to Hybrid, Auto, and Shadow modes.</div>
  <div id="settingsModeDisplay" class="dim" style="font-size:11px;margin-bottom:4px">Current: &mdash;</div>
</div>

<!-- NOTIFICATIONS -->
<div class="settings-card">
  <div class="sc-title">NOTIFICATIONS</div>
  <div id="pushStatus" class="dim" style="margin-bottom:6px;font-size:11px">Checking...</div>
  <button class="btn btn-blue" id="pushToggleBtn" onclick="togglePush()" style="display:none;margin-bottom:8px">
    Enable Notifications
  </button>

  <div class="sc-hint" style="margin-bottom:6px">Trade events:</div>
  <div class="toggle" style="margin-top:0"><label class="tog"><input type="checkbox" id="notifyWins" checked onchange="saveSetting('btc_15m.push_notify_wins',this.checked)"><span class="tpill"></span></label><span class="dim">Wins</span></div>
  <div class="toggle"><label class="tog"><input type="checkbox" id="notifyLosses" checked onchange="saveSetting('btc_15m.push_notify_losses',this.checked)"><span class="tpill"></span></label><span class="dim">Losses</span></div>
  <div class="toggle"><label class="tog"><input type="checkbox" id="notifyBuys" onchange="saveSetting('btc_15m.push_notify_buys',this.checked)"><span class="tpill"></span></label><span class="dim">Buys</span></div>
  <div class="toggle"><label class="tog"><input type="checkbox" id="notifySkips" onchange="saveSetting('btc_15m.push_notify_observed',this.checked)"><span class="tpill"></span></label><span class="dim">Observed</span></div>
  <div class="toggle"><label class="tog"><input type="checkbox" id="notifyTradeUpdates" onchange="saveSetting('btc_15m.push_notify_trade_updates',this.checked)"><span class="tpill"></span></label><span class="dim">Trade updates (silent, every 1m)</span></div>

  <div class="sc-hint" style="margin-top:8px;margin-bottom:6px">System events:</div>
  <div class="toggle" style="margin-top:0"><label class="tog"><input type="checkbox" id="notifyErrors" checked onchange="saveSetting('btc_15m.push_notify_errors',this.checked)"><span class="tpill"></span></label><span class="dim">Errors &amp; stops</span></div>
  <div class="toggle"><label class="tog"><input type="checkbox" id="notifyEarlyExit" onchange="saveSetting('btc_15m.push_notify_early_exit',this.checked)" checked><span class="tpill"></span></label><span class="dim">Early exit</span></div>

  <div class="sc-hint" style="margin-top:8px;margin-bottom:6px">Regime data:</div>
  <div class="toggle" style="margin-top:0"><label class="tog"><input type="checkbox" id="notifyNewRegime" onchange="saveSetting('btc_15m.push_notify_new_regime',this.checked)" checked><span class="tpill"></span></label><span class="dim">New regime discovered</span></div>
  <div class="toggle"><label class="tog"><input type="checkbox" id="notifyRegimeClassified" onchange="saveSetting('btc_15m.push_notify_regime_classified',this.checked)" checked><span class="tpill"></span></label><span class="dim">Regime risk classified</span></div>
  <div class="toggle"><label class="tog"><input type="checkbox" id="notifyStrategyDiscovery" onchange="saveSetting('btc_15m.push_notify_strategy_discovery',this.checked)" checked><span class="tpill"></span></label><span class="dim">Strategy discovered (+EV)</span></div>
  <div class="toggle"><label class="tog"><input type="checkbox" id="notifyGlobalBest" onchange="saveSetting('btc_15m.push_notify_global_best',this.checked)" checked><span class="tpill"></span></label><span class="dim">Global best strategy changed</span></div>

  <div class="sc-sub">QUIET HOURS</div>
  <div class="input-row" style="margin-top:0">
    <label>From</label>
    <input type="number" id="quietStart" min="0" max="23" value="0" style="width:50px"
           onchange="saveSetting('btc_15m.push_quiet_start',parseInt(this.value))">
    <span class="dim">to</span>
    <input type="number" id="quietEnd" min="0" max="23" value="0" style="width:50px"
           onchange="saveSetting('btc_15m.push_quiet_end',parseInt(this.value))">
    <span class="dim">CT (0-0 = off)</span>
  </div>

  <button onclick="showPushLog()" style="background:none;border:1px solid var(--border);border-radius:6px;padding:4px 10px;font-size:11px;color:var(--dim);cursor:pointer;margin-top:10px;-webkit-tap-highlight-color:transparent">
    Notification History
  </button>
</div>
"""


def render_js():
    """ALL client-side JavaScript for the BTC 15m plugin.
    Ported from legacy dashboard JS sections — faithfully preserving logic."""
    return r"""
// ═══════════════════════════════════════════════════════════════
//  BTC 15m Plugin JavaScript
//  Ported from legacy dashboard.py
// ═══════════════════════════════════════════════════════════════

// ── Globals ──
let _uiState = {};
let _polling = false;
let _tradeFilters = new Set(['all']);
const _tradeFilterColors = {win:'active-green', loss:'active-red', cashed_out:'active-red',
                        skipped:'active', error:'active-yellow', incomplete:'active-red', ignored:'active-yellow', shadow:'active-purple',
                        yes:'active-green', no:'active-red'};
let _tradeOffset = 0;
let _tradeHasMore = false;
let _tradeLoading = false;
const _TRADE_PAGE_SIZE = 30;
let _lastFilterStatsKey = '';
let _lastActiveTradeKey = '';
let _statsCurrentPage = null;
let _livePriceBuf = {ticker: null, data: [], closeTime: null};
let _riskLevelActions = {low:'normal',moderate:'normal',high:'normal',terrible:'skip',unknown:'skip'};
let _regimeOverrides = {};
let _regimeFilters = {};
let currentRegimeFilter = 'all';
let currentBankroll = 0;
let currentLocked = 0;
let _sessTrack = {maxWinStreak:0, maxLossStreak:0, curStreakType:null, curStreakLen:0, peakPnl:0, prevWins:0, prevLosses:0};
let lastStateData = {_lastState:null, _lastTrade:null, _nextMarketOpen:null, _delayEndISO:null};
let _regimeGroupMap = {};

// ── Helpers ──
function fmtPnl(val) {
  const n = parseFloat(val) || 0;
  return (n >= 0 ? '+' : '-') + '$' + Math.abs(n).toFixed(2);
}

function fmtMmSs(sec) {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return m + ':' + (s < 10 ? '0' : '') + s;
}

function escHtml(s) { return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/'/g,'&#39;'); }

function riskTag(level) {
  const colors = {low:'var(--green)',moderate:'var(--yellow)',high:'var(--orange)',terrible:'var(--red)',unknown:'var(--dim)'};
  const labels = {low:'LOW',moderate:'MOD',high:'HIGH',terrible:'EXT',unknown:'?'};
  const c = colors[level] || 'var(--dim)';
  return '<span class="regime-tag" style="border-color:' + c + ';color:' + c + '">' + (labels[level]||'?') + '</span>';
}

function trendLabel(v) {
  if (v == null) return '?';
  if (v > 1) return 'Up (' + v + ')';
  if (v < -1) return 'Down (' + v + ')';
  return 'Flat (' + v + ')';
}

function hideSkel(id) {
  const el = document.getElementById(id);
  if (el && !el.classList.contains('skel-hidden')) {
    el.classList.add('skel-hidden');
    setTimeout(function() { el.style.display = 'none'; }, 300);
  }
}

function _autoFitStatVals() {
  document.querySelectorAll('.stat-grid .stat .val').forEach(function(el) {
    el.style.fontSize = '';
    let size = 18;
    while (el.scrollWidth > el.clientWidth + 1 && size > 10) {
      size--;
      el.style.fontSize = size + 'px';
    }
  });
}

// ── API helper (uses platform api() function) ──
// The platform dashboard shell provides: api(), showToast(), openModal(), closeModal(), toggleDetail(), $()

// ── State Polling ──
async function pollState() {
  if (_polling) return;
  _polling = true;
  try {
    const s = await api('/api/btc_15m/state');
    if (s) {
      _uiState = s;
      lastStateData._lastState = s;
      renderUI(s);
    }
  } catch(e) { console.error('BTC15m poll error:', e); }
  finally { _polling = false; }
}

// ── Main UI Render ──
function renderUI(s) {
  try {
    hideSkel('skelHome');

    const state = s.state || {};
    const status = s.status || 'stopped';
    const detail = s.status_detail || '';
    const tradingMode = state.trading_mode || 'observe';

    // Sync mode strip highlight
    _syncModeStrip(tradingMode);

    // Status dot + text
    const dot = document.getElementById('statusDot');
    const text = document.getElementById('statusText');
    const sub = document.getElementById('statusSub');

    const isRunning = !!state.auto_trading;
    const hasActiveTrade = !!state.active_trade;
    const hasPendingTrade = !!state.pending_trade;
    const _isDataMode = (tradingMode === 'observe' || tradingMode === 'shadow' || tradingMode === 'hybrid');

    let statusMain = '';
    let statusColor = '';
    let dotClass = 'dot-red';

    // Bot staleness detection
    const _offBanner = document.getElementById('offlineBanner');
    let _botStale = false;
    if (s.last_updated) {
      const _luDt = new Date(s.last_updated.replace(' ', 'T').replace('Z', '+00:00'));
      const _staleSec = (Date.now() - _luDt.getTime()) / 1000;
      if (_staleSec > 90) {
        _botStale = true;
        const _staleMin = Math.floor(_staleSec / 60);
        const _staleLabel = _staleMin >= 60 ? Math.floor(_staleMin/60) + 'h ' + (_staleMin%60) + 'm' : _staleMin + 'm';
        const _offText = document.getElementById('offlineText');
        if (_offText) _offText.textContent = 'Bot Offline \u2014 no heartbeat for ' + _staleLabel;
        if (_offBanner) _offBanner.style.display = '';
      } else {
        if (_offBanner) _offBanner.style.display = 'none';
      }
    }

    if (_botStale) {
      statusMain = 'Bot Offline';
      statusColor = 'var(--red)';
      dotClass = 'dot-red';
    } else if (!isRunning) {
      const _modeLabels = {observe:'Observing',shadow:'Shadow Idle',hybrid:'Hybrid Idle',auto:'Stopped',manual:'Stopped'};
      if (_isDataMode) {
        statusMain = _modeLabels[tradingMode] || 'Observing';
        dotClass = tradingMode === 'shadow' ? 'dot-purple' : 'dot-yellow';
        statusColor = tradingMode === 'shadow' ? '#a371f7' : 'var(--yellow)';
      } else {
        statusMain = 'Stopped';
        if (hasActiveTrade) statusMain = 'Stopped \u00b7 trade active';
      }
    } else if (state.cashing_out) {
      statusMain = 'CASHING OUT';
      dotClass = 'dot-red';
      statusColor = 'var(--red)';
    } else if (hasPendingTrade) {
      const pt = state.pending_trade;
      statusMain = 'Buying ' + (pt.side || '').toUpperCase();
      dotClass = 'dot-yellow';
      statusColor = 'var(--yellow)';
    } else if (hasActiveTrade) {
      const at0 = state.active_trade;
      const tSide = (at0.side || 'yes').toUpperCase();
      const tBid = at0.current_bid || 0;
      const tEntry = at0.avg_price_c || 0;
      statusMain = 'Trading \u00b7 ' + tSide + '@' + tEntry + '\u00a2 \u2192 ' + tBid + '\u00a2';
      statusColor = tBid >= tEntry ? 'var(--green)' : 'var(--red)';
      dotClass = 'dot-green';
    } else {
      dotClass = 'dot-yellow';
      const _modeStatusLabel = tradingMode === 'hybrid' ? 'Hybrid' : tradingMode === 'shadow' ? 'Shadow' : 'Observing';
      if (detail.includes('Observing') || detail.includes('Observed') || detail.includes('Skipped')) {
        statusMain = _isDataMode ? _modeStatusLabel : 'Observing';
        statusColor = _isDataMode ? (tradingMode === 'shadow' ? '#a371f7' : tradingMode === 'hybrid' ? 'var(--blue)' : 'var(--yellow)') : 'var(--yellow)';
        if (tradingMode === 'shadow') dotClass = 'dot-purple';
      } else if (detail.includes('Watching')) {
        statusMain = 'Watching prices';
        statusColor = 'var(--blue)';
      } else if (detail.includes('Entry delay')) {
        statusMain = 'Entry delay';
        statusColor = 'var(--yellow)';
      } else if (detail.includes('Resolving')) {
        statusMain = 'Resolving trade...';
        statusColor = 'var(--blue)';
      } else if (detail.includes('WIN') || detail.includes('+$')) {
        statusMain = detail;
        dotClass = 'dot-green';
        statusColor = 'var(--green)';
      } else if (detail.includes('LOSS')) {
        statusMain = detail;
        statusColor = 'var(--red)';
      } else if (detail.includes('Starting')) {
        statusMain = 'Starting...';
        statusColor = 'var(--blue)';
      } else {
        statusMain = _isDataMode ? _modeStatusLabel : 'Waiting for next market';
        statusColor = _isDataMode ? (tradingMode === 'shadow' ? '#a371f7' : tradingMode === 'hybrid' ? 'var(--blue)' : 'var(--yellow)') : 'var(--dim)';
      }
    }

    if (dot) dot.className = 'status-dot ' + dotClass;
    if (text) {
      text.textContent = statusMain;
      if (statusColor) text.style.color = statusColor;
    }
    if (sub) sub.innerHTML = detail || '';

    // Bankroll
    const bankrollCents = state.bankroll_cents;
    if (bankrollCents !== undefined) {
      currentBankroll = bankrollCents / 100;
      const hdrBal = document.getElementById('hdrBal');
      if (hdrBal) hdrBal.textContent = '$' + currentBankroll.toFixed(2);
    }
    const pnl = state.lifetime_pnl;
    if (pnl !== undefined) {
      const hdrPnl = document.getElementById('hdrPnl');
      if (hdrPnl) {
        hdrPnl.textContent = fmtPnl(pnl);
        hdrPnl.style.color = pnl >= 0 ? 'var(--green)' : 'var(--red)';
      }
    }

    // Live market monitor vs active trade
    const live = state.live_market;
    const at = state.active_trade;
    const pt = state.pending_trade;
    const mon = document.getElementById('monitorCard');
    const trade = document.getElementById('tradeCard');
    const pend = document.getElementById('pendingCard');

    // Active trade
    if (at && at.side) {
      if (mon) mon.style.display = 'none';
      if (pend) pend.style.display = 'none';
      if (trade) {
        trade.style.display = '';
        const sideEl = document.getElementById('tradeSide');
        if (sideEl) sideEl.innerHTML = '<span class="side-' + at.side + '">' + at.side.toUpperCase() + '</span> @ ' + (at.avg_price_c || at.entry_price_c || 0) + '\u00a2';
        const bidEl = document.getElementById('tradeBid');
        if (bidEl) bidEl.textContent = (at.current_bid || '\u2014') + '\u00a2';
        const sellEl = document.getElementById('tradeSell');
        if (sellEl) sellEl.textContent = (at.sell_price_c || '\u2014') + '\u00a2';
        const costEl = document.getElementById('tradeCost');
        if (costEl) costEl.textContent = '$' + (at.actual_cost || 0).toFixed(2);
        const hwmEl = document.getElementById('tradeHwm');
        if (hwmEl) hwmEl.textContent = (at.price_high_water_c || '\u2014') + '\u00a2';
        const sharesEl = document.getElementById('tradeShares');
        if (sharesEl) sharesEl.textContent = at.fill_count || 0;
        const marketEl = document.getElementById('tradeMarket');
        if (marketEl) marketEl.textContent = at.ticker || '\u2014';
        // Time left
        if (at.minutes_left !== undefined) {
          const timeEl = document.getElementById('tradeTime');
          if (timeEl) timeEl.textContent = fmtMmSs(at.minutes_left * 60);
        }
        // Regime
        const regEl = document.getElementById('tradeRegimeLabel');
        if (regEl && at.regime_label) regEl.textContent = at.regime_label.replace(/_/g, ' ');
        // Est P&L
        if (at.current_bid && at.avg_price_c && at.fill_count) {
          const estPnl = ((at.current_bid - at.avg_price_c) * at.fill_count) / 100;
          const estEl = document.getElementById('tradeEstPnl');
          if (estEl) {
            estEl.textContent = fmtPnl(estPnl);
            estEl.style.color = estPnl >= 0 ? 'var(--green)' : 'var(--red)';
          }
        }
        // Win probability bar
        if (at.current_bid) {
          const winPct = Math.min(99, Math.max(1, at.current_bid));
          const barEl = document.getElementById('winProbBar');
          const pctEl = document.getElementById('winProbPct');
          if (barEl) {
            barEl.style.width = winPct + '%';
            barEl.style.background = winPct >= 50 ? 'var(--green)' : 'var(--red)';
          }
          if (pctEl) pctEl.textContent = winPct + '%';
        }
        // Progress bar (time)
        if (at.minutes_left !== undefined && at.total_minutes) {
          const pct = Math.max(0, Math.min(100, (1 - at.minutes_left / at.total_minutes) * 100));
          const progEl = document.getElementById('tradeProgress');
          if (progEl) progEl.style.width = pct + '%';
        }
      }

    } else if (pt && pt.side) {
      // Pending trade
      if (mon) mon.style.display = 'none';
      if (trade) trade.style.display = 'none';
      if (pend) {
        pend.style.display = '';
        const pendSideEl = document.getElementById('pendSide');
        if (pendSideEl) pendSideEl.innerHTML = '<span class="side-' + pt.side + '">' + pt.side.toUpperCase() + '</span> @ ' + (pt.price_c || 0) + '\u00a2';
        const pendFillsEl = document.getElementById('pendFills');
        if (pendFillsEl) pendFillsEl.textContent = (pt.fill_count || 0) + '/' + (pt.shares_ordered || 0);
        if (pt.fill_count && pt.shares_ordered) {
          const pPct = (pt.fill_count / pt.shares_ordered * 100);
          const progEl = document.getElementById('pendProgress');
          if (progEl) progEl.style.width = pPct + '%';
        }
      }

    } else if (live) {
      // Monitor mode
      if (trade) trade.style.display = 'none';
      if (pend) pend.style.display = 'none';
      if (mon) {
        mon.style.display = '';
        const mMarket = document.getElementById('monMarket');
        if (mMarket) mMarket.textContent = live.ticker || '\u2014';
        const mYes = document.getElementById('monYesAsk');
        if (mYes) mYes.textContent = (live.yes_ask || '\u2014') + '\u00a2';
        const mNo = document.getElementById('monNoAsk');
        if (mNo) mNo.textContent = (live.no_ask || '\u2014') + '\u00a2';
        // Spread
        if (live.yes_ask && live.no_ask) {
          const spread = live.yes_ask + live.no_ask - 100;
          const mYesSpread = document.getElementById('monYesSpread');
          const mNoSpread = document.getElementById('monNoSpread');
          if (mYesSpread) mYesSpread.textContent = 'spread ' + spread + '\u00a2';
        }
        // Regime
        const mRisk = document.getElementById('monRisk');
        const mRegime = document.getElementById('monRegimeLabel');
        if (mRegime && live.regime_label) mRegime.textContent = live.regime_label.replace(/_/g, ' ');
        if (mRisk && live.risk_level) {
          const riskColors = {low:'var(--green)',moderate:'var(--yellow)',high:'var(--orange)',terrible:'var(--red)'};
          mRisk.textContent = (live.risk_level || '?').toUpperCase();
          mRisk.style.borderColor = riskColors[live.risk_level] || 'var(--dim)';
          mRisk.style.color = riskColors[live.risk_level] || 'var(--dim)';
        }
        // Time left
        if (live.close_time) {
          const closeMs = new Date(live.close_time).getTime();
          const diff = Math.max(0, (closeMs - Date.now()) / 1000);
          const mTime = document.getElementById('monTime');
          if (mTime) mTime.textContent = fmtMmSs(diff);
          _livePriceBuf.closeTime = live.close_time;
        }
        // Fair value
        if (live.fv_yes != null) {
          const fvEl = document.getElementById('monFairValue');
          if (fvEl) {
            fvEl.style.display = '';
            fvEl.innerHTML = '<span style="font-size:10px;font-weight:600;letter-spacing:.3px;color:rgba(136,132,216,0.9)">FAIR VALUE</span><br>' +
              '<span style="font-size:14px;font-family:monospace;color:rgba(136,132,216,0.9)">YES ' + live.fv_yes.toFixed(1) + '\u00a2 | NO ' + live.fv_no.toFixed(1) + '\u00a2</span>';
          }
          const fvYes = document.getElementById('monYesFV');
          const fvNo = document.getElementById('monNoFV');
          if (fvYes) { fvYes.style.display = ''; fvYes.textContent = 'FV ' + live.fv_yes.toFixed(1) + '\u00a2'; }
          if (fvNo) { fvNo.style.display = ''; fvNo.textContent = 'FV ' + live.fv_no.toFixed(1) + '\u00a2'; }
        }
        // Shadow trade section
        const shadowSec = document.getElementById('shadowTradeSection');
        const activeShadow = state.active_shadow;
        if (shadowSec && activeShadow && activeShadow.trade_id) {
          shadowSec.style.display = '';
          const shSide = document.getElementById('shadowSide');
          if (shSide) shSide.innerHTML = '<span class="side-' + (activeShadow.side||'yes') + '">' + (activeShadow.side||'').toUpperCase() + '</span>';
          const shFill = document.getElementById('shadowFill');
          if (shFill) shFill.textContent = (activeShadow.fill_price_c || '\u2014') + '\u00a2';
          const shSlip = document.getElementById('shadowSlip');
          if (shSlip) {
            const slip = (activeShadow.fill_price_c || 0) - (activeShadow.decision_price_c || 0);
            shSlip.textContent = (slip >= 0 ? '+' : '') + slip + '\u00a2';
          }
        } else if (shadowSec) {
          shadowSec.style.display = 'none';
        }
        // Auto strategy info
        const autoStrat = document.getElementById('monAutoStrategy');
        if (autoStrat && live.auto_strategy) {
          autoStrat.style.display = '';
          autoStrat.innerHTML = '<span style="font-size:10px;background:var(--blue);color:#fff;padding:1px 5px;border-radius:3px">AUTO</span> ' +
            '<span style="font-size:11px;font-family:monospace">' + live.auto_strategy + '</span>' +
            (live.auto_strategy_ev ? ' <span style="font-size:10px;color:var(--green)">' + (live.auto_strategy_ev >= 0 ? '+' : '') + live.auto_strategy_ev.toFixed(1) + '\u00a2</span>' : '');
        } else if (autoStrat) {
          autoStrat.style.display = 'none';
        }
        // Regime detail grid
        const regGrid = document.getElementById('monRegimeGrid');
        if (regGrid && live.vol_regime != null) {
          regGrid.innerHTML = '<div class="stat"><div class="label">Vol</div><div class="val">' + live.vol_regime + '/5</div></div>' +
            '<div class="stat"><div class="label">Trend</div><div class="val">' + trendLabel(live.trend_regime) + '</div></div>' +
            '<div class="stat"><div class="label">Volume</div><div class="val">' + (live.volume_regime||'?') + '/5</div></div>';
        }
        // Push price to live chart buffer
        if (live.yes_ask && live.no_ask) {
          const nowTs = Date.now();
          const cheaper = Math.min(live.yes_ask, live.no_ask);
          if (cheaper < 90 && cheaper > 2) {
            const lastBufTs = _livePriceBuf.data.length ? _livePriceBuf.data[_livePriceBuf.data.length-1].ts : 0;
            if (nowTs - lastBufTs > 800) {
              if (_livePriceBuf.ticker !== live.ticker) {
                _livePriceBuf = {ticker: live.ticker, data: [], closeTime: live.close_time};
              }
              _livePriceBuf.data.push({ts: nowTs, ya: live.yes_ask, na: live.no_ask, yb: live.yes_bid||0, nb: live.no_bid||0});
              if (_livePriceBuf.data.length > 1200) _livePriceBuf.data = _livePriceBuf.data.slice(-900);
            }
          }
        }
        drawLiveMarketChart('liveChart');
      }
    } else {
      if (mon) mon.style.display = 'none';
      if (trade) trade.style.display = 'none';
      if (pend) pend.style.display = 'none';
    }

    // Session stats
    const sw = state.session_wins || 0;
    const sl = state.session_losses || 0;
    const sp = state.session_pnl || 0;
    const sTotal = sw + sl;
    const sWr = sTotal > 0 ? (sw / sTotal * 100).toFixed(0) : '\u2014';
    const sAvg = sTotal > 0 ? (sp / sTotal) : 0;
    const sStatWins = document.getElementById('statWins');
    const sStatLosses = document.getElementById('statLosses');
    const sStatWinRate = document.getElementById('statWinRate');
    const sStatPnl = document.getElementById('statSessionPnl');
    const sStatTrades = document.getElementById('statTrades');
    const sStatSkips = document.getElementById('statSkips');
    const sStatAvgPnl = document.getElementById('statAvgPnl');
    if (sStatWins) sStatWins.textContent = sw;
    if (sStatLosses) sStatLosses.textContent = sl;
    if (sStatWinRate) sStatWinRate.textContent = sWr === '\u2014' ? '\u2014' : sWr + '%';
    if (sStatPnl) { sStatPnl.textContent = fmtPnl(sp); sStatPnl.className = 'val ' + (sp >= 0 ? 'pos' : 'neg'); }
    if (sStatTrades) sStatTrades.textContent = sTotal;
    if (sStatSkips) sStatSkips.textContent = state.session_skips || 0;
    if (sStatAvgPnl) sStatAvgPnl.textContent = sTotal > 0 ? fmtPnl(sAvg) : '\u2014';

    // Streaks
    const wStreak = document.getElementById('statWinStreak');
    const lStreak = document.getElementById('statLossStreak');
    const peakEl = document.getElementById('statPeakPnl');
    const ddEl = document.getElementById('statDrawdown');
    if (wStreak) wStreak.textContent = _sessTrack.maxWinStreak;
    if (lStreak) lStreak.textContent = _sessTrack.maxLossStreak;
    if (peakEl) peakEl.textContent = fmtPnl(_sessTrack.peakPnl);

    _autoFitStatVals();

  } catch(e) { console.error('BTC15m renderUI error:', e); }
}

// ── Mode switching ──
const MODE_META = {
  observe: { label: 'Observe', color: 'var(--yellow)', toast: 'Observe mode \u2014 recording data' },
  shadow:  { label: 'Shadow',  color: '#a371f7',       toast: 'Shadow mode \u2014 1-contract trades' },
  hybrid:  { label: 'Hybrid',  color: 'var(--blue)',    toast: 'Hybrid mode \u2014 auto + shadow fallback' },
  auto:    { label: 'Auto',    color: 'var(--green)',   toast: 'Auto mode \u2014 full trades only' },
  manual:  { label: 'Manual',  color: 'var(--text)',    toast: 'Manual mode \u2014 picker strategy' },
};

function _syncModeStrip(mode) {
  document.querySelectorAll('#modeStrip .mode-btn').forEach(function(b) {
    var m = b.dataset.mode;
    b.className = 'mode-btn' + (m === mode ? ' m-active-' + m : '');
  });
  var sd = document.getElementById('settingsModeDisplay');
  if (sd) {
    var meta = MODE_META[mode] || MODE_META.observe;
    sd.innerHTML = 'Current: <strong style="color:' + meta.color + '">' + meta.label + '</strong>';
  }
}

async function setTradingMode(mode) {
  if (!MODE_META[mode]) return;
  var meta = MODE_META[mode];
  await saveSetting('btc_15m.trading_mode', mode);
  _syncModeStrip(mode);
  // Send command to plugin
  await api('/api/command', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({plugin_id: 'btc_15m', command: 'update_config', params: {trading_mode: mode}})
  });
  showToast(meta.toast, mode === 'observe' ? 'yellow' : mode === 'shadow' ? 'purple' :
            mode === 'hybrid' ? 'blue' : mode === 'auto' ? 'green' : 'yellow');
  setTimeout(pollState, 800);
}

// ── Config save ──
async function saveSetting(key, val) {
  await api('/api/config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({[key]: val})
  });
  setTimeout(pollState, 800);
}

// ── Bot control ──
async function cmd(action, params) {
  await api('/api/command', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({plugin_id: 'btc_15m', command: action, params: params || {}})
  });
}

// ── Trade list ──
function _getTradeFilterParams() {
  var filters = _tradeFilters.has('all') ? 'all' : Array.from(_tradeFilters).join(',');
  var regime = (document.getElementById('tradeRegimeFilter') || {}).value || '';
  return { filters: filters, regime: regime };
}

function resetTradeCache() {
  _tradeOffset = 0;
  _lastFilterStatsKey = '';
  _lastActiveTradeKey = '';
  var el = document.getElementById('tradeList');
  if (el) el.dataset.key = '';
}

function setTradeFilter(filter) {
  if (filter === 'all') {
    _tradeFilters = new Set(['all']);
  } else {
    _tradeFilters.delete('all');
    if (_tradeFilters.has(filter)) {
      _tradeFilters.delete(filter);
      if (_tradeFilters.size === 0) _tradeFilters.add('all');
    } else {
      _tradeFilters.add(filter);
    }
  }
  document.querySelectorAll('#tradeFilters .chip').forEach(function(c) {
    var f = c.dataset.filter;
    c.className = _tradeFilters.has(f) ? 'chip ' + (_tradeFilterColors[f] || 'active') : 'chip';
  });
  resetTradeCache();
  loadTrades();
  // Show/hide delete incomplete bar
  var delBar = document.getElementById('deleteIncompleteBar');
  if (delBar) delBar.style.display = _tradeFilters.has('incomplete') ? '' : 'none';
}

function _renderFilterStats(stats) {
  var el = document.getElementById('tradeFilterStats');
  if (!el) return;
  if (!stats || stats.total === 0) {
    if (el.style.display !== 'none') el.style.display = 'none';
    return;
  }
  var key = stats.total + '|' + stats.wins + '|' + stats.losses + '|' + stats.pnl + '|' + (stats.errors||0);
  if (key === _lastFilterStatsKey && el.style.display !== 'none') return;
  _lastFilterStatsKey = key;
  el.style.display = '';
  var wr = stats.win_rate > 0 ? stats.win_rate + '%' : '\u2014';
  var wrCls = stats.win_rate >= 55 ? 'pos' : stats.win_rate > 0 && stats.win_rate < 45 ? 'neg' : '';
  var pnlCls = stats.pnl > 0 ? 'pos' : stats.pnl < 0 ? 'neg' : '';
  el.innerHTML = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">' +
    '<span style="font-size:12px;font-weight:600">' + stats.total + ' trades</span>' +
    '<span class="' + pnlCls + '" style="font-size:14px;font-weight:700;font-family:monospace">' + fmtPnl(stats.pnl) + '</span>' +
    '</div>' +
    '<div style="display:flex;gap:12px;font-size:11px;color:var(--dim);flex-wrap:wrap">' +
    '<span><span class="pos">' + stats.wins + 'W</span> \u00b7 <span class="neg">' + stats.losses + 'L</span></span>' +
    '<span>Win Rate: <strong class="' + wrCls + '">' + wr + '</strong></span>' +
    (stats.skips ? '<span>Observed: ' + stats.skips + '</span>' : '') +
    (stats.errors ? '<span style="color:#d29922">Errors: ' + stats.errors + '</span>' : '') +
    (stats.cashouts ? '<span>Cashouts: ' + stats.cashouts + '</span>' : '') +
    (stats.best > 0 ? '<span>Best: <span class="pos">' + fmtPnl(stats.best) + '</span></span>' : '') +
    (stats.worst < 0 ? '<span>Worst: <span class="neg">' + fmtPnl(stats.worst) + '</span></span>' : '') +
    '</div>';
}

async function loadTrades() {
  if (_tradeLoading) return;
  _tradeLoading = true;
  _tradeOffset = 0;
  var p = _getTradeFilterParams();
  try {
    var d = await api('/api/btc_15m/trades?filters=' + encodeURIComponent(p.filters) + '&regime=' + encodeURIComponent(p.regime) + '&offset=0&limit=' + _TRADE_PAGE_SIZE);
    hideSkel('skelTrades');
    // Populate regime dropdown
    var sel = document.getElementById('tradeRegimeFilter');
    var curVal = sel ? sel.value : '';
    if (d.regimes && sel) {
      var existing = new Set(Array.from(sel.options).map(function(o) { return o.value; }));
      d.regimes.forEach(function(r) {
        if (!existing.has(r)) {
          var opt = document.createElement('option');
          opt.value = r;
          opt.textContent = r.replace(/_/g, ' ');
          sel.appendChild(opt);
        }
      });
      sel.value = curVal || p.regime;
    }
    _renderFilterStats(d.stats);
    var el = document.getElementById('tradeList');
    if (!el) { _tradeLoading = false; return; }
    if (!d.trades.length) {
      if (!el.querySelector('.dim')) {
        el.innerHTML = '<div class="dim" style="text-align:center;padding:20px 0">No matching trades</div>';
      }
      var lm = document.getElementById('tradeLoadMore');
      if (lm) lm.style.display = 'none';
      var em = document.getElementById('tradeEndMarker');
      if (em) em.style.display = 'none';
    } else {
      var tradeKey = d.trades.map(function(t) { return t.id + ':' + t.outcome; }).join(',');
      if (tradeKey !== el.dataset.key) {
        var html = d.trades.map(function(t) { try { return renderTradeCard(t); } catch(e) { console.error('renderTradeCard error:', e, t.id); return ''; } }).join('');
        el.innerHTML = html;
        el.dataset.key = tradeKey;
      }
      _tradeOffset = d.trades.length;
      _tradeHasMore = d.has_more;
      var lm2 = document.getElementById('tradeLoadMore');
      if (lm2) lm2.style.display = d.has_more ? '' : 'none';
      var em2 = document.getElementById('tradeEndMarker');
      if (em2) em2.style.display = d.has_more ? 'none' : '';
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
  var p = _getTradeFilterParams();
  try {
    var d = await api('/api/btc_15m/trades?filters=' + encodeURIComponent(p.filters) + '&regime=' + encodeURIComponent(p.regime) + '&offset=' + _tradeOffset + '&limit=' + _TRADE_PAGE_SIZE);
    if (d.trades.length) {
      var el = document.getElementById('tradeList');
      if (el) el.innerHTML += d.trades.map(function(t) { try { return renderTradeCard(t); } catch(e) { return ''; } }).join('');
      _tradeOffset += d.trades.length;
      _tradeHasMore = d.has_more;
    } else {
      _tradeHasMore = false;
    }
    var lm = document.getElementById('tradeLoadMore');
    if (lm) lm.style.display = _tradeHasMore ? '' : 'none';
    var em = document.getElementById('tradeEndMarker');
    if (em) em.style.display = _tradeHasMore ? 'none' : '';
  } catch(e) { console.error('loadMoreTrades error:', e); }
  _tradeLoading = false;
}

// Infinite scroll
try {
  document.getElementById('contentWrap').addEventListener('scroll', function() {
    if (!_tradeHasMore || _tradeLoading) return;
    if (this.scrollHeight - this.scrollTop - this.clientHeight < 300) {
      loadMoreTrades();
    }
  });
} catch(e) {}

function renderTradeCard(t) {
  var o = t.outcome || 'unknown';
  var pnl = t.pnl || 0;
  var isShadow = t.is_shadow === 1 || t.is_shadow === true;
  var cardCls = isShadow ? 'tc-shadow' :
                o === 'win' ? 'tc-win' : o === 'loss' ? 'tc-loss' :
                o === 'error' ? 'tc-error' :
                o === 'cashed_out' ? 'tc-cashout' : o === 'open' ? 'tc-open' : 'tc-skip';
  var pnlCls = pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : 'dim';
  var outLabel = isShadow ? (o === 'win' ? 'SHADOW WIN' : o === 'loss' ? 'SHADOW LOSS' : 'SHADOW') :
    ({win:'WIN',loss:'LOSS',cashed_out:'CASHED OUT',skipped:'OBSERVED',no_fill:'NO FILL',error:'ERROR',open:'OPEN'})[o] || o.toUpperCase();
  var side = (t.side || '').toUpperCase();
  var entry = t.avg_fill_price_c || t.entry_price_c || 0;
  var sell = t.sell_price_c || 0;
  var hwm = t.price_high_water_c || 0;
  var progress = t.pct_progress_toward_target || 0;
  var filled = t.shares_filled || 0;
  var cost = t.actual_cost || 0;
  var fees = t.fees_paid || 0;
  var riskLvl = t.regime_risk_level || 'unknown';
  var regLabel = (t.regime_label || 'unknown').replace(/_/g, ' ');
  var tags = '';
  function tag(label, cls, filter) {
    return '<span class="tc-tag ' + cls + '" onclick="event.stopPropagation();setTradeFilter(\'' + filter + '\')">' + label + '</span>';
  }
  if (o === 'win') tags += tag('WIN', 'tag-win', 'win');
  else if (o === 'loss') tags += tag('LOSS', 'tag-loss', 'loss');
  else if (o === 'cashed_out') tags += tag('CASHED OUT', 'tag-cashout', 'cashed_out');
  else if (o === 'skipped' || o === 'no_fill') tags += tag(o === 'no_fill' ? 'NO FILL' : 'OBSERVED', 'tag-skip', 'skipped');
  else if (o === 'error') tags += tag('ERROR', 'tag-error', 'error');
  else if (o === 'open') tags += tag('OPEN', 'tag-open', 'open');
  if (['win', 'loss', 'cashed_out', 'open'].indexOf(o) >= 0 || isShadow) {
    if (t.side === 'yes') tags += tag('YES', 'tag-yes', 'yes');
    else if (t.side === 'no') tags += tag('NO', 'tag-no', 'no');
  }
  if (isShadow) tags += tag('SHADOW', 'tag-shadow', 'shadow');
  if (o === 'skipped' && !t.market_result) tags += tag('INCOMPLETE', 'tag-incomplete', 'incomplete');
  var skipLine = (o === 'skipped' || o === 'no_fill' || o === 'error') && t.skip_reason ?
    '<div style="font-size:11px;color:' + (o === 'error' ? '#d29922' : 'var(--dim)') + ';margin-top:4px">' + escHtml(t.skip_reason) + '</div>' : '';
  var skipOutcomeLine = '';
  if (o === 'skipped' && t.market_result) {
    var mr = t.market_result;
    skipOutcomeLine = '<div style="font-size:11px;color:var(--dim);margin-top:3px">Market result: <span class="' + (mr==='yes'?'side-yes':'side-no') + '">' + mr.toUpperCase() + '</span></div>';
  }
  var isReal = ['win', 'loss', 'cashed_out', 'open'].indexOf(o) >= 0 || isShadow;
  var detailHtml = '';
  if (isReal) {
    var shadowExtra = '';
    if (isShadow) {
      var decPrice = t.shadow_decision_price_c || 0;
      var fillPrice = t.avg_fill_price_c || 0;
      var slip = fillPrice - decPrice;
      var latency = t.shadow_fill_latency_ms || 0;
      shadowExtra = '<div>Ask at decision: <strong>' + decPrice + '\u00a2</strong></div>' +
        '<div>Slippage: <strong>' + (slip >= 0 ? '+' : '') + slip + '\u00a2</strong></div>' +
        '<div>Fill latency: <strong>' + latency + 'ms</strong></div>';
    }
    detailHtml = '<div class="tc-details">' +
      '<div>Side: <strong><span class="' + (t.side==='yes'?'side-yes':'side-no') + '">' + side + '</span> @ ' + entry + '\u00a2</strong></div>' +
      (isShadow ? '' : '<div>Sell target: <strong>' + sell + '\u00a2</strong></div>') +
      '<div>Shares: <strong>' + filled + '</strong></div>' +
      '<div>Cost: <strong>$' + cost.toFixed(2) + '</strong>' + (fees > 0 ? ' <span style="color:var(--dim)">(+$' + fees.toFixed(2) + ' fees)</span>' : '') + '</div>' +
      (isShadow ? shadowExtra : '<div>HWM: <strong>' + hwm + '\u00a2</strong></div><div>Progress: <strong>' + progress.toFixed(0) + '%</strong></div>') +
      '</div>';
  }
  return '<div class="trade-card ' + cardCls + '" onclick="showTradeDetail(' + t.id + ')" style="cursor:pointer">' +
    '<div class="tc-header"><div><span class="tc-outcome ' + pnlCls + '">' + outLabel + '</span>' +
    (isReal ? riskTag(riskLvl) : '') + '</div>' +
    '<span class="tc-pnl ' + pnlCls + '">' + fmtPnl(pnl) + '</span></div>' +
    '<div style="display:flex;justify-content:space-between;margin-top:4px">' +
    '<span class="dim" style="font-size:12px">' + regLabel + '</span>' +
    '<span class="dim" style="font-size:11px">' + (t.created_ct || '') + '</span></div>' +
    detailHtml + skipLine + skipOutcomeLine +
    '<div class="tc-tags">' + tags + '</div>' +
    '<div style="text-align:right;margin-top:4px">' +
    '<span style="opacity:0.35;cursor:pointer;padding:2px;display:inline-block" title="Delete trade"' +
    ' onclick="event.stopPropagation();showDeleteTrade(' + t.id + ',\'' + escHtml(outLabel) + '\',\'' + fmtPnl(pnl) + '\')">' +
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--dim)" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="m14.74 9-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 0 1-2.244 2.077H8.084a2.25 2.25 0 0 1-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 0 0-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 0 1 3.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 0 0-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 0 0-7.5 0"/></svg></span></div></div>';
}

// ── Trade detail popup ──
async function showTradeDetail(tradeId) {
  try {
    var d = await api('/api/btc_15m/trade/' + tradeId + '/detail');
    var t = d.trade;
    var path = d.price_path || [];
    var el = document.getElementById('tradeDetailContent');
    var o = t.outcome || 'open';
    var pnl = t.pnl || 0;
    var pCls = pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : '';
    var sideCls = t.side === 'yes' ? 'side-yes' : 'side-no';
    var entry = t.avg_fill_price_c || t.entry_price_c || 0;
    var sell = t.sell_price_c || 0;
    var hwm = t.price_high_water_c || 0;
    var lwm = t.price_low_water_c || 0;
    var osc = t.oscillation_count || 0;
    var prog = t.pct_progress_toward_target || 0;
    var stab = t.price_stability_c;
    var delay = t.entry_delay_minutes || 0;
    var regime = (t.regime_label || '\u2014').replace(/_/g, ' ');
    var riskLvl = t.regime_risk_level || 'unknown';
    var html = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">' +
      '<span class="tc-outcome ' + pCls + '" style="font-size:16px">' + (o === 'skipped' ? 'OBSERVED' : o === 'error' ? 'ERROR' : o.toUpperCase()) + '</span>' +
      '<span class="tc-pnl ' + pCls + '" style="font-size:18px">' + fmtPnl(pnl) + '</span></div>';
    if (o === 'skipped' || o === 'no_fill' || o === 'error') {
      var mr = t.market_result;
      if (o === 'error') {
        html += '<div style="padding:10px;border-radius:8px;background:#2a2010;border:1px solid #d29922;margin-bottom:10px">' +
          '<div style="font-size:12px;font-weight:600;color:#d29922;margin-bottom:4px">Order Error</div>' +
          '<div style="font-size:11px;color:var(--text);word-break:break-word">' + escHtml(t.skip_reason||'Unknown error') + '</div></div>';
      }
      if (mr) {
        html += '<div style="padding:10px;border-radius:8px;background:var(--bg);border:1px solid var(--border);margin-bottom:10px;text-align:center">' +
          '<div style="font-size:14px;font-weight:600">Market Result: <span class="' + (mr==='yes'?'side-yes':'side-no') + '">' + mr.toUpperCase() + '</span></div></div>';
      }
      html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:3px 12px;font-size:12px;color:var(--dim)">' +
        '<div style="grid-column:1/-1">Reason: <strong>' + escHtml(t.skip_reason||'\u2014') + '</strong></div>' +
        '<div>Vol Level: <strong>' + (t.vol_regime ? t.vol_regime + '/5' : '\u2014') + '</strong></div>' +
        '<div>Trend: <strong>' + (t.trend_regime != null ? (t.trend_regime > 0 ? '+' : '') + t.trend_regime : '\u2014') + '</strong></div>' +
        '<div>Spread: <strong>' + (t.spread_at_entry_c != null ? t.spread_at_entry_c + '\u00a2' : '\u2014') + '</strong></div>' +
        '<div>Stability: <strong>' + (stab != null ? stab+'\u00a2' : '\u2014') + '</strong></div></div>';
    } else {
      var _fmr = t.market_result ? '<span class="' + (t.market_result==='yes'?'side-yes':'side-no') + '">' + t.market_result.toUpperCase() + '</span>' : 'N/A';
      html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:3px 12px;font-size:12px;color:var(--dim)">' +
        '<div>Side: <strong><span class="' + sideCls + '">' + (t.side||'').toUpperCase() + '</span> @ ' + entry + '\u00a2</strong></div>' +
        '<div>Sell Target: <strong>' + sell + '\u00a2</strong></div>' +
        '<div>Shares: <strong>' + (t.shares_filled||0) + '</strong></div>' +
        '<div>Sold: <strong>' + (t.sell_filled||0) + '</strong></div>' +
        '<div>Cost: <strong>$' + (t.actual_cost||0).toFixed(2) + '</strong></div>' +
        '<div>Gross: <strong>$' + (t.gross_proceeds||0).toFixed(2) + '</strong></div>' +
        '<div>Fees: <strong>$' + (t.fees_paid||0).toFixed(2) + '</strong></div>' +
        '<div>Market Result: <strong>' + _fmr + '</strong></div>' +
        '<div>HWM: <strong>' + hwm + '\u00a2</strong></div>' +
        '<div>LWM: <strong>' + lwm + '\u00a2</strong></div>' +
        '<div>Oscillations: <strong>' + osc + '</strong></div>' +
        '<div>Progress: <strong>' + prog.toFixed(0) + '%</strong></div>' +
        '<div>Stability: <strong>' + (stab != null ? stab+'\u00a2' : '\u2014') + '</strong></div>' +
        '<div>Entry Delay: <strong>' + delay + 'm</strong></div></div>';
    }
    html += '<div style="margin-top:6px;display:flex;align-items:center;gap:6px">' +
      riskTag(riskLvl) + ' <span style="font-size:12px">' + regime + '</span></div>';
    html += '<div style="margin-top:6px;font-size:11px;color:var(--dim)">Traded: ' + (t.created_ct || '\u2014') + (t.notes ? '<br>Notes: ' + escHtml(t.notes) : '') + '</div>';
    el.innerHTML = html;
    openModal('tradeDetailOverlay');
    // Draw price path chart
    var canvas = document.getElementById('tradeDetailChart');
    if (path.length >= 2 && canvas) {
      canvas.style.display = '';
      var ctx = canvas.getContext('2d');
      var rect = canvas.getBoundingClientRect();
      if (rect.width > 0) {
        var dpr = window.devicePixelRatio || 1;
        canvas.width = rect.width * dpr;
        canvas.height = 100 * dpr;
        ctx.scale(dpr, dpr);
        var W2 = rect.width, H2 = 100;
        var pad2 = {t:8, b:14, l:4, r:4};
        ctx.clearRect(0, 0, W2, H2);
        var bids2 = path.map(function(p) { return p.our_side_bid || 0; }).filter(function(b) { return b > 0; });
        if (o === 'win' && sell > 0 && t.sell_filled > 0) bids2.push(sell);
        else if (o === 'win' && t.market_result) bids2.push(99);
        else if (o === 'loss' && t.sell_filled > 0 && t.exit_price_c) bids2.push(t.exit_price_c);
        else if (o === 'loss' && t.market_result) bids2.push(1);
        if (bids2.length >= 2) {
          var allV = bids2.concat([entry]); if (sell > 0) allV.push(sell);
          var yMin2 = Math.min.apply(null, allV) - 3, yMax2 = Math.max.apply(null, allV) + 3;
          if (yMax2 - yMin2 < 10) { yMin2 -= 5; yMax2 += 5; }
          var toX2 = function(i) { return pad2.l + (i / (bids2.length - 1)) * (W2 - pad2.l - pad2.r); };
          var toY2 = function(v) { return pad2.t + (1 - (v - yMin2) / (yMax2 - yMin2)) * (H2 - pad2.t - pad2.b); };
          // Entry line
          ctx.strokeStyle = 'rgba(88,166,255,0.3)'; ctx.setLineDash([4,3]); ctx.lineWidth = 1;
          ctx.beginPath(); ctx.moveTo(pad2.l, toY2(entry)); ctx.lineTo(W2-pad2.r, toY2(entry)); ctx.stroke();
          if (sell > 0) {
            ctx.strokeStyle = 'rgba(63,185,80,0.3)';
            ctx.beginPath(); ctx.moveTo(pad2.l, toY2(sell)); ctx.lineTo(W2-pad2.r, toY2(sell)); ctx.stroke();
          }
          ctx.setLineDash([]);
          var lastB = bids2[bids2.length-1];
          var lc2 = lastB >= entry ? '#3fb950' : '#f85149';
          ctx.strokeStyle = lc2; ctx.lineWidth = 1.5; ctx.beginPath();
          for (var i = 0; i < bids2.length; i++) {
            var x = toX2(i), y = toY2(bids2[i]);
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
          }
          ctx.stroke();
          ctx.font = '9px monospace'; ctx.fillStyle = 'rgba(88,166,255,0.5)';
          ctx.fillText(entry + '\u00a2', W2-pad2.r-25, toY2(entry)-2);
          if (sell > 0) { ctx.fillStyle = 'rgba(63,185,80,0.5)'; ctx.fillText(sell + '\u00a2', W2-pad2.r-25, toY2(sell)-2); }
        }
      }
    } else if (canvas) {
      canvas.style.display = 'none';
    }
  } catch(e) { console.error('Trade detail error:', e); }
}

// ── Delete trade ──
var pendingDeleteId = null;
function showDeleteTrade(tradeId, label, pnlStr) {
  pendingDeleteId = tradeId;
  var info = document.getElementById('deleteInfo');
  var btns = document.getElementById('deleteBtns');
  if (info) info.innerHTML = '<div style="font-size:13px;color:var(--text)"><strong>' + label + '</strong> ' + pnlStr + '</div>';
  if (btns) btns.innerHTML = '<div class="confirm-btns">' +
    '<button class="btn btn-dim" onclick="hideDelete()">Cancel</button>' +
    '<button class="btn btn-red" onclick="doDeleteTrade(' + tradeId + ')">Delete This Trade</button></div>';
  openModal('deleteOverlay');
}
function hideDelete() { closeModal('deleteOverlay'); pendingDeleteId = null; }
async function doDeleteTrade(tradeId) {
  hideDelete();
  showToast('Deleting trade...', 'yellow');
  try {
    var r = await api('/api/btc_15m/trade/' + tradeId + '/delete', {method: 'POST'});
    if (r.ok) {
      showToast('Trade deleted \u2014 stats recomputed', 'green');
      loadTrades();
      loadRegimes();
    } else {
      showToast('Delete failed: ' + (r.error || ''), 'red');
    }
  } catch(e) { showToast('Delete error: ' + e, 'red'); }
}
async function deleteAllIncomplete() {
  try {
    var r = await api('/api/btc_15m/trades/delete_incomplete', {method: 'POST'});
    showToast('Deleted ' + (r.deleted || 0) + ' incomplete', 'green');
    resetTradeCache();
    loadTrades();
  } catch(e) { showToast('Delete failed', 'red'); }
}

// ── CSV export ──
async function exportBtc15mCSV() {
  try {
    var resp = await fetch('/api/btc_15m/trades/csv');
    var blob = await resp.blob();
    var fname = 'btc15m_trades_' + new Date().toISOString().split('T')[0] + '.csv';
    if (navigator.share && /mobile|iphone|android/i.test(navigator.userAgent)) {
      var file = new File([blob], fname, {type: 'text/csv'});
      try { await navigator.share({files: [file], title: fname}); return; }
      catch(e) { if (e.name === 'AbortError') return; }
    }
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url; a.download = fname; a.click();
    setTimeout(function() { URL.revokeObjectURL(url); }, 5000);
  } catch(e) { if (e.name !== 'AbortError') console.error('CSV export error:', e); }
}

// ── Risk Actions ──
function setRiskAction(risk, action, btn) {
  _riskLevelActions[risk] = action;
  btn.parentElement.querySelectorAll('.abtn').forEach(function(b) { b.classList.remove('abtn-active'); });
  btn.classList.add('abtn-active');
  saveSetting('btc_15m.risk_level_actions', _riskLevelActions);
}

function _loadRiskActionButtons(actions) {
  if (!actions) return;
  _riskLevelActions = actions;
  document.querySelectorAll('[data-risk]').forEach(function(row) {
    var risk = row.dataset.risk;
    var action = actions[risk] || 'normal';
    if (action === 'data') action = 'skip';
    row.querySelectorAll('.abtn').forEach(function(b) {
      b.classList.toggle('abtn-active', b.dataset.action === action);
    });
  });
}

// ── Regime Filter ──
function setRegimeFilter(filter, btn) {
  currentRegimeFilter = filter;
  document.querySelectorAll('#regimeFilters .chip').forEach(function(c) {
    c.className = 'chip';
    if (c.dataset.filter === filter) c.className = 'chip active';
  });
  loadRegimes();
}

// ── Regime List ──
async function loadRegimes(force) {
  try {
    var regimes = await api('/api/btc_15m/regimes');
    hideSkel('skelRegimes');
    var el = document.getElementById('regimeList');
    if (!el) return;
    if (!regimes || !regimes.length) {
      el.innerHTML = '<div class="dim">No regime data yet \u2014 waiting for market observations</div>';
      return;
    }
    // Filter
    if (currentRegimeFilter === 'has_ev') {
      regimes = regimes.filter(function(r) { return r.best_ev_c != null; });
    } else if (currentRegimeFilter === 'positive_ev') {
      regimes = regimes.filter(function(r) { return r.best_ev_c != null && r.best_ev_c > 0; });
    }
    var html = '';
    regimes.forEach(function(r) {
      var label = (r.regime_label || 'unknown').replace(/_/g, ' ');
      var obsN = r.obs_count || 0;
      var totalTrades = r.total_trades || 0;
      var w = r.wins || 0;
      var l = r.losses || 0;
      var rpnl = r.total_pnl || 0;
      var pnlCls = rpnl > 0 ? 'pos' : rpnl < 0 ? 'neg' : '';
      var ev = r.best_ev_c;
      var evBadge = '';
      if (ev != null) {
        var evColor = ev > 0 ? 'var(--green)' : ev < 0 ? 'var(--red)' : 'var(--dim)';
        evBadge = '<span style="font-size:10px;color:' + evColor + ';font-weight:600;font-family:monospace">' + (ev>=0?'+':'') + ev.toFixed(1) + '\u00a2</span>';
      }
      var borderColor = ev > 5 ? 'var(--green)' : ev > 0 ? 'var(--blue)' : ev != null && ev < 0 ? 'var(--red)' : 'var(--border)';
      var escLabel = (r.regime_label||'').replace(/'/g, "\\'");
      html += '<div style="background:var(--card);border:1px solid var(--border);border-radius:6px;padding:8px;margin-bottom:6px;border-left:3px solid ' + borderColor + ';cursor:pointer" onclick="showRegimeDetail(\'' + escLabel + '\')">' +
        '<div style="display:flex;justify-content:space-between;align-items:center">' +
        '<span style="font-size:13px;font-weight:600">' + label + '</span>' +
        '<div style="display:flex;align-items:center;gap:6px">' + evBadge + '</div></div>' +
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-top:4px">' +
        '<span style="font-size:11px;color:var(--dim)">' + w + 'W / ' + l + 'L \u00b7 <span class="' + pnlCls + '">' + fmtPnl(rpnl) + '</span> \u00b7 ' + obsN + ' obs</span>' +
        '</div></div>';
    });
    el.innerHTML = html;
  } catch(e) { console.error('loadRegimes error:', e); }
}

// ── Regime Detail Modal ──
async function showRegimeDetail(label) {
  document.getElementById('regimeDetailTitle').textContent = label.replace(/_/g, ' ');
  var content = document.getElementById('regimeDetailContent');
  content.innerHTML = '<div class="dim">Loading...</div>';
  openModal('regimeDetailOverlay');
  try {
    var data = await api('/api/btc_15m/strategy_results?setup_type=coarse_regime&setup_value=' + encodeURIComponent(label));
    if (!data || !data.length) {
      content.innerHTML = '<div class="dim">No strategy data for this regime</div>';
      return;
    }
    var html = '<div class="dim" style="font-size:10px;font-weight:600;margin-bottom:6px">BEST STRATEGIES</div>';
    data.slice(0, 15).forEach(function(s) {
      var evColor = (s.tw_ev_c||0) > 0 ? 'var(--green)' : 'var(--red)';
      html += '<div class="stat-row"><span class="sr-label" style="font-family:monospace;font-size:11px">' + (s.strategy_key||'') + '</span>' +
        '<span class="sr-val" style="color:' + evColor + '">' + (s.tw_ev_c||0).toFixed(2) + '\u00a2 (' + (s.sample_size||0) + ' obs)</span></div>';
    });
    content.innerHTML = html;
  } catch(e) {
    content.innerHTML = '<div class="dim" style="color:var(--red)">Error: ' + e.message + '</div>';
  }
}

// ── Stats ──
async function _loadLifetimeStats() {
  try {
    var data = await api('/api/btc_15m/lifetime_stats');
    if (!data) return;
    var wr = document.getElementById('ssWinRate');
    var pnl = document.getElementById('ssTotalPnl');
    var roi = document.getElementById('ssROI');
    var pf = document.getElementById('ssProfitFactor');
    if (wr) wr.textContent = (data.win_rate_pct || 0).toFixed(1) + '%';
    if (pnl) { pnl.textContent = fmtPnl(data.total_pnl || 0); pnl.style.color = (data.total_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)'; }
    if (roi) roi.textContent = (data.roi_pct || 0).toFixed(1) + '%';
    if (pf) pf.textContent = (data.profit_factor || 0).toFixed(2);
    // Hub preview
    var prev = document.getElementById('hubPerfPreview');
    if (prev) {
      var w = data.wins || 0, l = data.losses || 0;
      prev.innerHTML = '<span class="pos">' + w + 'W</span> <span class="neg">' + l + 'L</span> \u00b7 ' + fmtPnl(data.total_pnl || 0);
    }
  } catch(e) { console.error('Lifetime stats error:', e); }
}

function statsNavTo(page) {
  _statsCurrentPage = page;
  var hub = document.getElementById('statsHub');
  if (hub) hub.style.display = 'none';
  var sub = document.getElementById('statsSubPage');
  if (sub) sub.style.display = '';
  var titles = {
    performance: 'Performance', conditions: 'Market Conditions', regimes: 'Regime Analysis',
    observatory: 'Observatory', models: 'Models & Calibration',
    validation: 'Validation & Execution', shadow: 'Shadow Trading', convergence: 'Data Convergence'
  };
  var titleEl = document.getElementById('statsSubTitle');
  if (titleEl) titleEl.textContent = titles[page] || page;
  var content = document.getElementById('statsSubContent');
  if (content) content.innerHTML = '<div class="dim" style="padding:20px 0;text-align:center">Loading...</div>';
  var csvBtn = document.getElementById('statsSubCsvBtn');
  if (csvBtn) csvBtn.style.display = ['performance','conditions','regimes'].indexOf(page) >= 0 ? '' : 'none';
  _statsLoadPage(page);
  var cw = document.getElementById('contentWrap');
  if (cw) cw.scrollTop = 0;
}

function statsGoBack() {
  _statsCurrentPage = null;
  var hub = document.getElementById('statsHub');
  if (hub) hub.style.display = '';
  var sub = document.getElementById('statsSubPage');
  if (sub) sub.style.display = 'none';
}

function _sRow(label, val, cls) {
  return '<div class="stat-row"><span class="sr-label">' + label + '</span><span class="sr-val ' + (cls||'') + '">' + val + '</span></div>';
}
function _sFmt(v) { return v >= 0 ? '+$' + v.toFixed(2) : '-$' + Math.abs(v).toFixed(2); }

async function _statsLoadPage(page) {
  var el = document.getElementById('statsSubContent');
  try {
    switch(page) {
      case 'performance': await _statsRenderPerformance(el); break;
      case 'regimes': await _statsRenderRegimes(el); break;
      case 'observatory': await _statsRenderObservatory(el); break;
      case 'shadow': await _statsRenderShadowPage(el); break;
      case 'models': await _statsRenderModels(el); break;
      case 'validation': await _statsRenderValidation(el); break;
      default: el.innerHTML = '<div class="dim">Coming soon</div>';
    }
  } catch(e) {
    el.innerHTML = '<div class="dim" style="color:var(--red)">Error loading: ' + e.message + '</div>';
    console.error('Stats page error:', e);
  }
}

async function _statsRenderPerformance(el) {
  var s = await api('/api/btc_15m/lifetime_stats');
  if (!s) { el.innerHTML = '<div class="dim">No data</div>'; return; }
  var w = s.wins||0, l = s.losses||0, total = w + l;
  var wr = total > 0 ? (w/total*100).toFixed(1) : '0';
  var html = '';
  html += '<div class="stat-category">Record</div>';
  html += _sRow('Record', w + 'W \u2013 ' + l + 'L');
  html += _sRow('Win Rate', wr + '%', w > l ? 'pos' : l > w ? 'neg' : '');
  html += '<div class="stat-category">Streaks</div>';
  html += _sRow('Best Win Streak', s.best_win_streak||0, 'pos');
  html += _sRow('Worst Loss Streak', s.worst_loss_streak||0, 'neg');
  html += '<div class="stat-category">Money</div>';
  html += _sRow('Total Wagered', '$' + (s.total_wagered||0).toFixed(2));
  html += _sRow('Total Fees', '$' + (s.total_fees||0).toFixed(2), 'neg');
  html += _sRow('Avg Win', _sFmt(s.avg_win_pnl||0), 'pos');
  html += _sRow('Avg Loss', _sFmt(s.avg_loss_pnl||0), 'neg');
  html += '<div class="stat-category">Extremes</div>';
  html += _sRow('Best Trade', s.best_trade_str || '\u2014', 'pos');
  html += _sRow('Worst Trade', s.worst_trade_str || '\u2014', 'neg');
  html += _sRow('Peak P&L', _sFmt(s.peak_pnl||0));
  html += _sRow('Max Drawdown', '-$' + (s.max_drawdown||0).toFixed(2), 'neg');
  // Hourly
  try {
    var hourly = await api('/api/btc_15m/hourly_stats');
    if (hourly && hourly.length) {
      html += '<div class="stat-category" style="margin-top:12px">Hourly Breakdown</div>';
      hourly.forEach(function(h) {
        var hWr = (h.wins||0) + (h.losses||0) > 0 ? ((h.wins||0) / ((h.wins||0)+(h.losses||0)) * 100).toFixed(0) : '\u2014';
        html += _sRow(h.hour_et + ':00 ET', (h.wins||0) + 'W/' + (h.losses||0) + 'L \u00b7 ' + hWr + '% WR \u00b7 ' + fmtPnl(h.total_pnl||0));
      });
    }
  } catch(e) {}
  el.innerHTML = html;
}

async function _statsRenderRegimes(el) {
  var data = await api('/api/btc_15m/regime_stats');
  if (!data || !data.length) { el.innerHTML = '<div class="dim">No regime data yet</div>'; return; }
  var html = '';
  data.forEach(function(r) {
    var pnlColor = (r.total_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)';
    var wr = (r.wins||0) + (r.losses||0) > 0 ? ((r.wins||0) / ((r.wins||0)+(r.losses||0)) * 100).toFixed(0) : '\u2014';
    html += '<div class="card" style="padding:10px;margin-bottom:6px">' +
      '<div style="display:flex;justify-content:space-between;align-items:center">' +
      '<span style="font-weight:600;font-size:13px">' + (r.regime_label || '?').replace(/_/g,' ') + '</span>' +
      '<span style="font-family:monospace;font-weight:700;color:' + pnlColor + '">' + fmtPnl(r.total_pnl||0) + '</span></div>' +
      '<div class="dim" style="font-size:11px;margin-top:2px">' + (r.total_trades||0) + ' trades \u00b7 ' + wr + '% WR \u00b7 ' + (r.obs_count||0) + ' obs</div></div>';
  });
  el.innerHTML = html;
}

async function _statsRenderObservatory(el) {
  try {
    var results = await Promise.all([
      api('/api/btc_15m/observation_count'),
      api('/api/btc_15m/strategy_results?setup_type=global')
    ]);
    var count = results[0];
    var strats = results[1];
    var html = '<div class="card" style="padding:10px">' +
      '<div style="font-size:22px;font-weight:700;font-family:monospace">' + ((count && count.total) || 0) + '</div>' +
      '<div class="dim" style="font-size:10px">Observations Recorded</div></div>';
    if (strats && strats.length) {
      html += '<div class="dim" style="font-size:10px;font-weight:600;margin:8px 0 4px">TOP STRATEGIES (Global)</div>';
      strats.slice(0, 15).forEach(function(s) {
        var evColor = (s.tw_ev_c||0) > 0 ? 'var(--green)' : 'var(--red)';
        html += '<div class="stat-row"><span class="sr-label" style="font-family:monospace;font-size:11px">' + (s.strategy_key||'') + '</span>' +
          '<span class="sr-val" style="color:' + evColor + '">' + (s.tw_ev_c||0).toFixed(2) + '\u00a2 (' + (s.sample_size||0) + ' obs)</span></div>';
      });
    }
    el.innerHTML = html;
  } catch(e) { el.innerHTML = '<div class="dim">Error: ' + e.message + '</div>'; }
}

async function _statsRenderShadowPage(el) {
  var data = await api('/api/btc_15m/shadow_stats');
  if (!data || !data.total) { el.innerHTML = '<div class="dim">No shadow trade data</div>'; return; }
  var wr = (data.wins||0) + (data.losses||0) > 0 ? ((data.wins||0) / ((data.wins||0)+(data.losses||0)) * 100) : 0;
  el.innerHTML = '<div class="stat-summary-grid">' +
    '<div class="stat-summary-card"><div class="ssc-val">' + (data.total||0) + '</div><div class="ssc-label">Shadow Trades</div></div>' +
    '<div class="stat-summary-card"><div class="ssc-val">' + wr.toFixed(1) + '%</div><div class="ssc-label">Win Rate</div></div>' +
    '<div class="stat-summary-card"><div class="ssc-val" style="color:' + ((data.pnl||0) >= 0 ? 'var(--green)' : 'var(--red)') + '">' + fmtPnl(data.pnl||0) + '</div><div class="ssc-label">Total P&L</div></div>' +
    '<div class="stat-summary-card"><div class="ssc-val">' + (data.avg_spread||0).toFixed(1) + '\u00a2</div><div class="ssc-label">Avg Spread</div></div>' +
    '</div>' +
    '<div class="stat-summary-grid" style="margin-top:6px">' +
    '<div class="stat-summary-card"><div class="ssc-val">' + Math.round(data.avg_latency||0) + 'ms</div><div class="ssc-label">Avg Latency</div></div>' +
    '</div>';
}

async function _statsRenderModels(el) {
  try {
    var results = await Promise.all([
      api('/api/btc_15m/feature_importance'),
      api('/api/btc_15m/validation_summary')
    ]);
    var features = results[0] || [];
    var validation = results[1] || {};
    var html = '<div class="stat-category">Walk-Forward Validation</div>';
    html += _sRow('Total Strategies', validation.total || 0);
    html += _sRow('OOS Validated', validation.validated || 0, 'pos');
    html += _sRow('Positive EV', validation.positive_ev || 0, 'pos');
    if (features.length) {
      html += '<div class="stat-category" style="margin-top:12px">Feature Importance</div>';
      features.slice(0, 15).forEach(function(f) {
        html += _sRow(f.feature_name || f.name || '?', ((f.importance||0)*100).toFixed(1) + '%');
      });
    }
    el.innerHTML = html;
  } catch(e) { el.innerHTML = '<div class="dim">Error: ' + e.message + '</div>'; }
}

async function _statsRenderValidation(el) {
  try {
    var data = await api('/api/btc_15m/walkforward_selection');
    if (!data || !data.length) { el.innerHTML = '<div class="dim">No validated strategies yet</div>'; return; }
    var html = '<div class="dim" style="font-size:10px;font-weight:600;margin-bottom:6px">OOS VALIDATED STRATEGIES</div>';
    data.forEach(function(s) {
      var evColor = (s.tw_ev_c||0) > 0 ? 'var(--green)' : 'var(--red)';
      html += '<div class="stat-row"><span class="sr-label" style="font-family:monospace;font-size:11px">' + (s.strategy_key||'') + '</span>' +
        '<span class="sr-val" style="color:' + evColor + '">' + (s.tw_ev_c||0).toFixed(2) + '\u00a2' +
        ' | OOS: ' + ((s.oos_ev_c||0) >= 0 ? '+' : '') + (s.oos_ev_c||0).toFixed(2) + '\u00a2 (' + (s.oos_sample_size||0) + ')</span></div>';
    });
    el.innerHTML = html;
  } catch(e) { el.innerHTML = '<div class="dim">Error: ' + e.message + '</div>'; }
}

function _statsExportCsv() {
  exportBtc15mCSV();
}

// ── Live Chart Drawing ──
function drawLiveMarketChart(canvasId) {
  var canvas = document.getElementById(canvasId);
  if (!canvas || !_livePriceBuf.data.length) return;
  var ctx = canvas.getContext('2d');
  var rect = canvas.getBoundingClientRect();
  if (rect.width === 0) return;
  var dpr = window.devicePixelRatio || 1;
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);
  var W = rect.width, H = rect.height;
  var pad = {t:8, b:20, l:4, r:4};
  ctx.clearRect(0, 0, W, H);
  var prices = _livePriceBuf.data;
  var yesVals = prices.map(function(p) { return p.ya || 50; });
  var noVals = prices.map(function(p) { return p.na || 50; });
  var allVals = yesVals.concat(noVals);
  var mn = Math.min.apply(null, allVals) - 2;
  var mx = Math.max.apply(null, allVals) + 2;
  var range = mx - mn || 1;
  var toX = function(i) { return pad.l + (i / Math.max(1, prices.length - 1)) * (W - pad.l - pad.r); };
  var toY = function(v) { return pad.t + (1 - (v - mn) / range) * (H - pad.t - pad.b); };
  // 50 cent line
  if (mn < 50 && mx > 50) {
    ctx.strokeStyle = 'rgba(255,255,255,0.06)';
    ctx.lineWidth = 1;
    ctx.setLineDash([4,4]);
    ctx.beginPath(); ctx.moveTo(pad.l, toY(50)); ctx.lineTo(W-pad.r, toY(50)); ctx.stroke();
    ctx.setLineDash([]);
  }
  // YES line (green)
  ctx.beginPath();
  ctx.strokeStyle = '#3fb950';
  ctx.lineWidth = 1.5;
  for (var i = 0; i < prices.length; i++) {
    var x = toX(i), y = toY(yesVals[i]);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.stroke();
  // NO line (red)
  ctx.beginPath();
  ctx.strokeStyle = '#f85149';
  ctx.lineWidth = 1.5;
  for (var j = 0; j < prices.length; j++) {
    var x2 = toX(j), y2 = toY(noVals[j]);
    if (j === 0) ctx.moveTo(x2, y2); else ctx.lineTo(x2, y2);
  }
  ctx.stroke();
  // Labels
  ctx.font = '9px monospace';
  if (prices.length > 0) {
    var lastYes = yesVals[yesVals.length-1];
    var lastNo = noVals[noVals.length-1];
    ctx.fillStyle = '#3fb950';
    ctx.fillText('Y ' + lastYes + '\u00a2', W - pad.r - 38, toY(lastYes) - 2);
    ctx.fillStyle = '#f85149';
    ctx.fillText('N ' + lastNo + '\u00a2', W - pad.r - 38, toY(lastNo) + 10);
  }
  // Time label
  if (_livePriceBuf.closeTime) {
    var closeMs = new Date(_livePriceBuf.closeTime).getTime();
    var diff = Math.max(0, (closeMs - Date.now()) / 1000);
    var labelEl = document.getElementById(canvasId + 'Label');
    if (labelEl) labelEl.textContent = fmtMmSs(diff) + ' left';
  }
}

async function loadLivePriceHistory(force) {
  try {
    var prices = await api('/api/btc_15m/live_prices?limit=500');
    if (!prices || !prices.length) return false;
    var currentTicker = prices[prices.length - 1].ticker;
    if (!force && _livePriceBuf.data.length > 5 && _livePriceBuf.ticker === currentTicker) return true;
    var backfill = [];
    for (var i = 0; i < prices.length; i++) {
      var p = prices[i];
      if (p.ticker !== currentTicker) continue;
      var cheaper = Math.min(p.yes_ask || 99, p.no_ask || 99);
      if (cheaper >= 90 || cheaper <= 2) continue;
      var ts = new Date(p.ts || p.created_at).getTime();
      backfill.push({ts: ts, ya: p.yes_ask || 0, na: p.no_ask || 0, yb: p.yes_bid || 0, nb: p.no_bid || 0});
    }
    if (!backfill.length) return false;
    _livePriceBuf = {ticker: currentTicker, data: backfill, closeTime: _livePriceBuf.closeTime};
    drawLiveMarketChart('liveChart');
    drawLiveMarketChart('pendChart');
    return true;
  } catch(e) {
    console.error('Live price backfill error:', e);
    return false;
  }
}

// ── BTC Chart ──
var _btcChartRange = 60;
async function loadBtcChart(minutes, btn) {
  if (minutes) _btcChartRange = minutes;
  if (btn) {
    document.querySelectorAll('[data-btcrange]').forEach(function(c) { c.className = 'chip'; });
    btn.className = 'chip active';
  }
  try {
    var data = await api('/api/candles?asset=BTC&source=binance&minutes=' + _btcChartRange);
    if (!data || !data.length) return;
    // Update BTC price display
    var last = data[data.length - 1];
    var btcEl = document.getElementById('btcPriceMain');
    if (btcEl && last.close) btcEl.textContent = '$' + Math.round(last.close).toLocaleString();
    // Draw chart
    var canvas = document.getElementById('btcChart');
    if (!canvas) return;
    var ctx = canvas.getContext('2d');
    var rect = canvas.getBoundingClientRect();
    if (rect.width === 0) return;
    var dpr = window.devicePixelRatio || 1;
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    ctx.scale(dpr, dpr);
    var W = rect.width, H = rect.height;
    var pad = {t:8, b:14, l:4, r:4};
    ctx.clearRect(0, 0, W, H);
    var closes = data.map(function(c) { return c.close; });
    var mn = Math.min.apply(null, closes) * 0.9999;
    var mx = Math.max.apply(null, closes) * 1.0001;
    var range = mx - mn || 1;
    var first = closes[0];
    var lastC = closes[closes.length-1];
    var lineColor = lastC >= first ? '#3fb950' : '#f85149';
    ctx.beginPath();
    ctx.strokeStyle = lineColor;
    ctx.lineWidth = 1.5;
    for (var i = 0; i < closes.length; i++) {
      var x = pad.l + (i / (closes.length - 1)) * (W - pad.l - pad.r);
      var y = pad.t + (1 - (closes[i] - mn) / range) * (H - pad.t - pad.b);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
    // Label
    var labelEl = document.getElementById('btcChartLabel');
    if (labelEl) {
      var change = ((lastC - first) / first * 100);
      labelEl.innerHTML = '$' + Math.round(lastC).toLocaleString() + ' <span style="color:' + lineColor + '">' + (change >= 0 ? '+' : '') + change.toFixed(2) + '%</span>';
    }
  } catch(e) { console.error('BTC chart error:', e); }
}

// ── Config Load ──
async function loadConfig() {
  try {
    var cfg = await api('/api/config');
    if (!cfg) return;
    // Strategy
    var side = document.getElementById('strategySide');
    if (side && cfg['btc_15m.strategy_side']) side.value = cfg['btc_15m.strategy_side'];
    // Bet
    var bm = document.getElementById('betMode');
    if (bm && cfg['btc_15m.bet_mode']) { bm.value = cfg['btc_15m.bet_mode']; _onBetModeChange(cfg['btc_15m.bet_mode'], true); }
    var bs = document.getElementById('betSize');
    if (bs && cfg['btc_15m.bet_size']) bs.value = cfg['btc_15m.bet_size'];
    // Execution
    var ae = document.getElementById('adaptiveEntry');
    if (ae && cfg['btc_15m.adaptive_entry'] !== undefined) ae.checked = cfg['btc_15m.adaptive_entry'];
    var ds = document.getElementById('dynamicSellEnabled');
    if (ds && cfg['btc_15m.dynamic_sell_enabled'] !== undefined) {
      ds.checked = cfg['btc_15m.dynamic_sell_enabled'];
      var dsFloor = document.getElementById('dynamicSellFloorRow');
      if (dsFloor) dsFloor.style.display = cfg['btc_15m.dynamic_sell_enabled'] ? '' : 'none';
    }
    if (cfg['btc_15m.dynamic_sell_floor_c']) { var dsf = document.getElementById('dynamicSellFloor'); if (dsf) dsf.value = cfg['btc_15m.dynamic_sell_floor_c']; }
    var ee = document.getElementById('earlyExitEv');
    if (ee && cfg['btc_15m.early_exit_ev'] !== undefined) ee.checked = cfg['btc_15m.early_exit_ev'];
    var ts = document.getElementById('trailingStopPct');
    if (ts && cfg['btc_15m.trailing_stop_pct'] !== undefined) ts.value = cfg['btc_15m.trailing_stop_pct'];
    // Risk
    if (cfg['btc_15m.risk_level_actions']) _loadRiskActionButtons(cfg['btc_15m.risk_level_actions']);
    if (cfg['btc_15m.regime_overrides']) _regimeOverrides = cfg['btc_15m.regime_overrides'];
    // Automation
    var aMinN = document.getElementById('autoStrategyMinN');
    if (aMinN && cfg['btc_15m.auto_strategy_min_samples'] !== undefined) aMinN.value = cfg['btc_15m.auto_strategy_min_samples'];
    var aMinEv = document.getElementById('autoStrategyMinEv');
    if (aMinEv && cfg['btc_15m.auto_strategy_min_ev_c'] !== undefined) aMinEv.value = cfg['btc_15m.auto_strategy_min_ev_c'];
    var fb = document.getElementById('feeBuffer');
    if (fb && cfg['btc_15m.min_breakeven_fee_buffer'] !== undefined) fb.value = cfg['btc_15m.min_breakeven_fee_buffer'];
    // Polling
    var pp = document.getElementById('pricePollInterval');
    if (pp && cfg['btc_15m.price_poll_interval'] !== undefined) pp.value = cfg['btc_15m.price_poll_interval'];
    var op = document.getElementById('orderPollInterval');
    if (op && cfg['btc_15m.order_poll_interval'] !== undefined) op.value = cfg['btc_15m.order_poll_interval'];
    // Ignore mode
    var im = document.getElementById('ignoreMode');
    if (im && cfg['btc_15m.ignore_mode'] !== undefined) im.checked = cfg['btc_15m.ignore_mode'];
    // Notifications
    var _setCheck = function(id, key) { var el = document.getElementById(id); if (el && cfg[key] !== undefined) el.checked = cfg[key]; };
    _setCheck('notifyWins', 'btc_15m.push_notify_wins');
    _setCheck('notifyLosses', 'btc_15m.push_notify_losses');
    _setCheck('notifyErrors', 'btc_15m.push_notify_errors');
    _setCheck('notifyBuys', 'btc_15m.push_notify_buys');
    _setCheck('notifySkips', 'btc_15m.push_notify_observed');
    _setCheck('notifyTradeUpdates', 'btc_15m.push_notify_trade_updates');
    _setCheck('notifyEarlyExit', 'btc_15m.push_notify_early_exit');
    _setCheck('notifyNewRegime', 'btc_15m.push_notify_new_regime');
    _setCheck('notifyRegimeClassified', 'btc_15m.push_notify_regime_classified');
    _setCheck('notifyStrategyDiscovery', 'btc_15m.push_notify_strategy_discovery');
    _setCheck('notifyGlobalBest', 'btc_15m.push_notify_global_best');
    var qs = document.getElementById('quietStart');
    if (qs && cfg['btc_15m.push_quiet_start']) qs.value = cfg['btc_15m.push_quiet_start'];
    var qe = document.getElementById('quietEnd');
    if (qe && cfg['btc_15m.push_quiet_end']) qe.value = cfg['btc_15m.push_quiet_end'];
    // Loss protection
    var mcl = document.getElementById('maxConsecLosses');
    if (mcl && cfg['btc_15m.max_consecutive_losses']) mcl.value = cfg['btc_15m.max_consecutive_losses'];
    var cal = document.getElementById('cooldownAfterLoss');
    if (cal && cfg['btc_15m.cooldown_after_loss_stop']) cal.value = cfg['btc_15m.cooldown_after_loss_stop'];
    // Mode
    if (cfg['btc_15m.trading_mode']) _syncModeStrip(cfg['btc_15m.trading_mode']);
  } catch(e) { console.error('loadConfig error:', e); }
}

function _onBetModeChange(mode, skipSave) {
  var hint = document.getElementById('betSizeHint');
  var edgeDiv = document.getElementById('edgeScaledSettings');
  if (mode === 'flat') {
    if (hint) hint.textContent = '$ per trade';
  } else if (mode === 'percent') {
    if (hint) hint.textContent = '% of bankroll';
  } else if (mode === 'edge_scaled') {
    if (hint) hint.textContent = 'base $ (scaled by edge)';
    if (edgeDiv) edgeDiv.style.display = '';
  }
  if (mode !== 'edge_scaled' && edgeDiv) edgeDiv.style.display = 'none';
  if (!skipSave) saveSetting('btc_15m.bet_mode', mode);
}

function _applyStrategyPicker() {
  var side = (document.getElementById('strategySide') || {}).value || 'cheaper';
  var timing = (document.getElementById('strategyTiming') || {}).value || 'early';
  var entry = (document.getElementById('strategyEntry') || {}).value || '45';
  var sell = (document.getElementById('strategySell') || {}).value || 'hold';
  var key = side + '_' + timing + '_' + entry + '_' + sell;
  var keyEl = document.getElementById('strategyKeyDisplay');
  if (keyEl) keyEl.textContent = key;
  saveSetting('btc_15m.strategy_side', side);
  saveSetting('btc_15m.entry_price_max_c', parseInt(entry) || 45);
  saveSetting('btc_15m.sell_target_c', sell === 'hold' ? 0 : parseInt(sell) || 90);
  // Model side warning
  var msw = document.getElementById('modelSideWarning');
  var mer = document.getElementById('modelEdgeRow');
  if (msw) msw.style.display = side === 'model' ? '' : 'none';
  if (mer) mer.style.display = side === 'model' ? '' : 'none';
}

function _onAutoStratTradeAllToggle(checked) {
  var banner = document.getElementById('autoStratTradeAllBanner');
  if (banner) banner.style.display = checked ? '' : 'none';
  saveSetting('btc_15m.auto_strat_trade_all', checked);
}

// ── Session ──
function resetSession() {
  cmd('reset_session');
  _sessTrack = {maxWinStreak:0, maxLossStreak:0, curStreakType:null, curStreakLen:0, peakPnl:0, prevWins:0, prevLosses:0};
  showToast('Session reset', 'green');
}

function recoverSession() {
  cmd('recover_session');
  showToast('Recovering session...', 'blue');
}

// ── Cash out ──
function showCashOut() {
  // Send cash out command to bot
  cmd('cash_out');
  showToast('Cash out initiated...', 'yellow');
}

function cancelCashOut() {
  cmd('cancel_cash_out');
  showToast('Cash out cancelled', 'blue');
}

// ── Simulated trade shuffle ──
function simShuffle() {
  cmd('sim_shuffle');
}

// ── Push Notifications ──
var pushSubscription = null;

async function initPush() {
  var statusEl = document.getElementById('pushStatus');
  var btnEl = document.getElementById('pushToggleBtn');
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
    if (statusEl) statusEl.textContent = 'Push not supported';
    return;
  }
  try {
    var reg = await navigator.serviceWorker.ready;
    var sub = await reg.pushManager.getSubscription();
    pushSubscription = sub;
    if (sub) {
      if (statusEl) statusEl.innerHTML = '<span style="color:var(--green)">Notifications active</span>';
      if (btnEl) { btnEl.style.display = ''; btnEl.textContent = 'Disable Notifications'; }
    } else {
      if (statusEl) statusEl.textContent = 'Notifications disabled';
      if (btnEl) { btnEl.style.display = ''; btnEl.textContent = 'Enable Notifications'; }
    }
  } catch(e) {
    if (statusEl) statusEl.textContent = 'Push error: ' + e.message;
  }
}

async function togglePush() {
  try {
    if (pushSubscription) {
      await pushSubscription.unsubscribe();
      await api('/api/push/unsubscribe', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({endpoint: pushSubscription.endpoint})
      });
      pushSubscription = null;
      showToast('Notifications disabled', 'yellow');
    } else {
      var reg = await navigator.serviceWorker.ready;
      var resp = await api('/api/push/vapid_public');
      var key = resp.key;
      var sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: key
      });
      await api('/api/push/subscribe', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify(sub.toJSON())
      });
      pushSubscription = sub;
      showToast('Notifications enabled!', 'green');
    }
    initPush();
  } catch(e) {
    showToast('Push error: ' + e.message, 'red');
  }
}

async function showPushLog(filterTag) {
  try {
    var url = filterTag ? '/api/push/log?tag=' + encodeURIComponent(filterTag) : '/api/push/log';
    var logs = await api(url);
    var el = document.getElementById('tradeDetailContent');
    var canvas = document.getElementById('tradeDetailChart');
    if (canvas) canvas.style.display = 'none';
    var tags = ['trade-result', 'buy', 'skip', 'max-loss', 'loss-stop', 'auto-lock',
                'early-exit', 'cash-out', 'error', 'deploy'];
    var chips = tags.map(function(t) {
      var active = filterTag === t ? ' active' : '';
      return '<button class="chip' + active + '" onclick="showPushLog(' + (filterTag === t ? '' : "'" + t + "'") + ')" style="font-size:10px;padding:2px 6px">' + t.replace(/-/g, ' ') + '</button>';
    }).join('');
    var html = '<div class="dim" style="font-size:11px;font-weight:600;margin-bottom:6px">NOTIFICATION HISTORY</div>';
    html += '<div class="filter-chips" style="margin-bottom:8px;flex-wrap:wrap">' +
      '<button class="chip' + (!filterTag ? ' active' : '') + '" onclick="showPushLog()" style="font-size:10px;padding:2px 6px">All</button>' + chips + '</div>';
    if (!logs || !logs.length) {
      html += '<div class="dim" style="text-align:center;padding:20px">No notifications found.</div>';
    } else {
      html += '<div style="max-height:60vh;overflow-y:auto;-webkit-overflow-scrolling:touch">';
      logs.forEach(function(l) {
        html += '<div style="padding:6px 0;border-bottom:1px solid rgba(255,255,255,0.04)">' +
          '<div style="display:flex;justify-content:space-between;align-items:center">' +
          '<span style="font-size:13px;font-weight:600">' + (l.title||'') + '</span>' +
          '<span class="dim" style="font-size:10px;white-space:nowrap;margin-left:8px">' + (l.sent_ct || '') + '</span></div>' +
          '<div style="font-size:12px;color:var(--dim);margin-top:1px">' + (l.body || '') + '</div></div>';
      });
      html += '</div>';
    }
    el.innerHTML = html;
    var overlay = document.getElementById('tradeDetailOverlay');
    if (overlay && (overlay.style.display === 'none' || overlay.style.display === '')) {
      openModal('tradeDetailOverlay');
    }
  } catch(e) { console.error('Push log error:', e); }
}

// ── Regime Worker Status ──
async function loadRegimeWorkerStatus() {
  try {
    var s = await api('/api/btc_15m/regime_worker_status');
    // Could update engine status section
  } catch(e) {}
}

// ── shareFile helper ──
async function shareFile(url, fname) {
  try {
    var resp = await fetch(url);
    var blob = await resp.blob();
    if (navigator.share && /mobile|iphone|android/i.test(navigator.userAgent)) {
      var file = new File([blob], fname, {type: blob.type || 'text/csv'});
      try { await navigator.share({files: [file], title: fname}); return; }
      catch(e) { if (e.name === 'AbortError') return; }
    }
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = fname;
    a.click();
    setTimeout(function() { URL.revokeObjectURL(a.href); }, 5000);
  } catch(e) { console.error('shareFile error:', e); }
}

// ── Foreground refresh ──
var _lastForegroundRefresh = 0;
async function _refreshOnForeground() {
  var now = Date.now();
  if (now - _lastForegroundRefresh < 2000) return;
  _lastForegroundRefresh = now;
  await pollState();
  loadLivePriceHistory(true);
  loadTrades();
  loadRegimes();
  _loadLifetimeStats();
}

document.addEventListener('visibilitychange', function() {
  if (!document.hidden) _refreshOnForeground();
});
window.addEventListener('pageshow', function(e) {
  if (e.persisted) _refreshOnForeground();
});
window.addEventListener('focus', _refreshOnForeground);

// ═══════════════════════════════════════════════════════════════
//  INITIALIZATION
// ═══════════════════════════════════════════════════════════════

(function _initBtc15m() {
  // Start polling
  var _pollRate = 1000;
  function schedulePoll() {
    setTimeout(async function() {
      await pollState();
      _pollRate = (_uiState.state && _uiState.state.cashing_out) ? 500 : 1000;
      schedulePoll();
    }, _pollRate);
  }
  schedulePoll();

  // Periodic refreshes
  setInterval(loadTrades, 15000);
  setInterval(function() { loadRegimes(); }, 30000);
  setInterval(_loadLifetimeStats, 30000);
  setInterval(loadRegimeWorkerStatus, 30000);
  setInterval(function() { loadBtcChart(); }, 30000);

  // Initial loads
  loadLivePriceHistory();
  loadConfig();
  loadTrades();
  loadRegimes();
  _loadLifetimeStats();
  loadRegimeWorkerStatus();
  loadBtcChart();
  initPush();
  pollState();
})();
"""
