"""
market_db.py — Database layer for the BTC 15-minute binary options plugin.
All tables prefixed with btc15m_. Uses platform DB infrastructure.
"""

import sys
import os
import math
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from db import get_conn, now_utc, row_to_dict, rows_to_list
from config import REGIME_THRESHOLDS, KALSHI_FEE_RATE


# ═══════════════════════════════════════════════════════════════
#  SCHEMA
# ═══════════════════════════════════════════════════════════════

def init_btc15m_tables():
    """Create all BTC 15-minute plugin tables."""
    with get_conn() as c:

        # ── 1. Markets (one row per Kalshi 15-min market) ───────
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

        # ── 2. Trades (comprehensive, clean schema) ─────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS btc15m_trades (
                id                          INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id                   INTEGER REFERENCES btc15m_markets(id),
                regime_snapshot_id          INTEGER,

                -- Position
                ticker                      TEXT NOT NULL,
                side                        TEXT NOT NULL,
                entry_price_c               INTEGER,
                entry_time_utc              TEXT,
                minutes_before_close        REAL,

                -- Execution
                shares_ordered              INTEGER DEFAULT 0,
                shares_filled               INTEGER DEFAULT 0,
                actual_cost                 REAL DEFAULT 0,
                fees_paid                   REAL DEFAULT 0,
                avg_fill_price_c            INTEGER DEFAULT 0,
                buy_order_id                TEXT,

                -- Exit
                sell_price_c                INTEGER,
                sell_order_id               TEXT,
                sell_filled                 INTEGER DEFAULT 0,
                exit_price_c                INTEGER,
                exit_time_utc               TEXT,
                gross_proceeds              REAL DEFAULT 0,
                pnl                         REAL DEFAULT 0,

                -- Outcome
                outcome                     TEXT NOT NULL,
                skip_reason                 TEXT,

                -- Price path summary
                price_high_water_c          INTEGER,
                price_low_water_c           INTEGER,
                pct_progress_toward_target  REAL,
                oscillation_count           INTEGER DEFAULT 0,

                -- Regime context (denormalized)
                regime_label                TEXT,
                coarse_regime               TEXT,
                vol_regime                  INTEGER,
                trend_regime                INTEGER,
                volume_regime               INTEGER,
                regime_risk_level           TEXT,
                regime_confidence           REAL,

                -- Market context
                btc_price_at_entry          REAL,
                btc_price_at_exit           REAL,
                btc_move_pct                REAL,
                btc_distance_pct            REAL,
                market_result               TEXT,
                hour_et                     INTEGER,
                minute_et                   INTEGER,
                day_of_week                 INTEGER,

                -- Entry details
                spread_at_entry_c           INTEGER,
                entry_delay_minutes         INTEGER DEFAULT 0,
                cheaper_side                TEXT,
                cheaper_side_price_c        INTEGER,

                -- Orderbook at entry
                yes_ask_at_entry            INTEGER,
                no_ask_at_entry             INTEGER,
                yes_bid_at_entry            INTEGER,
                no_bid_at_entry             INTEGER,
                kalshi_market_volume        INTEGER,
                kalshi_open_interest        INTEGER,

                -- Strategy tracking
                auto_strategy_key           TEXT,
                auto_strategy_setup         TEXT,
                auto_strategy_ev_c          REAL,

                -- FV model
                model_edge_at_entry         REAL,
                model_ev_at_entry           REAL,
                model_source_at_entry       TEXT,
                market_implied_pct          REAL,
                predicted_edge_pct          REAL,
                ev_per_contract_c           REAL,

                -- Shadow
                is_shadow                   INTEGER DEFAULT 0,
                shadow_decision_price_c     INTEGER,
                shadow_fill_latency_ms      INTEGER,

                -- Execution quality
                fill_duration_seconds       REAL,
                exit_method                 TEXT,
                num_price_samples           INTEGER,
                bet_size_dollars            REAL,
                bankroll_at_entry_c         INTEGER,

                -- Technical
                bollinger_width             REAL,
                atr_15m                     REAL,
                realized_vol                REAL,
                trend_direction             INTEGER,
                trend_strength              REAL,
                bollinger_squeeze           INTEGER DEFAULT 0,
                trend_acceleration          TEXT,
                btc_return_15m              REAL,
                btc_return_1h               REAL,
                btc_return_4h               REAL,
                volume_spike                INTEGER DEFAULT 0,
                ema_slope_15m               REAL,
                ema_slope_1h                REAL,

                -- Skip hypothetical
                skip_hypo_outcome           TEXT,

                -- Flags
                is_data_collection          INTEGER DEFAULT 0,
                is_early_exit               INTEGER DEFAULT 0,
                early_exit_price_c          INTEGER,

                -- Notes
                notes                       TEXT,
                created_at                  TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_trades_outcome ON btc15m_trades(outcome)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_trades_regime ON btc15m_trades(regime_label)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_trades_coarse ON btc15m_trades(coarse_regime)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_trades_hour ON btc15m_trades(hour_et)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_trades_created ON btc15m_trades(created_at)")

        # ── 3. Observations (every market seen, with full price path) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS btc15m_observations (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker                  TEXT NOT NULL UNIQUE,
                market_id               INTEGER,
                close_time_utc          TEXT NOT NULL,

                -- Outcome (backfilled after market closes)
                market_result           TEXT,

                -- Regime context at market start
                regime_label            TEXT,
                vol_regime              INTEGER,
                trend_regime            INTEGER,
                volume_regime           INTEGER,
                risk_level              TEXT,
                regime_confidence       REAL,

                -- BTC technicals at market start
                btc_price               REAL,
                btc_return_15m          REAL,
                btc_return_1h           REAL,
                btc_return_4h           REAL,
                realized_vol            REAL,
                atr_15m                 REAL,
                bollinger_width         REAL,
                ema_slope_15m           REAL,
                ema_slope_1h            REAL,
                trend_direction         INTEGER,
                trend_strength          REAL,
                bollinger_squeeze       INTEGER DEFAULT 0,
                volume_spike            INTEGER DEFAULT 0,

                -- Timing
                hour_et                 INTEGER,
                minute_et               INTEGER,
                day_of_week             INTEGER,

                -- Kalshi price path (JSON array of snapshots)
                price_snapshots         TEXT,
                snapshot_count          INTEGER DEFAULT 0,
                obs_quality             TEXT DEFAULT 'full',

                -- Price summary (derived on close)
                yes_open_c              INTEGER,
                yes_high_c              INTEGER,
                yes_low_c               INTEGER,
                yes_close_c             INTEGER,
                no_open_c               INTEGER,
                no_high_c               INTEGER,
                no_low_c                INTEGER,
                no_close_c              INTEGER,

                -- BTC movement during market
                btc_price_at_open       REAL,
                btc_price_at_close      REAL,
                btc_move_during_pct     REAL,
                btc_distance_pct_at_close REAL,
                btc_max_distance_pct    REAL,
                btc_min_distance_pct    REAL,

                -- Bot action
                bot_action              TEXT,
                trade_id                INTEGER,
                active_strategy_key     TEXT,

                -- Kalshi market liquidity
                kalshi_volume           INTEGER,
                kalshi_open_interest    INTEGER,

                created_at              TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_obs_ticker ON btc15m_observations(ticker)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_obs_close ON btc15m_observations(close_time_utc)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_obs_regime ON btc15m_observations(regime_label)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_obs_result ON btc15m_observations(market_result)")

        # ── 4. Strategy results (simulation results per setup x strategy) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS btc15m_strategy_results (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                setup_key               TEXT NOT NULL,
                setup_type              TEXT NOT NULL,
                strategy_key            TEXT NOT NULL,
                side_rule               TEXT,
                exit_rule               TEXT,
                entry_time_rule         TEXT,
                entry_price_max         INTEGER,
                sell_target             TEXT,

                -- Aggregate results
                sample_size             INTEGER DEFAULT 0,
                wins                    INTEGER DEFAULT 0,
                losses                  INTEGER DEFAULT 0,
                win_rate                REAL,
                total_pnl_c             INTEGER DEFAULT 0,
                avg_pnl_c               REAL,
                best_pnl_c              INTEGER,
                worst_pnl_c             INTEGER,
                max_drawdown_c          INTEGER DEFAULT 0,

                -- Risk metrics
                profit_factor           REAL,
                expectancy_c            REAL,
                max_consecutive_losses  INTEGER DEFAULT 0,

                -- Confidence
                ci_lower                REAL,
                ci_upper                REAL,
                ev_per_trade_c          REAL,
                pnl_std_c               REAL,

                -- Time-weighted metrics
                weighted_win_rate       REAL,
                weighted_ev_c           REAL,

                -- Walk-forward out-of-sample validation
                oos_ev_c                REAL,
                oos_win_rate            REAL,
                oos_sample_size         INTEGER DEFAULT 0,

                -- FDR
                fdr_significant         INTEGER DEFAULT 0,
                fdr_q_value             REAL,

                -- Robustness
                slippage_1c_ev          REAL,
                slippage_2c_ev          REAL,
                breakeven_fee_rate      REAL,

                -- Quality
                quality_full_ev_c       REAL,
                quality_degraded_ev_c   REAL,

                -- Time range
                first_observation       TEXT,
                last_observation        TEXT,
                updated_at              TEXT NOT NULL,

                UNIQUE(setup_key, strategy_key)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_sr_setup ON btc15m_strategy_results(setup_key)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_btc15m_sr_ev ON btc15m_strategy_results(ev_per_trade_c)")

        # ── 5. Price path (per-second price during active trade) ──
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

        # ── 6. Live prices (for dashboard) ──────────────────────
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

        # ── 7. BTC probability surface (vol-conditioned) ────────
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

        # ── 8. Feature importance ───────────────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS btc15m_feature_importance (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                feature_name    TEXT NOT NULL UNIQUE,
                importance      REAL,
                correlation     REAL,
                sample_size     INTEGER DEFAULT 0,
                method          TEXT DEFAULT 'point_biserial',
                updated_at      TEXT NOT NULL
            )
        """)

        # ── 9. Regime stats (aggregated per regime label) ───────
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

        # ── 10. Hourly stats (win rates by ET hour) ────────────
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

    print("[market_db] BTC 15m plugin tables initialized")


# ═══════════════════════════════════════════════════════════════
#  HELPERS
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
      1. EV Signal (30%) -- profitability from weighted/unweighted EV
      2. Statistical Confidence (20%) -- sample size, CI width, FDR significance
      3. Out-of-Sample Validation (20%) -- walk-forward OOS performance
      4. Downside Risk (15%) -- PnL volatility, max consec losses, profit factor
      5. Robustness (15%) -- slippage survival, fee rate margin
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
      1. Win Rate (35%) -- scaled 30-70% range to 0-100
      2. Avg PnL Direction (30%) -- positive avg PnL is good
      3. CI Confidence (20%) -- narrow CI = more trusted
      4. Sample Size (15%) -- more data = more reliable
    """
    # 1. Win Rate (35%)
    wr_score = max(0, min(100, (win_rate - 0.30) / 0.35 * 100))

    # 2. Avg PnL (30%)
    if avg_pnl <= -5:
        pnl_score = 0
    elif avg_pnl <= 0:
        pnl_score = 40 * (1 + avg_pnl / 5)
    elif avg_pnl <= 5:
        pnl_score = 40 + 60 * (avg_pnl / 5)
    else:
        pnl_score = 100

    # 3. CI Confidence (20%)
    ci_width = ci_upper - ci_lower
    ci_score = max(0, min(100, 100 * (1 - ci_width * 2)))

    # 4. Sample Size (15%)
    n_score = min(100, 40 * math.log10(max(total, 1)))

    score = wr_score * 0.35 + pnl_score * 0.30 + ci_score * 0.20 + n_score * 0.15
    return round(min(100, max(0, score)), 1)


