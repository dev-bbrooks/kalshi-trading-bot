# Kalshi BTC 15-Minute Binary Options Bot

## Project Location & Structure
- All code lives in `/opt/15-min-btc-bot/`
- Database: `botdata.db` (SQLite, WAL mode) — same directory
- Dashboard: `dash.btcbotapp.com` (Flask on port 8050, nginx reverse proxy)
- Process manager: supervisor (`kalshi-bot` = bot.py, `kalshi-dashboard` = dashboard.py)

## Files (~20,000 lines total)
- `bot.py` (~4870 lines) — trading engine, continuous loop
- `strategy.py` (~3620 lines) — Observatory, simulation, FV model, analysis
- `dashboard.py` (~9500 lines) — Flask dashboard, all HTML/CSS/JS inline
- `db.py` (~1550 lines) — SQLite layer, 20+ tables
- `regime.py` (~910 lines) — BTC regime classification (Binance candles)
- `kalshi.py` (~460 lines) — Kalshi API client, RSA-PSS auth
- `config.py` (~190 lines) — constants and DEFAULT_BOT_CONFIG
- `push.py` (~490 lines) — VAPID web push notifications
- `replay.py` (~300 lines) — historical replay/regression testing
- `game.py` — prediction game feature

## Current Phase
Data collection via shadow trading. ~820+ observations logged. Bot records every market via Strategy Observatory and places 1-contract shadow trades for execution data. NOT yet trading real money at scale.

## Architecture: Three-Layer Strategy Observatory
1. **Observatory**: Records every market's full price path + regime + BTC distance + strategy key + outcome
2. **Laboratory**: Simulates 627 strategy variants against observations every 30 min. Spread-based slippage, absolute timing, walk-forward validation, FDR correction
3. **Advisor**: Surfaces best strategy per coarse regime / hour / global

## Working Style
- When the architecture doc or prompt is ambiguous, or you're choosing between multiple reasonable approaches, or you're about to make an assumption that could affect the design — stop and ask Brandon before proceeding. Don't guess. A quick question saves hours of rework. However, don't ask about things that are clearly specified or that are routine implementation details you can handle yourself. Use good judgment: ask when it matters, execute when it's clear.
- At the end of each phase or major milestone, summarize what was built, flag anything that felt unclear or where you made a judgment call, and ask if there's anything to adjust before moving on.

## Critical Rules — DO NOT VIOLATE
1. **Never normalize regime labels** — stripping modifiers (_accel/_decel/thin_/squeeze_) was tried and rejected. It reduces filter flexibility and corrupts data integrity
2. **Regime modifiers are labels, not overrides** — squeeze/thin are prefixes on composite labels (e.g. `squeeze_trending_down_strong`), not hard gates
3. **YES/NO side matters** — initially considered noise, turned out to be one of the most important performance differentiators. Never prune this dimension
4. **Simulations and real trades merge** — never display separately. Real trades are higher-fidelity sims
5. **Strategy space must match simulation exactly** — live execution and Observatory use identical assumptions
6. **Late entries create bad data** — never re-evaluate skipped markets mid-window if regime shifts
7. **Global fallbacks mislead** — if regime samples are insufficient, return "unknown", don't fall back to global stats
8. **Stale state after crashes must be explicitly cleared** — active_shadow/active_skip reset on startup
9. **Observatory discards incomplete data on shutdown** — intentional, prevents partial price paths from distorting sims
10. **No restructuring for clarity alone** — single developer project, readability refactors are not prioritized
11. **Fine regime labels are passive data** — ~63 fine labels recorded but NOT used for strategy eval. Coarse labels (~15 buckets) are the active dimension
12. **Confidence model was removed** — was structurally miscalibrated (22pp error). DB columns remain in schema but aren't written
13. **No sessions, no money management** — removed entirely. All stats are lifetime. Bot modes are the risk control
14. **Trading mode is single source of truth** — `trading_mode` config drives behavior. Legacy booleans derived automatically, never set directly

## Deploy Rules
- Always `python3 -m py_compile` on every modified file before deploying
- Dashboard-only deploys: `supervisorctl restart kalshi-dashboard` (preserves bot observations)
- Full deploy: `supervisorctl restart kalshi-bot kalshi-dashboard` — do at start of market round to minimize lost observations
- Observatory discards in-progress market data on restart — this is by design

## Strategy Key Format
`side:timing:entry_max:sell_target` — e.g. `cheaper:early:45:90`
- Side rules (4): cheaper, yes, no, model
- Timings (3): early (0s), mid (300s), late (600s)
- Entry max: 5c steps
- Sell target: absolute cents or "hold"
- The `favored` side rule was removed — it bought expensive side, had -8.2¢ EV

## BTC Fair Value Model
Empirical probability surface: given BTC distance from market-open at time T, compute P(YES). Three-tier fallback: vol-conditioned → global → analytical Brownian bridge. Powers model-side trading and edge-scaled sizing.

## Key Patterns
- `_build_trade_context()` centralizes ALL trade fields — strategy key always included
- `_skip_wait_loop()` is the consolidated skip-wait helper for all 7 skip paths
- Command queue pattern: dashboard enqueues to `bot_commands` table, bot dequeues
- Bot state is single-row JSON columns in `bot_state` table
- iOS PWA: no localStorage anywhere — all state in JS memory or server-side
- Observatory quality tags: full (≥80 snapshots), short (<80), partial (joined mid), few (<3)
- Tri-state push returns: True=sent, False=dead subscription (remove), None=temporary failure (keep)

## Upcoming Work
Platform refactor: separate market-agnostic platform (Kalshi client, regime, DB, dashboard, push, deploy) from market-specific plugin layer (observer, simulator, FV model, timing, tickers). 15m bot = first plugin. Platform must be fully market-ignorant. Each step deployable without breaking live 15m data collection.

## Tech Stack
- Python 3, Flask, SQLite (WAL), supervisor, nginx, Let's Encrypt
- Kalshi API (CFTC-regulated), Binance (BTC candle data)
- VAPID web push, iOS PWA at dash.btcbotapp.com
