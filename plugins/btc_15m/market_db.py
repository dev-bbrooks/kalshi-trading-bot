"""
market_db.py — BTC 15-minute plugin database schema and queries.
Creates plugin-specific tables with btc15m_ prefix.
"""

import math
import sys
from datetime import datetime, timezone, timedelta
sys.path.insert(0, "/opt/trading-platform")

from db import get_conn, now_utc, row_to_dict, rows_to_list
from config import REGIME_THRESHOLDS, KALSHI_FEE_RATE


def init_btc15m_tables():
    with get_conn() as c:

        # ── Markets (one row per Kalshi 15-min market) ────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS btc15m_markets (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker          TEXT NOT NULL UNIQUE,
                close_time_utc  TEXT NOT NULL,
                hour_et         INTEGER NOT NULL,
                minute_et       INTEGER NOT NULL,
                day_of_week     INTEGER NOT NULL,
                is_weekend      INTEGER DEFAULT 0,
                outcome         TEXT,
                created_at      TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_markets_ticker ON btc15m_markets(ticker)")

        # ── Trades (comprehensive — the main analysis table) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS btc15m_trades (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id               INTEGER REFERENCES btc15m_markets(id),
                regime_snapshot_id      INTEGER REFERENCES regime_snapshots(id),

                -- Position
                ticker                  TEXT NOT NULL,
                side                    TEXT NOT NULL,
                entry_price_c           INTEGER,
                entry_time_utc          TEXT,
                minutes_before_close    REAL,

                -- Execution
                shares_ordered          INTEGER DEFAULT 0,
                shares_filled           INTEGER DEFAULT 0,
                actual_cost             REAL DEFAULT 0,
                fees_paid               REAL DEFAULT 0,
                avg_fill_price_c        INTEGER DEFAULT 0,
                buy_order_id            TEXT,

                -- Exit
                sell_price_c            INTEGER,
                sell_order_id           TEXT,
                sell_filled             INTEGER DEFAULT 0,
                exit_price_c            INTEGER,
                exit_time_utc           TEXT,
                gross_proceeds          REAL DEFAULT 0,
                pnl                     REAL DEFAULT 0,

                -- Outcome
                outcome                 TEXT NOT NULL,
                skip_reason             TEXT,

                -- Price path summary
                price_high_water_c      INTEGER,
                price_low_water_c       INTEGER,
                pct_progress_toward_target REAL,
                oscillation_count       INTEGER DEFAULT 0,

                -- Regime context (denormalized for fast queries)
                regime_label            TEXT,
                vol_regime              INTEGER,
                trend_regime            INTEGER,
                volume_regime           INTEGER,
                regime_risk_level       TEXT,

                -- Flags
                is_data_collection      INTEGER DEFAULT 0,
                is_ignored              INTEGER DEFAULT 0,

                -- Market context
                btc_price_at_entry      REAL,
                market_result           TEXT,
                entry_delay_minutes     INTEGER DEFAULT 0,

                notes                   TEXT,
                created_at              TEXT NOT NULL,

                -- Migrated columns (all included in schema)
                trade_mode              TEXT,
                price_stability_c       INTEGER,
                is_early_exit           INTEGER DEFAULT 0,
                early_exit_price_c      INTEGER,
                spread_at_entry_c       INTEGER,
                btc_price_at_exit       REAL,
                btc_move_pct            REAL,
                prev_regime_label       TEXT,
                coarse_regime           TEXT,
                hour_et                 INTEGER,
                day_of_week             INTEGER,
                is_regime_bet           INTEGER DEFAULT 0,
                skip_hypo_outcome       TEXT,
                bankroll_at_entry_c     INTEGER,
                bet_size_dollars        REAL,
                fill_duration_seconds   REAL,
                exit_method             TEXT,
                num_price_samples       INTEGER,
                session_trade_num       INTEGER,
                time_to_target_seconds  REAL,
                market_close_time_utc   TEXT,
                cheaper_side            TEXT,
                cheaper_side_price_c    INTEGER,
                spread_regime           TEXT,
                regime_confidence       REAL,
                bollinger_width         REAL,
                atr_15m                 REAL,
                realized_vol            REAL,
                trend_direction         INTEGER,
                trend_strength          REAL,
                bollinger_squeeze       INTEGER DEFAULT 0,
                trend_acceleration      TEXT,
                btc_return_15m          REAL,
                btc_return_1h           REAL,
                btc_return_4h           REAL,
                volume_spike            INTEGER DEFAULT 0,
                ema_slope_15m           REAL,
                ema_slope_1h            REAL,
                yes_ask_at_entry        INTEGER,
                no_ask_at_entry         INTEGER,
                yes_bid_at_entry        INTEGER,
                no_bid_at_entry         INTEGER,
                kalshi_market_volume    INTEGER,
                kalshi_open_interest    INTEGER,
                session_pnl_at_entry    REAL,
                session_wins_at_entry   INTEGER,
                session_losses_at_entry INTEGER,
                prediction_factors      TEXT,
                market_implied_pct      REAL,
                ev_per_contract_c       REAL,
                auto_strategy_key       TEXT,
                auto_strategy_setup     TEXT,
                auto_strategy_ev_c      REAL,
                model_edge_at_entry     REAL,
                model_ev_at_entry       REAL,
                model_source_at_entry   TEXT,
                is_shadow               INTEGER DEFAULT 0,
                shadow_decision_price_c INTEGER,
                shadow_fill_latency_ms  INTEGER,
                minute_et               INTEGER,
                btc_distance_pct        REAL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_trades_outcome ON btc15m_trades(outcome)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_trades_regime ON btc15m_trades(regime_label)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_trades_created ON btc15m_trades(created_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_trades_coarse ON btc15m_trades(coarse_regime)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_trades_hour ON btc15m_trades(hour_et)")

        # ── Market observations (every market seen, with full price path) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS btc15m_observations (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker              TEXT NOT NULL UNIQUE,
                market_id           INTEGER,
                close_time_utc      TEXT NOT NULL,

                -- Outcome (backfilled after market closes)
                market_result       TEXT,

                -- Regime context at market start
                regime_label        TEXT,
                vol_regime          INTEGER,
                trend_regime        INTEGER,
                volume_regime       INTEGER,
                risk_level          TEXT,
                regime_confidence   REAL,

                -- BTC technicals at market start
                btc_price           REAL,
                btc_return_15m      REAL,
                btc_return_1h       REAL,
                btc_return_4h       REAL,
                realized_vol        REAL,
                atr_15m             REAL,
                bollinger_width     REAL,
                ema_slope_15m       REAL,
                ema_slope_1h        REAL,
                trend_direction     INTEGER,
                trend_strength      REAL,
                bollinger_squeeze   INTEGER DEFAULT 0,
                volume_spike        INTEGER DEFAULT 0,

                -- Timing
                hour_et             INTEGER,
                minute_et           INTEGER,
                day_of_week         INTEGER,

                -- Kalshi price path (JSON array of snapshots)
                price_snapshots     TEXT,

                -- Price summary (derived on close)
                yes_open_c          INTEGER,
                yes_high_c          INTEGER,
                yes_low_c           INTEGER,
                yes_close_c         INTEGER,
                no_open_c           INTEGER,
                no_high_c           INTEGER,
                no_low_c            INTEGER,
                no_close_c          INTEGER,
                snapshot_count      INTEGER DEFAULT 0,

                -- BTC movement during market
                btc_price_at_close  REAL,
                btc_move_during_pct REAL,

                -- Bot action
                bot_action          TEXT,
                trade_id            INTEGER,
                active_strategy_key TEXT,

                -- Kalshi market liquidity
                kalshi_volume       INTEGER,
                kalshi_open_interest INTEGER,

                created_at          TEXT NOT NULL,

                -- Migrated columns
                obs_quality         TEXT DEFAULT 'full',
                btc_price_at_open   REAL,
                btc_distance_pct_at_close REAL,
                btc_max_distance_pct REAL,
                btc_min_distance_pct REAL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_obs_ticker ON btc15m_observations(ticker)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_obs_close ON btc15m_observations(close_time_utc)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_obs_regime ON btc15m_observations(regime_label)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_obs_result ON btc15m_observations(market_result)")

        # ── Strategy simulation results (aggregated per setup × strategy) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS btc15m_strategy_results (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                setup_key           TEXT NOT NULL,
                setup_type          TEXT NOT NULL,
                strategy_key        TEXT NOT NULL,
                side_rule           TEXT,
                exit_rule           TEXT,
                entry_time_rule     TEXT,
                entry_price_max     INTEGER,

                -- Aggregate results
                sample_size         INTEGER DEFAULT 0,
                wins                INTEGER DEFAULT 0,
                losses              INTEGER DEFAULT 0,
                win_rate            REAL,
                total_pnl_c         INTEGER DEFAULT 0,
                avg_pnl_c           REAL,
                best_pnl_c          INTEGER,
                worst_pnl_c         INTEGER,
                max_drawdown_c      INTEGER DEFAULT 0,

                -- Risk metrics
                profit_factor       REAL,
                expectancy_c        REAL,
                max_consecutive_losses INTEGER DEFAULT 0,

                -- Confidence
                ci_lower            REAL,
                ci_upper            REAL,
                ev_per_trade_c      REAL,

                -- Time-weighted metrics
                weighted_win_rate   REAL,
                weighted_ev_c       REAL,

                -- Walk-forward out-of-sample validation
                oos_ev_c            REAL,
                oos_win_rate        REAL,
                oos_sample_size     INTEGER DEFAULT 0,

                -- Time range
                first_observation   TEXT,
                last_observation    TEXT,
                updated_at          TEXT NOT NULL,

                -- FDR / statistical rigor
                fdr_significant     INTEGER DEFAULT 0,
                fdr_q_value         REAL,
                sell_target         TEXT,

                -- Simulation hardening
                slippage_1c_ev      REAL,
                slippage_2c_ev      REAL,
                breakeven_fee_rate  REAL,
                quality_full_ev_c   REAL,
                quality_degraded_ev_c REAL,

                -- FDR t-test and rolling walk-forward
                pnl_std_c           REAL,

                UNIQUE(setup_key, strategy_key)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_sr_setup ON btc15m_strategy_results(setup_key)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_sr_ev ON btc15m_strategy_results(ev_per_trade_c)")

        # ── Trade price path (per-second during active trade) ─
        c.execute("""
            CREATE TABLE IF NOT EXISTS btc15m_price_path (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id        INTEGER NOT NULL REFERENCES btc15m_trades(id),
                captured_at     TEXT NOT NULL,
                minutes_left    REAL,
                yes_bid         INTEGER,
                yes_ask         INTEGER,
                no_bid          INTEGER,
                no_ask          INTEGER,
                our_side_bid    INTEGER,
                our_side_ask    INTEGER,
                btc_price       REAL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_pp_trade ON btc15m_price_path(trade_id)")

        # ── Live market price history (for dashboard chart backfill) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS btc15m_live_prices (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                ticker      TEXT,
                yes_ask     INTEGER,
                no_ask      INTEGER,
                yes_bid     INTEGER,
                no_bid      INTEGER
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_lp_ts ON btc15m_live_prices(ts)")

        # ── BTC probability surface (vol-conditioned) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS btc15m_probability_surface (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                distance_bucket TEXT NOT NULL,
                time_bucket     TEXT NOT NULL,
                vol_bucket      TEXT NOT NULL DEFAULT 'all',
                total           INTEGER DEFAULT 0,
                yes_wins        INTEGER DEFAULT 0,
                no_wins         INTEGER DEFAULT 0,
                yes_win_rate    REAL,
                avg_yes_price   REAL,
                avg_no_price    REAL,
                updated_at      TEXT NOT NULL,
                UNIQUE(distance_bucket, time_bucket, vol_bucket)
            )
        """)

        # ── Feature importance (which features predict outcomes) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS btc15m_feature_importance (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                feature_name    TEXT NOT NULL UNIQUE,
                importance      REAL,
                correlation     REAL,
                sample_size     INTEGER DEFAULT 0,
                method          TEXT DEFAULT 'logistic',
                updated_at      TEXT NOT NULL
            )
        """)

        # ── Regime stats (aggregated per regime label) ────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS btc15m_regime_stats (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                regime_label    TEXT NOT NULL UNIQUE,
                total_trades    INTEGER DEFAULT 0,
                wins            INTEGER DEFAULT 0,
                losses          INTEGER DEFAULT 0,
                total_pnl       REAL DEFAULT 0,
                avg_pnl         REAL DEFAULT 0,
                win_rate        REAL DEFAULT 0,
                ci_lower        REAL DEFAULT 0,
                ci_upper        REAL DEFAULT 1,
                risk_level      TEXT DEFAULT 'unknown',
                last_updated    TEXT
            )
        """)

        # ── Hourly stats (win rates by ET hour) ───────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS btc15m_hourly_stats (
                hour_et         INTEGER NOT NULL,
                day_of_week     INTEGER,
                total_trades    INTEGER DEFAULT 0,
                wins            INTEGER DEFAULT 0,
                losses          INTEGER DEFAULT 0,
                total_pnl       REAL DEFAULT 0,
                win_rate        REAL DEFAULT 0,
                ci_lower        REAL DEFAULT 0,
                ci_upper        REAL DEFAULT 1,
                risk_level      TEXT DEFAULT 'unknown',
                last_updated    TEXT,
                UNIQUE(hour_et, day_of_week)
            )
        """)

        # ── Regime opportunities (tracked during skipped markets) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS btc15m_regime_opportunities (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id            INTEGER REFERENCES btc15m_trades(id),
                ticker              TEXT NOT NULL,
                market_id           INTEGER REFERENCES btc15m_markets(id),

                -- Regime context
                regime_label        TEXT,
                coarse_regime       TEXT,
                trend_regime        INTEGER,
                vol_regime          INTEGER,

                -- Prediction
                predicted_side      TEXT,
                prediction_confidence REAL,
                prediction_basis    TEXT,

                -- Price tracking during skip (for the predicted side)
                predicted_side_best_ask  INTEGER,
                predicted_side_worst_ask INTEGER,
                predicted_side_avg_ask   REAL,
                predicted_side_price_count INTEGER DEFAULT 0,
                cheaper_side_avg_c       REAL,

                -- Market result (backfilled)
                market_result       TEXT,
                prediction_correct  INTEGER,

                -- Hypothetical PnL if we had bought 1 share at best ask
                hypo_cost_c         INTEGER,
                hypo_pnl_c          INTEGER,
                hypo_edge_pct       REAL,

                -- Timing
                hour_et             INTEGER,
                day_of_week         INTEGER,
                created_at          TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_ro_regime ON btc15m_regime_opportunities(regime_label)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_ro_predicted ON btc15m_regime_opportunities(predicted_side)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_ro_ticker ON btc15m_regime_opportunities(ticker)")

        # ── Exit simulations (what-if tracking for early exits) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS btc15m_exit_simulations (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id            INTEGER NOT NULL REFERENCES btc15m_trades(id),
                threshold_pct       INTEGER NOT NULL,
                trigger_price_c     INTEGER NOT NULL,
                trigger_mins_left   REAL NOT NULL,
                est_sell_pnl        REAL NOT NULL,
                actual_outcome      TEXT,
                actual_pnl          REAL,
                regime_label        TEXT,
                created_at          TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_exsim_trade ON btc15m_exit_simulations(trade_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_exsim_regime ON btc15m_exit_simulations(regime_label)")

        # ── Convergence metric snapshots ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS btc15m_metric_snapshots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at TEXT NOT NULL,
                metrics     TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_ms_ts ON btc15m_metric_snapshots(recorded_at)")


# ═══════════════════════════════════════════════════════════════
#  SHARED HELPERS
# ═══════════════════════════════════════════════════════════════

def _wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple:
    """Wilson score confidence interval for a proportion."""
    if n == 0:
        return 0.0, 1.0
    p = wins / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    margin = (z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2))) / denom
    return round(max(0, center - margin), 4), round(min(1, center + margin), 4)


def _classify_risk(score: float, count: int, min_known: int) -> str:
    """Map a composite risk score (0-100) to a risk level string."""
    if count < min_known:
        return "unknown"
    if score >= REGIME_THRESHOLDS["low_risk_floor"]:
        return "low"
    elif score >= REGIME_THRESHOLDS["moderate_risk_floor"]:
        return "moderate"
    elif score >= REGIME_THRESHOLDS["high_risk_floor"]:
        return "high"
    else:
        return "terrible"


def compute_strategy_risk_score(row: dict) -> float:
    """
    Composite risk score (0-100, higher = safer) for a strategy_results row.

    Five weighted components:
      1. EV Signal (30%) — profitability from weighted/unweighted EV
      2. Statistical Confidence (20%) — sample size, CI width, FDR significance
      3. Out-of-Sample Validation (20%) — walk-forward OOS performance
      4. Downside Risk (15%) — PnL volatility, max consec losses, profit factor
      5. Robustness (15%) — slippage survival, fee rate margin
    """
    components = []  # list of (score_0_100, weight)

    # ── 1. EV Signal (30%) ──
    ev = row.get("weighted_ev_c")
    if ev is None:
        ev = row.get("ev_per_trade_c") or 0
    if ev <= -5:
        ev_score = 0
    elif ev <= 0:
        ev_score = 20 * (1 + ev / 5)
    elif ev <= 3:
        ev_score = 20 + 45 * (ev / 3)
    elif ev <= 8:
        ev_score = 65 + 35 * ((ev - 3) / 5)
    else:
        ev_score = 100
    components.append((ev_score, 0.30))

    # ── 2. Statistical Confidence (20%) ──
    n = row.get("sample_size") or 0
    ci_lo = row.get("ci_lower") or 0
    ci_hi = row.get("ci_upper") or 1
    ci_width = ci_hi - ci_lo

    n_score = min(100, 40 * math.log10(max(n, 1)))
    ci_score = max(0, min(100, 100 * (1 - ci_width * 2)))
    fdr_sig = row.get("fdr_significant") or 0
    fdr_bonus = 15 if fdr_sig else 0

    conf_score = min(100, n_score * 0.40 + ci_score * 0.35 + fdr_bonus + (10 if ev > 0 else 0))
    components.append((conf_score, 0.20))

    # ── 3. Out-of-Sample Validation (20%) ──
    oos_ev = row.get("oos_ev_c")
    oos_n = row.get("oos_sample_size") or 0
    if oos_ev is not None and oos_n >= 5:
        if oos_ev >= 5:
            oos_score = 100
        elif oos_ev >= 2:
            oos_score = 65 + 35 * ((oos_ev - 2) / 3)
        elif oos_ev >= 0:
            oos_score = 30 + 35 * (oos_ev / 2)
        elif oos_ev >= -3:
            oos_score = 10 * (1 + oos_ev / 3)
        else:
            oos_score = 0

        if ev > 0 and oos_ev > 0:
            oos_score = min(100, oos_score + 10)
        elif ev > 0 and oos_ev < 0:
            oos_score = max(0, oos_score - 15)

        components.append((oos_score, 0.20))

    # ── 4. Downside Risk (15%) ──
    pnl_std = row.get("pnl_std_c")
    max_cl = row.get("max_consecutive_losses") or 0
    pf = row.get("profit_factor")

    dd_parts = []
    if pnl_std is not None and pnl_std > 0:
        sharpe = ev / max(pnl_std, 0.1)
        sharpe_score = min(100, max(0, 50 + sharpe * 40))
        dd_parts.append(sharpe_score)
    if n >= 10:
        cl_score = max(0, min(100, 100 - max_cl * 12))
        dd_parts.append(cl_score)
    if pf is not None:
        pf_score = min(100, max(0, (pf - 0.5) * 66))
        dd_parts.append(pf_score)

    if dd_parts:
        down_score = sum(dd_parts) / len(dd_parts)
        components.append((down_score, 0.15))

    # ── 5. Robustness (15%) ──
    slip1 = row.get("slippage_1c_ev")
    slip2 = row.get("slippage_2c_ev")
    bfe = row.get("breakeven_fee_rate")

    rob_parts = []
    if slip1 is not None and ev > 0:
        rob_parts.append(80 if slip1 > 0 else 20)
    if slip2 is not None and ev > 0:
        rob_parts.append(100 if slip2 > 0 else 30)
    if bfe is not None:
        fee_margin = bfe - 0.085
        if fee_margin > 0.10:
            rob_parts.append(100)
        elif fee_margin > 0.05:
            rob_parts.append(70)
        elif fee_margin > 0:
            rob_parts.append(40)
        else:
            rob_parts.append(10)

    if rob_parts:
        robust_score = sum(rob_parts) / len(rob_parts)
        components.append((robust_score, 0.15))

    # ── Normalize ──
    if not components:
        return 0.0
    total_weight = sum(w for _, w in components)
    score = sum(s * w for s, w in components) / total_weight if total_weight > 0 else 0
    return round(min(100, max(0, score)), 1)


def compute_trade_risk_score(win_rate: float, avg_pnl: float,
                              ci_lower: float, ci_upper: float,
                              total: int) -> float:
    """
    Simpler composite risk score (0-100) for real-trade regime stats.

    Four components:
      1. Win Rate (35%) — scaled 30-70% range to 0-100
      2. Avg PnL Direction (30%) — positive avg PnL is good
      3. CI Confidence (20%) — narrow CI = more trusted
      4. Sample Size (15%) — more data = more reliable
    """
    wr_score = max(0, min(100, (win_rate - 0.30) / 0.35 * 100))

    if avg_pnl <= -5:
        pnl_score = 0
    elif avg_pnl <= 0:
        pnl_score = 40 * (1 + avg_pnl / 5)
    elif avg_pnl <= 5:
        pnl_score = 40 + 60 * (avg_pnl / 5)
    else:
        pnl_score = 100

    ci_width = ci_upper - ci_lower
    ci_score = max(0, min(100, 100 * (1 - ci_width * 2)))

    n_score = min(100, 40 * math.log10(max(total, 1)))

    score = wr_score * 0.35 + pnl_score * 0.30 + ci_score * 0.20 + n_score * 0.15
    return round(min(100, max(0, score)), 1)


# ═══════════════════════════════════════════════════════════════
#  MARKETS
# ═══════════════════════════════════════════════════════════════

def upsert_market(ticker: str, close_time_utc: str, hour_et: int,
                  minute_et: int, day_of_week: int) -> int:
    with get_conn() as c:
        c.execute("""
            INSERT INTO btc15m_markets (ticker, close_time_utc, hour_et, minute_et,
                                        day_of_week, is_weekend, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                close_time_utc = excluded.close_time_utc
        """, (ticker, close_time_utc, hour_et, minute_et,
              day_of_week, int(day_of_week >= 5), now_utc()))
        row = c.execute("SELECT id FROM btc15m_markets WHERE ticker = ?",
                        (ticker,)).fetchone()
        return row["id"]


def update_market_outcome(market_id: int, outcome: str):
    with get_conn() as c:
        c.execute("UPDATE btc15m_markets SET outcome = ? WHERE id = ?",
                  (outcome, market_id))


# ═══════════════════════════════════════════════════════════════
#  TRADES
# ═══════════════════════════════════════════════════════════════

def insert_trade(data: dict) -> int:
    data["created_at"] = now_utc()
    cols = ", ".join(data.keys())
    placeholders = ", ".join(["?"] * len(data))
    with get_conn() as c:
        cur = c.execute(f"INSERT INTO btc15m_trades ({cols}) VALUES ({placeholders})",
                        list(data.values()))
        return cur.lastrowid


def update_trade(trade_id: int, data: dict):
    sets = ", ".join(f"{k} = ?" for k in data.keys())
    with get_conn() as c:
        c.execute(f"UPDATE btc15m_trades SET {sets} WHERE id = ?",
                  list(data.values()) + [trade_id])


def get_trade(trade_id: int) -> dict | None:
    with get_conn() as c:
        row = c.execute("SELECT * FROM btc15m_trades WHERE id = ?",
                        (trade_id,)).fetchone()
        return row_to_dict(row)


def get_open_trade() -> dict | None:
    with get_conn() as c:
        row = c.execute("""
            SELECT * FROM btc15m_trades WHERE outcome = 'open'
            ORDER BY created_at DESC LIMIT 1
        """).fetchone()
        return row_to_dict(row)


def get_recent_trades(limit: int = 50) -> list:
    with get_conn() as c:
        rows = c.execute("""
            SELECT * FROM btc15m_trades
            WHERE outcome IN ('win', 'loss', 'skipped', 'no_fill', 'error', 'open')
            ORDER BY created_at DESC LIMIT ?
        """, (limit,)).fetchall()
        return rows_to_list(rows)


def delete_trades(trade_ids: list):
    """Delete trades and their price paths, exit sims, regime opportunities."""
    if not trade_ids:
        return 0
    placeholders = ",".join(["?"] * len(trade_ids))
    with get_conn() as c:
        c.execute(f"DELETE FROM btc15m_price_path WHERE trade_id IN ({placeholders})",
                  trade_ids)
        c.execute(f"DELETE FROM btc15m_exit_simulations WHERE trade_id IN ({placeholders})",
                  trade_ids)
        c.execute(f"DELETE FROM btc15m_regime_opportunities WHERE trade_id IN ({placeholders})",
                  trade_ids)
        c.execute(f"DELETE FROM btc15m_trades WHERE id IN ({placeholders})",
                  trade_ids)
        return len(trade_ids)


# ═══════════════════════════════════════════════════════════════
#  PRICE PATH
# ═══════════════════════════════════════════════════════════════

def insert_price_point(trade_id: int, data: dict):
    data["trade_id"] = trade_id
    data["captured_at"] = now_utc()
    cols = ", ".join(data.keys())
    placeholders = ", ".join(["?"] * len(data))
    with get_conn() as c:
        c.execute(f"INSERT INTO btc15m_price_path ({cols}) VALUES ({placeholders})",
                  list(data.values()))


def get_price_path(trade_id: int) -> list:
    with get_conn() as c:
        rows = c.execute("""
            SELECT * FROM btc15m_price_path WHERE trade_id = ?
            ORDER BY captured_at ASC
        """, (trade_id,)).fetchall()
        return rows_to_list(rows)


# ═══════════════════════════════════════════════════════════════
#  LIVE PRICES
# ═══════════════════════════════════════════════════════════════

def insert_live_price(ticker: str, yes_ask, no_ask, yes_bid, no_bid):
    """Record a live market price snapshot. Called each poll."""
    with get_conn() as c:
        c.execute("""
            INSERT INTO btc15m_live_prices (ts, ticker, yes_ask, no_ask, yes_bid, no_bid)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (now_utc(), ticker, yes_ask, no_ask, yes_bid, no_bid))
        # Cleanup: keep only last 20 minutes of data
        c.execute("""
            DELETE FROM btc15m_live_prices WHERE ts < datetime('now', '-20 minutes')
        """)


