# Trading Platform Refactor — Architecture

## Three-Layer Architecture

1. **Platform** (market-agnostic infrastructure)
   - `config.py` — constants, paths, credentials
   - `db.py` — connection manager + platform tables + shared queries
   - `kalshi.py` — API client (auth, orders, balance)
   - `push.py` — push notification delivery
   - `regime.py` — asset regime engine (parameterized by asset)
   - `engine.py` — universal plugin launcher
   - `plugin_base.py` — MarketPlugin base class
   - `dashboard.py` — platform dashboard shell + plugin component injection

2. **Asset Engines** (regime tracking per underlying asset)
   - BTC regime worker (shared across all BTC-based plugins)
   - DB heartbeat for cross-process coordination
   - Heavy computation (sim batch, surface, features) NOT in regime worker

3. **Market Plugins** (one per tradeable market type)
   - `plugins/btc_15m/` — BTC 15-minute binary options
   - Each plugin owns: bot loop, strategy, plugin-specific DB, dashboard components, notifications

## Directory Structure

```
/opt/trading-platform/
├── config.py              Platform constants
├── db.py                  Connection manager + platform tables
├── kalshi.py              API client (market-agnostic)
├── push.py                Push notification delivery
├── regime.py              Asset regime engine (parameterized)
├── engine.py              Plugin launcher: python3 engine.py btc_15m
├── plugin_base.py         MarketPlugin base class
├── dashboard.py           Platform dashboard shell (Phase 4)
├── plugins/
│   └── btc_15m/
│       ├── __init__.py
│       ├── plugin.py      MarketPlugin subclass
│       ├── bot.py         Trading loop, execution
│       ├── strategy.py    Observatory, simulation, FV model
│       ├── market_db.py   Plugin-specific DB tables (btc15m_*)
│       ├── dashboard.py   Plugin UI components
│       └── notifications.py  Plugin notify_* functions
├── .env                   Environment variables
├── vapid_keys.json        VAPID keys for push notifications
├── BTC.txt                Kalshi RSA private key
└── platform.db            SQLite database (WAL mode)
```

## Process Model

- One supervisor process per plugin: `python3 engine.py btc_15m`
- One shared dashboard process: `python3 dashboard.py`
- `engine.py` loads plugin, starts regime worker thread, calls `plugin.run()`
- Plugin owns its entire trading loop

## Database Schema

### Platform Tables
- `bot_config` — namespaced key-value (e.g. `btc_15m.trading_mode`)
- `plugin_state` — one row per plugin (replaces single-row bot_state)
- `bot_commands` — with plugin_id column
- `log_entries` — with source column
- `push_subscriptions`, `push_log`
- `bankroll_snapshots` — with plugin_id column
- `audit_log`

### Asset Tables (all with asset column)
- `candles` — 1-minute OHLCV data
- `baselines` — statistical norms per hour/dow
- `regime_snapshots` — point-in-time classifications
- `regime_stability_log` — label change tracking
- `regime_heartbeat` — cross-process coordination

### Plugin Tables (prefixed btc15m_*)
- Created by plugin's `init_db()` method
- Markets, trades, observations, strategy_results, price_path, live_prices, probability_surface, feature_importance

## Plugin Interface

```python
class MarketPlugin(ABC):
    plugin_id: str          # 'btc_15m'
    display_name: str       # 'BTC 15-Minute'
    asset: str              # 'BTC'

    def init_db(self)
    def run(self, stop_event)
    def get_default_config(self) -> dict

    # Dashboard components
    def render_home_card_html(self, state) -> str
    def render_trade_card_template(self) -> str
    def render_regime_config_html(self) -> str
    def render_stats_section_html(self) -> str
    def render_settings_html(self) -> str
    def render_header_html(self, state) -> str
    def register_routes(self, app)
```

## Dashboard — Unified Tabs

Five tabs always: Trades, Regimes, Home, Stats, Settings.
Platform owns the tabs. Plugins provide COMPONENTS that get composed INTO tabs.

- **Home** = shared BTC chart + stacked market cards (one per plugin)
- **Trades** = unified trade list with Market filter dimension
- **Regimes** = shared asset chart + per-plugin config sections
- **Stats** = global totals + per-market breakdowns
- **Settings** = platform settings + per-plugin settings

With one plugin, dashboard looks identical to current app.

## Regime Engine

- Parameterized by asset name
- DB heartbeat prevents duplicate workers for same asset
- Multiple plugins sharing same asset share one regime worker
- Heavy computation (sim batch, surface, features) handled by plugin, not regime worker

## Phased Migration

1. **Phase 1** (DONE) — Platform scaffold: all platform-level files
2. **Phase 2** — Plugin data/strategy layer: btc_15m plugin with market_db, strategy
3. **Phase 3** — Trading engine: bot.py migration
4. **Phase 4** — Dashboard: unified dashboard with plugin component injection
