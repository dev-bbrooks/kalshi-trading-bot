"""
config.py — Constants, paths, and credentials for the trading platform.
"""

import os
from zoneinfo import ZoneInfo

# ── Load .env file (no dependencies needed) ───────────────────
def _load_env_file():
    """Load key=value pairs from .env or _env into os.environ."""
    platform_dir = os.environ.get("PLATFORM_DIR", "/opt/trading-platform")
    for name in (".env", "_env"):
        path = os.path.join(platform_dir, name)
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
PLATFORM_DIR = os.environ.get("PLATFORM_DIR", "/opt/trading-platform")
DB_PATH = os.path.join(PLATFORM_DIR, "platform.db")
LOG_FILE = os.path.join(PLATFORM_DIR, "platform.log")

# ── Kalshi API ─────────────────────────────────────────────────
KALSHI_API_KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH",
                                          os.path.join(PLATFORM_DIR, "BTC.txt"))
KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# ── Kalshi Fee Schedule ────────────────────────────────────────
KALSHI_FEE_RATE = 0.07  # 7% of contract price per contract (buys only)

# ── Binance (public, no key needed) ───────────────────────────
BINANCE_BASE_URL = "https://api.binance.us"

# ── Dashboard ──────────────────────────────────────────────────
DASHBOARD_HOST = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", 8050))
DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "admin")
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "CHANGE_ME")

# ── Regime Risk Thresholds ────────────────────────────────────
REGIME_THRESHOLDS = {
    "min_trades_known":   10,    # Below this = "unknown" (real trades)
    "min_sim_known":      10,    # Below this = "unknown" (Observatory sims)
    # Composite risk score thresholds (0-100, higher = safer)
    "low_risk_floor":     65,    # Score >= 65 = low risk
    "moderate_risk_floor": 45,   # Score >= 45 = moderate
    "high_risk_floor":    25,    # Score >= 25 = high risk
    # Below 25 = terrible (extreme)
}
