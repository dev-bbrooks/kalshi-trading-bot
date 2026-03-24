"""
config.py — Constants, paths, and defaults for the Kalshi BTC trading bot.
"""

import os
from zoneinfo import ZoneInfo

# ── Load .env file (no dependencies needed) ───────────────────
def _load_env_file():
    """Load key=value pairs from .env or _env into os.environ."""
    bot_dir = os.environ.get("BOT_DIR", "/opt/15-min-btc-bot")
    for name in (".env", "_env"):
        path = os.path.join(bot_dir, name)
        if os.path.isfile(path):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = value
            break

_load_env_file()

# ── Timezones ──────────────────────────────────────────────────
ET = ZoneInfo("America/New_York")       # Kalshi markets run on ET
CT = ZoneInfo("America/Chicago")        # Display timezone

# ── Paths ──────────────────────────────────────────────────────
BOT_DIR = os.environ.get("BOT_DIR", "/opt/15-min-btc-bot")
DB_PATH = os.path.join(BOT_DIR, "botdata.db")
LOG_FILE = os.path.join(BOT_DIR, "bot.log")

# ── Kalshi API ─────────────────────────────────────────────────
KALSHI_API_KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH",
                                          os.path.join(BOT_DIR, "BTC.txt"))
KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# ── Kalshi Fee Schedule ────────────────────────────────────────
KALSHI_FEE_RATE = 0.07  # 7% of contract price per contract (buys only)

# ── Binance (public, no key needed) ───────────────────────────
BINANCE_BASE_URL = "https://api.binance.us"

# ── Dashboard ──────────────────────────────────────────────────
DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", 8050))
DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "admin")
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "CHANGE_ME")

# ── Regime Risk Thresholds ────────────────────────────────────
REGIME_THRESHOLDS = {
    "min_trades_known":   10,    # Below this = "unknown" (real trades)
    "min_sim_known":      10,    # Below this = "unknown" (Observatory sims)
    # Composite risk score thresholds (0-100, higher = safer)
    # Score incorporates EV, confidence, OOS validation, downside, robustness
    "low_risk_floor":     65,    # Score >= 65 = low risk
    "moderate_risk_floor": 45,   # Score >= 45 = moderate
    "high_risk_floor":    25,    # Score >= 25 = high risk
    # Below 25 = terrible (extreme)
}