def get_live_prices(ticker: str = None, limit: int = 900) -> list:
    """Get recent live prices, optionally filtered by ticker."""
    with get_conn() as c:
        if ticker:
            rows = c.execute("""
                SELECT ts, ticker, yes_ask, no_ask, yes_bid, no_bid
                FROM btc15m_live_prices WHERE ticker = ?
                ORDER BY id DESC LIMIT ?
            """, (ticker, limit)).fetchall()
        else:
            rows = c.execute("""
                SELECT ts, ticker, yes_ask, no_ask, yes_bid, no_bid
                FROM btc15m_live_prices ORDER BY id DESC LIMIT ?
            """, (limit,)).fetchall()
        return rows_to_list(list(reversed(rows)))


# ═══════════════════════════════════════════════════════════════
#  TRADE SUMMARY & LIFETIME STATS
# ═══════════════════════════════════════════════════════════════

def get_trade_summary() -> dict:
    """Dashboard summary stats (excludes ignored trades)."""
    with get_conn() as c:
        row = c.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                SUM(CASE WHEN outcome IN ('skipped','no_fill') THEN 1 ELSE 0 END) as skips,
                COALESCE(SUM(pnl), 0) as total_pnl,
                AVG(CASE WHEN outcome='win' THEN pnl END) as avg_win,
                AVG(CASE WHEN outcome='loss' THEN pnl END) as avg_loss
            FROM btc15m_trades
            WHERE outcome IN ('win','loss','skipped','no_fill')
              AND COALESCE(is_ignored, 0) = 0
        """).fetchone()
        return row_to_dict(row)


def get_lifetime_stats() -> dict:
    """Comprehensive lifetime stats for the dashboard."""
    with get_conn() as c:
        # Core W/L stats
        core = c.execute("""
            SELECT
                COUNT(*) as trades_placed,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                SUM(CASE WHEN outcome IN ('skipped','no_fill') THEN 1 ELSE 0 END) as skips,
                COALESCE(SUM(pnl), 0) as total_pnl,
                COALESCE(SUM(actual_cost), 0) as total_wagered,
                COALESCE(SUM(fees_paid), 0) as total_fees,
                MAX(pnl) as best_trade_pnl,
                MIN(pnl) as worst_trade_pnl,
                AVG(CASE WHEN outcome='win' THEN pnl END) as avg_win_pnl,
                AVG(CASE WHEN outcome='loss' THEN pnl END) as avg_loss_pnl,
                SUM(CASE WHEN is_data_collection=1 THEN 1 ELSE 0 END) as data_bets,
                MIN(created_at) as first_trade_at,
                MAX(created_at) as last_trade_at
            FROM btc15m_trades
            WHERE outcome IN ('win','loss')
              AND COALESCE(is_ignored, 0) = 0
        """).fetchone()
        stats = row_to_dict(core) or {}

        # Win/loss streaks
        rows = c.execute("""
            SELECT outcome FROM btc15m_trades
            WHERE outcome IN ('win','loss')
              AND COALESCE(is_ignored, 0) = 0
            ORDER BY created_at ASC
        """).fetchall()
        outcomes = [r["outcome"] for r in rows]

        best_win_streak = 0
        worst_loss_streak = 0
        current_streak_type = None
        current_streak_len = 0
        for o in outcomes:
            if o == current_streak_type:
                current_streak_len += 1
            else:
                current_streak_type = o
                current_streak_len = 1
            if o == "win":
                best_win_streak = max(best_win_streak, current_streak_len)
            else:
                worst_loss_streak = max(worst_loss_streak, current_streak_len)

        stats["best_win_streak"] = best_win_streak
        stats["worst_loss_streak"] = worst_loss_streak
        stats["current_streak_type"] = current_streak_type
        stats["current_streak_len"] = current_streak_len

        # Format best/worst trade
        best = stats.get("best_trade_pnl")
        worst = stats.get("worst_trade_pnl")
        stats["best_trade_str"] = f"+${best:.2f}" if best and best > 0 else ("$0.00" if not best else f"-${abs(best):.2f}")
        stats["worst_trade_str"] = f"-${abs(worst):.2f}" if worst and worst < 0 else ("$0.00" if not worst else f"+${worst:.2f}")

        # Max drawdown + peak PnL
        pnl_rows = c.execute("""
            SELECT pnl FROM btc15m_trades
            WHERE outcome IN ('win','loss')
              AND COALESCE(is_ignored, 0) = 0
            ORDER BY created_at ASC
        """).fetchall()
        running = 0.0
        peak = 0.0
        max_dd = 0.0
        for r in pnl_rows:
            running += r["pnl"] or 0
            peak = max(peak, running)
            max_dd = max(max_dd, peak - running)
        stats["peak_pnl"] = round(peak, 2)
        stats["max_drawdown"] = round(max_dd, 2)

        # ROI
        wagered = stats.get("total_wagered", 0)
        stats["roi_pct"] = round(
            (stats.get("total_pnl", 0) / wagered * 100) if wagered > 0 else 0, 1
        )

        # Win rate
        w = stats.get("wins", 0) or 0
        l = stats.get("losses", 0) or 0
        total = w + l
        stats["win_rate_pct"] = round(w / total * 100, 1) if total > 0 else 0

        # Profit factor
        total_wins = c.execute("""
            SELECT COALESCE(SUM(pnl), 0) as s FROM btc15m_trades
            WHERE outcome='win' AND COALESCE(is_ignored,0)=0
        """).fetchone()["s"]
        total_losses = abs(c.execute("""
            SELECT COALESCE(SUM(pnl), 0) as s FROM btc15m_trades
            WHERE outcome='loss' AND COALESCE(is_ignored,0)=0
        """).fetchone()["s"])
        stats["profit_factor"] = round(
            total_wins / total_losses if total_losses > 0 else 0, 2
        )

        # Daily P&L (last 14 days)
        daily_rows = c.execute("""
            SELECT
                DATE(created_at) as day,
                COUNT(*) as trades,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                COALESCE(SUM(pnl), 0) as pnl
            FROM btc15m_trades
            WHERE outcome IN ('win','loss')
              AND COALESCE(is_ignored, 0) = 0
              AND created_at >= DATE('now', '-14 days')
            GROUP BY DATE(created_at)
            ORDER BY day DESC
        """).fetchall()
        stats["daily_pnl"] = rows_to_list(daily_rows)

        # Entry delay breakdown
        delay_rows = c.execute("""
            SELECT
                COALESCE(entry_delay_minutes, 0) as delay_min,
                COUNT(*) as total,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                COALESCE(SUM(pnl), 0) as net_pnl
            FROM btc15m_trades
            WHERE outcome IN ('win','loss')
              AND COALESCE(is_ignored, 0) = 0
            GROUP BY COALESCE(entry_delay_minutes, 0)
            ORDER BY delay_min
        """).fetchall()
        stats["delay_breakdown"] = rows_to_list(delay_rows)

        # Price stability breakdown
        stability_rows = c.execute("""
            SELECT
                CASE
                    WHEN price_stability_c IS NULL THEN 'N/A'
                    WHEN price_stability_c <= 3 THEN 'Tight (0-3c)'
                    WHEN price_stability_c <= 8 THEN 'Normal (4-8c)'
                    WHEN price_stability_c <= 15 THEN 'Wide (9-15c)'
                    ELSE 'Very Wide (16c+)'
                END as stability_bucket,
                COUNT(*) as total,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                COALESCE(SUM(pnl), 0) as net_pnl,
                AVG(price_stability_c) as avg_stability
            FROM btc15m_trades
            WHERE outcome IN ('win','loss')
              AND COALESCE(is_ignored, 0) = 0
            GROUP BY stability_bucket
            ORDER BY avg_stability
        """).fetchall()
        stats["stability_breakdown"] = rows_to_list(stability_rows)

        # Volatility level breakdown
        vol_rows = c.execute("""
            SELECT
                COALESCE(vol_regime, 0) as vol_level,
                COUNT(*) as total,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                COALESCE(SUM(pnl), 0) as net_pnl
            FROM btc15m_trades
            WHERE outcome IN ('win','loss')
              AND COALESCE(is_ignored, 0) = 0
            GROUP BY vol_level
            ORDER BY vol_level
        """).fetchall()
        stats["vol_breakdown"] = rows_to_list(vol_rows)

        # Hourly performance (by CT hour)
        hourly_rows = c.execute("""
            SELECT
                CASE WHEN hour_et IS NOT NULL THEN (hour_et - 1 + 24) % 24
                     ELSE CAST(STRFTIME('%H', created_at, '-5 hours') AS INTEGER)
                END as hour_ct,
                COUNT(*) as total,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                COALESCE(SUM(pnl), 0) as net_pnl
            FROM btc15m_trades
            WHERE outcome IN ('win','loss')
              AND COALESCE(is_ignored, 0) = 0
            GROUP BY hour_ct
            ORDER BY hour_ct
        """).fetchall()
        stats["hourly_breakdown"] = rows_to_list(hourly_rows)

        # Side performance (YES vs NO)
        side_rows = c.execute("""
            SELECT
                UPPER(side) as side,
                COUNT(*) as total,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                COALESCE(SUM(pnl), 0) as net_pnl,
                AVG(CASE WHEN outcome='win' THEN pnl END) as avg_win,
                AVG(CASE WHEN outcome='loss' THEN pnl END) as avg_loss
            FROM btc15m_trades
            WHERE outcome IN ('win','loss')
              AND COALESCE(is_ignored, 0) = 0
              AND side IN ('yes','no')
            GROUP BY UPPER(side)
        """).fetchall()
        stats["side_breakdown"] = rows_to_list(side_rows)

        # Entry price performance
        price_rows = c.execute("""
            SELECT
                avg_fill_price_c as price_c,
                COUNT(*) as total,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                COALESCE(SUM(pnl), 0) as net_pnl
            FROM btc15m_trades
            WHERE outcome IN ('win','loss')
              AND COALESCE(is_ignored, 0) = 0
              AND avg_fill_price_c > 0
            GROUP BY avg_fill_price_c
            ORDER BY avg_fill_price_c
        """).fetchall()
        stats["price_breakdown"] = rows_to_list(price_rows)

        # Top/bottom regimes by net PnL (min 3 trades)
        regime_perf_rows = c.execute("""
            SELECT
                regime_label,
                COUNT(*) as total,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                COALESCE(SUM(pnl), 0) as net_pnl,
                ROUND(CAST(SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) AS REAL) / COUNT(*) * 100, 1) as win_rate
            FROM btc15m_trades
            WHERE outcome IN ('win','loss')
              AND COALESCE(is_ignored, 0) = 0
              AND regime_label IS NOT NULL
            GROUP BY regime_label
            HAVING COUNT(*) >= 3
            ORDER BY net_pnl DESC
        """).fetchall()
        stats["regime_performance"] = rows_to_list(regime_perf_rows)

        # Coarse regime performance
        coarse_rows = c.execute("""
            SELECT
                coarse_regime,
                COUNT(*) as total,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                COALESCE(SUM(pnl), 0) as net_pnl,
                ROUND(CAST(SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) AS REAL)
                    / NULLIF(COUNT(*), 0) * 100, 1) as win_rate
            FROM btc15m_trades
            WHERE outcome IN ('win','loss')
              AND COALESCE(is_ignored,0)=0
              AND coarse_regime IS NOT NULL
            GROUP BY coarse_regime
            HAVING COUNT(*) >= 2
            ORDER BY net_pnl DESC
        """).fetchall()
        stats["coarse_regime_performance"] = rows_to_list(coarse_rows)

        # Spread at entry breakdown
        spread_rows = c.execute("""
            SELECT
                CASE
                    WHEN spread_at_entry_c IS NULL THEN 'N/A'
                    WHEN spread_at_entry_c <= 3 THEN 'Tight (1-3c)'
                    WHEN spread_at_entry_c <= 6 THEN 'Normal (4-6c)'
                    WHEN spread_at_entry_c <= 10 THEN 'Wide (7-10c)'
                    ELSE 'Very Wide (11c+)'
                END as spread_bucket,
                COUNT(*) as total,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                COALESCE(SUM(pnl), 0) as net_pnl,
                AVG(spread_at_entry_c) as avg_spread
            FROM btc15m_trades
            WHERE outcome IN ('win','loss')
              AND COALESCE(is_ignored, 0) = 0
            GROUP BY spread_bucket
            ORDER BY avg_spread
        """).fetchall()
        stats["spread_breakdown"] = rows_to_list(spread_rows)

        # BTC move breakdown
        btc_move_rows = c.execute("""
            SELECT
                CASE
                    WHEN btc_move_pct IS NULL THEN 'N/A'
                    WHEN ABS(btc_move_pct) <= 0.05 THEN 'Flat (<0.05%)'
                    WHEN ABS(btc_move_pct) <= 0.15 THEN 'Small (0.05-0.15%)'
                    WHEN ABS(btc_move_pct) <= 0.3 THEN 'Medium (0.15-0.3%)'
                    ELSE 'Large (>0.3%)'
                END as btc_move_bucket,
                COUNT(*) as total,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                COALESCE(SUM(pnl), 0) as net_pnl,
                AVG(ABS(btc_move_pct)) as avg_btc_move
            FROM btc15m_trades
            WHERE outcome IN ('win','loss')
              AND COALESCE(is_ignored, 0) = 0
            GROUP BY btc_move_bucket
            ORDER BY avg_btc_move
        """).fetchall()
        stats["btc_move_breakdown"] = rows_to_list(btc_move_rows)

        return stats


# ═══════════════════════════════════════════════════════════════
#  REGIME STATS
# ═══════════════════════════════════════════════════════════════

def update_regime_stats(regime_label: str) -> dict:
    """Recompute stats for a specific regime label from real trade data only."""
    with get_conn() as c:
        existing = c.execute(
            "SELECT risk_level, total_trades FROM btc15m_regime_stats WHERE regime_label = ?",
            (regime_label,)
        ).fetchone()
        old_risk = existing["risk_level"] if existing else None
        old_total = existing["total_trades"] if existing else 0
        is_new = existing is None

        real = c.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) as losses,
                SUM(COALESCE(pnl, 0)) as total_pnl
            FROM btc15m_trades
            WHERE regime_label = ? AND outcome IN ('win', 'loss')
              AND COALESCE(is_ignored, 0) = 0
        """, (regime_label,)).fetchone()

        total = real["total"] or 0
        wins = real["wins"] or 0
        losses = real["losses"] or 0
        total_pnl = real["total_pnl"] or 0

        avg_pnl = total_pnl / total if total > 0 else 0
        win_rate = wins / total if total > 0 else 0
        ci_low, ci_high = _wilson_ci(wins, total)

        min_known = REGIME_THRESHOLDS["min_trades_known"]
        risk_score = compute_trade_risk_score(win_rate, avg_pnl, ci_low, ci_high, total)
        risk_level = _classify_risk(risk_score, total, min_known)

        c.execute("""
            INSERT INTO btc15m_regime_stats
                (regime_label, total_trades, wins, losses, total_pnl,
                 avg_pnl, win_rate, ci_lower, ci_upper, risk_level, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(regime_label) DO UPDATE SET
                total_trades = excluded.total_trades,
                wins = excluded.wins,
                losses = excluded.losses,
                total_pnl = excluded.total_pnl,
                avg_pnl = excluded.avg_pnl,
                win_rate = excluded.win_rate,
                ci_lower = excluded.ci_lower,
                ci_upper = excluded.ci_upper,
                risk_level = excluded.risk_level,
                last_updated = excluded.last_updated
        """, (regime_label, total, wins, losses, round(total_pnl, 2),
              round(avg_pnl, 2), round(win_rate, 4), ci_low, ci_high,
              risk_level, now_utc()))

        return {
            "is_new": is_new,
            "old_risk": old_risk,
            "new_risk": risk_level,
            "old_total": old_total,
            "total": total,
            "win_rate": win_rate,
            "risk_score": risk_score,
        }


