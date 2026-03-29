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
- `dashboard.py` (13,425) — Flask dashboard, surgical port of legacy. Owns terminal UI (Dev tab).
- `terminal.py` (777) — backend-only Flask+SocketIO service (port 8051). WebSocket handlers, REST API, PTY, Claude Code sessions. No HTML — dashboard.py serves the UI.
- `static/terminal.js` (1,374) — terminal JavaScript (PanelScroller, Claude Code session, shell, design widgets, sidebar nav)
- `static/terminal.css` (355) — terminal CSS (panel headers/footers, chat, shell, dev sidebar, design widgets)

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

## Terminal Code Separation

Terminal UI code lives in two dedicated files:
- `static/terminal.js` — all terminal JavaScript (widget renderer, controls, shell, Claude Code session, hub navigation, etc.)
- `static/terminal.css` — all terminal CSS
- `dashboard.py` includes these via `<script>` and `<link>` tags

**Rules for terminal code:**
- ALL new terminal JS goes in `static/terminal.js`, never inline in dashboard.py
- ALL new terminal CSS goes in `static/terminal.css`, never inline in dashboard.py
- If a task requires editing terminal JS or CSS that is still in dashboard.py (legacy remnants from before the separation), move that specific code to the appropriate file as part of the edit
- Do not go looking for code to migrate — only move code that you're already editing for the current task

## Prompt Enhancer
- All terminal prompts pass through an enhancer layer (fresh isolated Claude Code instance) that converts casual requests into detailed structured prompts. Conversational and iterative follow-ups pass through unchanged. Enhancer config in `_enhancer_config` dict. Toggle via `/terminal/api/enhancer` endpoint.

## Design Widget

When the user asks to see, adjust, or tweak styling/visual properties of a UI element, respond with a design widget instead of text. This includes requests like:
- "show me the header styles"
- "let me adjust the card colors"
- "what can I change on the sidebar"
- "let me choose the background color"

Do NOT emit a widget for functional/behavioral requests like "fix the header bug" or "add a new button." If you can't find the element or are unsure what the user is referring to, respond with normal text and ask for clarification.

### How to investigate

1. Identify which file(s) contain the element's styles (usually dashboard.py for UI).
2. Read the relevant code section.
3. Identify the most useful visual properties — colors, sizes, padding, margins, borders, border-radius, font sizes, font weights, opacity, shadows. Filter to what's most likely to be tweaked. Do not include every single CSS property — surface the most useful ones. The user can ask you to add more.
4. For each property, capture:
   - Current value
   - Exact line number
   - The full CSS declaration string (e.g., `background-color: #1a1a2e`) for verification
   - A CSS selector that targets the element in the browser (if identifiable with confidence)
5. Scan for other color values used in the file to build a palette array.

### Widget payload format

Emit the widget JSON wrapped in `<!--DESIGN_WIDGET-->` / `<!--/DESIGN_WIDGET-->` delimiters. Do not include any other text in your response — no preamble, no explanation. The frontend strips the delimiters and renders the widget directly.

Schema:

```json
{
  "element_id": "header-bar",
  "element_label": "Header Bar",
  "palette": ["#1a1a2e", "#16213e", "#0f3460", "#e94560"],
  "properties": [
    {
      "key": "bg_color",
      "label": "Background",
      "type": "color",
      "value": "#1a1a2e",
      "source": {
        "file": "dashboard.py",
        "line": 342,
        "match": "background-color: #1a1a2e"
      },
      "preview": {
        "selector": "#main-header",
        "css_property": "background-color",
        "confidence": "high"
      }
    }
  ]
}
```

**Control types and their fields:**

