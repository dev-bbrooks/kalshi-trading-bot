"""
plugin_base.py — Base class defining the market plugin interface.
Every market plugin must subclass MarketPlugin and implement all abstract methods.
"""

from abc import ABC, abstractmethod


class MarketPlugin(ABC):
    """
    Base class for market plugins.

    Each plugin represents one tradeable market type (e.g. BTC 15-minute binary options).
    The platform calls these methods — plugins never import platform internals except
    config, db, kalshi, push, and regime.
    """

    @property
    @abstractmethod
    def plugin_id(self) -> str:
        """Unique identifier, e.g. 'btc_15m'. Used as DB prefix and config namespace."""
        ...

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name, e.g. 'BTC 15-Minute'."""
        ...

    @property
    @abstractmethod
    def asset(self) -> str:
        """Underlying asset ticker, e.g. 'BTC'. Shared with regime engine."""
        ...

    # ── Lifecycle ──────────────────────────────────────────────

    @abstractmethod
    def init_db(self):
        """Create plugin-specific DB tables (prefixed with plugin_id)."""
        ...

    @abstractmethod
    def run(self, stop_event):
        """
        Main trading loop. Called by engine.py.
        Must respect stop_event (threading.Event) for clean shutdown.
        """
        ...

    # ── Dashboard Components ──────────────────────────────────
    # Each returns an HTML string fragment that gets composed into
    # the platform's unified tabs. Return "" if nothing to show.

    @abstractmethod
    def render_home_card_html(self, state: dict) -> str:
        """Market card for the Home tab. Shows current status, active trade, etc."""
        ...

    @abstractmethod
    def render_trade_card_template(self) -> str:
        """HTML template for a single trade row in the Trades tab."""
        ...

    @abstractmethod
    def render_regime_config_html(self) -> str:
        """Plugin-specific regime configuration section for the Regimes tab."""
        ...

    @abstractmethod
    def render_stats_section_html(self) -> str:
        """Per-market stats breakdown for the Stats tab."""
        ...

    @abstractmethod
    def render_settings_html(self) -> str:
        """Plugin-specific settings section for the Settings tab."""
        ...

    @abstractmethod
    def render_header_html(self, state: dict) -> str:
        """Header status fragment (status line, countdown, etc.)."""
        ...

    @abstractmethod
    def register_routes(self, app):
        """
        Register plugin-specific Flask routes on the app.
        All routes should be prefixed with /api/{plugin_id}/.
        """
        ...

    # ── Config Defaults ────────────────────────────────────────

    @abstractmethod
    def get_default_config(self) -> dict:
        """Return default config key-value pairs for this plugin."""
        ...