def get_all_regime_stats() -> list:
    with get_conn() as c:
        rows = c.execute("""
            SELECT * FROM btc15m_regime_stats ORDER BY total_trades DESC
        """).fetchall()
        return rows_to_list(rows)


def get_regime_risk(regime_label: str) -> dict:
    """Get risk level for a regime. Returns dict with risk info."""
    with get_conn() as c:
        row = c.execute(
            "SELECT * FROM btc15m_regime_stats WHERE regime_label = ?",
            (regime_label,)
        ).fetchone()
        if row:
            return row_to_dict(row)
        return {
            "regime_label": regime_label,
            "total_trades": 0,
            "risk_level": "unknown",
            "win_rate": 0,
            "ci_lower": 0,
            "ci_upper": 1,
        }


def get_strategy_risk(regime_label: str, strategy_key: str,
                      min_known: int = 10) -> dict:
    """Get risk level for a specific strategy in a specific regime.
    Uses strategy_results table. No global fallback."""
    with get_conn() as c:
        setup_key = f"regime:{regime_label}"
        row = c.execute("""
            SELECT win_rate, sample_size, ev_per_trade_c,
                   ci_lower, ci_upper, profit_factor,
                   weighted_ev_c, weighted_win_rate,
                   oos_ev_c, oos_win_rate, oos_sample_size,
                   fdr_significant, fdr_q_value,
                   pnl_std_c, max_consecutive_losses, max_drawdown_c,
                   slippage_1c_ev, slippage_2c_ev, breakeven_fee_rate
            FROM btc15m_strategy_results
            WHERE setup_key = ? AND strategy_key = ?
        """, (setup_key, strategy_key)).fetchone()

        if row and (row["sample_size"] or 0) >= min_known:
            rd = dict(row)
            risk_score = compute_strategy_risk_score(rd)
            min_sim = REGIME_THRESHOLDS.get("min_sim_known", min_known)
            risk = _classify_risk(risk_score, row["sample_size"], min_sim)

            return {
                "risk_level": risk,
                "risk_score": risk_score,
                "win_rate": row["win_rate"] or 0,
                "ev_per_trade_c": row["ev_per_trade_c"],
                "sample_size": row["sample_size"],
                "ci_lower": row["ci_lower"] or 0,
                "ci_upper": row["ci_upper"] or 1,
                "profit_factor": row["profit_factor"],
                "setup_key": setup_key,
                "strategy_key": strategy_key,
            }

    return {
        "risk_level": "unknown",
        "risk_score": 0,
        "win_rate": 0,
        "ev_per_trade_c": None,
        "sample_size": 0,
        "ci_lower": 0,
        "ci_upper": 1,
        "profit_factor": None,
        "setup_key": None,
        "strategy_key": strategy_key,
    }


