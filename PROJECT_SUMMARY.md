# Kalshi BTC Trading Platform — Project Summary

Last updated: March 25, 2026

## Overview

Multi-market trading platform for Kalshi binary option markets. Uses a **Strategy Observatory** system that records every market's price evolution, simulates strategy variants against historical data, and surfaces evidence-based recommendations. Includes a full mobile-first Flask dashboard (iOS PWA), per-regime granular filtering, comprehensive data tracking, and push notifications.

**Architecture**: Platform/plugin split. Market-agnostic platform handles infrastructure (API client, regime engine, database, dashboard shell, push delivery). Market-specific plugins handle trading logic, strategy, and market discovery. BTC 15-minute binary options (`KXBTC15M`) is the first plugin.

**Current phase**: Fresh deployment — data collection restart. The bot records all markets via the Strategy Observatory and can place 1-contract shadow trades for execution data. Five trading modes (Observe, Shadow, Hybrid, Auto, Manual) selected via a mode strip in the dashboard header.

## Server & Deployment

- **Server**: `root@24.199.99.19` (DigitalOcean droplet)
- **Platform directory**: `/opt/trading-platform/`
- **Database**: `/opt/trading-platform/platform.db` (SQLite, WAL mode)
- **Domain**: `dash.btcbotapp.com` (HTTPS via nginx + Let's Encrypt)
- **Process manager**: Supervisor
- **Legacy code**: `/opt/15-min-btc-bot/legacy/` (preserved for reference)

### Supervisor Programs

| Program | What it runs |
|---|---|
| `plugin-btc-15m` | `python3 engine.py btc_15m` — regime worker + trading engine |
| `platform-dashboard` | `python3 dashboard.py` — Flask web dashboard (port 8050, proxied via nginx) |

### Deploy Methods

**From computer:**
```
scp dashboard.py bot.py db.py config.py root@24.199.99.19:/opt/trading-platform/
ssh root@24.199.99.19 "supervisorctl restart plugin-btc-15m platform-dashboard"
```

**From dashboard:**
Settings → Deploy → Upload .py files → Upload & Restart

## Architecture

### Platform Layer (market-agnostic)

| File | Lines | Role |
|------|-------|------|
| `config.py` | 65 | Constants, paths, credentials. No DEFAULT_BOT_CONFIG. |
| `db.py` | 821 | Connection manager, platform tables (bot_config, plugin_state, bot_commands, log_entries, push/bankroll/audit), asset tables (candles, baselines, regime_snapshots, regime_stability_log, regime_heartbeat). 40 query functions. |
| `engine.py` | 111 | Universal plugin launcher: `python3 engine.py <plugin_id>`. Loads plugin, inits DB, starts regime worker thread, runs plugin. |
| `plugin_base.py` | 76 | MarketPlugin ABC — properties (plugin_id, display_name, asset, asset_source), methods (init_db, run, get_default_config, register_routes), 7 dashboard render methods. |
| `kalshi.py` | 401 | Pure API client. RSA-PSS auth. Dollar/FP normalization. No market discovery, no DB access. |
| `push.py` | 125 | VAPID push infrastructure. send_push() with tri-state returns, send_to_all(). No notify_* functions. |
| `regime.py` | 887 | Asset-parameterized regime engine. Every function takes `asset` param. Candle fetch from Binance, indicator calculations, classification, composite labels, regime worker with DB heartbeat coordination. |
| `dashboard.py` | 13,601 | Flask dashboard. Surgical port of legacy — same HTML/CSS/JS, backend wiring updated. get_bot_state() wrapper translates plugin_state to legacy flat dict shape. |

### Plugin Layer (`plugins/btc_15m/`)

| File | Lines | Role |
|------|-------|------|
| `plugin.py` | 160 | Btc15mPlugin(MarketPlugin) subclass. Owns default config (39 keys), delegates to bot/dashboard modules. |
| `bot.py` | 2,968 | Trading engine. Market discovery, regime gating, trade execution, Observatory integration, command processing. |
| `strategy.py` | 3,039 | MarketObserver + full simulation engine + BTC Fair Value Model + recommendations + analysis. |
| `market_db.py` | 2,443 | Plugin-specific tables (btc15m_* prefix, 13 tables) + 56 query functions. |
| `notifications.py` | 296 | All notify_* push notification formatters using platform send_to_all(). |

### Three-Layer Strategy Observatory

1. **Observatory** (always running): Records every market's full Kalshi price path + BTC regime context + BTC distance-from-open + active strategy key + outcome. One row per 15-minute market in `btc15m_observations`. All observations stored with quality tag (full/partial/short/few) — never silently dropped.
2. **Laboratory** (runs every 30 min via regime worker): Simulates all strategy variants against recorded observations. Spread-based slippage, absolute timing, configurable fee rate. 4 side rules × 19 entry maxes × variable sell targets × 3 timings. Time-weighted metrics (14-day half-life). 5-fold walk-forward validation. Benjamini-Hochberg FDR correction. Slippage sensitivity at +1¢/+2¢. Writes to `btc15m_strategy_results`.
3. **Advisor** (live trading): Surfaces best strategy per coarse regime, hour, or global setup. Hierarchical fallback. Time-weighted EV ranking. Fee resilience filtering.

### Process Model

- One supervisor process per plugin: `python3 engine.py btc_15m`
- One shared dashboard process: `python3 dashboard.py`
- `engine.py` loads plugin, starts regime worker thread, calls `plugin.run()`
- Regime worker uses DB heartbeat for cross-process coordination
- Regime worker triggers plugin sim batches via command queue
- Plugin owns its entire trading loop

### Database Schema

**Platform tables**: bot_config (namespaced key-value, e.g. `btc_15m.trading_mode`), plugin_state (one row per plugin, JSON state), bot_commands (with plugin_id), log_entries (with source), push_subscriptions, push_log, bankroll_snapshots (with plugin_id), audit_log.

**Asset tables** (all with `asset` column): candles, baselines, regime_snapshots, regime_stability_log, regime_heartbeat.

**Plugin tables** (btc15m_* prefix): markets, trades (~50 columns), observations, strategy_results, price_path, live_prices, probability_surface, feature_importance, regime_stats, hourly_stats, regime_opportunities, exit_simulations, metric_snapshots.

### Communication Patterns

- **Bot ↔ Dashboard**: Through `plugin_state` table. Bot writes state via `update_plugin_state()`. Dashboard reads via `get_bot_state()` wrapper that flattens plugin_state to legacy dict shape.
- **Dashboard → Bot**: Commands flow through `bot_commands` table (dashboard enqueues with plugin_id, bot dequeues).
- **Regime → Plugin**: Regime worker triggers sim batches via command queue: `enqueue_command(plugin_id, "run_sim_batch")`.
- **Config**: Namespaced in `bot_config` table. Plugin keys prefixed: `btc_15m.trading_mode`, `btc_15m.bet_size`, etc.

## Strategy Dimensions

**Side rules** (4): cheaper, yes, no, model (BTC FV).

**Setup types** (3): global, coarse_regime (~15 buckets), hour (24 buckets). Fine-grained regime labels (~63 buckets) recorded passively in observations. Coarse labels are the primary regime dimension.

### BTC Fair Value Model

Real-time fair value model using the empirical BTC Probability Surface. Given BTC's current distance from market-open price and time remaining, computes P(YES wins). Three-tier fallback: vol-conditioned surface → global surface → analytical Brownian bridge. Powers model-side trading, edge-scaled bet sizing, dynamic sell adjustments, and dashboard display.

### Trading Modes

Single `trading_mode` config value. Selected via mode strip in the dashboard header.

- **Observe**: Records everything, places no trades. Pure data collection.
- **Shadow**: Observe + places 1-contract trades per market using Observatory's best strategy. 60-second fill timeout.
- **Hybrid**: Auto-strategy full trades when confident. Shadow 1-contract fallback when not.
- **Auto**: Auto-strategy full trades when confident. Observe-only when not.
- **Manual**: Uses strategy parameters set in dashboard. Regime gates decide skip/trade.

## Dashboard

Flask web dashboard. Single-page PWA. 5 tabs: **Trades · Regimes · Home · Stats · Settings** (Home centered). Mode selector strip in sticky header.

**Features:**
- **Home tab**: Status bar, bankroll display, active trade card, live market monitor with BTC chart, observatory health
- **Trades tab**: Three-state filter system (include/exclude/default) with 17 filter dimensions. Trade cards with parsed strategy tags.
- **Regimes tab**: BTC candle chart, regime classification, per-regime filter configuration, regime worker status
- **Stats tab**: Hub with summary cards. Sub-pages: Performance, Regime Analysis, Shadow Trading.
- **Settings tab**: Trading mode, strategy parameters, risk & regime actions, per-regime overrides, execution settings, push notifications, services, security, reset/wipe, deploy
- **Bankroll modal**: Balance, In Trade, Lifetime P&L, bankroll history chart, P&L history chart
- **Push notifications**: Per-type toggles, quiet hours, notification history log

## Entry Controls (5 checkpoints)

1. Regime gate (risk levels + overrides + strategy risk)
2. Per-regime condition filters (vol range, blocked hours, blocked days)
3. Price polling + entry range (Observatory fed during polling)
4. Side filter (per-regime blocked sides) + spread filter (per-regime max spread)
5. Stability filter + bankroll safety (insufficient bankroll → skip + observe, not stop)

## Execution Features

- **Adaptive entry** (configurable): starts below ask when spread >= 4c, walks up on retries. Side-switching when original side leaves range. Wait-repoll when both sides out of range.
- **Trailing stop** (configurable): activates once price reaches X% of target progress.
- **Early exit EV** (configurable): exits when <2 min left and holding is negative EV.
- **Dynamic sell** (configurable): FV model adjusts sell target mid-trade. Configurable minimum move floor (default 3¢).
- **Shadow trades**: 1-contract trades with 60-second fill timeout. Collect real execution data.

## Safeguards

1. **Regime snapshot freshness**: Snapshots >10 minutes old force regime to "unknown".
2. **Inline result resolution**: All skip paths resolve market results inline.
3. **Backfill correction**: `_backfill_trade_market_results` corrects misclassified outcomes and triggers recompute.
4. **Balance API migration safety**: `get_balance_cents()` falls back to `balance_dollars` field.
5. **Observatory health tracking**: Counters for written/dropped observations.
6. **Fee resilience**: Advisor skips strategies that don't survive configurable fee buffer.
7. **Walk-forward validation**: 5-fold rolling with expanding training window.
8. **FDR correction**: Benjamini-Hochberg at 10% across all strategy variants.
9. **Balance anomaly detection**: Flags unexpected large balance drops.
10. **Observation quality tracking**: ALL observations stored with quality tag. Simulation filters by quality.
11. **Slippage sensitivity**: Positive-EV strategies re-tested at +1¢ and +2¢.
12. **Orphan trade recovery**: On restart, resolves trades left in "open" outcome.
13. **Consecutive error circuit breaker**: 5 consecutive unhandled errors auto-stops trading.
14. **Bankroll safety as skip**: Insufficient bankroll records a skip observation and continues, doesn't stop the bot.

## Known Quirks & Decisions

1. **Regime risk "terrible"** — internal DB value stays `terrible`, display shows "EXTREME"
2. **Plugin state is JSON** — `plugin_state.state_json` column, merged on update (not replaced)
3. **get_bot_state() wrapper** — translates plugin_state to legacy flat dict for dashboard JS compatibility
4. **Command queue pattern** — dashboard never directly mutates bot state for trading
5. **`_build_trade_context()`** — centralizes all trade fields. Strategy key always included.
6. **`_skip_wait_loop()`** — consolidated skip-wait helper for all skip paths, all with `resolve_inline=True`
7. **iOS PWA** — no localStorage. All state in JS memory or server-side.
8. **Observatory quality tags** — ALL observations written (never silently dropped). Quality: full (≥80 snapshots), short (<80), partial (joined mid-market), few (<3).
9. **Simulation absolute timing** — entry timing uses fixed seconds (0/300/600) not relative to observed duration.
10. **New features default off** — adaptive_entry, trailing_stop_pct, early_exit_ev, dynamic_sell_enabled all default to disabled.
11. **Trading mode is single source of truth** — `btc_15m.trading_mode` config drives behavior. Legacy booleans derived automatically.
12. **Observatory discard on shutdown** — incomplete market data discarded on bot stop/restart to prevent partial price paths.
13. **No sessions** — all stats are lifetime. Time-based filtering may be added later.
14. **No money management** — locked bankroll, auto-lock, profit goals, circuit breakers removed. Bot modes are the risk control.
15. **Config namespacing** — all plugin config keys prefixed: `btc_15m.trading_mode`, `btc_15m.bet_size`, etc. Platform keys unprefixed.
16. **Asset-parameterized regime** — regime engine takes `asset` param everywhere. Multiple plugins sharing same asset share one regime worker.
17. **Dashboard preserved** — surgical migration from legacy. Same HTML/CSS/JS. Only backend wiring (imports, table names, config keys, service names) changed.

## Future Expansion

The platform architecture supports adding new market plugins:
- **BTC 1-hour directional** (KXBTCD): Multi-strike binary options. Would share BTC regime engine with 15m plugin.
- **BTC 1-hour range** (KXBTC): Volatility/range containment. Different strategy type entirely.
- **Other crypto assets**: New regime worker per asset, same classification logic.
- **Stock markets**: Kalshi's CFTC regulation sidesteps PDT rule.

Each new plugin: create `plugins/<id>/` directory, implement MarketPlugin interface, register with engine. Dashboard composition (multi-plugin tabs) is a future enhancement — with one plugin, dashboard looks identical to legacy.

## Tech Stack

- Python 3, Flask, SQLite (WAL), supervisor, nginx, Let's Encrypt
- Kalshi API (CFTC-regulated), Binance (BTC candle data, public API)
- VAPID web push, iOS PWA at dash.btcbotapp.com
- GitHub: https://github.com/dev-bbrooks/15-min-btc-bot