# ═══════════════════════════════════════════════════════════════
#  MARKETS
# ═══════════════════════════════════════════════════════════════

def upsert_market(ticker: str, close_time_utc: str, hour_et: int,
                  minute_et: int, day_of_week: int) -> int:
    """Insert or update a market. Returns the market id."""
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
    """Update the outcome for a market."""
    with get_conn() as c:
        c.execute("UPDATE btc15m_markets SET outcome = ? WHERE id = ?",
                  (outcome, market_id))


# ═══════════════════════════════════════════════════════════════
#  TRADES
# ═══════════════════════════════════════════════════════════════

def insert_trade(data: dict) -> int:
    """Insert a new trade. Returns the trade id."""
    data["created_at"] = now_utc()
    cols = ", ".join(data.keys())
    placeholders = ", ".join(["?"] * len(data))
    with get_conn() as c:
        cur = c.execute(f"INSERT INTO btc15m_trades ({cols}) VALUES ({placeholders})",
                        list(data.values()))
        return cur.lastrowid


def update_trade(trade_id: int, data: dict):
    """Update trade fields by id."""
    sets = ", ".join(f"{k} = ?" for k in data.keys())
    with get_conn() as c:
        c.execute(f"UPDATE btc15m_trades SET {sets} WHERE id = ?",
                  list(data.values()) + [trade_id])