# ═══════════════════════════════════════════════════════════════
#  COARSE REGIME STATS
# ═══════════════════════════════════════════════════════════════

def update_coarse_regime_stats(coarse_label: str):
    """Compute stats for a coarse regime label from real trade data only."""
    with get_conn() as c:
        real = c.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) as losses,
                SUM(COALESCE(pnl, 0)) as total_pnl
            FROM btc15m_trades
            WHERE coarse_regime = ? AND outcome IN ('win', 'loss')
              AND COALESCE(is_ignored, 0) = 0
        """, (coarse_label,)).fetchone()

        total = real["total"] or 0
        total_pnl = real["total_pnl"] or 0
        wins = real["wins"] or 0
        losses = real["losses"] or 0

        avg_pnl = total_pnl / total if total > 0 else 0
        win_rate = wins / total if total > 0 else 0
        ci_low, ci_high = _wilson_ci(wins, total)

        min_known = REGIME_THRESHOLDS["min_trades_known"]
        risk_score = compute_trade_risk_score(win_rate, avg_pnl, ci_low, ci_high, total)
        risk_level = _classify_risk(risk_score, total, min_known)

        prefixed = f"coarse:{coarse_label}"
        c.execute("""
            INSERT INTO btc15m_regime_stats
                (regime_label, total_trades, wins, losses, total_pnl,
                 avg_pnl, win_rate, ci_lower, ci_upper, risk_level, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(regime_label) DO UPDATE SET
                total_trades = excluded.total_trades,
                wins = excluded.wins,
                losses = excluded.losses,
                total_pnl = excluded.total_pnl,
                avg_pnl = excluded.avg_pnl,
                win_rate = excluded.win_rate,
                ci_lower = excluded.ci_lower,
                ci_upper = excluded.ci_upper,
                risk_level = excluded.risk_level,
                last_updated = excluded.last_updated
        """, (prefixed, total, wins, losses, round(total_pnl, 2),
              round(avg_pnl, 2), round(win_rate, 4), ci_low, ci_high,
              risk_level, now_utc()))


def refresh_all_coarse_regime_stats():
    """Recompute stats for all coarse regime labels."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT DISTINCT coarse_regime FROM btc15m_trades
            WHERE coarse_regime IS NOT NULL
              AND outcome IN ('win', 'loss')
              AND COALESCE(is_ignored, 0) = 0
        """).fetchall()
    for row in rows:
        update_coarse_regime_stats(row["coarse_regime"])


def get_coarse_regime_risk(coarse_label: str) -> dict:
    """Get risk info for a coarse regime (looks up with coarse: prefix)."""
    prefixed = f"coarse:{coarse_label}"
    with get_conn() as c:
        row = c.execute(
            "SELECT * FROM btc15m_regime_stats WHERE regime_label = ?",
            (prefixed,)
        ).fetchone()
        if row:
            return row_to_dict(row)
        return {
            "regime_label": prefixed,
            "total_trades": 0,
            "risk_level": "unknown",
            "win_rate": 0,
            "ci_lower": 0,
            "ci_upper": 1,
        }


# ═══════════════════════════════════════════════════════════════
#  HOURLY STATS
# ═══════════════════════════════════════════════════════════════

def update_hourly_stats(hour_et: int, day_of_week: int = None):
    """Compute win rate stats for a specific ET hour (and optionally day)."""
    with get_conn() as c:
        if day_of_week is not None:
            real = c.execute("""
                SELECT COUNT(*) as total,
                    SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) as losses,
                    SUM(COALESCE(pnl, 0)) as total_pnl
                FROM btc15m_trades
                WHERE hour_et = ? AND day_of_week = ?
                  AND outcome IN ('win', 'loss')
                  AND COALESCE(is_ignored, 0) = 0
            """, (hour_et, day_of_week)).fetchone()
        else:
            real = c.execute("""
                SELECT COUNT(*) as total,
                    SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) as losses,
                    SUM(COALESCE(pnl, 0)) as total_pnl
                FROM btc15m_trades
                WHERE hour_et = ? AND outcome IN ('win', 'loss')
                  AND COALESCE(is_ignored, 0) = 0
            """, (hour_et,)).fetchone()

        total = real["total"] or 0
        total_pnl = real["total_pnl"] or 0
        wins = real["wins"] or 0
        losses = real["losses"] or 0
        win_rate = wins / total if total > 0 else 0
        ci_low, ci_high = _wilson_ci(wins, total)

        min_known = REGIME_THRESHOLDS["min_trades_known"]
        if total < min_known:
            risk_level = "unknown"
        elif win_rate >= REGIME_THRESHOLDS["low_risk_floor"]:
            risk_level = "low"
        elif win_rate >= REGIME_THRESHOLDS["moderate_risk_floor"]:
            risk_level = "moderate"
        elif win_rate >= REGIME_THRESHOLDS["high_risk_floor"]:
            risk_level = "high"
        else:
            risk_level = "terrible"

        c.execute("""
            INSERT INTO btc15m_hourly_stats
                (hour_et, day_of_week, total_trades, wins, losses,
                 total_pnl, win_rate, ci_lower, ci_upper, risk_level,
                 last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(hour_et, day_of_week) DO UPDATE SET
                total_trades = excluded.total_trades,
                wins = excluded.wins,
                losses = excluded.losses,
                total_pnl = excluded.total_pnl,
                win_rate = excluded.win_rate,
                ci_lower = excluded.ci_lower,
                ci_upper = excluded.ci_upper,
                risk_level = excluded.risk_level,
                last_updated = excluded.last_updated
        """, (hour_et, day_of_week, total, wins, losses,
              round(total_pnl, 2), round(win_rate, 4),
              ci_low, ci_high, risk_level, now_utc()))


def refresh_all_hourly_stats():
    """Recompute hourly stats for all hours (with and without day breakdown)."""
    with get_conn() as c:
        hours = c.execute("""
            SELECT DISTINCT hour_et FROM btc15m_trades
            WHERE hour_et IS NOT NULL
              AND outcome IN ('win', 'loss')
              AND COALESCE(is_ignored, 0) = 0
        """).fetchall()
        hour_days = c.execute("""
            SELECT DISTINCT hour_et, day_of_week FROM btc15m_trades
            WHERE hour_et IS NOT NULL AND day_of_week IS NOT NULL
              AND outcome IN ('win', 'loss')
              AND COALESCE(is_ignored, 0) = 0
        """).fetchall()

    for row in hours:
        update_hourly_stats(row["hour_et"], day_of_week=None)
    for row in hour_days:
        update_hourly_stats(row["hour_et"], day_of_week=row["day_of_week"])


def get_all_hourly_stats() -> list:
    """Get all hourly stats for dashboard display."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT * FROM btc15m_hourly_stats
            WHERE day_of_week IS NULL
            ORDER BY hour_et
        """).fetchall()
        return rows_to_list(rows)


def get_hourly_risk(hour_et: int, day_of_week: int = None) -> dict:
    """Get risk info for a specific ET hour."""
    with get_conn() as c:
        for dow in ([day_of_week, None] if day_of_week is not None else [None]):
            if dow is not None:
                row = c.execute(
                    "SELECT * FROM btc15m_hourly_stats WHERE hour_et = ? AND day_of_week = ?",
                    (hour_et, dow)
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT * FROM btc15m_hourly_stats WHERE hour_et = ? AND day_of_week IS NULL",
                    (hour_et,)
                ).fetchone()
            if row:
                return row_to_dict(row)
    return {
        "hour_et": hour_et,
        "total_trades": 0,
        "risk_level": "unknown",
        "win_rate": 0,
    }


# ═══════════════════════════════════════════════════════════════
#  RECOMPUTE ALL STATS
# ═══════════════════════════════════════════════════════════════

def recompute_all_stats(plugin_id: str = "btc_15m"):
    """Recompute all derived stats from the trades table.
    Called after deleting trades to ensure consistency."""
    from db import update_plugin_state

    with get_conn() as c:
        row = c.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END), 0) as wins,
                COALESCE(SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END), 0) as losses,
                COALESCE(SUM(pnl), 0) as total_pnl
            FROM btc15m_trades
            WHERE outcome IN ('win', 'loss')
              AND COALESCE(is_ignored, 0) = 0
        """).fetchone()

    update_plugin_state(plugin_id, {
        "lifetime_wins": row["wins"],
        "lifetime_losses": row["losses"],
        "lifetime_pnl": round(row["total_pnl"], 2),
    })

    # Recompute all regime stats
    with get_conn() as c:
        c.execute("DELETE FROM btc15m_regime_stats")
        rows = c.execute("""
            SELECT DISTINCT regime_label FROM btc15m_trades
            WHERE regime_label IS NOT NULL AND regime_label != 'unknown'
        """).fetchall()

    for row in rows:
        update_regime_stats(row["regime_label"])


