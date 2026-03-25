# Kalshi BTC 15-Minute Binary Options Bot — Project Summary

Last updated: March 24, 2026

## Overview

Automated trading bot for Kalshi's 15-minute BTC binary option markets (KXBTC15M). Uses a **Strategy Observatory** system that records every market's price evolution, simulates strategy variants against historical data, and surfaces evidence-based recommendations. Includes a full mobile-first Flask dashboard (iOS PWA), per-regime granular filtering, comprehensive data tracking, and push notifications.

**Current phase**: Phase 1 — data collection via shadow trading. The bot records all markets via the Strategy Observatory and places 1-contract shadow trades for execution data. Five trading modes (Observe, Shadow, Hybrid, Auto, Manual) selected via a mode strip in the dashboard header.

## Server & Deployment

- **Server**: `root@24.199.99.19` (DigitalOcean droplet)
- **Bot directory**: `/opt/15-min-btc-bot/`
- **Database**: `/opt/15-min-btc-bot/botdata.db` (SQLite, WAL mode)
- **Domain**: `dash.btcbotapp.com` (HTTPS via nginx + Let's Encrypt)
- **Process manager**: Supervisor

### Supervisor Programs

| Program | What it runs |
|---|---|
| `kalshi-bot` | `bot.py` — trading engine + regime worker |
| `kalshi-dashboard` | `dashboard.py` — Flask web dashboard (port 8050, proxied via nginx) |

### Deploy Methods

**From computer:**
```
scp dashboard.py bot.py db.py config.py root@24.199.99.19:/opt/15-min-btc-bot/
ssh root@24.199.99.19 "supervisorctl restart kalshi-bot kalshi-dashboard"
```

**From phone (iOS):**
1. Download artifacts from Claude
2. Email .py files to the bot's auto-deploy email address (configured in .env)
3. Files auto-deploy to server within seconds, services restart automatically

**From dashboard:**
Settings → Deploy → Upload .py files → Upload & Restart

## Architecture

### Three-layer Strategy Observatory

1. **Observatory** (always running): Records every market's full Kalshi price path + BTC regime context + BTC distance-from-open + active strategy key + outcome. One row per 15-minute market in `market_observations`. All observations stored with quality tag (full/partial/short/few) — never silently dropped. Tracks health metrics (written/dropped counts, drop rate).
2. **Laboratory** (runs every 30 min via regime worker): Simulates all strategy variants against recorded observations using unified simulation engine with spread-based slippage, absolute timing, and configurable fee rate. Tests 4 side rules × 19 entry maxes × variable sell targets × 3 timings. Computes both unweighted and time-weighted (14-day half-life) metrics. Runs 5-fold rolling walk-forward validation. Applies Benjamini-Hochberg FDR correction with t-test p-values. Tests slippage sensitivity at +1¢/+2¢ for positive-EV strategies. Writes aggregated results to `strategy_results`. First batch after deploy cleans stale results from removed setup types.
3. **Advisor** (live trading): Surfaces best strategy per coarse regime, hour, or global setup. Hierarchical fallback: `coarse_regime → hour → global`. Uses time-weighted EV for ranking. Filters out fee-fragile strategies (those that go negative with configurable fee buffer). Min samples configurable (default 30).

### Strategy Dimensions

**Side rules** (4): cheaper, yes, no, model (BTC FV). The `favored` side rule was removed — it bought the expensive side of binary options and had -8.2¢ global EV.

**Setup types** (3): global, coarse_regime (~15 buckets), hour (24 buckets). Fine-grained regime labels (~63 buckets with 13% persistence) were removed from strategy evaluation — too sparse to reach significance. Fine labels are still recorded passively in observations for future research. Coarse labels (66% persistence) are the primary regime dimension.

### BTC Fair Value Model

Real-time fair value model using the empirical BTC Probability Surface. Given BTC's current distance from market-open price and time remaining, computes P(YES wins). That probability × 100 = fair YES price in cents. Compares against Kalshi ask after fees to compute edge per side. Three-tier fallback: vol-conditioned surface → global surface → analytical Brownian bridge estimate. Fed by `btc_probability_surface` table (rebuilt every 30 min). Used for model-side trading, edge-scaled bet sizing, and idle dashboard display.

### Trading Modes

Single `trading_mode` config value. Selected via mode strip in the dashboard header.

- **Observe** (`trading_mode: observe`): Records everything, places no trades. Pure data collection.
- **Shadow** (`trading_mode: shadow`): Observe + places 1-contract trades per market using Observatory's best strategy. 60-second fill timeout. Measures sim-to-reality gap. ~$48/day worst case.
- **Hybrid** (`trading_mode: hybrid`): Auto-strategy full trades when confident (min EV, min samples, fee buffer pass). Shadow 1-contract fallback when they don't.
- **Auto** (`trading_mode: auto`): Auto-strategy full trades when confident. Observe-only when not. No shadow fallback.
- **Manual** (`trading_mode: manual`): Uses strategy parameters set in dashboard (side, sell target, entry max, timing). Regime gates decide skip/trade.

Legacy booleans (`observe_only`, `shadow_trading`, `auto_strategy_enabled`) are derived automatically when `trading_mode` is saved — kept for backward compatibility.

## Files

### bot.py (~4870 lines)
The trading engine. Runs as a continuous loop.

**Key functions:**
- `load_config()`, `get_effective_bankroll_cents()` — config and bankroll management
- `get_trading_mode(cfg)` — derives trading mode from config with legacy boolean fallback
- `get_r1_bet_dollars()` — bet sizing: flat (fixed dollar), percent (% of bankroll), or edge_scaled (base bet × FV model edge tier multiplier)
- `check_regime_gate()` — decides skip/trade per regime based on risk level actions, per-regime overrides, and strategy-based risk
- `build_strategy_key()` — maps current bot settings to Observatory strategy key format (side:timing:entry_max:sell_target)
- `_build_trade_context()` — builds common context dict for ALL trade inserts. Strategy key always included.
- `_skip_wait_loop()` — consolidated skip-wait helper used by all 7 skip paths. All paths use `resolve_inline=True`.
- `_place_shadow_trade()` — places 1-contract execution data trades with 60-second fill timeout
- `run_trade()` — main trading function with 5 filter checkpoints + adaptive entry + trailing stop + early exit EV + dynamic sell. In hybrid mode, auto-strategy failures fall through to shadow trade.
- `poll_live_market()` — polls Kalshi for current prices, feeds Observatory, computes FV model edge
- `backfill_trade_market_results()` — corrects outcomes and triggers `recompute_all_stats()` when corrections are made
- `process_commands()` — reads command queue from DB (elif chain)

**Architecture notes:**
- Bot and dashboard communicate through SQLite `bot_state` table
- Commands flow through `bot_commands` table (dashboard enqueues, bot dequeues)
- All skip paths record full trade context identical to real trades
- Strategy key stored on every trade (auto or picker-configured) via `auto_strategy_key`
- Observatory fed during both idle polling and active price polling
- Regime snapshot freshness check: snapshots >10 minutes old treated as "unknown"
- Sell-fill fast path derives outcome from actual PnL (not hardcoded win)
- Observatory health metrics written to bot_state every 5 minutes (JSON auto-parsed)
- Adaptive entry: starts below ask when spread is wide, walks up on retries
- Trailing stop: locks in gains once price reaches configurable % of target progress
- Early exit EV: exits losing trades when holding is negative EV with <2 min left
- Dynamic sell: fair value model adjusts sell target mid-trade when FV shifts significantly
- BTC open price tracked per market for fair value model distance computation
- Edge-scaled sizing computes FV model edge for chosen side at entry time
- `is_ignored` hardcoded False for config path; stop-mid-trade and shadow still set it independently

### strategy.py (~3620 lines)
Strategy Observatory: data collection + simulation + evaluation + analysis + monitoring + BTC Fair Value Model.

**MarketObserver**: Accumulates price snapshots in memory during each market. Writes observations on market transition with quality tag. Tracks market_id, bot_action, trade_id, active_strategy_key, and BTC distance-from-open (`bd` field). Fed by both `poll_live_market()` and the price polling loop. Tracks health metrics (written, dropped_partial, dropped_short, dropped_few) with `get_health()` accessor.

**Simulation engine**: Single unified `_simulate_one()` function with spread-based slippage (uses actual bid/ask spread per snapshot, not flat 1c), configurable `fee_rate` parameter, and absolute timing (early=0s, mid=300s, late=600s). Four side rules: cheaper, yes, no, model (BTC fair value). Entry_max in 5c steps, sell_target as absolute cents or hold. Two-snapshot fill delay on both entry and exit for realistic execution modeling. Fee sensitivity analysis reuses the same function. Full recompute every 30 min.

**Strategy results**: Three setup types: global, coarse_regime (~15 buckets), hour (24 buckets). Computes both unweighted and time-weighted metrics (14-day half-life exponential decay). 5-fold rolling walk-forward validation (expanding training window, each observation in exactly one test fold). Quality-split EV tracking (full vs degraded observations). PnL standard deviation for t-test FDR. Breakeven fee rate estimation.

**Advisor**: `get_recommendation()` — hierarchical fallback: `coarse_regime → hour → global`. Uses time-weighted EV for ranking. Skips fee-fragile strategies (configurable buffer). OOS validation enforced. Reads `auto_strategy_min_samples` from config (default 30).

**BTC Fair Value Model** (`BtcFairValueModel`): Loads empirical probability surface from `btc_probability_surface` table. Vol-conditioned surfaces (calm/normal/volatile) with global fallback. Distance-weighted interpolation between neighboring cells. Analytical Brownian bridge fallback when surface data insufficient. `compute_edge()` compares model fair value to Kalshi prices after fees — returns recommended side and edge per side. 5-minute cache with auto-reload.

**BTC Probability Surface** (`compute_btc_probability_surface()`): Builds 4 empirical surfaces (all + 3 vol buckets) from observations. 8 distance buckets × 5 time buckets. Written every 30 min.

**FDR correction** (`_apply_fdr_correction()`): Benjamini-Hochberg procedure using one-sample t-test on PnL (H0: mean PnL ≤ 0). Agnostic to strategy payoff structure. 10% FDR threshold.

**Analysis**: `analyze_correlated_losses()` — identifies whether losses for a strategy cluster in specific conditions (vol_regime, trend_direction, hour_et, day_of_week). Flags "danger zones" where loss rate is >1.5x overall.

**Feature importance**: `compute_feature_importance()` — point-biserial correlation ranking of 21 observation features against market outcome. Runs hourly.

**Execution analytics**: `analyze_execution_quality()` — measures slippage vs target price, fill quality by spread bucket, exit method effectiveness (sell fill vs hold-to-expiry comparison).

### dashboard.py (~13560 lines)
Flask web dashboard. Single-page PWA. 5 tabs: **Trades · Regimes · Home · Stats · Settings** (Home centered). Mode selector strip in sticky header for switching between Observe/Shadow/Hybrid/Auto/Manual modes.

**Dashboard features:**
- **Home tab**: Status bar (5 states: Offline/Stopped/Trading/Buying/mode label), bankroll display, active trade card, live market monitor with BTC chart, trade summary cards
- **Trades tab**: Three-state filter system (tap=include, tap again=exclude, tap again=default) with 17 filter dimensions across 6 groups (Outcome, Side, Timing, Strategy, Exit, Meta). Enhanced stats card with avg PnL, wagered, ROI%, avg entry, shadows, observed, errors, best/worst. Trade cards show parsed strategy tags (CHEAPER/MODEL, EARLY/MID/LATE, SOLD/HOLD)
- **Regimes tab**: BTC candle chart, regime classification display, per-regime filter configuration, regime worker status
- **Stats tab**: Hub with summary cards (Win Rate, Total P&L, ROI, Profit Factor). Sub-pages: Performance, Regime Analysis, Shadow Trading. Backend APIs preserved for removed pages (Observatory, Models, Validation, Convergence)
- **Settings tab**: Trading mode, strategy parameters, risk & regime actions, per-regime overrides, automation (auto-strategy, deploy), execution settings, push notifications, services (start/stop/restart), security, reset/wipe tools, deploy
- **Bankroll modal**: Balance, In Trade, Kalshi Total, Lifetime P&L with W-L stats, bankroll history chart, P&L history chart, Kalshi link
- **Toast notifications**: Translucent design with backdrop blur, color-coded borders
- **Push notifications**: Per-type toggles, quiet hours, notification history log

**Removed features (March 24, 2026):**
- Cash out system (execute_cash_out, finalize, overlay, all command handlers)
- AI chat (tab, overlay, Anthropic API integration, report generation)
- Arcade (game.py integration, hidden trigger, tab)
- Sessions (session stats, reset/recover, session P&L in header/bankroll modal, client-side streak tracking)
- Money management (locked bankroll, auto-lock profits, profit goal, min/max bankroll, session target/loss limit, circuit breakers)
- Ignore mode toggle (hardcoded False in bot.py; stop-mid-trade and shadow still set is_ignored independently)
- Center play/stop button (toggleBot, confirmStop; use services card in Settings or mode strip)
- Stats sub-pages: Observatory, Models & Calibration, Validation & Execution, Data Convergence (backend APIs preserved)

### db.py (~3550 lines)
SQLite database layer. WAL mode. 20+ tables including `market_observations` (with `active_strategy_key`, `obs_quality`, `btc_distance_pct_at_close` columns), `strategy_results` (with `weighted_win_rate`, `weighted_ev_c`, `oos_ev_c`, `oos_win_rate`, `oos_sample_size`, `fdr_significant`, `fdr_q_value`, `pnl_std_c`, `breakeven_fee_rate` columns), `btc_probability_surface`, `feature_importance`, `regime_stability_log`. Confidence model tables (confidence_factors, confidence_calibration, edge_calibration) exist in schema but are no longer maintained.

### config.py (~190 lines)
Constants and DEFAULT_BOT_CONFIG. Key defaults: `trading_mode: "observe"`, `auto_strategy_min_samples: 30`, `adaptive_entry: false`, `trailing_stop_pct: 0`, `early_exit_ev: false`, `dynamic_sell_enabled: false`, `edge_tiers` for edge_scaled sizing, `deploy_cooldown_minutes: 0`. Legacy booleans (`observe_only`, `shadow_trading`, `auto_strategy_enabled`) kept for backward compatibility.

### regime.py (~910 lines)
Regime classification engine. BTC candles from Binance, volatility/trend/volume regimes, composite labels. Triggers Observatory simulation batch, BTC probability surface rebuild, feature importance computation, and regime stability tracking.

### kalshi.py (~460 lines)
Kalshi API client. RSA-PSS auth. Full normalization layer for Kalshi's _dollars/_fp API migration (markets, orders, and balance). `get_balance_cents()` handles both legacy integer and `balance_dollars` string fields with log warning on migration detection.

### replay.py (~300 lines)
Historical replay and regression testing framework. Replays recorded market observations through the bot's decision pipeline (regime gate, strategy selection, simulation) and compares against Observatory-optimal strategies. Produces per-regime breakdowns and efficiency metrics. Run as `python3 replay.py --days 7 --verbose`. Reads from DB only, never touches Kalshi API.

### push.py (~490 lines)
VAPID-based web push notifications. `send_push()` uses tri-state returns (`True`/`False`/`None`) — subscriptions only deleted on explicit `False` (confirmed dead), not on temporary failures. 12+ notification types with per-type toggles and quiet hours support.

## Trading Strategy

### Strategy Observatory (primary approach)
Records every market's price path and simulates strategies against historical data. Uses spread-based slippage for realistic fill modeling. Time-weighted results prioritize recent performance. 5-fold walk-forward validation catches overfitting. FDR correction controls false discovery rate across thousands of strategy variants. Fee resilience check filters fragile strategies. Best strategies surfaced per coarse regime, per hour, and globally.

### BTC Fair Value Model (primary edge signal)
Empirical probability surface models the core settlement mechanism: given BTC's distance from market-open at time T, what is P(YES)? Compares model fair value to Kalshi prices after fees. Powers the `model` side rule (best global performer at +2.2¢ weighted EV). Also drives edge-scaled bet sizing.

### Regime Classification
Composite label from: Volatility (1-5), Trend (-3 to +3), Volume (1-5). Modifiers: squeeze, thin market, trend acceleration/deceleration, post-spike settling. Fine-grained labels (~63 buckets) recorded passively in observations. Coarse labels (~15 buckets) used for strategy evaluation — much denser data and 66% persistence vs 13% for fine labels.

### Entry Controls (5 checkpoints)
1. Regime gate (risk levels + overrides + strategy risk)
2. Per-regime condition filters (vol range, blocked hours, blocked days)
3. Price polling + entry range (Observatory fed during polling)
4. Side filter (per-regime blocked sides) + spread filter (per-regime max spread)
5. Stability filter (per-regime max stability) + bankroll safety

### Bet Sizing
Three modes: flat (fixed dollar), percent (% of bankroll), edge_scaled (base bet × FV model edge tier multiplier). Edge-scaled computes the BtcFairValueModel edge for the selected side at entry time and scales the base bet using configurable tiers (e.g., edge <2% → 0.5×, 2-5% → 1×, 5-10% → 1.5×, >10% → 2×).

### Execution
- **Adaptive entry** (configurable): starts below ask when spread >= 4c, walks up on retries. Saves ~1-2c/contract on wide-spread markets.
- **Trailing stop** (configurable): activates once price reaches X% of target progress. Locks in gains at HWM minus buffer.
- **Early exit EV** (configurable): when <2 min left and bid below entry, compares selling now vs expected value of holding with time-decay haircut.
- **Dynamic sell** (configurable): fair value model adjusts sell target mid-trade when model FV shifts significantly from initial target. Configurable minimum move floor (default 3¢).
- **Shadow trades**: 1-contract trades placed in shadow/hybrid mode with 60-second fill timeout. Collect real execution data for sim-vs-reality validation.

## Safeguards

1. **Regime snapshot freshness**: Snapshots >10 minutes old force regime to "unknown", preventing stale data from influencing gating decisions.
2. **Inline result resolution**: All 7 skip paths resolve market results inline, ensuring complete data during collection phase.
3. **Backfill stat correction**: When `backfill_trade_market_results` corrects a misclassified outcome, it triggers `recompute_all_stats()`.
4. **Balance API migration safety**: `get_balance_cents()` falls back to `balance_dollars` field if Kalshi removes legacy integer.
5. **Observatory health tracking**: Counters for written/dropped observations with drop rate, stored in bot_state.
6. **Fee resilience**: Advisor automatically skips strategies that don't survive configurable fee buffer (default current + 3%).
7. **Walk-forward validation**: 5-fold rolling validation with expanding training window. Each observation in exactly one test fold.
8. **FDR correction**: Benjamini-Hochberg procedure controls false discovery rate at 10% across all strategy variants.
9. **Deploy cooldown**: Configurable delay before auto-resuming trading after a code push / restart.
10. **Balance anomaly detection**: Flags unexpected large balance drops (>50% and >$50) with push notification.
11. **Observation quality tracking**: ALL observations stored with quality tag (full/partial/short/few). Simulation filters by quality.
12. **Slippage sensitivity**: Positive-EV strategies re-tested at +1¢ and +2¢ additional slippage. Fragile strategies flagged.
13. **Orphan trade recovery**: On restart, detects active trades from previous run. Monitors to completion or cancels.
14. **Consecutive error circuit breaker**: 5 consecutive unhandled errors auto-stops trading.

## Known Quirks & Decisions

1. **Regime risk "terrible"** — internal DB value stays `terrible`, display shows "EXTREME"
2. **Bot state is single-row** — JSON columns, every update writes whole row
3. **Command queue pattern** — dashboard never directly mutates bot state for trading
4. **`_build_trade_context()`** — centralizes all trade fields. Strategy key always included.
5. **`_skip_wait_loop()`** — consolidated skip-wait helper for all 7 skip paths, all with `resolve_inline=True`
6. **iOS PWA** — no localStorage. All state in JS memory or server-side.
7. **Observatory quality tags** — ALL observations written (never silently dropped). Quality: full (≥80 snapshots), short (<80), partial (joined mid-market), few (<3). Simulation uses full+short by default.
8. **Simulation absolute timing** — entry timing uses fixed seconds (0/300/600) not relative to observed duration.
9. **Spread-based slippage** — simulation uses actual bid/ask spread at entry snapshot (max(1, spread//2)) instead of flat 1c.
10. **New features default off** — adaptive_entry, trailing_stop_pct, early_exit_ev, dynamic_sell_enabled all default to disabled. Enable individually when ready.
11. **Trading mode is single source of truth** — `trading_mode` config drives behavior. Legacy booleans (`observe_only`, `shadow_trading`, `auto_strategy_enabled`) are derived when mode is saved and read as fallback by `get_trading_mode()`. Never set the legacy booleans directly.
12. **Observatory discard on shutdown** — incomplete market data intentionally discarded on bot stop/restart to prevent partial price paths from distorting simulation results.
13. **Model side fallback** — when `strategy_side=model` but Observatory has no data for model-side strategies, risk assessment falls back to the cheaper-side variant as closest proxy.
14. **Confidence model removed** — The Bayesian multi-factor confidence model and its edge calculator were removed (structurally miscalibrated, 22pp error). Trade columns (`predicted_win_pct`, `confidence_level`, `predicted_edge_pct`) remain in schema but are no longer written. Confidence/calibration tables exist but are orphaned.
15. **Fine regime labels are passive data** — Fine-grained regime labels (~63 labels) are recorded in observations but not used for strategy evaluation. Coarse labels (~15 buckets) are the active dimension. Fine labels can be reactivated for analysis later if needed.
16. **Stale results cleanup** — First simulation batch after deploy deletes strategy_results for removed setup types (regime, regime_hour) and removed side rule (favored).
17. **No sessions** — the session concept was removed entirely. All stats are lifetime. Time-based filtering may be added later at the platform level.
18. **No money management** — locked bankroll, auto-lock, profit goals, circuit breakers, and session limits were removed. Simplifies multi-market future. Bot modes (observe/shadow) serve as the primary risk control.
19. **Removed DB columns kept in schema** — `is_ignored`, `cashed_out`, session fields, and confidence columns remain in database schema but are no longer written by removed features. `is_ignored` is still actively set by stop-mid-trade and shadow trade paths.
