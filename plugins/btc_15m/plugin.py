"""
plugin.py — BTC 15-Minute market plugin.
"""

from config import KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH
from plugin_base import MarketPlugin


class Btc15mPlugin(MarketPlugin):

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

    # ── Lifecycle ──────────────────────────────────────────────

    def init_db(self):
        from plugins.btc_15m.market_db import init_btc15m_tables
        init_btc15m_tables()

    def run(self, stop_event):
        from kalshi import KalshiClient
        from plugins.btc_15m.bot import run_loop
        client = KalshiClient(KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH)
        run_loop(client, stop_event)

    def get_default_config(self) -> dict:
        """Plugin defaults — no namespace prefix. Platform adds 'btc_15m.' when storing."""
        return {
            # Trading mode
            "trading_mode":       "observe",
            "observe_only":       True,
            "shadow_trading":     False,
            "trade_mode":         "continuous",

            # Bet sizing
            "bet_mode":           "flat",
            "bet_size":           50.0,
            "edge_tiers":         [
                {"min_edge": 0, "multiplier": 0.5},
                {"min_edge": 2, "multiplier": 1.0},
                {"min_edge": 5, "multiplier": 1.5},
                {"min_edge": 10, "multiplier": 2.0},
            ],

            # Entry
            "entry_price_max_c":  45,
            "entry_delay_minutes": 0,
            "adaptive_entry":     False,

            # Exit
            "sell_target_c":      90,
            "trailing_stop_pct":  0,
            "early_exit_ev":      False,
            "dynamic_sell_enabled": False,
            "dynamic_sell_floor_c": 3,

            # Manual strategy — side selection
            "strategy_side":        "cheaper",
            "min_model_edge_pct":   3.0,

            # Regime gating
            "risk_level_actions": {
                "low": "normal",
                "moderate": "normal",
                "high": "normal",
                "terrible": "skip",
                "unknown": "skip",
            },
            "regime_overrides":     {},
            "regime_filters":       {},

            # Auto-strategy
            "auto_strategy_enabled": False,
            "auto_strategy_min_samples": 30,
            "auto_strategy_min_ev_c": 0,
            "min_breakeven_fee_buffer": 0.03,

            # Loss safety stop
            "max_consecutive_losses": 0,
            "cooldown_after_loss_stop": 0,

            # Health check
            "health_check_enabled":     False,
            "health_check_timeout_min": 5,

            # Deploy safety
            "deploy_cooldown_minutes":  0,

            # Polling
            "price_poll_interval":   2,
            "order_poll_interval":   3,

            # Maintenance
            "log_retention_days":    7,

            # Push notifications
            "push_notify_wins":     True,
            "push_notify_losses":   True,
            "push_notify_errors":   True,
            "push_notify_buys":     False,
            "push_notify_observed":    False,
            "push_notify_health_check": True,
            "push_notify_new_regime": True,
            "push_notify_regime_classified": True,
            "push_notify_trade_updates": False,
            "push_notify_early_exit": True,
            "push_notify_strategy_discovery": True,
            "push_notify_global_best": True,
            "push_quiet_start":     0,
            "push_quiet_end":       0,
        }

    # ── Routes ─────────────────────────────────────────────────

    def register_routes(self, app):
        from plugins.btc_15m.dashboard import register_routes
        register_routes(app, self)

    # ── Dashboard rendering ────────────────────────────────────

    def render_header_html(self) -> str:
        from plugins.btc_15m.dashboard import render_header_html
        return render_header_html(self)

    def render_home_card_html(self) -> str:
        from plugins.btc_15m.dashboard import render_home_card_html
        return render_home_card_html(self)

    def render_trade_card_template(self) -> str:
        from plugins.btc_15m.dashboard import render_trade_card_template
        return render_trade_card_template(self)

    def render_regime_config_html(self) -> str:
        from plugins.btc_15m.dashboard import render_regime_config_html
        return render_regime_config_html(self)

    def render_stats_section_html(self) -> str:
        from plugins.btc_15m.dashboard import render_stats_section_html
        return render_stats_section_html(self)

    def render_settings_html(self) -> str:
        from plugins.btc_15m.dashboard import render_settings_html
        return render_settings_html(self)

    def render_js(self) -> str:
        from plugins.btc_15m.dashboard import render_js
        return render_js(self)
