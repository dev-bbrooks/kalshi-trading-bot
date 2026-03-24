"""
plugin_base.py — Base class defining the market plugin interface.
Every market plugin must subclass MarketPlugin and implement these methods.
"""

from abc import ABC, abstractmethod


class MarketPlugin(ABC):
    """
    Base class for market plugins. Each plugin represents one tradeable
    market type (e.g., BTC 15-minute binary options).

    Lifecycle:
      1. engine.py loads the plugin module and calls Plugin(plugin_id, db, kalshi, config)
      2. Platform calls plugin.init_db(conn) to create plugin-specific tables
      3. Platform starts the regime worker for the plugin's asset (if not already running)
      4. Platform calls plugin.run() — plugin owns its entire trading loop
      5. On shutdown, platform sets stop_event; plugin exits run() cleanly
    """

    @property
    @abstractmethod
    def plugin_id(self) -> str:
        """Unique identifier for this plugin (e.g., 'btc_15m').
        Used for config namespacing, DB table prefixes, and logging."""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name for dashboard display (e.g., 'BTC 15-Min')."""

    @property
    @abstractmethod
    def asset(self) -> str:
        """Underlying asset this plugin trades (e.g., 'btc').
        Used to determine which regime worker to share."""

    @property
    @abstractmethod
    def asset_source(self) -> str:
        """Data source for asset prices (e.g., 'binance').
        Paired with asset to parameterize the regime engine."""

    @abstractmethod
    def init_db(self, conn):
        """Create plugin-specific database tables.
        Called once on startup with an open SQLite connection.
        Table names MUST be prefixed with the plugin's table_prefix."""

    @property
    def table_prefix(self) -> str:
        """DB table name prefix. Default: plugin_id with dots replaced by underscores."""
        return self.plugin_id.replace(".", "_").replace("-", "_")

    @abstractmethod
    def run(self, stop_event):
        """Main trading loop. Called by engine.py.
        Must respect stop_event (threading.Event) — check periodically and exit when set.
        Plugin owns its entire loop: find market → evaluate → trade → resolve."""

    # ── Dashboard component rendering ─────────────────────────

    def render_home_card_html(self) -> str:
        """Return HTML for this plugin's card on the Home tab."""
        return ""

    def render_trade_card_template(self) -> str:
        """Return HTML/JS template for rendering trades from this plugin."""
        return ""

    def render_regime_config_html(self) -> str:
        """Return HTML for plugin-specific regime config on the Regimes tab."""
        return ""

    def render_stats_section_html(self) -> str:
        """Return HTML for plugin stats on the Stats tab."""
        return ""

    def render_settings_html(self) -> str:
        """Return HTML for plugin settings on the Settings tab."""
        return ""

    def render_header_html(self) -> str:
        """Return HTML for plugin-specific header elements."""
        return ""

    def register_routes(self, app):
        """Register Flask routes for this plugin's API endpoints.
        Called during dashboard setup with the Flask app instance."""