def get_trade(trade_id: int) -> dict | None:
    """Get a single trade by id."""
    with get_conn() as c:
        row = c.execute("SELECT * FROM btc15m_trades WHERE id = ?",
                        (trade_id,)).fetchone()
        return row_to_dict(row)


def get_recent_trades(limit: int = 50) -> list:
    """Get recent trades for dashboard display."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT * FROM btc15m_trades
            WHERE outcome IN ('win', 'loss', 'skipped', 'no_fill', 'error', 'open')
            ORDER BY created_at DESC LIMIT ?
        """, (limit,)).fetchall()
        return rows_to_list(rows)


def get_open_trade() -> dict | None:
    """Get the currently open trade, if any."""
    with get_conn() as c:
        row = c.execute("""
            SELECT * FROM btc15m_trades WHERE outcome = 'open'
            ORDER BY created_at DESC LIMIT 1
        """).fetchone()
        return row_to_dict(row)


def delete_trades(trade_ids: list) -> int:
    """Delete trades and their price paths. Returns count deleted."""
    if not trade_ids:
        return 0
    placeholders = ",".join(["?"] * len(trade_ids))
    with get_conn() as c:
        c.execute(f"DELETE FROM btc15m_price_path WHERE trade_id IN ({placeholders})",
                  trade_ids)
        c.execute(f"DELETE FROM btc15m_trades WHERE id IN ({placeholders})",
                  trade_ids)
        return len(trade_ids)