# ═══════════════════════════════════════════════════════════════
#  SKIPPED TRADE BACKFILL
# ═══════════════════════════════════════════════════════════════

def get_skipped_trades_needing_result(limit: int = 50) -> list:
    """Find skipped trades where market_result is NULL and ticker exists."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT t.id, t.ticker, t.market_id, t.regime_label,
                   t.coarse_regime, t.hour_et, t.day_of_week,
                   t.side, t.avg_fill_price_c,
                   t.created_at
            FROM btc15m_trades t
            WHERE t.outcome = 'skipped'
              AND t.market_result IS NULL
              AND t.ticker IS NOT NULL
              AND t.ticker != 'n/a'
              AND datetime(t.created_at) < datetime('now', '-3 minutes')
            ORDER BY t.created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return rows_to_list(rows)


def backfill_skipped_result(trade_id: int, market_result: str,
                            cheaper_side: str = None):
    """Update a skipped trade with the actual market result."""
    with get_conn() as c:
        c.execute("UPDATE btc15m_trades SET market_result = ? WHERE id = ?",
                  (market_result, trade_id))


def get_skip_analysis() -> dict:
    """Analyze skipped trades — market results for observed markets."""
    with get_conn() as c:
        overview = c.execute("""
            SELECT
                COUNT(*) as total_skipped,
                SUM(CASE WHEN market_result IS NOT NULL THEN 1 ELSE 0 END) as with_result,
                SUM(CASE WHEN market_result = 'yes' THEN 1 ELSE 0 END) as result_yes,
                SUM(CASE WHEN market_result = 'no' THEN 1 ELSE 0 END) as result_no
            FROM btc15m_trades
            WHERE outcome = 'skipped'
        """).fetchone()

        by_regime = c.execute("""
            SELECT
                regime_label,
                SUM(CASE WHEN market_result IS NOT NULL THEN 1 ELSE 0 END) as n,
                SUM(CASE WHEN market_result = 'yes' THEN 1 ELSE 0 END) as result_yes,
                SUM(CASE WHEN market_result = 'no' THEN 1 ELSE 0 END) as result_no
            FROM btc15m_trades
            WHERE outcome = 'skipped' AND regime_label IS NOT NULL
              AND market_result IS NOT NULL
            GROUP BY regime_label
            ORDER BY SUM(CASE WHEN market_result IS NOT NULL THEN 1 ELSE 0 END) DESC
        """).fetchall()

        by_coarse = c.execute("""
            SELECT
                coarse_regime,
                SUM(CASE WHEN market_result IS NOT NULL THEN 1 ELSE 0 END) as n,
                SUM(CASE WHEN market_result = 'yes' THEN 1 ELSE 0 END) as result_yes,
                SUM(CASE WHEN market_result = 'no' THEN 1 ELSE 0 END) as result_no
            FROM btc15m_trades
            WHERE outcome = 'skipped' AND coarse_regime IS NOT NULL
              AND market_result IS NOT NULL
            GROUP BY coarse_regime
            ORDER BY SUM(CASE WHEN market_result IS NOT NULL THEN 1 ELSE 0 END) DESC
        """).fetchall()

        return {
            "overview": row_to_dict(overview),
            "by_regime": rows_to_list(by_regime),
            "by_coarse_regime": rows_to_list(by_coarse),
        }


# ═══════════════════════════════════════════════════════════════
#  STRATEGY
# ═══════════════════════════════════════════════════════════════

def get_top_strategies(min_samples: int = 20, limit: int = 20) -> list:
    """Get best strategy results by EV, filtering by minimum confidence."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT * FROM btc15m_strategy_results
            WHERE sample_size >= ?
              AND ev_per_trade_c > 0
              AND ci_lower > 0.4
            ORDER BY ev_per_trade_c DESC
            LIMIT ?
        """, (min_samples, limit)).fetchall()
        return rows_to_list(rows)


# ═══════════════════════════════════════════════════════════════
#  REGIME STABILITY
# ═══════════════════════════════════════════════════════════════

def get_prev_regime_label() -> str | None:
    """Get the regime label from the 2nd most recent snapshot."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT composite_label FROM regime_snapshots
            ORDER BY captured_at DESC LIMIT 2
        """).fetchall()
        if len(rows) >= 2:
            return rows[1]["composite_label"]
        return None


def get_regime_stability_summary(hours: int = 24) -> dict:
    """Get regime label stability metrics over a time window."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with get_conn() as c:
        rows = c.execute("""
            SELECT * FROM regime_stability_log
            WHERE captured_at > ?
            ORDER BY captured_at DESC
        """, (since,)).fetchall()
    data = rows_to_list(rows)
    if not data:
        return {"n": 0, "label_persistence": None, "coarse_persistence": None}

    n = len(data)
    label_changes = sum(1 for d in data if d.get("label_changed"))
    coarse_changes = sum(1 for d in data if d.get("coarse_changed"))
    return {
        "n": n,
        "hours": hours,
        "label_changes": label_changes,
        "coarse_changes": coarse_changes,
        "label_persistence_pct": round((1 - label_changes / n) * 100, 1) if n > 0 else None,
        "coarse_persistence_pct": round((1 - coarse_changes / n) * 100, 1) if n > 0 else None,
        "recent": data[:10],
    }


