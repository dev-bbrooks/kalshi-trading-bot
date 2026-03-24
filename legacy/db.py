"""
db.py — Database layer for the Kalshi BTC trading bot.
SQLite with WAL mode for concurrent dashboard + bot access.
"""

import json
import os
import sqlite3
import math
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager

from config import DB_PATH

# ═══════════════════════════════════════════════════════════════
#  CONNECTION
# ═══════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════
#  SCHEMA
# ═══════════════════════════════════════════════════════════════

def init_db():
    with get_conn() as c:

        # ── BTC candle history (1-minute from Binance) ────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS btc_candles (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      TEXT NOT NULL UNIQUE,
                open    REAL NOT NULL,
                high    REAL NOT NULL,
                low     REAL NOT NULL,
                close   REAL NOT NULL,
                volume  REAL NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_candles_ts ON btc_candles(ts)")

        # ── Market baselines (statistical norms per hour/dow) ─
        c.execute("""
            CREATE TABLE IF NOT EXISTS baselines (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                computed_at     TEXT NOT NULL,
                hour_et         INTEGER,
                day_of_week     INTEGER,
                avg_vol_15m     REAL,
                p25_vol_15m     REAL,
                p75_vol_15m     REAL,
                p90_vol_15m     REAL,
                avg_atr_15m     REAL,
                avg_volume_15m  REAL,
                p25_volume_15m  REAL,
                p75_volume_15m  REAL,
                p90_volume_15m  REAL,
                avg_range_15m   REAL,
                sample_count    INTEGER
            )
        """)

        # ── Regime snapshots (written every ~5 min) ───────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS regime_snapshots (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at             TEXT NOT NULL,
                btc_price               REAL,
                btc_return_15m          REAL,
                btc_return_1h           REAL,
                btc_return_4h           REAL,
                atr_15m                 REAL,
                atr_1h                  REAL,
                bollinger_width_15m     REAL,
                realized_vol_15m        REAL,
                realized_vol_1h         REAL,
                ema_slope_15m           REAL,
                ema_slope_1h            REAL,
                vol_regime              INTEGER,
                trend_regime            INTEGER,
                trend_direction         INTEGER,
                trend_strength          REAL,
                volume_15m              REAL,
                volume_regime           INTEGER,
                volume_spike            INTEGER DEFAULT 0,
                post_spike              INTEGER DEFAULT 0,
                trend_exhaustion        INTEGER DEFAULT 0,
                composite_label         TEXT,
                regime_confidence       REAL
            )
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_regime_captured
            ON regime_snapshots(captured_at)
        """)

        # ── Markets (one row per Kalshi 15-min market) ────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS markets (
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
        c.execute("CREATE INDEX IF NOT EXISTS idx_markets_ticker ON markets(ticker)")

        # ── Trades (comprehensive — the main analysis table) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id               INTEGER REFERENCES markets(id),
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
                    -- 'win', 'loss', 'skipped', 'no_fill',
                    -- 'cashed_out', 'open', 'expired'
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
                created_at              TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_trades_outcome ON trades(outcome)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_trades_regime ON trades(regime_label)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at)")

        # Migrate: add new columns to existing trades table
        for col, coltype in [("is_ignored", "INTEGER DEFAULT 0"),
                             ("entry_delay_minutes", "INTEGER DEFAULT 0"),
                             ("trade_mode", "TEXT"),
                             ("price_stability_c", "INTEGER"),
                             ("is_early_exit", "INTEGER DEFAULT 0"),
                             ("early_exit_price_c", "INTEGER"),
                             # Data collection improvements (v2)
                             ("spread_at_entry_c", "INTEGER"),
                             ("btc_price_at_exit", "REAL"),
                             ("btc_move_pct", "REAL"),
                             ("prev_regime_label", "TEXT"),
                             ("coarse_regime", "TEXT"),
                             ("hour_et", "INTEGER"),
                             ("day_of_week", "INTEGER"),
                             # Regime bet system
                             ("is_regime_bet", "INTEGER DEFAULT 0"),
                             # Skip hypothetical outcome
                             ("skip_hypo_outcome", "TEXT"),
                             # v3 — comprehensive tracking
                             ("bankroll_at_entry_c", "INTEGER"),
                             ("bet_size_dollars", "REAL"),
                             ("fill_duration_seconds", "REAL"),
                             ("exit_method", "TEXT"),
                             ("num_price_samples", "INTEGER"),
                             ("session_trade_num", "INTEGER"),
                             ("time_to_target_seconds", "REAL"),
                             ("market_close_time_utc", "TEXT"),
                             ("cheaper_side", "TEXT"),
                             ("cheaper_side_price_c", "INTEGER"),
                             # v4 — spread regime + enhanced classification
                             ("spread_regime", "TEXT"),
                             # v5 — comprehensive data tracking (denormalized from snapshot)
                             ("regime_confidence", "REAL"),
                             ("bollinger_width", "REAL"),
                             ("atr_15m", "REAL"),
                             ("realized_vol", "REAL"),
                             ("trend_direction", "INTEGER"),
                             ("trend_strength", "REAL"),
                             ("bollinger_squeeze", "INTEGER DEFAULT 0"),
                             ("trend_acceleration", "TEXT"),
                             ("btc_return_15m", "REAL"),
                             ("btc_return_1h", "REAL"),
                             ("btc_return_4h", "REAL"),
                             ("volume_spike", "INTEGER DEFAULT 0"),
                             ("ema_slope_15m", "REAL"),
                             ("ema_slope_1h", "REAL"),
                             # v5 — Kalshi market orderbook at entry
                             ("yes_ask_at_entry", "INTEGER"),
                             ("no_ask_at_entry", "INTEGER"),
                             ("yes_bid_at_entry", "INTEGER"),
                             ("no_bid_at_entry", "INTEGER"),
                             ("kalshi_market_volume", "INTEGER"),
                             ("kalshi_open_interest", "INTEGER"),
                             # v5 — session context at time of trade
                             ("session_pnl_at_entry", "REAL"),
                             ("session_wins_at_entry", "INTEGER"),
                             ("session_losses_at_entry", "INTEGER"),
                             # v6 — confidence model
                             ("predicted_win_pct", "REAL"),
                             ("confidence_level", "REAL"),
                             ("prediction_factors", "TEXT"),
                             # v6 — edge tracking
                             ("market_implied_pct", "REAL"),
                             ("predicted_edge_pct", "REAL"),
                             ("ev_per_contract_c", "REAL"),
                             # v7 — auto-strategy tracking
                             ("auto_strategy_key", "TEXT"),
                             ("auto_strategy_setup", "TEXT"),
                             ("auto_strategy_ev_c", "REAL"),
                             # v8 — fair value model tracking
                             ("model_edge_at_entry", "REAL"),
                             ("model_ev_at_entry", "REAL"),
                             ("model_source_at_entry", "TEXT"),
                             # v9 — shadow trading (execution data collection)
                             ("is_shadow", "INTEGER DEFAULT 0"),
                             ("shadow_decision_price_c", "INTEGER"),  # ask at decision time
                             ("shadow_fill_latency_ms", "INTEGER"),   # ms from order to fill
                             # v10 — minute tracking
                             ("minute_et", "INTEGER"),
                             # v11 — BTC distance from open
                             ("btc_distance_pct", "REAL"),
                             ]:
            try:
                c.execute(f"ALTER TABLE trades ADD COLUMN {col} {coltype}")
            except Exception:
                pass

        # Migrate: add new columns to regime_snapshots
        for col, coltype in [("bollinger_squeeze", "INTEGER DEFAULT 0"),
                             ("trend_acceleration", "TEXT"),
                             ("thin_market", "INTEGER DEFAULT 0")]:
            try:
                c.execute(f"ALTER TABLE regime_snapshots ADD COLUMN {col} {coltype}")
            except Exception:
                pass

        # ── Regime opportunities (tracked during skipped markets) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS regime_opportunities (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id            INTEGER REFERENCES trades(id),
                ticker              TEXT NOT NULL,
                market_id           INTEGER REFERENCES markets(id),

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
        c.execute("CREATE INDEX IF NOT EXISTS idx_ro_regime ON regime_opportunities(regime_label)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ro_predicted ON regime_opportunities(predicted_side)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ro_ticker ON regime_opportunities(ticker)")

        # ── Exit simulations (what-if tracking for early exits) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS exit_simulations (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id            INTEGER NOT NULL REFERENCES trades(id),
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
        c.execute("CREATE INDEX IF NOT EXISTS idx_exsim_trade ON exit_simulations(trade_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_exsim_regime ON exit_simulations(regime_label)")

        # ── Trade price path (per-second during active trade) ─
        c.execute("""
            CREATE TABLE IF NOT EXISTS price_path (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id        INTEGER NOT NULL REFERENCES trades(id),
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
        c.execute("CREATE INDEX IF NOT EXISTS idx_pp_trade ON price_path(trade_id)")

        # ── Live market price history (for dashboard chart backfill) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS live_prices (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                ticker      TEXT,
                yes_ask     INTEGER,
                no_ask      INTEGER,
                yes_bid     INTEGER,
                no_bid      INTEGER
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_lp_ts ON live_prices(ts)")

        # ── Regime stats (aggregated per regime label) ────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS regime_stats (
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
            CREATE TABLE IF NOT EXISTS hourly_stats (
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

        # Indexes for new trade columns
        c.execute("CREATE INDEX IF NOT EXISTS idx_trades_coarse ON trades(coarse_regime)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_trades_hour ON trades(hour_et)")

        # ── Bot state (single row — live status, session tracking) ─
        c.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                id                  INTEGER PRIMARY KEY DEFAULT 1,
                status              TEXT DEFAULT 'stopped',
                status_detail       TEXT DEFAULT '',
                auto_trading        INTEGER DEFAULT 0,
                trades_remaining    INTEGER DEFAULT 0,

                -- Consecutive loss tracking
                loss_streak         INTEGER DEFAULT 0,
                cooldown_remaining  INTEGER DEFAULT 0,

                -- Live info
                bankroll_cents      INTEGER DEFAULT 0,
                session_pnl         REAL DEFAULT 0,
                session_wins        INTEGER DEFAULT 0,
                session_losses      INTEGER DEFAULT 0,
                session_skips       INTEGER DEFAULT 0,
                lifetime_pnl        REAL DEFAULT 0,
                lifetime_wins       INTEGER DEFAULT 0,
                lifetime_losses     INTEGER DEFAULT 0,

                -- Active trade snapshot (JSON)
                active_trade        TEXT,
                -- Live market info (JSON, updated even when stopped)
                live_market         TEXT,
                last_ticker         TEXT,
                last_updated        TEXT
            )
        """)
        c.execute("""
            INSERT OR IGNORE INTO bot_state (id, last_updated)
            VALUES (1, ?)
        """, (now_utc(),))

        # Migrate: add new columns to existing bot_state table
        for col, coltype in [("live_market", "TEXT"),
                             ("cooldown_remaining", "INTEGER DEFAULT 0"),
                             ("last_completed_trade", "TEXT"),
                             ("_delay_end_iso", "TEXT"),
                             ("cashing_out", "INTEGER"),
                             ("cancel_cash_out", "INTEGER"),
                             ("pending_trade", "TEXT"),
                             ("session_data_bets", "INTEGER DEFAULT 0"),
                             ("session_stopped_at", "TEXT DEFAULT ''"),
                             ("_prev_session", "TEXT DEFAULT ''"),
                             ("auto_trading_since", "TEXT DEFAULT ''"),
                             ("active_skip", "TEXT"),
                             ("regime_engine_phase", "TEXT"),
                             ("loss_streak", "INTEGER DEFAULT 0"),
                             ("observatory_health", "TEXT"),
                             ("active_shadow", "TEXT")]:
            try:
                c.execute(f"ALTER TABLE bot_state ADD COLUMN {col} {coltype}")
            except Exception:
                pass  # Column already exists

        # Migrate: add new columns to baselines table
        for col, coltype in [("avg_bollinger_width", "REAL"),
                             ("p10_bollinger_width", "REAL")]:
            try:
                c.execute(f"ALTER TABLE baselines ADD COLUMN {col} {coltype}")
            except Exception:
                pass

        # ── Push notification subscriptions ──────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint        TEXT NOT NULL UNIQUE,
                subscription_json TEXT NOT NULL,
                created_at      TEXT NOT NULL
            )
        """)

        # ── Push notification log ─────────────────────────────
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

        # ── Bankroll snapshots (recorded after each trade settles) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS bankroll_snapshots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at     TEXT NOT NULL,
                bankroll_cents  INTEGER NOT NULL,
                trade_id        INTEGER
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_bs_time ON bankroll_snapshots(captured_at)")

        # ── Bot config (key-value store) ──────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS bot_config (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)

        # ── Bot commands (dashboard → bot) ────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS bot_commands (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                command_type    TEXT NOT NULL,
                parameters      TEXT DEFAULT '{}',
                status          TEXT DEFAULT 'pending',
                created_at      TEXT NOT NULL,
                result          TEXT
            )
        """)

        # ── Log entries (structured, for dashboard display) ───
        c.execute("""
            CREATE TABLE IF NOT EXISTS log_entries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                level       TEXT NOT NULL,
                category    TEXT DEFAULT 'bot',
                message     TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts ON log_entries(ts)")

        # ── Confidence model ──────────────────────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS confidence_factors (
                factor_name     TEXT NOT NULL,
                factor_value    TEXT NOT NULL,
                wins            INTEGER DEFAULT 0,
                losses          INTEGER DEFAULT 0,
                total           INTEGER DEFAULT 0,
                win_rate        REAL DEFAULT 0,
                updated_at      TEXT NOT NULL,
                PRIMARY KEY (factor_name, factor_value)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS confidence_calibration (
                bucket          TEXT NOT NULL PRIMARY KEY,
                predictions     INTEGER DEFAULT 0,
                actual_wins     INTEGER DEFAULT 0,
                actual_total    INTEGER DEFAULT 0,
                actual_win_rate REAL DEFAULT 0,
                calibration_err REAL DEFAULT 0,
                updated_at      TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS edge_calibration (
                bucket          TEXT NOT NULL PRIMARY KEY,
                total           INTEGER DEFAULT 0,
                wins            INTEGER DEFAULT 0,
                actual_win_rate REAL DEFAULT 0,
                avg_entry_price REAL DEFAULT 0,
                avg_ev_c        REAL DEFAULT 0,
                total_pnl       REAL DEFAULT 0,
                updated_at      TEXT NOT NULL
            )
        """)

        # ── Market observations (every market seen, with full price path) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS market_observations (
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

                created_at          TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_mobs_ticker ON market_observations(ticker)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mobs_close ON market_observations(close_time_utc)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mobs_regime ON market_observations(regime_label)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mobs_result ON market_observations(market_result)")

        # Migrate: add new columns to market_observations
        for col, coltype in [("active_strategy_key", "TEXT"),
                             # v2 — observation quality & BTC distance tracking
                             ("obs_quality", "TEXT DEFAULT 'full'"),
                             ("btc_price_at_open", "REAL"),
                             ("btc_distance_pct_at_close", "REAL"),
                             ("btc_max_distance_pct", "REAL"),
                             ("btc_min_distance_pct", "REAL"),
                             ]:
            try:
                c.execute(f"ALTER TABLE market_observations ADD COLUMN {col} {coltype}")
            except Exception:
                pass

        # ── Strategy simulation results (aggregated per setup × strategy) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS strategy_results (
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

                -- Time-weighted metrics (exponential decay, recent data weighted more)
                weighted_win_rate   REAL,
                weighted_ev_c       REAL,

                -- Walk-forward out-of-sample validation
                oos_ev_c            REAL,       -- EV on test set (last 30% of data)
                oos_win_rate        REAL,
                oos_sample_size     INTEGER DEFAULT 0,

                -- Time range
                first_observation   TEXT,
                last_observation    TEXT,
                updated_at          TEXT NOT NULL,

                UNIQUE(setup_key, strategy_key)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_sr_setup ON strategy_results(setup_key)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sr_ev ON strategy_results(ev_per_trade_c)")

        # Migrate: add new columns to strategy_results
        for col, coltype in [("weighted_win_rate", "REAL"),
                             ("weighted_ev_c", "REAL"),
                             ("oos_ev_c", "REAL"),
                             ("oos_win_rate", "REAL"),
                             ("oos_sample_size", "INTEGER DEFAULT 0"),
                             # v2 — statistical rigor
                             ("fdr_significant", "INTEGER DEFAULT 0"),
                             ("fdr_q_value", "REAL"),
                             ("sell_target", "TEXT"),
                             # v3 — simulation hardening
                             ("slippage_1c_ev", "REAL"),      # EV at +1¢ slippage
                             ("slippage_2c_ev", "REAL"),      # EV at +2¢ slippage
                             ("breakeven_fee_rate", "REAL"),   # fee rate where EV hits 0
                             ("quality_full_ev_c", "REAL"),    # EV on full-quality obs only
                             ("quality_degraded_ev_c", "REAL"),# EV on short/partial obs only
                             # v4 — FDR t-test and rolling walk-forward
                             ("pnl_std_c", "REAL"),            # PnL standard deviation (for t-test FDR)
                             ]:
            try:
                c.execute(f"ALTER TABLE strategy_results ADD COLUMN {col} {coltype}")
            except Exception:
                pass

        # ── Regime stability log (tracks label persistence) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS regime_stability_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at     TEXT NOT NULL,
                prev_label      TEXT,
                curr_label      TEXT,
                prev_coarse     TEXT,
                curr_coarse     TEXT,
                label_changed   INTEGER DEFAULT 0,
                coarse_changed  INTEGER DEFAULT 0,
                btc_price       REAL,
                btc_change_pct  REAL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_rstab_ts ON regime_stability_log(captured_at)")

        # ── BTC probability surface (empirical: distance × time → win rate) ──
        # ── BTC probability surface (vol-conditioned) ──
        # Check if we need to recreate table with vol_bucket column
        try:
            c.execute("SELECT vol_bucket FROM btc_probability_surface LIMIT 1")
        except Exception:
            # Table exists without vol_bucket — drop and recreate
            # (fully derived data, rebuilt every 30 min, no loss)
            c.execute("DROP TABLE IF EXISTS btc_probability_surface")

        c.execute("""
            CREATE TABLE IF NOT EXISTS btc_probability_surface (
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
            CREATE TABLE IF NOT EXISTS feature_importance (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                feature_name    TEXT NOT NULL UNIQUE,
                importance      REAL,
                correlation     REAL,
                sample_size     INTEGER DEFAULT 0,
                method          TEXT DEFAULT 'logistic',
                updated_at      TEXT NOT NULL
            )
        """)

        # ── Audit log (security — tracks destructive operations) ──
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

        # ── Convergence metric snapshots ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS metric_snapshots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at TEXT NOT NULL,
                metrics     TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_ms_ts ON metric_snapshots(recorded_at)")

    print(f"[db] Initialized at {DB_PATH}")


def run_migration_v2_clean_slate():
    """One-time migration: clear all trade-derived data for clean restart.

    Preserves market_observations (820+ pure price paths) and config.
    Clears everything else so the system rebuilds cleanly with:
    - Directional coarse regime labels
    - 3 setup types (global, coarse_regime, hour)
    - 4 side rules (no favored)
    - No confidence model artifacts

    Safe to call multiple times — checks migration version first.
    """
    # Use raw connection with autocommit — PRAGMA foreign_keys must be set
    # outside any transaction, and Python's sqlite3 driver auto-starts them
    _ensure_dir()
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        c = conn.cursor()

        # Create migrations table if needed
        c.execute("""
            CREATE TABLE IF NOT EXISTS _migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
        """)
        already = c.execute(
            "SELECT 1 FROM _migrations WHERE version = 2"
        ).fetchone()
        if already:
            conn.close()
            return False  # Already applied

        print("[db] Running migration v2: clean slate (preserving observations)...")

        c.execute("BEGIN")

        # ── Clear tables that reference trades ──
        c.execute("DELETE FROM regime_opportunities")
        c.execute("DELETE FROM exit_simulations")
        c.execute("DELETE FROM price_path")

        # ── Clear all trade-derived data ──
        c.execute("DELETE FROM trades")
        c.execute("DELETE FROM strategy_results")
        c.execute("DELETE FROM regime_stats")
        c.execute("DELETE FROM hourly_stats")
        c.execute("DELETE FROM metric_snapshots")
        c.execute("DELETE FROM regime_stability_log")
        c.execute("DELETE FROM feature_importance")

        # ── Clear orphaned confidence model tables ──
        c.execute("DELETE FROM confidence_factors")
        c.execute("DELETE FROM confidence_calibration")
        c.execute("DELETE FROM edge_calibration")

        # ── Reset bot_state counters ──
        c.execute("""
            UPDATE bot_state SET
                session_pnl = 0, session_wins = 0,
                session_losses = 0, session_skips = 0,
                lifetime_pnl = 0, lifetime_wins = 0,
                lifetime_losses = 0
            WHERE id = 1
        """)

        # ── Mark migration complete ──
        c.execute(
            "INSERT INTO _migrations (version, applied_at) VALUES (2, ?)",
            (now_utc(),)
        )

        conn.commit()

        # Count preserved observations
        obs_count = c.execute(
            "SELECT COUNT(*) as n FROM market_observations"
        ).fetchone()[0]

        print(f"[db] Migration v2 complete: cleared all trades/stats, "
              f"preserved {obs_count} observations")
        conn.close()
        return True

    except Exception:
        conn.rollback()
        conn.close()
        raise


def insert_audit_log(action: str, detail: str = "", ip: str = "", success: bool = True):
    """Log a security-relevant action to the audit trail."""
    try:
        with get_conn() as c:
            c.execute(
                "INSERT INTO audit_log (created_at, action, detail, ip, success) VALUES (?,?,?,?,?)",
                (now_utc(), action, detail, ip, 1 if success else 0)
            )
    except Exception:
        pass  # Never block on audit failure


def backup_database(reason: str = "manual") -> str | None:
    """Create a timestamped backup of the database. Returns backup path or None."""
    import shutil
    try:
        backup_dir = os.path.join(os.path.dirname(DB_PATH), "_db_backups")
        os.makedirs(backup_dir, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(backup_dir, f"botdata_{reason}_{ts}.db")
        # WAL-safe file copy: checkpoint flushes WAL into main DB first
        src_conn = sqlite3.connect(DB_PATH, timeout=10)
        src_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        src_conn.close()
        shutil.copy2(DB_PATH, backup_path)
        # Keep only last 10 backups
        backups = sorted(
            [f for f in os.listdir(backup_dir) if f.endswith('.db')],
            reverse=True
        )
        for old in backups[10:]:
            try:
                os.remove(os.path.join(backup_dir, old))
            except Exception:
                pass
        return backup_path
    except Exception as e:
        print(f"[db] Backup failed: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
#  BOT STATE
# ═══════════════════════════════════════════════════════════════

def get_bot_state() -> dict:
    with get_conn() as c:
        row = c.execute("SELECT * FROM bot_state WHERE id = 1").fetchone()
        state = row_to_dict(row) or {}
        # Parse JSON fields
        for key in ("active_trade", "active_skip", "active_shadow", "live_market", "last_completed_trade", "pending_trade", "observatory_health"):
            if state.get(key):
                try:
                    state[key] = json.loads(state[key])
                except (json.JSONDecodeError, TypeError):
                    state[key] = None
        return state


def update_bot_state(data: dict):
    # Serialize any dict fields to JSON
    for key in ("active_trade", "active_skip", "active_shadow", "live_market", "last_completed_trade", "pending_trade"):
        if key in data and isinstance(data[key], (dict, list)):
            data = {**data, key: json.dumps(data[key])}
    data["last_updated"] = now_utc()
    sets = ", ".join(f"{k} = ?" for k in data.keys())
    with get_conn() as c:
        c.execute(f"UPDATE bot_state SET {sets} WHERE id = 1",
                  list(data.values()))


def clear_active_trade():
    with get_conn() as c:
        c.execute("""
            UPDATE bot_state SET active_trade = NULL, last_updated = ?
            WHERE id = 1
        """, (now_utc(),))


# ═══════════════════════════════════════════════════════════════
#  BOT CONFIG
# ═══════════════════════════════════════════════════════════════

def get_config(key: str, default=None):
    with get_conn() as c:
        row = c.execute("SELECT value FROM bot_config WHERE key = ?",
                        (key,)).fetchone()
        if row:
            return json.loads(row["value"])
        return default


def set_config(key: str, value):
    with get_conn() as c:
        c.execute("""
            INSERT INTO bot_config (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                           updated_at = excluded.updated_at
        """, (key, json.dumps(value), now_utc()))


def get_all_config() -> dict:
    with get_conn() as c:
        rows = c.execute("SELECT key, value FROM bot_config").fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}


# ═══════════════════════════════════════════════════════════════
#  BOT COMMANDS
# ═══════════════════════════════════════════════════════════════

def enqueue_command(command_type: str, parameters: dict = None) -> int:
    with get_conn() as c:
        cur = c.execute("""
            INSERT INTO bot_commands (command_type, parameters, created_at)
            VALUES (?, ?, ?)
        """, (command_type, json.dumps(parameters or {}), now_utc()))
        return cur.lastrowid


def get_pending_commands() -> list:
    """Atomically claim all pending commands (pending → executing) and return them."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT * FROM bot_commands WHERE status = 'pending'
            ORDER BY created_at ASC
        """).fetchall()
        cmds = rows_to_list(rows)
        # Atomically mark them executing so no other processor grabs them
        for cmd in cmds:
            c.execute("""
                UPDATE bot_commands SET status = 'executing'
                WHERE id = ? AND status = 'pending'
            """, (cmd["id"],))
        return cmds


def complete_command(cmd_id: int, result: dict = None):
    with get_conn() as c:
        c.execute("""
            UPDATE bot_commands SET status = 'completed', result = ?
            WHERE id = ?
        """, (json.dumps(result or {}), cmd_id))


def cancel_command(cmd_id: int, reason: str = ""):
    with get_conn() as c:
        c.execute("""
            UPDATE bot_commands SET status = 'cancelled',
                result = ? WHERE id = ?
        """, (json.dumps({"reason": reason}), cmd_id))


def flush_pending_commands():
    """Cancel all pending/executing commands. Called on startup to clear stale queue."""
    with get_conn() as c:
        c.execute("""
            UPDATE bot_commands SET status = 'cancelled',
                result = '{"reason": "flushed on startup"}'
            WHERE status IN ('pending', 'executing')
        """)


# ═══════════════════════════════════════════════════════════════
#  MARKETS
# ═══════════════════════════════════════════════════════════════

def upsert_market(ticker: str, close_time_utc: str, hour_et: int,
                  minute_et: int, day_of_week: int) -> int:
    with get_conn() as c:
        c.execute("""
            INSERT INTO markets (ticker, close_time_utc, hour_et, minute_et,
                                 day_of_week, is_weekend, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                close_time_utc = excluded.close_time_utc
        """, (ticker, close_time_utc, hour_et, minute_et,
              day_of_week, int(day_of_week >= 5), now_utc()))
        row = c.execute("SELECT id FROM markets WHERE ticker = ?",
                        (ticker,)).fetchone()
        return row["id"]


def update_market_outcome(market_id: int, outcome: str):
    with get_conn() as c:
        c.execute("UPDATE markets SET outcome = ? WHERE id = ?",
                  (outcome, market_id))


# ═══════════════════════════════════════════════════════════════
#  TRADES
# ═══════════════════════════════════════════════════════════════

def insert_trade(data: dict) -> int:
    data["created_at"] = now_utc()
    cols = ", ".join(data.keys())
    placeholders = ", ".join(["?"] * len(data))
    with get_conn() as c:
        cur = c.execute(f"INSERT INTO trades ({cols}) VALUES ({placeholders})",
                        list(data.values()))
        return cur.lastrowid


def update_trade(trade_id: int, data: dict):
    sets = ", ".join(f"{k} = ?" for k in data.keys())
    with get_conn() as c:
        c.execute(f"UPDATE trades SET {sets} WHERE id = ?",
                  list(data.values()) + [trade_id])


def get_recent_trades(limit: int = 50) -> list:
    with get_conn() as c:
        rows = c.execute("""
            SELECT * FROM trades
            WHERE outcome IN ('win', 'loss', 'cashed_out', 'skipped', 'no_fill', 'error', 'open')
            ORDER BY created_at DESC LIMIT ?
        """, (limit,)).fetchall()
        return rows_to_list(rows)


def get_trade(trade_id: int) -> dict | None:
    with get_conn() as c:
        row = c.execute("SELECT * FROM trades WHERE id = ?",
                        (trade_id,)).fetchone()
        return row_to_dict(row)


def delete_trades(trade_ids: list):
    """Delete trades and their price paths.
    Observatory observations are preserved — they have independent value
    for strategy simulation even if the associated trade is deleted.
    Bulk resets (dashboard 'Delete All Trades') handle observation cleanup separately."""
    if not trade_ids:
        return 0
    placeholders = ",".join(["?"] * len(trade_ids))
    with get_conn() as c:
        # Delete price path data
        c.execute(f"DELETE FROM price_path WHERE trade_id IN ({placeholders})",
                  trade_ids)
        # Delete exit simulations
        c.execute(f"DELETE FROM exit_simulations WHERE trade_id IN ({placeholders})",
                  trade_ids)
        # Delete regime opportunities linked to these trades
        c.execute(f"DELETE FROM regime_opportunities WHERE trade_id IN ({placeholders})",
                  trade_ids)
        # Delete the trades
        c.execute(f"DELETE FROM trades WHERE id IN ({placeholders})",
                  trade_ids)
        return len(trade_ids)


def recompute_all_stats():
    """
    Recompute all derived stats from the trades table.
    Called after deleting trades to ensure consistency.
    Updates: regime_stats, bot_state lifetime counters.
    """
    with get_conn() as c:
        # Recompute lifetime stats from non-ignored trades
        row = c.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END), 0) as wins,
                COALESCE(SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END), 0) as losses,
                COALESCE(SUM(pnl), 0) as total_pnl
            FROM trades
            WHERE outcome IN ('win', 'loss')
              AND COALESCE(is_ignored, 0) = 0
        """).fetchone()

        c.execute("""
            UPDATE bot_state SET
                lifetime_wins = ?,
                lifetime_losses = ?,
                lifetime_pnl = ?
            WHERE id = 1
        """, (row["wins"], row["losses"], round(row["total_pnl"], 2)))

    # Recompute all regime stats
    with get_conn() as c:
        # Clear existing stats
        c.execute("DELETE FROM regime_stats")
        # Get all distinct regime labels (including observed-only regimes)
        rows = c.execute("""
            SELECT DISTINCT regime_label FROM trades
            WHERE regime_label IS NOT NULL AND regime_label != 'unknown'
        """).fetchall()

    for row in rows:
        update_regime_stats(row["regime_label"])


def get_open_trade() -> dict | None:
    with get_conn() as c:
        row = c.execute("""
            SELECT * FROM trades WHERE outcome = 'open'
            ORDER BY created_at DESC LIMIT 1
        """).fetchone()
        return row_to_dict(row)


# ═══════════════════════════════════════════════════════════════
#  PRICE PATH
# ═══════════════════════════════════════════════════════════════

def insert_price_point(trade_id: int, data: dict):
    data["trade_id"] = trade_id
    data["captured_at"] = now_utc()
    cols = ", ".join(data.keys())
    placeholders = ", ".join(["?"] * len(data))
    with get_conn() as c:
        c.execute(f"INSERT INTO price_path ({cols}) VALUES ({placeholders})",
                  list(data.values()))


def get_price_path(trade_id: int) -> list:
    with get_conn() as c:
        rows = c.execute("""
            SELECT * FROM price_path WHERE trade_id = ?
            ORDER BY captured_at ASC
        """, (trade_id,)).fetchall()
        return rows_to_list(rows)


# ═══════════════════════════════════════════════════════════════
#  BTC CANDLES
# ═══════════════════════════════════════════════════════════════

def insert_btc_candles(candles: list):
    with get_conn() as c:
        c.executemany("""
            INSERT OR IGNORE INTO btc_candles (ts, open, high, low, close, volume)
            VALUES (:ts, :open, :high, :low, :close, :volume)
        """, candles)


def get_btc_candles(since: str, limit: int = 1500) -> list:
    with get_conn() as c:
        rows = c.execute("""
            SELECT * FROM btc_candles WHERE ts >= ?
            ORDER BY ts ASC LIMIT ?
        """, (since, limit)).fetchall()
        return rows_to_list(rows)


def get_latest_btc_candle() -> dict | None:
    with get_conn() as c:
        row = c.execute(
            "SELECT * FROM btc_candles ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        return row_to_dict(row)


def count_btc_candles() -> int:
    with get_conn() as c:
        return c.execute("SELECT COUNT(*) as n FROM btc_candles").fetchone()["n"]


# ═══════════════════════════════════════════════════════════════
#  BASELINES
# ═══════════════════════════════════════════════════════════════

def upsert_baseline(hour_et: int | None, day_of_week: int | None, data: dict):
    with get_conn() as c:
        c.execute("""
            DELETE FROM baselines
            WHERE (hour_et IS ? OR (hour_et IS NULL AND ? IS NULL))
              AND (day_of_week IS ? OR (day_of_week IS NULL AND ? IS NULL))
        """, (hour_et, hour_et, day_of_week, day_of_week))

        fields = {"computed_at": now_utc(), "hour_et": hour_et,
                  "day_of_week": day_of_week, **data}
        cols = ", ".join(fields.keys())
        placeholders = ", ".join(["?"] * len(fields))
        c.execute(f"INSERT INTO baselines ({cols}) VALUES ({placeholders})",
                  list(fields.values()))


def get_baseline(hour_et: int = None, day_of_week: int = None) -> dict | None:
    with get_conn() as c:
        for h, d in [(hour_et, day_of_week), (hour_et, None), (None, None)]:
            row = c.execute("""
                SELECT * FROM baselines
                WHERE (hour_et IS ? OR (hour_et IS NULL AND ? IS NULL))
                  AND (day_of_week IS ? OR (day_of_week IS NULL AND ? IS NULL))
                ORDER BY computed_at DESC LIMIT 1
            """, (h, h, d, d)).fetchone()
            if row:
                return row_to_dict(row)
        return None


# ═══════════════════════════════════════════════════════════════
#  REGIME SNAPSHOTS
# ═══════════════════════════════════════════════════════════════

def insert_regime_snapshot(data: dict) -> int:
    data["captured_at"] = now_utc()
    cols = ", ".join(data.keys())
    placeholders = ", ".join(["?"] * len(data))
    with get_conn() as c:
        cur = c.execute(
            f"INSERT INTO regime_snapshots ({cols}) VALUES ({placeholders})",
            list(data.values()))
        return cur.lastrowid


def get_latest_regime_snapshot() -> dict | None:
    with get_conn() as c:
        row = c.execute("""
            SELECT * FROM regime_snapshots
            ORDER BY captured_at DESC LIMIT 1
        """).fetchone()
        return row_to_dict(row)


# ═══════════════════════════════════════════════════════════════
#  REGIME STATS
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


# ── Composite Risk Scoring ──────────────────────────────────

def _classify_risk(score: float, count: int, min_known: int) -> str:
    """Map a composite risk score (0-100) to a risk level string."""
    from config import REGIME_THRESHOLDS
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

    Missing components are excluded and weights renormalized so the score
    is never penalized for data that hasn't been computed yet.
    """
    components = []  # list of (score_0_100, weight)

    # ── 1. EV Signal (30%) ──
    ev = row.get("weighted_ev_c")
    if ev is None:
        ev = row.get("ev_per_trade_c") or 0
    if ev <= -5:
        ev_score = 0
    elif ev <= 0:
        ev_score = 20 * (1 + ev / 5)       # -5→0, 0→20
    elif ev <= 3:
        ev_score = 20 + 45 * (ev / 3)      # 0→20, 3→65
    elif ev <= 8:
        ev_score = 65 + 35 * ((ev - 3) / 5)  # 3→65, 8→100
    else:
        ev_score = 100
    components.append((ev_score, 0.30))

    # ── 2. Statistical Confidence (20%) ──
    n = row.get("sample_size") or 0
    ci_lo = row.get("ci_lower") or 0
    ci_hi = row.get("ci_upper") or 1
    ci_width = ci_hi - ci_lo

    # Sample size: log-scaled, 10→40, 100→80, 300→99
    n_score = min(100, 40 * math.log10(max(n, 1)))
    # CI width: 0→100, 0.3→40, 0.5→0
    ci_score = max(0, min(100, 100 * (1 - ci_width * 2)))
    # FDR significance bonus
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

        # Consistency bonus/penalty: in-sample positive + OOS positive = good
        if ev > 0 and oos_ev > 0:
            oos_score = min(100, oos_score + 10)
        elif ev > 0 and oos_ev < 0:
            oos_score = max(0, oos_score - 15)  # overfitting penalty

        components.append((oos_score, 0.20))

    # ── 4. Downside Risk (15%) ──
    pnl_std = row.get("pnl_std_c")
    max_cl = row.get("max_consecutive_losses") or 0
    pf = row.get("profit_factor")

    dd_parts = []
    if pnl_std is not None and pnl_std > 0:
        # Sharpe-like: EV / std, higher is better
        sharpe = ev / max(pnl_std, 0.1)
        sharpe_score = min(100, max(0, 50 + sharpe * 40))
        dd_parts.append(sharpe_score)
    if n >= 10:
        # Max consecutive losses: <3 = great, 5 = ok, >8 = bad
        cl_score = max(0, min(100, 100 - max_cl * 12))
        dd_parts.append(cl_score)
    if pf is not None:
        # Profit factor: <0.8→bad, 1.0→40, 1.5→73, 2.0+→100
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
        # Breakeven fee rate vs current ~8.5% fee
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

    # ── Normalize: scale by actual weights used ──
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
    # 1. Win Rate (35%)
    # 30% WR → 0, 50% → 50, 65%+ → 100
    wr_score = max(0, min(100, (win_rate - 0.30) / 0.35 * 100))

    # 2. Avg PnL (30%)
    # avg_pnl in dollars: < -2 → 0, 0 → 40, +2 → 80, +5+ → 100
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


def update_regime_stats(regime_label: str) -> dict:
    """Recompute stats for a specific regime label from real trade data only.

    Returns dict with: is_new, old_risk, new_risk, total, win_rate, risk_score
    """
    from config import REGIME_THRESHOLDS

    with get_conn() as c:
        # Check if this regime already exists in stats
        existing = c.execute(
            "SELECT risk_level, total_trades FROM regime_stats WHERE regime_label = ?",
            (regime_label,)
        ).fetchone()
        old_risk = existing["risk_level"] if existing else None
        old_total = existing["total_trades"] if existing else 0
        is_new = existing is None

        # Real trades only (non-ignored)
        real = c.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) as losses,
                SUM(COALESCE(pnl, 0)) as total_pnl
            FROM trades
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

        # Determine risk level via composite score
        min_known = REGIME_THRESHOLDS["min_trades_known"]
        risk_score = compute_trade_risk_score(win_rate, avg_pnl, ci_low, ci_high, total)
        risk_level = _classify_risk(risk_score, total, min_known)

        c.execute("""
            INSERT INTO regime_stats
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


def get_regime_risk(regime_label: str) -> dict:
    """Get risk level for a regime. Returns dict with risk info."""
    with get_conn() as c:
        row = c.execute(
            "SELECT * FROM regime_stats WHERE regime_label = ?",
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
    """
    Get risk level for a specific strategy in a specific regime.
    Uses Strategy Observatory data (strategy_results table).

    Only uses regime-specific data. Does NOT fall back to global:all,
    because global risk can be misleading for a specific regime (e.g.,
    a strategy might be terrible globally but good in calm regimes).

    Risk classification uses composite scoring (0-100) incorporating:
    EV signal, statistical confidence, OOS validation, downside risk,
    and robustness (slippage/fee survival).

    Returns dict with: risk_level, risk_score, win_rate, ev_per_trade_c,
                        sample_size, ci_lower, ci_upper, setup_key
    """
    from config import REGIME_THRESHOLDS

    with get_conn() as c:
        # Regime-specific only — no global fallback
        setup_key = f"regime:{regime_label}"
        row = c.execute("""
            SELECT win_rate, sample_size, ev_per_trade_c,
                   ci_lower, ci_upper, profit_factor,
                   weighted_ev_c, weighted_win_rate,
                   oos_ev_c, oos_win_rate, oos_sample_size,
                   fdr_significant, fdr_q_value,
                   pnl_std_c, max_consecutive_losses, max_drawdown_c,
                   slippage_1c_ev, slippage_2c_ev, breakeven_fee_rate
            FROM strategy_results
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


def get_all_regime_stats() -> list:
    with get_conn() as c:
        rows = c.execute("""
            SELECT * FROM regime_stats ORDER BY total_trades DESC
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
            FROM trades t
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
        c.execute("UPDATE trades SET market_result = ? WHERE id = ?",
                  (market_result, trade_id))


# ═══════════════════════════════════════════════════════════════
#  COARSE REGIME STATS
# ═══════════════════════════════════════════════════════════════

def update_coarse_regime_stats(coarse_label: str):
    """Compute stats for a coarse regime label from real trade data only."""
    from config import REGIME_THRESHOLDS

    with get_conn() as c:
        real = c.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) as losses,
                SUM(COALESCE(pnl, 0)) as total_pnl
            FROM trades
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
            INSERT INTO regime_stats
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
            SELECT DISTINCT coarse_regime FROM trades
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
            "SELECT * FROM regime_stats WHERE regime_label = ?",
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
#  HOURLY STATS (time-of-day win rates)
# ═══════════════════════════════════════════════════════════════

def update_hourly_stats(hour_et: int, day_of_week: int = None):
    """Compute win rate stats for a specific ET hour (and optionally day).
    Real trades only."""
    from config import REGIME_THRESHOLDS

    with get_conn() as c:
        if day_of_week is not None:
            real = c.execute("""
                SELECT COUNT(*) as total,
                    SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) as losses,
                    SUM(COALESCE(pnl, 0)) as total_pnl
                FROM trades
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
                FROM trades
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
            INSERT INTO hourly_stats
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
            SELECT DISTINCT hour_et FROM trades
            WHERE hour_et IS NOT NULL
              AND outcome IN ('win', 'loss')
              AND COALESCE(is_ignored, 0) = 0
        """).fetchall()
        hour_days = c.execute("""
            SELECT DISTINCT hour_et, day_of_week FROM trades
            WHERE hour_et IS NOT NULL AND day_of_week IS NOT NULL
              AND outcome IN ('win', 'loss')
              AND COALESCE(is_ignored, 0) = 0
        """).fetchall()

    for row in hours:
        update_hourly_stats(row["hour_et"], day_of_week=None)
    for row in hour_days:
        update_hourly_stats(row["hour_et"], day_of_week=row["day_of_week"])


def get_hourly_risk(hour_et: int, day_of_week: int = None) -> dict:
    """Get risk info for a specific ET hour."""
    with get_conn() as c:
        # Try hour+day first, fall back to hour-only
        for dow in ([day_of_week, None] if day_of_week is not None else [None]):
            if dow is not None:
                row = c.execute(
                    "SELECT * FROM hourly_stats WHERE hour_et = ? AND day_of_week = ?",
                    (hour_et, dow)
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT * FROM hourly_stats WHERE hour_et = ? AND day_of_week IS NULL",
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


def get_all_hourly_stats() -> list:
    """Get all hourly stats for dashboard display."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT * FROM hourly_stats
            WHERE day_of_week IS NULL
            ORDER BY hour_et
        """).fetchall()
        return rows_to_list(rows)


# ═══════════════════════════════════════════════════════════════
#  REGIME TRANSITION TRACKING
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


# ═══════════════════════════════════════════════════════════════
#  SKIPPED TRADE ANALYTICS
# ═══════════════════════════════════════════════════════════════

def get_skip_analysis() -> dict:
    """Analyze skipped trades — market results for observed markets."""
    with get_conn() as c:
        overview = c.execute("""
            SELECT
                COUNT(*) as total_skipped,
                SUM(CASE WHEN market_result IS NOT NULL THEN 1 ELSE 0 END) as with_result,
                SUM(CASE WHEN market_result = 'yes' THEN 1 ELSE 0 END) as result_yes,
                SUM(CASE WHEN market_result = 'no' THEN 1 ELSE 0 END) as result_no
            FROM trades
            WHERE outcome = 'skipped'
        """).fetchone()

        by_regime = c.execute("""
            SELECT
                regime_label,
                SUM(CASE WHEN market_result IS NOT NULL THEN 1 ELSE 0 END) as n,
                SUM(CASE WHEN market_result = 'yes' THEN 1 ELSE 0 END) as result_yes,
                SUM(CASE WHEN market_result = 'no' THEN 1 ELSE 0 END) as result_no
            FROM trades
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
            FROM trades
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
#  LOG ENTRIES
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
#  LIVE PRICE HISTORY
# ═══════════════════════════════════════════════════════════════

def insert_live_price(ticker: str, yes_ask, no_ask, yes_bid, no_bid):
    """Record a live market price snapshot. Called each poll."""
    with get_conn() as c:
        c.execute("""
            INSERT INTO live_prices (ts, ticker, yes_ask, no_ask, yes_bid, no_bid)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (now_utc(), ticker, yes_ask, no_ask, yes_bid, no_bid))
        # Cleanup: keep only last 20 minutes of data
        c.execute("""
            DELETE FROM live_prices WHERE ts < datetime('now', '-20 minutes')
        """)


def get_live_prices(ticker: str = None, limit: int = 900) -> list:
    """Get recent live prices, optionally filtered by ticker."""
    with get_conn() as c:
        if ticker:
            rows = c.execute("""
                SELECT ts, ticker, yes_ask, no_ask, yes_bid, no_bid
                FROM live_prices WHERE ticker = ?
                ORDER BY id DESC LIMIT ?
            """, (ticker, limit)).fetchall()
        else:
            rows = c.execute("""
                SELECT ts, ticker, yes_ask, no_ask, yes_bid, no_bid
                FROM live_prices ORDER BY id DESC LIMIT ?
            """, (limit,)).fetchall()
        return rows_to_list(list(reversed(rows)))


def insert_log(level: str, message: str, category: str = "bot"):
    with get_conn() as c:
        c.execute("""
            INSERT INTO log_entries (ts, level, category, message)
            VALUES (?, ?, ?, ?)
        """, (now_utc(), level, category, message))


def get_logs(before_id: int = None, limit: int = 100, level: str = None) -> list:
    with get_conn() as c:
        conditions = []
        params = []
        if before_id:
            conditions.append("id < ?")
            params.append(before_id)
        if level:
            conditions.append("level = ?")
            params.append(level)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)
        rows = c.execute(f"""
            SELECT * FROM log_entries {where}
            ORDER BY id DESC LIMIT ?
        """, params).fetchall()
        return rows_to_list(rows)


def get_logs_after(after_id: int) -> list:
    with get_conn() as c:
        rows = c.execute("""
            SELECT * FROM log_entries WHERE id > ?
            ORDER BY id ASC
        """, (after_id,)).fetchall()
        return rows_to_list(rows)


# ═══════════════════════════════════════════════════════════════
#  ANALYTICS
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
                SUM(CASE WHEN outcome='cashed_out' THEN 1 ELSE 0 END) as cashouts,
                COALESCE(SUM(pnl), 0) as total_pnl,
                AVG(CASE WHEN outcome='win' THEN pnl END) as avg_win,
                AVG(CASE WHEN outcome='loss' THEN pnl END) as avg_loss
            FROM trades
            WHERE outcome IN ('win','loss','skipped','no_fill','cashed_out')
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
                SUM(CASE WHEN outcome='cashed_out' THEN 1 ELSE 0 END) as cashouts,
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
            FROM trades
            WHERE outcome IN ('win','loss','cashed_out')
              AND COALESCE(is_ignored, 0) = 0
        """).fetchone()
        stats = row_to_dict(core) or {}

        # Win/loss streaks — only count consecutive resolved trades
        rows = c.execute("""
            SELECT outcome FROM trades
            WHERE outcome IN ('win','loss')
              AND COALESCE(is_ignored, 0) = 0
              AND COALESCE(outcome, '') != 'skipped'
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
            SELECT pnl FROM trades
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
            SELECT COALESCE(SUM(pnl), 0) as s FROM trades
            WHERE outcome='win' AND COALESCE(is_ignored,0)=0
        """).fetchone()["s"]
        total_losses = abs(c.execute("""
            SELECT COALESCE(SUM(pnl), 0) as s FROM trades
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
            FROM trades
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
            FROM trades
            WHERE outcome IN ('win','loss')
              AND COALESCE(is_ignored, 0) = 0
            GROUP BY COALESCE(entry_delay_minutes, 0)
            ORDER BY delay_min
        """).fetchall()
        stats["delay_breakdown"] = rows_to_list(delay_rows)

        # Price stability breakdown (bucket into ranges)
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
            FROM trades
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
            FROM trades
            WHERE outcome IN ('win','loss')
              AND COALESCE(is_ignored, 0) = 0
            GROUP BY vol_level
            ORDER BY vol_level
        """).fetchall()
        stats["vol_breakdown"] = rows_to_list(vol_rows)

        # Hourly performance (by CT hour, derived from DST-correct hour_et)
        # CT is always ET - 1 hour (both zones switch DST on same dates)
        hourly_rows = c.execute("""
            SELECT
                CASE WHEN hour_et IS NOT NULL THEN (hour_et - 1 + 24) % 24
                     ELSE CAST(STRFTIME('%H', created_at, '-5 hours') AS INTEGER)
                END as hour_ct,
                COUNT(*) as total,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                COALESCE(SUM(pnl), 0) as net_pnl
            FROM trades
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
            FROM trades
            WHERE outcome IN ('win','loss')
              AND COALESCE(is_ignored, 0) = 0
              AND side IN ('yes','no')
            GROUP BY UPPER(side)
        """).fetchall()
        stats["side_breakdown"] = rows_to_list(side_rows)

        # Entry price performance (per cent, with implied odds comparison)
        price_rows = c.execute("""
            SELECT
                avg_fill_price_c as price_c,
                COUNT(*) as total,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                COALESCE(SUM(pnl), 0) as net_pnl
            FROM trades
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
            FROM trades
            WHERE outcome IN ('win','loss')
              AND COALESCE(is_ignored, 0) = 0
              AND regime_label IS NOT NULL
            GROUP BY regime_label
            HAVING COUNT(*) >= 3
            ORDER BY net_pnl DESC
        """).fetchall()
        stats["regime_performance"] = rows_to_list(regime_perf_rows)

        # Coarse regime performance (real trades only)
        coarse_rows = c.execute("""
            SELECT
                coarse_regime,
                COUNT(*) as total,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                COALESCE(SUM(pnl), 0) as net_pnl,
                ROUND(CAST(SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) AS REAL)
                    / NULLIF(COUNT(*), 0) * 100, 1) as win_rate
            FROM trades
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
            FROM trades
            WHERE outcome IN ('win','loss')
              AND COALESCE(is_ignored, 0) = 0
            GROUP BY spread_bucket
            ORDER BY avg_spread
        """).fetchall()
        stats["spread_breakdown"] = rows_to_list(spread_rows)

        # BTC move breakdown (how much BTC moved during trades)
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
            FROM trades
            WHERE outcome IN ('win','loss')
              AND COALESCE(is_ignored, 0) = 0
            GROUP BY btc_move_bucket
            ORDER BY avg_btc_move
        """).fetchall()
        stats["btc_move_breakdown"] = rows_to_list(btc_move_rows)

        return stats


def get_regime_worker_status() -> dict:
    """Get regime worker status for the dashboard."""
    with get_conn() as c:
        candle_count = c.execute("SELECT COUNT(*) as n FROM btc_candles").fetchone()["n"]
        latest_candle = c.execute(
            "SELECT ts FROM btc_candles ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        earliest_candle = c.execute(
            "SELECT ts FROM btc_candles ORDER BY ts ASC LIMIT 1"
        ).fetchone()
        snapshot_count = c.execute(
            "SELECT COUNT(*) as n FROM regime_snapshots"
        ).fetchone()["n"]
        latest_snap = c.execute(
            "SELECT * FROM regime_snapshots ORDER BY captured_at DESC LIMIT 1"
        ).fetchone()
        baseline_count = c.execute(
            "SELECT COUNT(*) as n FROM baselines"
        ).fetchone()["n"]
        regime_label_count = c.execute(
            "SELECT COUNT(DISTINCT regime_label) as n FROM regime_stats"
        ).fetchone()["n"]

        # Snapshot frequency (avg seconds between last 10 snapshots)
        recent_snaps = c.execute("""
            SELECT captured_at FROM regime_snapshots
            ORDER BY captured_at DESC LIMIT 10
        """).fetchall()

        avg_interval = None
        if len(recent_snaps) >= 2:
            from datetime import datetime as dt
            times = [dt.fromisoformat(r["captured_at"].replace("Z","+00:00"))
                     for r in recent_snaps]
            diffs = [(times[i] - times[i+1]).total_seconds()
                     for i in range(len(times)-1)]
            avg_interval = round(sum(diffs) / len(diffs))

        # Engine phase from bot_state
        try:
            state = c.execute("SELECT regime_engine_phase as phase FROM bot_state WHERE id=1").fetchone()
            phase = state["phase"] if state and state["phase"] else None
        except Exception:
            phase = None

        return {
            "candle_count": candle_count,
            "candles_expected": 525_600,
            "candle_pct": round(candle_count / 525_600 * 100, 1),
            "latest_candle_ts": latest_candle["ts"] if latest_candle else None,
            "earliest_candle_ts": earliest_candle["ts"] if earliest_candle else None,
            "snapshot_count": snapshot_count,
            "latest_snapshot": row_to_dict(latest_snap) if latest_snap else None,
            "baseline_count": baseline_count,
            "regime_labels_tracked": regime_label_count,
            "avg_snapshot_interval_s": avg_interval,
            "engine_phase": phase,
        }


# ═══════════════════════════════════════════════════════════════
#  BANKROLL SNAPSHOTS
# ═══════════════════════════════════════════════════════════════

def insert_bankroll_snapshot(bankroll_cents: int, trade_id: int = None):
    with get_conn() as c:
        c.execute("""
            INSERT INTO bankroll_snapshots (captured_at, bankroll_cents, trade_id)
            VALUES (?, ?, ?)
        """, (now_utc(), bankroll_cents, trade_id))


def get_bankroll_chart_data(hours: int = None) -> list:
    """Get bankroll snapshots for charting. hours=None means all."""
    with get_conn() as c:
        if hours:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            rows = c.execute("""
                SELECT captured_at, bankroll_cents FROM bankroll_snapshots
                WHERE captured_at >= ?
                ORDER BY captured_at ASC
            """, (cutoff,)).fetchall()
        else:
            rows = c.execute("""
                SELECT captured_at, bankroll_cents FROM bankroll_snapshots
                ORDER BY captured_at ASC
            """).fetchall()
        return rows_to_list(rows)


def get_pnl_chart_data(hours: int = None) -> list:
    """Get cumulative PnL over time from trades."""
    with get_conn() as c:
        if hours:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            rows = c.execute("""
                SELECT created_at, pnl FROM trades
                WHERE outcome IN ('win', 'loss', 'cashed_out')
                  AND COALESCE(is_ignored, 0) = 0
                  AND created_at >= ?
                ORDER BY created_at ASC
            """, (cutoff,)).fetchall()
        else:
            rows = c.execute("""
                SELECT created_at, pnl FROM trades
                WHERE outcome IN ('win', 'loss', 'cashed_out')
                  AND COALESCE(is_ignored, 0) = 0
                ORDER BY created_at ASC
            """).fetchall()

        # For filtered views, we need the running PnL up to the cutoff
        base_pnl = 0
        if hours:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            base = c.execute("""
                SELECT COALESCE(SUM(pnl), 0) as s FROM trades
                WHERE outcome IN ('win', 'loss', 'cashed_out')
                  AND COALESCE(is_ignored, 0) = 0
                  AND created_at < ?
            """, (cutoff,)).fetchone()
            base_pnl = base["s"] or 0

        result = []
        running = base_pnl
        for r in rows:
            running += r["pnl"] or 0
            result.append({
                "ts": r["created_at"],
                "pnl": round(running, 2),
            })
        return result


# ═══════════════════════════════════════════════════════════════
#  PUSH SUBSCRIPTIONS
# ═══════════════════════════════════════════════════════════════

def save_push_subscription(endpoint: str, subscription_json: str):
    with get_conn() as c:
        c.execute("""
            INSERT OR REPLACE INTO push_subscriptions
            (endpoint, subscription_json, created_at)
            VALUES (?, ?, ?)
        """, (endpoint, subscription_json, now_utc()))


def get_push_subscriptions() -> list:
    with get_conn() as c:
        rows = c.execute("SELECT * FROM push_subscriptions").fetchall()
        return rows_to_list(rows)


def remove_push_subscription(sub_id: int):
    with get_conn() as c:
        c.execute("DELETE FROM push_subscriptions WHERE id = ?", (sub_id,))


def remove_push_subscription_by_endpoint(endpoint: str):
    with get_conn() as c:
        c.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))


# ═══════════════════════════════════════════════════════════════
#  PUSH LOG
# ═══════════════════════════════════════════════════════════════

def insert_push_log(title: str, body: str, tag: str = ""):
    with get_conn() as c:
        c.execute("""
            INSERT INTO push_log (title, body, tag, sent_at) VALUES (?, ?, ?, ?)
        """, (title, body or "", tag or "", now_utc()))
        # Auto-clean: keep last 500
        c.execute("""
            DELETE FROM push_log WHERE id NOT IN (
                SELECT id FROM push_log ORDER BY sent_at DESC LIMIT 500
            )
        """)


def get_push_log(limit: int = 200, tag: str = None) -> list:
    with get_conn() as c:
        if tag:
            rows = c.execute("""
                SELECT * FROM push_log WHERE tag = ?
                ORDER BY sent_at DESC LIMIT ?
            """, (tag, limit)).fetchall()
        else:
            rows = c.execute("""
                SELECT * FROM push_log ORDER BY sent_at DESC LIMIT ?
            """, (limit,)).fetchall()
        return rows_to_list(rows)


# ═══════════════════════════════════════════════════════════════
#  MARKET OBSERVATIONS (Strategy Observatory)
# ═══════════════════════════════════════════════════════════════

def upsert_market_observation(data: dict) -> int:
    """Insert or update a market observation. Returns the row id."""
    ticker = data["ticker"]
    with get_conn() as c:
        existing = c.execute(
            "SELECT id FROM market_observations WHERE ticker = ?", (ticker,)
        ).fetchone()
        if existing:
            obs_id = existing["id"]
            cols = ", ".join(f"{k} = ?" for k in data if k != "ticker")
            vals = [data[k] for k in data if k != "ticker"]
            if cols:
                c.execute(f"UPDATE market_observations SET {cols} WHERE id = ?",
                          vals + [obs_id])
            return obs_id
        else:
            data.setdefault("created_at", now_utc())
            keys = ", ".join(data.keys())
            placeholders = ", ".join("?" for _ in data)
            c.execute(f"INSERT INTO market_observations ({keys}) VALUES ({placeholders})",
                      list(data.values()))
            return c.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_unresolved_observations(limit: int = 50) -> list:
    """Get observations missing market_result for backfill."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT id, ticker, close_time_utc
            FROM market_observations
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
    Default changed from 'full' to 'short' to avoid survivorship bias —
    excluding degraded observations systematically removes the hardest markets.
    limit=0 means no limit (fetch all)."""
    quality_filter = ""
    if min_quality == "full":
        quality_filter = " AND COALESCE(obs_quality, 'full') = 'full'"
    elif min_quality == "short":
        quality_filter = " AND COALESCE(obs_quality, 'full') IN ('full', 'short')"
    # 'any' = no filter
    with get_conn() as c:
        base = f"""
            SELECT * FROM market_observations
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
            FROM market_observations
        """).fetchone()
        return dict(row) if row else {}


def upsert_strategy_result(data: dict):
    """Insert or update a strategy result row."""
    setup_key = data["setup_key"]
    strategy_key = data["strategy_key"]
    data["updated_at"] = now_utc()
    with get_conn() as c:
        existing = c.execute(
            "SELECT id FROM strategy_results WHERE setup_key = ? AND strategy_key = ?",
            (setup_key, strategy_key)
        ).fetchone()
        if existing:
            cols = ", ".join(f"{k} = ?" for k in data
                            if k not in ("setup_key", "strategy_key"))
            vals = [data[k] for k in data
                    if k not in ("setup_key", "strategy_key")]
            c.execute(f"UPDATE strategy_results SET {cols} WHERE id = ?",
                      vals + [existing["id"]])
        else:
            keys = ", ".join(data.keys())
            placeholders = ", ".join("?" for _ in data)
            c.execute(f"INSERT INTO strategy_results ({keys}) VALUES ({placeholders})",
                      list(data.values()))


def get_top_strategies(min_samples: int = 20, limit: int = 20) -> list:
    """Get best strategy results by EV, filtering by minimum confidence."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT * FROM strategy_results
            WHERE sample_size >= ?
              AND ev_per_trade_c > 0
              AND ci_lower > 0.4
            ORDER BY ev_per_trade_c DESC
            LIMIT ?
        """, (min_samples, limit)).fetchall()
        return rows_to_list(rows)


def get_strategy_for_setup(setup_key: str, min_samples: int = 15) -> list:
    """Get all strategy results for a specific setup.
    Sorted by weighted_ev_c (time-weighted), falling back to ev_per_trade_c."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT * FROM strategy_results
            WHERE setup_key = ?
              AND sample_size >= ?
            ORDER BY COALESCE(weighted_ev_c, ev_per_trade_c) DESC
        """, (setup_key, min_samples)).fetchall()
        return rows_to_list(rows)


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
            FROM strategy_results
            WHERE sample_size >= 20
              AND ev_per_trade_c IS NOT NULL
            ORDER BY ranked_ev DESC
            LIMIT 10
        """).fetchall()

        # Worst setups to avoid
        avoid = c.execute("""
            SELECT setup_key, strategy_key, sample_size, win_rate,
                   ev_per_trade_c, ci_lower
            FROM strategy_results
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


def get_net_edge_summary() -> dict:
    """The single most important metric: estimated edge per contract with CI.
    Computed from the best FDR-significant strategy at global level,
    with fallback to best non-FDR strategy."""
    with get_conn() as c:
        # Try FDR-significant global strategy first
        best = c.execute("""
            SELECT strategy_key, sample_size, win_rate, ev_per_trade_c,
                   ci_lower, ci_upper, profit_factor,
                   weighted_ev_c, oos_ev_c, oos_win_rate, oos_sample_size,
                   fdr_significant, fdr_q_value, max_consecutive_losses,
                   first_observation, last_observation
            FROM strategy_results
            WHERE setup_key = 'global:all' AND sample_size >= 30
            ORDER BY COALESCE(weighted_ev_c, ev_per_trade_c) DESC
            LIMIT 1
        """).fetchone()

        best_fdr = c.execute("""
            SELECT strategy_key, sample_size, win_rate, ev_per_trade_c,
                   ci_lower, ci_upper, profit_factor,
                   weighted_ev_c, oos_ev_c, oos_win_rate, oos_sample_size,
                   fdr_significant, fdr_q_value
            FROM strategy_results
            WHERE setup_key = 'global:all' AND sample_size >= 30
              AND fdr_significant = 1
            ORDER BY COALESCE(weighted_ev_c, ev_per_trade_c) DESC
            LIMIT 1
        """).fetchone()

        # Count total observations
        obs = c.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN COALESCE(obs_quality,'full') = 'full' THEN 1 ELSE 0 END) as sim_eligible
            FROM market_observations WHERE market_result IS NOT NULL
        """).fetchone()

        # Count FDR-significant strategies
        fdr_count = c.execute("""
            SELECT COUNT(*) as n FROM strategy_results
            WHERE fdr_significant = 1 AND sample_size >= 30
        """).fetchone()["n"]

        total_strategies = c.execute("""
            SELECT COUNT(*) as n FROM strategy_results
            WHERE sample_size >= 30
        """).fetchone()["n"]

        return {
            "best_overall": dict(best) if best else None,
            "best_fdr": dict(best_fdr) if best_fdr else None,
            "total_observations": obs["total"] if obs else 0,
            "sim_eligible_observations": obs["sim_eligible"] if obs else 0,
            "fdr_significant_strategies": fdr_count,
            "total_evaluated_strategies": total_strategies,
            "min_observations_needed": 200,  # Rough threshold before trusting results
            "data_sufficient": (obs["total"] or 0) >= 200,
        }


def get_realized_edge(windows: list = None) -> dict:
    """
    The most important metric: actual P&L per contract from real trades,
    compared to simulated EV. The gap between these reveals whether
    execution is destroying the edge.

    Returns rolling averages at each window size (default: 50, 100, 200)
    plus the best simulated EV for comparison.

    If simulated EV says +3¢/contract but realized is -1¢/contract,
    no amount of strategy optimization will fix the execution gap.
    """
    if windows is None:
        windows = [50, 100, 200]

    result = {"windows": {}}

    with get_conn() as c:
        for w in windows:
            rows = c.execute("""
                SELECT pnl, shares_filled, actual_cost, gross_proceeds,
                       outcome, auto_strategy_key
                FROM trades
                WHERE outcome IN ('win', 'loss')
                  AND COALESCE(is_ignored, 0) = 0
                  AND shares_filled > 0
                ORDER BY created_at DESC
                LIMIT ?
            """, (w,)).fetchall()

            trades = rows_to_list(rows)
            if not trades:
                continue

            # Actual P&L per contract
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

        # Simulated EV for comparison (best global strategy)
        sim_row = c.execute("""
            SELECT ev_per_trade_c, weighted_ev_c, strategy_key
            FROM strategy_results
            WHERE setup_key = 'global:all' AND sample_size >= 30
            ORDER BY COALESCE(weighted_ev_c, ev_per_trade_c) DESC
            LIMIT 1
        """).fetchone()

        result["simulated_ev_c"] = (
            round(sim_row["weighted_ev_c"] or sim_row["ev_per_trade_c"], 1)
            if sim_row else None
        )
        result["simulated_strategy"] = sim_row["strategy_key"] if sim_row else None

        # Compute the sim-vs-live gap for the largest window we have data for
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


def get_pnl_attribution(days: int = 30) -> dict:
    """
    Decompose realized P&L into four components:
      1. Model edge: did predicted probability beat market-implied probability?
      2. Execution cost: total fees + slippage as fraction of gross
      3. Timing impact: did entry timing help or hurt vs immediate entry?
      4. Exit method impact: sell fill vs hold-to-expiry comparison

    Without this decomposition, you can't tell whether profits come from
    genuinely predicting outcomes or from lucky execution on a few trades.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_conn() as c:
        rows = c.execute("""
            SELECT pnl, shares_filled, actual_cost, gross_proceeds,
                   outcome, exit_method, side,
                   entry_price_c, avg_fill_price_c,
                   predicted_win_pct, market_implied_pct, predicted_edge_pct,
                   ev_per_contract_c,
                   yes_ask_at_entry, no_ask_at_entry,
                   spread_at_entry_c, regime_label,
                   created_at
            FROM trades
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

    # ── 1. Model edge component ──
    # Trades where the model had a prediction: was the predicted edge realized?
    model_trades = [t for t in trades
                    if t.get("predicted_edge_pct") is not None]
    if model_trades:
        positive_edge_trades = [t for t in model_trades
                                if (t.get("predicted_edge_pct") or 0) > 0]
        negative_edge_trades = [t for t in model_trades
                                if (t.get("predicted_edge_pct") or 0) <= 0]
        pos_edge_wr = (sum(1 for t in positive_edge_trades if t["outcome"] == "win")
                       / len(positive_edge_trades)) if positive_edge_trades else None
        neg_edge_wr = (sum(1 for t in negative_edge_trades if t["outcome"] == "win")
                       / len(negative_edge_trades)) if negative_edge_trades else None
        avg_predicted_edge = round(
            sum(t["predicted_edge_pct"] for t in model_trades) / len(model_trades), 2
        )
        model_component = {
            "n_with_predictions": len(model_trades),
            "avg_predicted_edge_pct": avg_predicted_edge,
            "positive_edge_trades": len(positive_edge_trades),
            "positive_edge_win_rate": round(pos_edge_wr, 4) if pos_edge_wr is not None else None,
            "negative_edge_trades": len(negative_edge_trades),
            "negative_edge_win_rate": round(neg_edge_wr, 4) if neg_edge_wr is not None else None,
            "edge_predicts_outcome": (pos_edge_wr is not None and neg_edge_wr is not None
                                      and pos_edge_wr > neg_edge_wr),
        }
    else:
        model_component = {"n_with_predictions": 0}

    # ── 2. Execution cost component ──
    # Total fees and slippage as fraction of capital deployed
    slippages = []
    total_cost = 0
    for t in trades:
        entry = t.get("entry_price_c") or 0
        fill = t.get("avg_fill_price_c") or 0
        if entry > 0 and fill > 0:
            slippages.append(fill - entry)  # Positive = paid more
        total_cost += t.get("actual_cost") or 0

    # Gross proceeds (what we got back before fees)
    total_gross = sum(t.get("gross_proceeds") or 0 for t in trades)
    # Implicit total fees = cost - (entry_price × contracts)
    total_entry_value = sum((t.get("avg_fill_price_c") or 0) * (t.get("shares_filled") or 0)
                            for t in trades) / 100  # Convert cents to dollars

    execution_component = {
        "total_cost_dollars": round(total_cost, 2),
        "total_gross_dollars": round(total_gross, 2),
        "avg_slippage_c": round(sum(slippages) / len(slippages), 2) if slippages else 0,
        "pct_zero_slippage": round(sum(1 for s in slippages if s <= 0) / len(slippages), 2) if slippages else 0,
        "implied_fee_pct": round((total_cost - total_entry_value) / total_entry_value * 100, 2) if total_entry_value > 0 else None,
    }

    # ── 3. Exit method component ──
    # Compare P&L for sell fills vs hold-to-expiry
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

    # ── 4. Side selection component ──
    # Does the chosen side predict outcome?
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
        "model_edge": model_component,
        "execution_cost": execution_component,
        "exit_method": exit_component,
        "side_selection": side_component,
    }


def get_shadow_trade_analysis() -> dict:
    """
    Analyze shadow trades to measure the simulation-to-reality gap.

    Shadow trades are 1-contract buys placed during observe-only mode to
    collect real execution data. By comparing actual fill prices and outcomes
    to what the simulation engine would have predicted, we measure:

    1. Fill slippage: actual fill price vs ask at decision time
    2. Fill rate: how often the 1-contract order actually filled
    3. Outcome accuracy: does the simulation's predicted win/loss match reality?
    4. Empirical execution cost: total real cost vs simulation assumption

    This is the single most important validation metric — if shadow trades
    systematically underperform simulation predictions, the Observatory's
    strategy EVs are inflated and can't be trusted for live trading.
    """
    with get_conn() as c:
        rows = c.execute("""
            SELECT outcome, pnl, side, shares_filled,
                   entry_price_c, avg_fill_price_c,
                   shadow_decision_price_c, shadow_fill_latency_ms,
                   regime_label, spread_at_entry_c,
                   yes_ask_at_entry, no_ask_at_entry,
                   created_at
            FROM trades
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
        # The key metric: if simulation assumes 0¢ slippage but reality
        # shows +1.5¢ average, every Observatory EV is inflated by 1.5¢
        "sim_reality_gap_c": round(
            sum(slippages) / len(slippages), 2
        ) if slippages else None,
    }


def reconcile_shadow_trades() -> dict:
    """
    Compare every resolved shadow trade's actual outcome to what the
    simulation engine would have predicted for that exact market.

    For each shadow trade, finds the matching market observation and
    simulates a cheaper:early:{entry_price}:hold strategy (matching
    what shadow trading does). Compares simulated PnL to actual PnL.

    The cumulative gap is the execution integrity metric. If simulation
    says +2¢ average but reality shows -1¢, every Observatory EV is
    inflated by 3¢ and can't be trusted.

    Returns rolling reconciliation stats and per-trade comparisons.
    """
    with get_conn() as c:
        # Get resolved shadow trades with their tickers
        rows = c.execute("""
            SELECT t.id, t.ticker, t.side, t.outcome, t.pnl,
                   t.avg_fill_price_c, t.shadow_decision_price_c,
                   t.shadow_fill_latency_ms, t.shares_filled,
                   t.actual_cost, t.spread_at_entry_c
            FROM trades t
            WHERE COALESCE(t.is_shadow, 0) = 1
              AND t.outcome IN ('win', 'loss')
            ORDER BY t.created_at DESC
            LIMIT 200
        """).fetchall()
        shadow_trades = rows_to_list(rows)

        if not shadow_trades:
            return {"n": 0, "message": "No resolved shadow trades"}

        # Get matching observations for these tickers
        tickers = [t["ticker"] for t in shadow_trades if t.get("ticker")]
        if not tickers:
            return {"n": 0, "message": "No tickers found"}

        placeholders = ",".join("?" for _ in tickers)
        obs_rows = c.execute(f"""
            SELECT ticker, market_result, price_snapshots,
                   btc_price_at_open, realized_vol
            FROM market_observations
            WHERE ticker IN ({placeholders})
              AND market_result IS NOT NULL
              AND price_snapshots IS NOT NULL
        """, tickers).fetchall()
        obs_by_ticker = {r["ticker"]: dict(r) for r in obs_rows}

    # Import simulation function
    from strategy import _simulate_one, _brownian_p_yes
    import json as _json
    from config import KALSHI_FEE_RATE

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

        # Simulate: cheaper side, early entry, hold to expiry
        # Match shadow trade's actual entry price for entry_max
        entry_max = (t.get("shadow_decision_price_c") or
                     t.get("avg_fill_price_c") or 50)
        # Round up to nearest 5 to match strategy grid
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
        sim_pnl = sim["pnl_c"] / 100.0  # Convert cents to dollars for 1 contract

        sim_pnls.append(sim["pnl_c"])
        real_pnls.append(real_pnl * 100)  # Convert to cents for comparison

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

    # Same-outcome rate: did simulation predict the same win/loss?
    same_outcome = sum(1 for c in comparisons
                       if (c["sim_pnl_c"] > 0) == (c["real_pnl_c"] > 0))

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
        # The critical metric: should Observatory EVs be adjusted by this amount?
        "ev_adjustment_needed_c": round(-gap, 1) if abs(gap) > 1 else 0,
    }


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


def get_btc_surface_data(vol_bucket: str = None) -> list:
    """Get BTC probability surface for dashboard visualization.
    vol_bucket: 'calm', 'normal', 'volatile', or None for all (including 'all' bucket)."""
    with get_conn() as c:
        if vol_bucket:
            rows = c.execute("""
                SELECT * FROM btc_probability_surface
                WHERE total >= 5 AND vol_bucket = ?
                ORDER BY distance_bucket, time_bucket
            """, (vol_bucket,)).fetchall()
        else:
            rows = c.execute("""
                SELECT * FROM btc_probability_surface
                WHERE total >= 5
                ORDER BY vol_bucket, distance_bucket, time_bucket
            """).fetchall()
        return rows_to_list(rows)


def get_feature_importance() -> list:
    """Get feature importance rankings for dashboard."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT * FROM feature_importance
            ORDER BY ABS(importance) DESC
        """).fetchall()
        return rows_to_list(rows)


def insert_regime_stability(data: dict):
    """Insert a regime stability comparison record."""
    data.setdefault("captured_at", now_utc())
    with get_conn() as c:
        keys = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        c.execute(f"INSERT INTO regime_stability_log ({keys}) VALUES ({placeholders})",
                  list(data.values()))


def upsert_btc_surface_cell(distance_bucket: str, time_bucket: str,
                             total: int, yes_wins: int, no_wins: int,
                             yes_win_rate: float,
                             avg_yes_price: float = None,
                             avg_no_price: float = None,
                             vol_bucket: str = "all"):
    """Insert or update a BTC probability surface cell.
    vol_bucket: 'all' (global), 'calm', 'normal', 'volatile'."""
    with get_conn() as c:
        c.execute("""
            INSERT INTO btc_probability_surface
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


def upsert_feature_importance(feature_name: str, importance: float,
                               correlation: float, sample_size: int,
                               method: str = "logistic"):
    """Insert or update feature importance record."""
    with get_conn() as c:
        c.execute("""
            INSERT INTO feature_importance
                (feature_name, importance, correlation, sample_size, method, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(feature_name) DO UPDATE SET
                importance = excluded.importance, correlation = excluded.correlation,
                sample_size = excluded.sample_size, method = excluded.method,
                updated_at = excluded.updated_at
        """, (feature_name, round(importance, 6), round(correlation, 6),
              sample_size, method, now_utc()))


# ═══════════════════════════════════════════════════════════════
#  CONVERGENCE METRIC SNAPSHOTS
# ═══════════════════════════════════════════════════════════════

def insert_metric_snapshot(metrics: dict):
    """Store a timestamped snapshot of key metrics for convergence tracking."""
    import json as _json
    with get_conn() as c:
        c.execute(
            "INSERT INTO metric_snapshots (recorded_at, metrics) VALUES (?, ?)",
            (now_utc(), _json.dumps(metrics))
        )
        # Keep last 2 weeks of snapshots (~672 at 30min intervals)
        c.execute("""
            DELETE FROM metric_snapshots
            WHERE id NOT IN (
                SELECT id FROM metric_snapshots ORDER BY recorded_at DESC LIMIT 700
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
                SELECT recorded_at, metrics FROM metric_snapshots
                WHERE ABS(JULIANDAY(recorded_at) - JULIANDAY(?)) <= ?
                ORDER BY ABS(JULIANDAY(recorded_at) - JULIANDAY(?))
                LIMIT 1
            """, (target_time, drift_days, target_time)).fetchone()
        else:
            row = c.execute("""
                SELECT recorded_at, metrics FROM metric_snapshots
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
            "SELECT recorded_at, metrics FROM metric_snapshots ORDER BY recorded_at DESC LIMIT 1"
        ).fetchone()
    if row:
        return row["recorded_at"], _json.loads(row["metrics"])
    return None, None


# ═══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    init_db()
    print("Database ready.")