# Dashboard Update Plan

## Architecture

Single Flask app (`dashboard.py`) at platform level. Plugins inject HTML components via render methods. Platform owns the 5-tab layout and composes plugin fragments into unified pages.

## Tab Composition

### Home Tab
- Platform renders: shared BTC price chart (from regime engine candle data)
- Plugins render: `render_home_card_html(state)` — one stacked card per plugin
- Card shows: current status, active trade, countdown, live prices

### Trades Tab
- Platform renders: unified trade list container with Market filter pills
- Plugins render: `render_trade_card_template()` — HTML template for trade rows
- API: each plugin registers `/api/{plugin_id}/trades` endpoint

### Regimes Tab
- Platform renders: shared asset regime chart (BTC price + regime labels over time)
- Plugins render: `render_regime_config_html()` — per-plugin regime gate settings

### Stats Tab
- Platform renders: global totals header (across all markets)
- Plugins render: `render_stats_section_html()` — per-market breakdown
- Sub-pages (Performance, Regime Analysis, Shadow) are per-market via inner pills

### Settings Tab
- Platform renders: platform settings (credentials, push config, log retention)
- Plugins render: `render_settings_html()` — plugin-specific settings

## Route Registration

Each plugin calls `register_routes(app)` to add its API endpoints:
```python
def register_routes(self, app):
    @app.route('/api/btc_15m/state')
    def btc15m_state(): ...

    @app.route('/api/btc_15m/trades')
    def btc15m_trades(): ...
```

## Key Constraints

- No localStorage (iOS PWA compatibility)
- Mobile-first: fixed header + fixed tab bar + scrollable content
- Safe area insets for notch/home indicator
- All HTML/CSS/JS inline in Python strings
- Pull-to-refresh blocked during chart touch
- With one plugin, looks identical to current dashboard