def get_trade_summary() -> dict:
    """Dashboard summary stats."""
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
        """).fetchone()
        stats = row_to_dict(core) or {}

        # Win/loss streaks
        rows = c.execute("""
            SELECT outcome FROM btc15m_trades
            WHERE outcome IN ('win','loss')
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
        total_wins_pnl = c.execute("""
            SELECT COALESCE(SUM(pnl), 0) as s FROM btc15m_trades
            WHERE outcome='win'
        """).fetchone()["s"]
        total_losses_pnl = abs(c.execute("""
            SELECT COALESCE(SUM(pnl), 0) as s FROM btc15m_trades
            WHERE outcome='loss'
        """).fetchone()["s"])
        stats["profit_factor"] = round(
            total_wins_pnl / total_losses_pnl if total_losses_pnl > 0 else 0, 2
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
            GROUP BY COALESCE(entry_delay_minutes, 0)
            ORDER BY delay_min
        """).fetchall()
        stats["delay_breakdown"] = rows_to_list(delay_rows)

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
            GROUP BY vol_level
            ORDER BY vol_level
        """).fetchall()
        stats["vol_breakdown"] = rows_to_list(vol_rows)

        # Hourly performance (by CT hour, derived from DST-correct hour_et)
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
            GROUP BY btc_move_bucket
            ORDER BY avg_btc_move
        """).fetchall()
        stats["btc_move_breakdown"] = rows_to_list(btc_move_rows)

        return stats


# ═══════════════════════════════════════════════════════════════
#  OBSERVATIONS
# ═══════════════════════════════════════════════════════════════

def upsert_observation(data: dict) -> int:
    """Insert or update a market observation by ticker. Returns the row id."""
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


# ═══════════════════════════════════════════════════════════════
#  STRATEGY RESULTS
# ═══════════════════════════════════════════════════════════════

def upsert_strategy_result(data: dict):
    """Insert or update a strategy result row by (setup_key, strategy_key)."""
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


def get_strategy_risk(regime_label: str, strategy_key: str,
                      min_known: int = 10) -> dict:
    """
    Get risk level for a specific strategy in a specific regime.
    Uses Strategy Observatory data (btc15m_strategy_results table).

    Only uses regime-specific data. Does NOT fall back to global:all.
    """
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
#  REGIME STATS
# ═══════════════════════════════════════════════════════════════

def update_regime_stats(regime_label: str) -> dict:
    """Recompute stats for a specific regime label from trade data.

    Returns dict with: is_new, old_risk, new_risk, total, win_rate, risk_score
    """
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


def update_coarse_regime_stats(coarse_label: str):
    """Compute stats for a coarse regime label from trade data."""
    with get_conn() as c:
        real = c.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) as losses,
                SUM(COALESCE(pnl, 0)) as total_pnl
            FROM btc15m_trades
            WHERE coarse_regime = ? AND outcome IN ('win', 'loss')
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


def get_all_regime_stats() -> list:
    """Get all regime stats for dashboard display."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT * FROM btc15m_regime_stats ORDER BY total_trades DESC
        """).fetchall()
        return rows_to_list(rows)


def refresh_all_coarse_regime_stats():
    """Recompute stats for all coarse regime labels."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT DISTINCT coarse_regime FROM btc15m_trades
            WHERE coarse_regime IS NOT NULL
              AND outcome IN ('win', 'loss')
        """).fetchall()
    for row in rows:
        update_coarse_regime_stats(row["coarse_regime"])


def recompute_all_stats():
    """Recompute all derived stats from the trades table.
    Called after deleting trades to ensure consistency."""
    # Recompute all regime stats
    with get_conn() as c:
        c.execute("DELETE FROM btc15m_regime_stats")
        rows = c.execute("""
            SELECT DISTINCT regime_label FROM btc15m_trades
            WHERE regime_label IS NOT NULL AND regime_label != 'unknown'
        """).fetchall()

    for row in rows:
        update_regime_stats(row["regime_label"])

    # Also recompute coarse and hourly
    refresh_all_coarse_regime_stats()
    refresh_all_hourly_stats()


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
            """, (hour_et, day_of_week)).fetchone()
        else:
            real = c.execute("""
                SELECT COUNT(*) as total,
                    SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) as losses,
                    SUM(COALESCE(pnl, 0)) as total_pnl
                FROM btc15m_trades
                WHERE hour_et = ? AND outcome IN ('win', 'loss')
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
        """).fetchall()
        hour_days = c.execute("""
            SELECT DISTINCT hour_et, day_of_week FROM btc15m_trades
            WHERE hour_et IS NOT NULL AND day_of_week IS NOT NULL
              AND outcome IN ('win', 'loss')
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


def backfill_skipped_result(trade_id: int, market_result: str):
    """Update a skipped trade with the actual market result."""
    with get_conn() as c:
        c.execute("UPDATE btc15m_trades SET market_result = ? WHERE id = ?",
                  (market_result, trade_id))


# ═══════════════════════════════════════════════════════════════
#  LIVE PRICES
# ═══════════════════════════════════════════════════════════════

def insert_live_price(ticker: str, yes_ask, no_ask, yes_bid, no_bid):
    """Record a live market price snapshot."""
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
#  PRICE PATH
# ═══════════════════════════════════════════════════════════════

def insert_price_point(trade_id: int, data: dict):
    """Insert a price point for an active trade."""
    data["trade_id"] = trade_id
    data["captured_at"] = now_utc()
    cols = ", ".join(data.keys())
    placeholders = ", ".join(["?"] * len(data))
    with get_conn() as c:
        c.execute(f"INSERT INTO btc15m_price_path ({cols}) VALUES ({placeholders})",
                  list(data.values()))


def get_price_path(trade_id: int) -> list:
    """Get the price path for a trade."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT * FROM btc15m_price_path WHERE trade_id = ?
            ORDER BY captured_at ASC
        """, (trade_id,)).fetchall()
        return rows_to_list(rows)


# ═══════════════════════════════════════════════════════════════
#  BTC PROBABILITY SURFACE
# ═══════════════════════════════════════════════════════════════

def get_btc_surface_data(vol_bucket: str = "all") -> list:
    """Get BTC probability surface for dashboard visualization.
    vol_bucket: 'calm', 'normal', 'volatile', 'all', or None for all buckets."""
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


# ═══════════════════════════════════════════════════════════════
#  FEATURE IMPORTANCE
# ═══════════════════════════════════════════════════════════════

def upsert_feature_importance(feature_name: str, importance: float,
                               correlation: float, sample_size: int):
    """Insert or update feature importance record."""
    with get_conn() as c:
        c.execute("""
            INSERT INTO btc15m_feature_importance
                (feature_name, importance, correlation, sample_size, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(feature_name) DO UPDATE SET
                importance = excluded.importance, correlation = excluded.correlation,
                sample_size = excluded.sample_size,
                updated_at = excluded.updated_at
        """, (feature_name, round(importance, 6), round(correlation, 6),
              sample_size, now_utc()))


def get_feature_importance() -> list:
    """Get feature importance rankings for dashboard."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT * FROM btc15m_feature_importance
            ORDER BY ABS(importance) DESC
        """).fetchall()
        return rows_to_list(rows)