# ── Bot Defaults ───────────────────────────────────────────────
DEFAULT_BOT_CONFIG = {
    # Bet sizing
    "bet_mode":           "flat",    # "flat", "percent", or "edge_scaled"
    "bet_size":           50.0,      # dollars (flat/edge_scaled base) or percent of bankroll
    "edge_tiers":         [          # edge_scaled mode: scale bet by FV model edge
        {"min_edge": 0, "multiplier": 0.5},
        {"min_edge": 2, "multiplier": 1.0},
        {"min_edge": 5, "multiplier": 1.5},
        {"min_edge": 10, "multiplier": 2.0},
    ],

    # Adaptive entry — start below ask, walk up to save on spread
    "adaptive_entry":     False,

    # Trading mode — single selector replaces observe_only + shadow_trading + auto_strategy_enabled
    #   observe: record data only, no trades
    #   shadow:  observe + 1-contract trades for execution data
    #   hybrid:  auto-strategy full trades when confident, shadow fallback otherwise
    #   auto:    auto-strategy full trades when confident, observe-only otherwise
    #   manual:  picker-configured strategy, regime gate decides
    "trading_mode":       "observe",

    # Legacy booleans — derived from trading_mode, kept for backward compatibility
    "observe_only":       True,
    "shadow_trading":     False,

    # Sell target — absolute price in cents (5¢ increments, or 99, or 0 for hold)
    "sell_target_c":      90,         # controlled by strategy picker
    # Trailing stop — locks in gains once price reaches threshold
    "trailing_stop_pct":  0,          # 0=off, e.g. 60 = activate at 60% of target progress
    # Early exit — sell when holding is negative EV with little time left
    "early_exit_ev":      False,
    # Dynamic sell — fair value model adjusts sell target during trade
    "dynamic_sell_enabled": False,
    "dynamic_sell_floor_c": 3,        # min move (¢) to trigger sell order replacement

    # Entry — aligned with Observatory simulation assumptions
    "entry_price_max_c":  45,        # controlled by strategy picker
    "entry_delay_minutes": 0,        # controlled by strategy picker timing rule

    # Trade mode
    "trade_mode":         "continuous",

    # Ignore mode
    "ignore_mode":        False,

    # Regime gating — action per risk level
    "risk_level_actions": {
        "low": "normal",
        "moderate": "normal",
        "high": "normal",
        "terrible": "skip",
        "unknown": "skip",
    },
    "regime_overrides":     {},   # per-regime: {label: "normal"|"skip"|"default"}
    "regime_filters":       {},   # per-regime granular filters (see below)

    # Auto-strategy — use Strategy Observatory to pick best strategy per regime
    "auto_strategy_enabled": False,
    "auto_strategy_min_samples": 30,   # min observations for a strategy to be trusted
    "auto_strategy_min_ev_c": 0,       # min EV per trade in cents (0 = any positive)
    "min_breakeven_fee_buffer": 0.03,  # strategy must survive fees up to current + this buffer

    # Manual strategy — side selection
    "strategy_side":        "cheaper",    # cheaper|yes|no|model
    "min_model_edge_pct":   3.0,          # min edge % for model side rule (0=any positive edge)

    # regime_filters structure per label:
    #   blocked_hours: [int]      — ET hours to skip (0-23)
    #   blocked_days: [int]       — weekdays to skip (0=Mon, 6=Sun)
    #   vol_min: int              — min volatility level (1-5, default 1)
    #   vol_max: int              — max volatility level (1-5, default 5)
    #   stability_max: int        — max price stability in cents (0=off)
    #   blocked_sides: [str]      — sides to skip ("yes", "no")
    #   max_spread_c: int         — max spread in cents (0=off). Skips wide spreads.

    # Bankroll management
    "locked_bankroll":       0.0,
    "auto_lock_enabled":     False,
    "auto_lock_threshold":   0.0,
    "auto_lock_amount":      0.0,
    "auto_lock_random":      False,   # 50/50 lock vs grow bankroll
    "profit_goal":           0.0,     # notify when locked profits reach this
    "profit_goal_reached":   False,
    "bankroll_min":          0.0,     # stop trading below this
    "bankroll_max":          0.0,     # stop trading above this

    # Session
    "session_profit_target": 0.0,
    "session_loss_limit":    0.0,     # max session loss $ (stops trading, 0=off)

    # Loss safety stop — pause after N consecutive losses (0=off)
    "max_consecutive_losses": 0,
    "cooldown_after_loss_stop": 0,   # skip N markets after loss stop triggers

    # Rolling win-rate circuit breaker (0=off)
    "rolling_wr_window":     0,      # check last N completed trades
    "rolling_wr_floor":      0,      # min win rate % over that window (e.g. 35)

    # Health check
    "health_check_enabled":     False,
    "health_check_timeout_min": 5,

    # Deploy safety — pause auto-trading after restart
    "deploy_cooldown_minutes":  0,          # 0=resume immediately, >0=wait N min
    # Polling
    "price_poll_interval":   2,
    "order_poll_interval":   3,

    # Maintenance
    "log_retention_days":    7,       # auto-clean log file + log_entries older than this

    # Push notifications
    "push_notify_wins":     True,
    "push_notify_losses":   True,
    "push_notify_errors":   True,
    "push_notify_buys":     False,
    "push_notify_observed":    False,
    "push_notify_auto_lock": True,
    "push_notify_health_check": True,
    "push_notify_new_regime": True,
    "push_notify_regime_classified": True,
    "push_notify_trade_updates": False,
    "push_quiet_start":     0,
    "push_quiet_end":       0,
}