# ═══════════════════════════════════════════════════════════════
#  SHADOW TRADE ANALYSIS
# ═══════════════════════════════════════════════════════════════

def get_shadow_trade_analysis() -> dict:
    """Analyze shadow trades to measure the simulation-to-reality gap."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT outcome, pnl, side, shares_filled,
                   entry_price_c, avg_fill_price_c,
                   shadow_decision_price_c, shadow_fill_latency_ms,
                   regime_label, spread_at_entry_c,
                   yes_ask_at_entry, no_ask_at_entry,
                   created_at
            FROM btc15m_trades
            WHERE COALESCE(is_shadow, 0) = 1
              AND outcome IN ('win', 'loss')
            ORDER BY created_at DESC
        """).fetchall()
        trades = rows_to_list(rows)

    if not trades:
        return {"n": 0, "message": "No completed shadow trades yet"}

    n = len(trades)
    wins = sum(1 for t in trades if t["outcome"] == "win")
    total_pnl = sum(t.get("pnl") or 0 for t in trades)

    # Fill slippage: actual fill vs decision-time ask
    slippages = []
    latencies = []
    for t in trades:
        decision_price = t.get("shadow_decision_price_c")
        fill_price = t.get("avg_fill_price_c")
        if decision_price and fill_price and decision_price > 0 and fill_price > 0:
            slippages.append(fill_price - decision_price)
        latency = t.get("shadow_fill_latency_ms")
        if latency is not None:
            latencies.append(latency)

    # Per-spread-bucket analysis
    spread_buckets = {"tight_1_3": [], "normal_4_6": [], "wide_7+": []}
    for t in trades:
        spread = t.get("spread_at_entry_c")
        decision_price = t.get("shadow_decision_price_c")
        fill_price = t.get("avg_fill_price_c")
        if spread is None or not decision_price or not fill_price:
            continue
        slip = fill_price - decision_price
        if spread <= 3:
            spread_buckets["tight_1_3"].append(slip)
        elif spread <= 6:
            spread_buckets["normal_4_6"].append(slip)
        else:
            spread_buckets["wide_7+"].append(slip)

    spread_analysis = {}
    for bucket, slips in spread_buckets.items():
        if slips:
            spread_analysis[bucket] = {
                "n": len(slips),
                "avg_slippage_c": round(sum(slips) / len(slips), 2),
                "pct_zero_or_better": round(
                    sum(1 for s in slips if s <= 0) / len(slips), 2),
            }

    return {
        "n": n,
        "win_rate": round(wins / n, 4),
        "total_pnl_c": round(total_pnl, 0),
        "avg_pnl_per_trade_c": round(total_pnl / n, 2),
        "slippage": {
            "n_measured": len(slippages),
            "avg_c": round(sum(slippages) / len(slippages), 2) if slippages else 0,
            "max_c": max(slippages) if slippages else 0,
            "pct_zero_or_better": round(
                sum(1 for s in slippages if s <= 0) / len(slippages), 2
            ) if slippages else 0,
        },
        "latency": {
            "n_measured": len(latencies),
            "avg_ms": round(sum(latencies) / len(latencies), 0) if latencies else 0,
            "max_ms": max(latencies) if latencies else 0,
        },
        "by_spread": spread_analysis,
        "sim_reality_gap_c": round(
            sum(slippages) / len(slippages), 2
        ) if slippages else None,
    }