|Type    |Renders as                              |Required fields                                                              |Optional fields     |
|--------|----------------------------------------|-----------------------------------------------------------------------------|--------------------|
|`color` |Swatch palette → full picker + hex input|`value` (hex string)                                                         |                    |
|`px`    |Number input + stepper buttons          |`value` (number)                                                             |`min`, `max`, `step`|
|`toggle`|On/off switch                           |`value` (bool), `toggle_values` (`{"on": "css string", "off": "css string"}`)|                    |
|`select`|Dropdown                                |`value` (string), `options` (array of strings)                               |                    |
|`range` |Slider + number input                   |`value` (number), `min`, `max`, `step`                                       |                    |

**All properties require:**

- `key` — unique within the widget
- `label` — human-readable label
- `type` — one of: color, px, toggle, select, range
- `value` — current value from code
- `source` — object with `file` (relative path from /opt/trading-platform/), `line` (line number), `match` (exact CSS declaration string for verification)

**Optional per-property:**

- `preview` — object with `selector` (CSS selector), `css_property` (CSS property name), `confidence` ("high" or "medium"). Omit entirely if no stable selector can be identified — do not include low-confidence previews.

**Top-level fields:**

- `element_id` — consistent identifier for the element (e.g., "header-bar"). Used to replace previous widgets on follow-up requests.
- `element_label` — human-readable name shown as widget title
- `palette` — array of hex colors found used in the codebase vicinity, shared across all color controls

### Follow-up requests

If the user asks to add or remove properties from an existing widget (e.g., "add blur and shadow too" or "I don't need the font weight"), re-read the code and emit a complete replacement payload with the same `element_id`. Include all desired properties with fresh values from the current code. The frontend replaces the previous widget.

### DESIGN_APPLY handling

When you receive a prompt wrapped in `[DESIGN_APPLY]` / `[/DESIGN_APPLY]` tags, it contains surgical style changes from the design widget. Each change specifies a file, line number, old value, and new value. Your job:

1. Open the file and verify the old value exists at the specified line.
1. If it matches, replace the old value with the new value using str_replace or equivalent.
1. If the line has shifted or the value doesn't match, search nearby (±10 lines) for the old value. If found, apply there. If not found, report the issue — do not guess.
1. After all changes, run `python3 -m py_compile` on any modified .py files.
1. Respond with a brief confirmation (e.g., "Applied 3 changes to dashboard.py"). Do not emit a new widget.

Format:

```
[DESIGN_APPLY]
Element: Header Bar
File: dashboard.py
1. Line 342: "background-color: #1a1a2e" → "background-color: #2a2a3e"
2. Line 345: "padding: 12px 16px" → "padding: 20px 16px"
[/DESIGN_APPLY]
```

## Direct Actions

When the user wants to PERFORM an action that maps to a built-in terminal action, emit a direct action marker instead of answering with text. The terminal intercepts these and renders a native widget instantly. Only emit when the user wants to perform the action, not when they're discussing, asking about, or debugging it.

Available actions:
- `<!--DIRECT_ACTION:git_status-->` — show uncommitted files, unpushed commits, repo state
- `<!--DIRECT_ACTION:git_push-->` — commit all changes and push to GitHub
- `<!--DIRECT_ACTION:git_diff-->` — show what files changed

When to emit:
- "what's the git status" → `<!--DIRECT_ACTION:git_status-->`
- "anything to push?" → `<!--DIRECT_ACTION:git_status-->`
- "show me what hasn't been committed" → `<!--DIRECT_ACTION:git_status-->`
- "push it" / "push to github" → `<!--DIRECT_ACTION:git_push-->`
- "what changed" / "show me the diff" → `<!--DIRECT_ACTION:git_diff-->`

When NOT to emit:
- User is talking about git-related code changes ("fix the git widget", "add git integration")
- User is asking ABOUT a feature ("tell me about the git status widget", "how does the push button work", "explain the direct action system")
- Message is conversational ("how does the git widget work?")
- User wants a specific git command run as part of a larger task
- User is asking you to build or modify the git feature itself

When you emit a direct action marker, emit ONLY the marker on its own — no other text, no explanation, no preamble.

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
