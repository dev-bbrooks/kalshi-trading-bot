"""
plugin_base.py — Abstract base class for market plugins.
"""

from abc import ABC, abstractmethod


class MarketPlugin(ABC):

    @property
    @abstractmethod
    def plugin_id(self) -> str:
        """Unique identifier, e.g. 'btc_15m'."""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name, e.g. 'BTC 15-Minute'."""

    @property
    @abstractmethod
    def asset(self) -> str:
        """Traded asset symbol, e.g. 'BTC'."""

    @property
    @abstractmethod
    def asset_source(self) -> str:
        """Price data source, e.g. 'binance'."""

    # ── Core lifecycle ────────────────────────────────────────

    @abstractmethod
    def init_db(self):
        """Create plugin-specific tables."""

    @abstractmethod
    def run(self, stop_event):
        """Main trading loop. Blocks until stop_event is set."""

    @abstractmethod
    def get_default_config(self) -> dict:
        """Return plugin defaults (no namespace prefix)."""

    @abstractmethod
    def register_routes(self, app):
        """Register Flask API routes for this plugin."""

    # ── Dashboard rendering (return HTML strings) ─────────────

    @abstractmethod
    def render_header_html(self) -> str:
        """Top-of-page header bar content."""

    @abstractmethod
    def render_home_card_html(self) -> str:
        """Summary card shown on the dashboard home page."""

    @abstractmethod
    def render_trade_card_template(self) -> str:
        """HTML template for an individual trade card."""

    @abstractmethod
    def render_regime_config_html(self) -> str:
        """Regime configuration panel HTML."""

    @abstractmethod
    def render_stats_section_html(self) -> str:
        """Statistics section HTML."""

    @abstractmethod
    def render_settings_html(self) -> str:
        """Plugin settings panel HTML."""

    @abstractmethod
    def render_js(self) -> str:
        """Plugin-specific JavaScript."""
