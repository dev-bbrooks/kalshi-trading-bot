## Working Directory
All source files live at `/opt/trading-platform/`. Always look here first before assuming files don't exist.
Plugin-specific code is under `plugins/btc_15m/`.

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
- `dashboard.py` (13,601) — Flask dashboard, surgical port of legacy. Owns terminal UI (Dev tab).
- `terminal.py` (777) — backend-only Flask+SocketIO service (port 8051). WebSocket handlers, REST API, PTY, Claude Code sessions. No HTML — dashboard.py serves the UI.

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

## Development Rules
- Always run `python3 -m py_compile` on every modified file before finishing
- Only deliver files that were actually changed
- Dashboard-only changes are preferred when bot.py changes aren't strictly necessary
- Do not restructure or rename for readability alone — this is a solo project
- After each meaningful code change (file edits, new endpoints, logic fixes — not simple config updates like editing CLAUDE.md), output a technical summary covering:
  - Which files were modified and why
  - What functions/blocks were added, changed, or removed
  - The before/after behavior for any logic that changed
  - Any assumptions made where the code was ambiguous
  Be specific and technical — include function names, variable names, and data flow. These summaries are pasted into a separate planning session to maintain continuity.

## Terminal Service (platform-terminal)
- terminal.py is a backend-only Flask+SocketIO service (port 8051, supervisor: platform-terminal). No HTML — dashboard.py owns the terminal UI (Dev tab).
- When editing terminal.py, always run `python3 -m py_compile /opt/trading-platform/terminal.py` before restarting
- Auto-restart detection covers all three services: platform-terminal, platform-dashboard, and plugin-btc-15m
- When your response mentions restarting any of these services, the terminal executes the restart automatically after delivering the response. Do NOT attempt to run supervisorctl yourself (claude-worker doesn't have permission). Just mention it needs to happen.
- Do NOT say you "can't restart" or "don't have permission" — the auto-restart system handles it
- After restart, the frontend auto-reconnects and reloads. No manual intervention needed.
- Do NOT include restart instructions as visible text in your response (e.g. "The service needs to be restarted with `supervisorctl restart platform-terminal`"). The user sees a system message when the restart happens automatically.
- IMPORTANT: You MUST still include the `supervisorctl restart <service>` command somewhere in your response text for the auto-restart detection to work. Put it on its own line — the terminal strips lines containing "supervisorctl restart" before displaying to the user. If you don't include it, the restart won't trigger.

## Prompt Enhancer
- All terminal prompts pass through an enhancer layer (fresh isolated Claude Code instance) that converts casual requests into detailed structured prompts. Conversational and iterative follow-ups pass through unchanged. Enhancer config in `_enhancer_config` dict. Toggle via `/terminal/api/enhancer` endpoint.

## Workflow: Plan Before Executing

When receiving a request (especially anything beyond a trivial one-line fix):

1. **Understand** — Restate what you think is being asked. If the request is vague or could be interpreted multiple ways, ask for clarification before proceeding.
2. **Assess** — Read the relevant files first. Identify which files need changes, what the risks are (data loss, breaking existing features, service disruption), and whether this touches the dashboard (protected surface).
3. **Plan** — Write out a numbered plan of what you'll do: which files, what changes, in what order. Include any compile checks, restarts, or migrations needed. Flag anything that seems risky or has tradeoffs.
4. **Confirm** — Present the plan and wait for approval before making changes. A short "yes", "go", "do it", or similar means proceed. If the user gives feedback, revise the plan.
5. **Execute** — Make the changes according to the plan. Compile-check every modified .py file. State which services need restarting (the terminal auto-restart system handles it).

### When to skip the plan:
- Explicit one-line fixes ("change X to Y in file Z")
- Requests that say "just do it" or "quick fix"
- Follow-up changes to something already planned and approved

### Style preferences:
- Be direct and technically precise — no filler
- Don't restructure code for clarity alone (single developer project)
- No emojis anywhere — use text or SVG icons
- Incremental changes preferred — don't rewrite files unnecessarily
- Always read the actual code before proposing changes — don't assume

## Development Context

### Current Phase
- Bot is in active data collection / shadow trading mode, collecting ~96 observations/day
- Need ~3,250 observations for FDR statistical significance at current EV levels
- One regime (trending_down_strong) has historically been strongest and is configured to trade while all others are observed
- Do NOT reset the database or modify observation/trade tables without explicit approval — data collection is the bottleneck

### Future Roadmap (in priority order)
1. Reach statistical significance on 15-minute BTC markets (current focus — weeks away)
2. BTC 1-hour directional markets (KXBTCD) — multi-strike, shares regime engine with 15m plugin
3. BTC 1-hour range markets (KXBTC) — different strategy type entirely (volatility/range containment)
4. Other crypto assets — new regime worker per asset
5. Stock markets — Kalshi's CFTC regulation sidesteps PDT rule
- Philosophy: prove the system on one market first, then replicate the playbook

### Key Lessons (from months of development)
- Simulations accelerate data collection, not replace it — sim and real trades always merge, never display separately
- YES/NO side matters — initially considered noise, became one of the most important performance differentiators. Don't prune dimensions prematurely.
- Normalizing labels hides problems — stripping modifiers was correctly rejected
- Late entries create bad data — never re-evaluate skipped markets mid-window
- Global fallbacks mislead — insufficient data returns "unknown", never falls back to global
- Confidence model was miscalibrated on small samples — don't trust per-regime recommendations until sufficient observations accumulate

### Brandon's Preferences
- Mobile-first, iOS PWA — test everything on phone-sized viewport
- No emojis — use SVG icons or text symbols
- No localStorage — iOS PWA doesn't reliably support it
- Direct, technically precise communication — explain what and why, skip filler
- Approves with short confirmations: "yes", "go", "do it", "looks good"
- Catches subtle issues through direct observation — don't dismiss his concerns
- Prioritize quality over speed or token efficiency ($200 Claude plan)
- Don't restructure code for clarity alone — single developer project
- External AI reviews are input, not authority — assess critically before acting

### Git Workflow
- Push to GitHub after significant changes: git add -A && git commit -m "description" && git push
- Repo: https://github.com/dev-bbrooks/15-min-btc-bot
- Brandon syncs project files in Claude.ai from this repo manually

### Dashboard is a Protected Surface
- Same HTML/CSS/JS structure as legacy — surgical changes only
- Don't rewrite UI sections, add new features incrementally
- CSS uses :root variables (--bg, --card, --border, --text, --green, --red, --yellow, --blue, --dim, --orange)
- Terminal styling must match dashboard styling (same CSS variables)

## About Brandon
- Solo developer building a Kalshi BTC trading platform from scratch
- Has been at this for months — deep domain knowledge, catches subtle issues AI often misses
- Values substance over ceremony — if something works, ship it
- Tests everything on his phone (iOS PWA) and notices things like 7px of button leak
- Gives detailed, well-thought-out specs when he wants something specific — follow them closely
- Sometimes explores ideas conversationally before committing to a direction

## Communication Style
- Match energy — short question gets short answer, detailed spec gets detailed work
- It's fine to be casual between tasks — not every message needs to be a technical briefing
- Don't over-explain unless asked — Brandon reads diffs and understands code
- When something is genuinely interesting or unexpected, say so — don't be a robot
- Between tasks, a normal human response is better than silence or a canned "How can I help?"
