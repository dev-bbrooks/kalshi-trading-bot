"""
plugin.py — BTC 15-minute binary options plugin.
Implements MarketPlugin interface for Kalshi BTC 15-min markets.
"""

import sys
import os
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from plugin_base import MarketPlugin

log = logging.getLogger("btc_15m")


class Btc15mPlugin(MarketPlugin):
    """BTC 15-minute binary options plugin for Kalshi."""

    @property
    def plugin_id(self) -> str:
        return "btc_15m"

    @property
    def display_name(self) -> str:
        return "BTC 15-Minute"

    @property
    def asset(self) -> str:
        return "BTC"

    @property
    def asset_source(self) -> str:
        return "binance"

    def init_db(self, conn=None):
        """Create all btc15m_* tables."""
        from plugins.btc_15m.market_db import init_btc15m_tables
        init_btc15m_tables()

    def get_default_config(self) -> dict:
        """Return BTC 15m defaults WITHOUT plugin prefix (platform adds it).

        These are the config keys that get namespaced as btc_15m.* in the DB.
        """
        return {
            # Trading mode
            "trading_mode": "observe",

            # Bet sizing
            "bet_mode": "flat",
            "bet_size": 50.0,
            "edge_tiers": [
                {"min_edge": 0, "multiplier": 0.5},
                {"min_edge": 2, "multiplier": 1.0},
                {"min_edge": 5, "multiplier": 1.5},
                {"min_edge": 10, "multiplier": 2.0},
            ],

            # Adaptive entry
            "adaptive_entry": False,

            # Sell target
            "sell_target_c": 90,
            "trailing_stop_pct": 0,
            "early_exit_ev": False,
            "dynamic_sell_enabled": False,
            "dynamic_sell_floor_c": 3,

            # Entry
            "entry_price_max_c": 45,
            "entry_delay_minutes": 0,

            # Regime gating
            "risk_level_actions": {
                "low": "normal",
                "moderate": "normal",
                "high": "normal",
                "terrible": "skip",
                "unknown": "skip",
            },
            "regime_overrides": {},
            "regime_filters": {},

            # Auto-strategy
            "auto_strategy_min_samples": 30,
            "auto_strategy_min_ev_c": 0,
            "min_breakeven_fee_buffer": 0.03,

            # Manual strategy
            "strategy_side": "cheaper",
            "min_model_edge_pct": 3.0,

            # Polling
            "price_poll_interval": 2,
            "order_poll_interval": 3,

            # Push notification per-type toggles
            "push_notify_wins": True,
            "push_notify_losses": True,
            "push_notify_errors": True,
            "push_notify_buys": False,
            "push_notify_observed": False,
            "push_notify_early_exit": True,
            "push_notify_new_regime": True,
            "push_notify_regime_classified": True,
            "push_notify_trade_updates": False,
            "push_notify_strategy_discovery": True,
            "push_notify_global_best": True,
            "push_quiet_start": 0,
            "push_quiet_end": 0,
        }

    def run(self, stop_event):
        """Main trading loop — stub for Phase 2. Phase 3 fills this in."""
        log.info(f"[{self.plugin_id}] Plugin running (Phase 2 stub)")
        stop_event.wait()
        log.info(f"[{self.plugin_id}] Plugin stopped")

    # Dashboard render methods — empty stubs for Phase 2, Phase 4 fills in
    def render_home_card_html(self) -> str:
        return ""

    def render_trade_card_template(self) -> str:
        return ""

    def render_regime_config_html(self) -> str:
        return ""

    def render_stats_section_html(self) -> str:
        return ""

    def render_settings_html(self) -> str:
        return ""

    def render_header_html(self) -> str:
        return ""

    def register_routes(self, app):
        """Register Flask routes — stub for Phase 2. Phase 4 fills this in."""
        pass
