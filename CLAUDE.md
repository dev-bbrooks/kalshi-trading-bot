# Kalshi BTC Trading Platform

## Project Location & Structure
- Platform code: `/opt/trading-platform/`
- Database: `platform.db` (SQLite, WAL mode) — same directory
- Dashboard: `dash.btcbotapp.com` (Flask on port 8050, nginx reverse proxy)
- Process manager: supervisor (`plugin-btc-15m` = engine.py btc_15m, `platform-dashboard` = dashboard.py)
- Legacy reference: `/opt/15-min-btc-bot/legacy/`

## Files (~25,000 lines total)

### Platform layer
- `config.py` (65) — constants, paths, credentials. No DEFAULT_BOT_CONFIG.
- `db.py` (821) — platform schema + 40 shared query functions
- `engine.py` (111) — CLI plugin launcher: `python3 engine.py btc_15m`
- `plugin_base.py` (76) — MarketPlugin ABC
- `kalshi.py` (401) — pure API client, RSA-PSS auth, dollar/FP normalization
- `push.py` (125) — VAPID push infrastructure only (no notify_* functions)
- `regime.py` (887) — asset-parameterized regime engine
- `dashboard.py` (13,601) — Flask dashboard, surgical port of legacy

### Plugin layer (plugins/btc_15m/)
- `plugin.py` (160) — Btc15mPlugin subclass, owns default config
- `bot.py` (2,968) — trading engine, market discovery, regime gating, execution
- `strategy.py` (3,039) — MarketObserver + simulation + FV model + analysis
- `market_db.py` (2,443) — plugin-specific tables (btc15m_*) + 56 query functions
- `notifications.py` (296) — push notification formatters

## Current Phase
Fresh deployment — data collection restart. No legacy data. Bot records all markets via Strategy Observatory.

## Architecture
- Platform is market-agnostic. Plugins are market-specific.
- Bot and dashboard communicate through `plugin_state` table (JSON state)
- Dashboard uses `get_bot_state()` wrapper to flatten plugin_state to legacy dict shape
- Commands flow through `bot_commands` table with plugin_id
- Config is namespaced: `btc_15m.trading_mode`, `btc_15m.bet_size`, etc.
- Regime engine is asset-parameterized, shared across plugins for same asset
- All plugin tables use `btc15m_` prefix

## Critical Rules — DO NOT VIOLATE
1. **Never normalize regime labels** — stripping modifiers (_accel/_decel/thin_/squeeze_) reduces filter flexibility and corrupts data
2. **Regime modifiers are labels, not overrides** — squeeze/thin are prefixes on composite labels
3. **YES/NO side matters** — one of the most important performance differentiators
4. **Simulations and real trades merge** — never display separately
5. **Strategy space must match simulation exactly** — live execution and Observatory use identical assumptions
6. **Late entries create bad data** — never re-evaluate skipped markets mid-window
7. **Global fallbacks mislead** — insufficient regime data returns "unknown", no global fallback
8. **Stale state after crashes must be explicitly cleared** — active_shadow/active_skip reset on startup
9. **Observatory discards incomplete data on shutdown** — prevents partial price paths
10. **No restructuring for clarity alone** — single developer project
11. **Fine regime labels are passive data** — coarse labels (~15 buckets) are the active dimension
12. **Trading mode is single source of truth** — `btc_15m.trading_mode` config drives behavior
13. **No sessions, no money management** — removed entirely. Bot modes are the risk control.
14. **Dashboard is a protected surface** — same HTML/CSS/JS as legacy. Only backend wiring changed. Do not rewrite UI.
15. **Config keys are namespaced** — plugin keys use `btc_15m.` prefix. Platform keys are unprefixed.

## Deploy Rules
- Always `python3 -m py_compile` on every modified file before deploying
- Dashboard-only deploys: `supervisorctl restart platform-dashboard`
- Full deploy: `supervisorctl restart plugin-btc-15m platform-dashboard` — do at start of market round
- Observatory discards in-progress market data on restart — by design

## Strategy Key Format
`side:timing:entry_max:sell_target` — e.g. `cheaper:early:45:90`
- Side rules (4): cheaper, yes, no, model
- Timings (3): early (0s), mid (300s), late (600s)
- Entry max: 5c steps
- Sell target: absolute cents or "hold"

## Key Patterns
- `_build_trade_context()` centralizes ALL trade fields — strategy key always included
- `_skip_wait_loop()` is the consolidated skip-wait helper for all skip paths
- Command queue pattern: dashboard enqueues to `bot_commands`, bot dequeues
- Plugin state: JSON columns in `plugin_state` table, merged on update
- iOS PWA: no localStorage anywhere — all state in JS memory or server-side
- Observatory quality tags: full (≥80 snapshots), short (<80), partial (joined mid), few (<3)
- Tri-state push returns: True=sent, False=dead subscription (remove), None=temporary failure (keep)