def reconcile_shadow_trades() -> dict:
    """Compare resolved shadow trades to simulation predictions.
    Requires strategy module for simulation — returns empty if unavailable."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT t.id, t.ticker, t.side, t.outcome, t.pnl,
                   t.avg_fill_price_c, t.shadow_decision_price_c,
                   t.shadow_fill_latency_ms, t.shares_filled,
                   t.actual_cost, t.spread_at_entry_c
            FROM btc15m_trades t
            WHERE COALESCE(t.is_shadow, 0) = 1
              AND t.outcome IN ('win', 'loss')
            ORDER BY t.created_at DESC
            LIMIT 200
        """).fetchall()
        shadow_trades = rows_to_list(rows)

        if not shadow_trades:
            return {"n": 0, "message": "No resolved shadow trades"}

        tickers = [t["ticker"] for t in shadow_trades if t.get("ticker")]
        if not tickers:
            return {"n": 0, "message": "No tickers found"}

        placeholders = ",".join("?" for _ in tickers)
        obs_rows = c.execute(f"""
            SELECT ticker, market_result, price_snapshots,
                   btc_price_at_open, realized_vol
            FROM btc15m_observations
            WHERE ticker IN ({placeholders})
              AND market_result IS NOT NULL
              AND price_snapshots IS NOT NULL
        """, tickers).fetchall()
        obs_by_ticker = {r["ticker"]: dict(r) for r in obs_rows}

    # Import simulation function
    try:
        from plugins.btc_15m.strategy import _simulate_one
    except ImportError:
        return {"n": 0, "message": "Strategy module not available"}

    import json as _json

    comparisons = []
    sim_pnls = []
    real_pnls = []

    for t in shadow_trades:
        obs = obs_by_ticker.get(t["ticker"])
        if not obs:
            continue

        snaps = _json.loads(obs.get("price_snapshots", "[]"))
        mr = obs.get("market_result")
        if not snaps or not mr or len(snaps) < 3:
            continue

        dur = max(s["t"] for s in snaps) if snaps else 0
        if dur < 60:
            continue

        entry_max = (t.get("shadow_decision_price_c") or
                     t.get("avg_fill_price_c") or 50)
        entry_max_sim = min(95, max(5, ((entry_max + 4) // 5) * 5))

        sim = _simulate_one(
            snaps, mr, dur,
            "cheaper", "early", entry_max_sim, "hold",
            btc_open=obs.get("btc_price_at_open"),
            realized_vol=obs.get("realized_vol"),
        )

        if not sim or not sim["entered"]:
            continue

        real_pnl = t.get("pnl") or 0
        sim_pnl = sim["pnl_c"] / 100.0

        sim_pnls.append(sim["pnl_c"])
        real_pnls.append(real_pnl * 100)

        comparisons.append({
            "ticker": t["ticker"],
            "side": t["side"],
            "outcome": t["outcome"],
            "real_pnl_c": round(real_pnl * 100, 1),
            "sim_pnl_c": sim["pnl_c"],
            "gap_c": round(real_pnl * 100 - sim["pnl_c"], 1),
            "fill_price_c": t.get("avg_fill_price_c"),
            "decision_price_c": t.get("shadow_decision_price_c"),
            "slippage_c": ((t.get("avg_fill_price_c") or 0)
                          - (t.get("shadow_decision_price_c") or 0)),
        })

    if not comparisons:
        return {"n": 0, "message": "No matchable shadow trades"}

    n = len(comparisons)
    avg_sim = sum(sim_pnls) / n
    avg_real = sum(real_pnls) / n
    gap = avg_real - avg_sim

    same_outcome = sum(1 for comp in comparisons
                       if (comp["sim_pnl_c"] > 0) == (comp["real_pnl_c"] > 0))

    return {
        "n": n,
        "avg_sim_pnl_c": round(avg_sim, 1),
        "avg_real_pnl_c": round(avg_real, 1),
        "systematic_gap_c": round(gap, 1),
        "gap_direction": ("sim_optimistic" if gap < -1
                          else "sim_pessimistic" if gap > 1
                          else "aligned"),
        "same_outcome_rate": round(same_outcome / n, 4),
        "recent_comparisons": comparisons[:20],
        "ev_adjustment_needed_c": round(-gap, 1) if abs(gap) > 1 else 0,
    }


# ═══════════════════════════════════════════════════════════════
#  PNL ATTRIBUTION
# ═══════════════════════════════════════════════════════════════

def get_pnl_attribution(days: int = 30) -> dict:
    """Decompose realized P&L into model edge, execution cost, exit method, side."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_conn() as c:
        rows = c.execute("""
            SELECT pnl, shares_filled, actual_cost, gross_proceeds,
                   outcome, exit_method, side,
                   entry_price_c, avg_fill_price_c,
                   market_implied_pct,
                   ev_per_contract_c,
                   yes_ask_at_entry, no_ask_at_entry,
                   spread_at_entry_c, regime_label,
                   created_at
            FROM btc15m_trades
            WHERE outcome IN ('win', 'loss')
              AND COALESCE(is_ignored, 0) = 0
              AND shares_filled > 0
              AND datetime(created_at) > datetime(?)
        """, (since,)).fetchall()
        trades = rows_to_list(rows)

    if not trades:
        return {"error": "No completed trades in window", "n": 0, "days": days}

    n = len(trades)
    total_pnl = sum(t.get("pnl") or 0 for t in trades)
    total_contracts = sum(t.get("shares_filled") or 1 for t in trades)

    # ── 1. Execution cost component ──
    slippages = []
    total_cost = 0
    for t in trades:
        entry = t.get("entry_price_c") or 0
        fill = t.get("avg_fill_price_c") or 0
        if entry > 0 and fill > 0:
            slippages.append(fill - entry)
        total_cost += t.get("actual_cost") or 0

    total_gross = sum(t.get("gross_proceeds") or 0 for t in trades)
    total_entry_value = sum((t.get("avg_fill_price_c") or 0) * (t.get("shares_filled") or 0)
                            for t in trades) / 100

    execution_component = {
        "total_cost_dollars": round(total_cost, 2),
        "total_gross_dollars": round(total_gross, 2),
        "avg_slippage_c": round(sum(slippages) / len(slippages), 2) if slippages else 0,
        "pct_zero_slippage": round(sum(1 for s in slippages if s <= 0) / len(slippages), 2) if slippages else 0,
        "implied_fee_pct": round((total_cost - total_entry_value) / total_entry_value * 100, 2) if total_entry_value > 0 else None,
    }

    # ── 2. Exit method component ──
    by_exit = {}
    for t in trades:
        method = t.get("exit_method") or "unknown"
        if method not in by_exit:
            by_exit[method] = {"pnl_sum": 0, "n": 0, "wins": 0}
        by_exit[method]["pnl_sum"] += t.get("pnl") or 0
        by_exit[method]["n"] += 1
        if t["outcome"] == "win":
            by_exit[method]["wins"] += 1

    exit_component = {}
    for method, data in by_exit.items():
        if data["n"] >= 3:
            exit_component[method] = {
                "n": data["n"],
                "avg_pnl_c": round(data["pnl_sum"] / data["n"], 2),
                "win_rate": round(data["wins"] / data["n"], 4),
                "total_pnl_c": round(data["pnl_sum"], 0),
            }

    # ── 3. Side selection component ──
    by_side = {}
    for t in trades:
        s = t.get("side") or "unknown"
        if s not in by_side:
            by_side[s] = {"n": 0, "wins": 0, "pnl_sum": 0}
        by_side[s]["n"] += 1
        by_side[s]["pnl_sum"] += t.get("pnl") or 0
        if t["outcome"] == "win":
            by_side[s]["wins"] += 1

    side_component = {}
    for s, data in by_side.items():
        if data["n"] >= 3:
            side_component[s] = {
                "n": data["n"],
                "win_rate": round(data["wins"] / data["n"], 4),
                "avg_pnl_c": round(data["pnl_sum"] / data["n"], 2),
            }

    return {
        "n": n,
        "days": days,
        "total_pnl_c": round(total_pnl, 0),
        "total_contracts": total_contracts,
        "avg_pnl_per_contract_c": round(total_pnl / total_contracts, 2) if total_contracts > 0 else 0,
        "execution_cost": execution_component,
        "exit_method": exit_component,
        "side_selection": side_component,
    }


# ═══════════════════════════════════════════════════════════════
#  OBSERVATIONS
# ═══════════════════════════════════════════════════════════════

def upsert_observation(data: dict) -> int:
    """Insert or update a market observation. Returns the row id."""
    ticker = data["ticker"]
    with get_conn() as c:
        existing = c.execute(
            "SELECT id FROM btc15m_observations WHERE ticker = ?", (ticker,)
        ).fetchone()
        if existing:
            obs_id = existing["id"]
            cols = ", ".join(f"{k} = ?" for k in data if k != "ticker")
            vals = [data[k] for k in data if k != "ticker"]
            if cols:
                c.execute(f"UPDATE btc15m_observations SET {cols} WHERE id = ?",
                          vals + [obs_id])
            return obs_id
        else:
            data.setdefault("created_at", now_utc())
            keys = ", ".join(data.keys())
            placeholders = ", ".join("?" for _ in data)
            c.execute(f"INSERT INTO btc15m_observations ({keys}) VALUES ({placeholders})",
                      list(data.values()))
            return c.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_unresolved_observations(limit: int = 50) -> list:
    """Get observations missing market_result for backfill."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT id, ticker, close_time_utc
            FROM btc15m_observations
            WHERE market_result IS NULL
              AND replace(replace(close_time_utc, 'T', ' '), 'Z', '')
                  < datetime('now', '-2 minutes')
            ORDER BY close_time_utc ASC
            LIMIT ?
        """, (limit,)).fetchall()
        return rows_to_list(rows)


def get_observations_for_simulation(since: str = None, limit: int = 0,
                                    min_quality: str = "short") -> list:
    """Get resolved observations with price paths for strategy simulation.
    min_quality: 'full' (only clean obs), 'short' (default, include short), 'any' (all).
    limit=0 means no limit (fetch all)."""
    quality_filter = ""
    if min_quality == "full":
        quality_filter = " AND COALESCE(obs_quality, 'full') = 'full'"
    elif min_quality == "short":
        quality_filter = " AND COALESCE(obs_quality, 'full') IN ('full', 'short')"
    # 'any' = no filter
    with get_conn() as c:
        base = f"""
            SELECT * FROM btc15m_observations
            WHERE market_result IS NOT NULL
              AND price_snapshots IS NOT NULL
              AND snapshot_count >= 5
              {quality_filter}
        """
        params = []
        if since:
            base += " AND close_time_utc > ?"
            params.append(since)
        base += " ORDER BY close_time_utc ASC"
        if limit > 0:
            base += " LIMIT ?"
            params.append(limit)
        rows = c.execute(base, params).fetchall()
        return rows_to_list(rows)


def get_observation_count() -> dict:
    """Get observation stats for dashboard, including quality breakdown."""
    with get_conn() as c:
        row = c.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN market_result IS NOT NULL THEN 1 ELSE 0 END) as resolved,
                SUM(CASE WHEN bot_action = 'traded' THEN 1 ELSE 0 END) as traded,
                SUM(CASE WHEN bot_action = 'observed' THEN 1 ELSE 0 END) as observed,
                SUM(CASE WHEN bot_action = 'idle' THEN 1 ELSE 0 END) as idle,
                MIN(close_time_utc) as first_obs,
                MAX(close_time_utc) as last_obs,
                SUM(CASE WHEN COALESCE(obs_quality,'full') = 'full' THEN 1 ELSE 0 END) as quality_full,
                SUM(CASE WHEN obs_quality = 'partial' THEN 1 ELSE 0 END) as quality_partial,
                SUM(CASE WHEN obs_quality = 'short' THEN 1 ELSE 0 END) as quality_short,
                SUM(CASE WHEN obs_quality = 'few' THEN 1 ELSE 0 END) as quality_few
            FROM btc15m_observations
        """).fetchone()
        return dict(row) if row else {}


def get_observatory_summary() -> dict:
    """Summary for dashboard display."""
    with get_conn() as c:
        obs_stats = get_observation_count()

        # Top strategies — prefer FDR-significant ones
        top = c.execute("""
            SELECT setup_key, strategy_key, sample_size, win_rate,
                   ev_per_trade_c, ci_lower, profit_factor,
                   COALESCE(fdr_significant, 0) as fdr_significant,
                   fdr_q_value,
                   COALESCE(weighted_ev_c, ev_per_trade_c) as ranked_ev
            FROM btc15m_strategy_results
            WHERE sample_size >= 20
              AND ev_per_trade_c IS NOT NULL
            ORDER BY ranked_ev DESC
            LIMIT 10
        """).fetchall()

        # Worst setups to avoid
        avoid = c.execute("""
            SELECT setup_key, strategy_key, sample_size, win_rate,
                   ev_per_trade_c, ci_lower
            FROM btc15m_strategy_results
            WHERE sample_size >= 20
              AND ev_per_trade_c IS NOT NULL
              AND ev_per_trade_c < 0
            ORDER BY ev_per_trade_c ASC
            LIMIT 5
        """).fetchall()

        return {
            "observations": obs_stats,
            "top_strategies": rows_to_list(top),
            "avoid_setups": rows_to_list(avoid),
        }


# ═══════════════════════════════════════════════════════════════
#  STRATEGY RESULTS
# ═══════════════════════════════════════════════════════════════

def upsert_strategy_result(data: dict):
    """Insert or update a strategy result row."""
    setup_key = data["setup_key"]
    strategy_key = data["strategy_key"]
    data["updated_at"] = now_utc()
    with get_conn() as c:
        existing = c.execute(
            "SELECT id FROM btc15m_strategy_results WHERE setup_key = ? AND strategy_key = ?",
            (setup_key, strategy_key)
        ).fetchone()
        if existing:
            cols = ", ".join(f"{k} = ?" for k in data
                            if k not in ("setup_key", "strategy_key"))
            vals = [data[k] for k in data
                    if k not in ("setup_key", "strategy_key")]
            c.execute(f"UPDATE btc15m_strategy_results SET {cols} WHERE id = ?",
                      vals + [existing["id"]])
        else:
            keys = ", ".join(data.keys())
            placeholders = ", ".join("?" for _ in data)
            c.execute(f"INSERT INTO btc15m_strategy_results ({keys}) VALUES ({placeholders})",
                      list(data.values()))


def get_strategy_for_setup(setup_key: str, min_samples: int = 15) -> list:
    """Get all strategy results for a specific setup.
    Sorted by weighted_ev_c (time-weighted), falling back to ev_per_trade_c."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT * FROM btc15m_strategy_results
            WHERE setup_key = ?
              AND sample_size >= ?
            ORDER BY COALESCE(weighted_ev_c, ev_per_trade_c) DESC
        """, (setup_key, min_samples)).fetchall()
        return rows_to_list(rows)


# ═══════════════════════════════════════════════════════════════
#  NET EDGE & REALIZED EDGE
# ═══════════════════════════════════════════════════════════════

def get_net_edge_summary() -> dict:
    """The single most important metric: estimated edge per contract with CI."""
    with get_conn() as c:
        best = c.execute("""
            SELECT strategy_key, sample_size, win_rate, ev_per_trade_c,
                   ci_lower, ci_upper, profit_factor,
                   weighted_ev_c, oos_ev_c, oos_win_rate, oos_sample_size,
                   fdr_significant, fdr_q_value, max_consecutive_losses,
                   first_observation, last_observation
            FROM btc15m_strategy_results
            WHERE setup_key = 'global:all' AND sample_size >= 30
            ORDER BY COALESCE(weighted_ev_c, ev_per_trade_c) DESC
            LIMIT 1
        """).fetchone()

        best_fdr = c.execute("""
            SELECT strategy_key, sample_size, win_rate, ev_per_trade_c,
                   ci_lower, ci_upper, profit_factor,
                   weighted_ev_c, oos_ev_c, oos_win_rate, oos_sample_size,
                   fdr_significant, fdr_q_value
            FROM btc15m_strategy_results
            WHERE setup_key = 'global:all' AND sample_size >= 30
              AND fdr_significant = 1
            ORDER BY COALESCE(weighted_ev_c, ev_per_trade_c) DESC
            LIMIT 1
        """).fetchone()

        obs = c.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN COALESCE(obs_quality,'full') = 'full' THEN 1 ELSE 0 END) as sim_eligible
            FROM btc15m_observations WHERE market_result IS NOT NULL
        """).fetchone()

        fdr_count = c.execute("""
            SELECT COUNT(*) as n FROM btc15m_strategy_results
            WHERE fdr_significant = 1 AND sample_size >= 30
        """).fetchone()["n"]

        total_strategies = c.execute("""
            SELECT COUNT(*) as n FROM btc15m_strategy_results
            WHERE sample_size >= 30
        """).fetchone()["n"]

        return {
            "best_overall": dict(best) if best else None,
            "best_fdr": dict(best_fdr) if best_fdr else None,
            "total_observations": obs["total"] if obs else 0,
            "sim_eligible_observations": obs["sim_eligible"] if obs else 0,
            "fdr_significant_strategies": fdr_count,
            "total_evaluated_strategies": total_strategies,
            "min_observations_needed": 200,
            "data_sufficient": (obs["total"] or 0) >= 200,
        }


def get_realized_edge(windows: list = None) -> dict:
    """Actual P&L per contract from real trades vs simulated EV."""
    if windows is None:
        windows = [50, 100, 200]

    result = {"windows": {}}

    with get_conn() as c:
        for w in windows:
            rows = c.execute("""
                SELECT pnl, shares_filled, actual_cost, gross_proceeds,
                       outcome, auto_strategy_key
                FROM btc15m_trades
                WHERE outcome IN ('win', 'loss')
                  AND COALESCE(is_ignored, 0) = 0
                  AND shares_filled > 0
                ORDER BY created_at DESC
                LIMIT ?
            """, (w,)).fetchall()

            trades = rows_to_list(rows)
            if not trades:
                continue

            total_pnl = sum(t.get("pnl") or 0 for t in trades)
            total_contracts = sum(t.get("shares_filled") or 1 for t in trades)
            n = len(trades)
            wins = sum(1 for t in trades if t["outcome"] == "win")

            avg_pnl_per_contract = round(total_pnl / total_contracts, 2) if total_contracts > 0 else 0
            avg_pnl_per_trade = round(total_pnl / n, 2) if n > 0 else 0

            result["windows"][w] = {
                "n_trades": n,
                "total_contracts": total_contracts,
                "avg_pnl_per_contract_c": avg_pnl_per_contract,
                "avg_pnl_per_trade_c": avg_pnl_per_trade,
                "total_pnl_c": round(total_pnl, 0),
                "win_rate": round(wins / n, 4) if n > 0 else 0,
            }

        # Simulated EV for comparison
        sim_row = c.execute("""
            SELECT ev_per_trade_c, weighted_ev_c, strategy_key
            FROM btc15m_strategy_results
            WHERE setup_key = 'global:all' AND sample_size >= 30
            ORDER BY COALESCE(weighted_ev_c, ev_per_trade_c) DESC
            LIMIT 1
        """).fetchone()

        result["simulated_ev_c"] = (
            round(sim_row["weighted_ev_c"] or sim_row["ev_per_trade_c"], 1)
            if sim_row else None
        )
        result["simulated_strategy"] = sim_row["strategy_key"] if sim_row else None

        largest_window = None
        for w in sorted(windows, reverse=True):
            if w in result["windows"]:
                largest_window = result["windows"][w]
                break

        if largest_window and result["simulated_ev_c"] is not None:
            result["sim_live_gap_c"] = round(
                result["simulated_ev_c"] - largest_window["avg_pnl_per_trade_c"], 1
            )
        else:
            result["sim_live_gap_c"] = None

    return result


# ═══════════════════════════════════════════════════════════════
#  PROBABILITY SURFACE
# ═══════════════════════════════════════════════════════════════

def upsert_surface_cell(distance_bucket: str, time_bucket: str,
                        total: int, yes_wins: int, no_wins: int,
                        yes_win_rate: float,
                        avg_yes_price: float = None,
                        avg_no_price: float = None,
                        vol_bucket: str = "all"):
    """Insert or update a BTC probability surface cell."""
    with get_conn() as c:
        c.execute("""
            INSERT INTO btc15m_probability_surface
                (distance_bucket, time_bucket, vol_bucket, total, yes_wins,
                 no_wins, yes_win_rate, avg_yes_price, avg_no_price, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(distance_bucket, time_bucket, vol_bucket) DO UPDATE SET
                total = excluded.total, yes_wins = excluded.yes_wins,
                no_wins = excluded.no_wins, yes_win_rate = excluded.yes_win_rate,
                avg_yes_price = excluded.avg_yes_price,
                avg_no_price = excluded.avg_no_price,
                updated_at = excluded.updated_at
        """, (distance_bucket, time_bucket, vol_bucket, total, yes_wins,
              no_wins, round(yes_win_rate, 4), avg_yes_price, avg_no_price,
              now_utc()))


def get_btc_surface_data(vol_bucket: str = None) -> list:
    """Get BTC probability surface for dashboard visualization."""
    with get_conn() as c:
        if vol_bucket:
            rows = c.execute("""
                SELECT * FROM btc15m_probability_surface
                WHERE total >= 5 AND vol_bucket = ?
                ORDER BY distance_bucket, time_bucket
            """, (vol_bucket,)).fetchall()
        else:
            rows = c.execute("""
                SELECT * FROM btc15m_probability_surface
                WHERE total >= 5
                ORDER BY vol_bucket, distance_bucket, time_bucket
            """).fetchall()
        return rows_to_list(rows)


# ═══════════════════════════════════════════════════════════════
#  FEATURE IMPORTANCE
# ═══════════════════════════════════════════════════════════════

def upsert_feature_importance(feature_name: str, importance: float,
                               correlation: float, sample_size: int,
                               method: str = "logistic"):
    """Insert or update feature importance record."""
    with get_conn() as c:
        c.execute("""
            INSERT INTO btc15m_feature_importance
                (feature_name, importance, correlation, sample_size, method, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(feature_name) DO UPDATE SET
                importance = excluded.importance, correlation = excluded.correlation,
                sample_size = excluded.sample_size, method = excluded.method,
                updated_at = excluded.updated_at
        """, (feature_name, round(importance, 6), round(correlation, 6),
              sample_size, method, now_utc()))


def get_feature_importance() -> list:
    """Get feature importance rankings for dashboard."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT * FROM btc15m_feature_importance
            ORDER BY ABS(importance) DESC
        """).fetchall()
        return rows_to_list(rows)


# ═══════════════════════════════════════════════════════════════
#  METRIC SNAPSHOTS
# ═══════════════════════════════════════════════════════════════

def insert_metric_snapshot(metrics: dict):
    """Store a timestamped snapshot of key metrics for convergence tracking."""
    import json as _json
    with get_conn() as c:
        c.execute(
            "INSERT INTO btc15m_metric_snapshots (recorded_at, metrics) VALUES (?, ?)",
            (now_utc(), _json.dumps(metrics))
        )
        # Keep last 2 weeks of snapshots (~672 at 30min intervals)
        c.execute("""
            DELETE FROM btc15m_metric_snapshots
            WHERE id NOT IN (
                SELECT id FROM btc15m_metric_snapshots ORDER BY recorded_at DESC LIMIT 700
            )
        """)


def get_metric_snapshot_near(target_time: str, max_drift_hours: float = 0) -> tuple[str | None, dict | None]:
    """Get the snapshot closest to a target ISO time string.
    Returns (recorded_at, metrics) or (None, None).
    If max_drift_hours > 0, rejects snapshots more than that many hours from target."""
    import json as _json
    with get_conn() as c:
        if max_drift_hours > 0:
            drift_days = max_drift_hours / 24.0
            row = c.execute("""
                SELECT recorded_at, metrics FROM btc15m_metric_snapshots
                WHERE ABS(JULIANDAY(recorded_at) - JULIANDAY(?)) <= ?
                ORDER BY ABS(JULIANDAY(recorded_at) - JULIANDAY(?))
                LIMIT 1
            """, (target_time, drift_days, target_time)).fetchone()
        else:
            row = c.execute("""
                SELECT recorded_at, metrics FROM btc15m_metric_snapshots
                ORDER BY ABS(JULIANDAY(recorded_at) - JULIANDAY(?))
                LIMIT 1
            """, (target_time,)).fetchone()
    if row:
        return row["recorded_at"], _json.loads(row["metrics"])
    return None, None


def get_latest_metric_snapshot() -> tuple[str | None, dict | None]:
    """Get the most recent snapshot. Returns (recorded_at, metrics) or (None, None)."""
    import json as _json
    with get_conn() as c:
        row = c.execute(
            "SELECT recorded_at, metrics FROM btc15m_metric_snapshots ORDER BY recorded_at DESC LIMIT 1"
        ).fetchone()
    if row:
        return row["recorded_at"], _json.loads(row["metrics"])
    return None, None
