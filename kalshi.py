"""
kalshi.py — Kalshi API client with RSA-PSS authentication.
Pure API layer — no trading logic, no database access.
"""

import time
import math
import base64
import logging
import requests
from datetime import datetime, timezone, timedelta
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from config import KALSHI_BASE_URL, KALSHI_FEE_RATE, ET

log = logging.getLogger("kalshi")


# ═══════════════════════════════════════════════════════════════
#  FIXED-POINT NORMALIZATION (March 2026 API migration)
#
#  Kalshi is removing legacy integer fields (yes_ask, no_ask,
#  fill_count, taker_fill_cost, etc.) and replacing them with
#  _dollars (string) and _fp (string) equivalents.
#
#  These helpers convert the new fields back to integer cents
#  so the rest of the codebase can continue using cents internally.
# ═══════════════════════════════════════════════════════════════

def _dollars_to_cents(val) -> int | None:
    """Convert a _dollars string like '0.4500' to integer cents (45).
    Returns None if val is missing/invalid."""
    if val is None:
        return None
    try:
        return round(float(val) * 100)
    except (ValueError, TypeError):
        return None


def _fp_to_int(val) -> int | None:
    """Convert a _fp string like '10.00' to integer.
    Returns None if val is missing/invalid."""
    if val is None:
        return None
    try:
        return int(round(float(val)))
    except (ValueError, TypeError):
        return None


def _normalize_market(market: dict) -> dict:
    """Populate legacy integer cent fields from _dollars equivalents.
    Prefers _dollars fields; falls back to legacy if _dollars missing."""
    if not market:
        return market

    _PRICE_FIELDS = [
        ("yes_ask", "yes_ask_dollars"),
        ("no_ask", "no_ask_dollars"),
        ("yes_bid", "yes_bid_dollars"),
        ("no_bid", "no_bid_dollars"),
        ("last_price", "last_price_dollars"),
    ]
    for legacy, dollars in _PRICE_FIELDS:
        new_val = _dollars_to_cents(market.get(dollars))
        if new_val is not None:
            market[legacy] = new_val
        # If neither exists, field stays absent — callers use .get() with defaults

    # Count fields: _fp → integer
    _COUNT_FIELDS = [
        ("volume", "volume_fp"),
        ("volume_24h", "volume_24h_fp"),
        ("open_interest", "open_interest_fp"),
    ]
    for legacy, fp in _COUNT_FIELDS:
        new_val = _fp_to_int(market.get(fp))
        if new_val is not None:
            market[legacy] = new_val

    return market


def _normalize_order(order: dict) -> dict:
    """Populate legacy integer fields from _fp/_dollars equivalents."""
    if not order:
        return order

    # Count fields: _fp → integer
    _COUNT_FIELDS = [
        ("fill_count", "fill_count_fp"),
        ("remaining_count", "remaining_count_fp"),
        ("initial_count", "initial_count_fp"),
    ]
    for legacy, fp in _COUNT_FIELDS:
        new_val = _fp_to_int(order.get(fp))
        if new_val is not None:
            order[legacy] = new_val

    # Cost/fee fields: _dollars → integer cents
    _COST_FIELDS = [
        ("taker_fill_cost", "taker_fill_cost_dollars"),
        ("maker_fill_cost", "maker_fill_cost_dollars"),
        ("taker_fees", "taker_fees_dollars"),
        ("maker_fees", "maker_fees_dollars"),
        ("yes_price", "yes_price_dollars"),
        ("no_price", "no_price_dollars"),
    ]
    for legacy, dollars in _COST_FIELDS:
        new_val = _dollars_to_cents(order.get(dollars))
        if new_val is not None:
            order[legacy] = new_val

    return order


# ═══════════════════════════════════════════════════════════════
#  RSA KEY HANDLING
# ═══════════════════════════════════════════════════════════════

def _load_private_key(path: str):
    with open(path, "rb") as f:
        data = f.read()
    try:
        return serialization.load_pem_private_key(data, password=None)
    except Exception:
        return serialization.load_der_private_key(data, password=None)


def _sign(private_key, method: str, path: str) -> tuple:
    """Generate RSA-PSS signature for Kalshi API request."""
    ts = str(int(time.time() * 1000))
    full_path = "/trade-api/v2" + path.split("?")[0]
    msg = (ts + method.upper() + full_path).encode("utf-8")
    sig = private_key.sign(
        msg,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8"), ts


# ═══════════════════════════════════════════════════════════════
#  CLIENT
# ═══════════════════════════════════════════════════════════════

class KalshiClient:
    def __init__(self, api_key_id: str, private_key_path: str):
        self._key = _load_private_key(private_key_path)
        self._key_id = api_key_id
        self._session = requests.Session()
        log.info("Kalshi client initialized")

    def _headers(self, method: str, path: str) -> dict:
        sig, ts = _sign(self._key, method, path)
        return {
            "KALSHI-ACCESS-KEY":       self._key_id,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type":            "application/json",
        }

    def get(self, path: str, params: dict = None) -> dict:
        r = self._session.get(
            KALSHI_BASE_URL + path,
            headers=self._headers("GET", path),
            params=params,
            timeout=15,
        )
        if not r.ok:
            try:
                err_body = r.text[:500]
            except Exception:
                err_body = "(could not read body)"
            log.error(f"GET {path} → {r.status_code}: {err_body}")
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: dict) -> dict:
        r = self._session.post(
            KALSHI_BASE_URL + path,
            headers=self._headers("POST", path),
            json=body,
            timeout=15,
        )
        if not r.ok:
            try:
                err_body = r.text[:500]
            except Exception:
                err_body = "(could not read body)"
            log.error(f"POST {path} → {r.status_code}: {err_body}")
        r.raise_for_status()
        return r.json()

    # ── Balance ────────────────────────────────────────────────

    def get_balance_cents(self) -> int:
        """Cash balance in cents. Handles both legacy and _dollars migration."""
        data = self.get("/portfolio/balance")
        balance = data.get("balance")
        if balance is not None:
            return int(balance)
        # Fallback for _dollars migration
        balance_dollars = data.get("balance_dollars")
        if balance_dollars is not None:
            log.warning("Balance API returned balance_dollars (migration detected)")
            return round(float(balance_dollars) * 100)
        log.error(f"Balance API returned no recognized balance field: {list(data.keys())}")
        return 0

    # ── Markets ────────────────────────────────────────────────

    def get_market(self, ticker: str) -> dict:
        """Fetch a single market by ticker. Returns {} on failure.
        Normalizes _dollars fields to legacy integer cents."""
        try:
            market = self.get(f"/markets/{ticker}").get("market", {})
            return _normalize_market(market)
        except Exception as e:
            log.warning(f"get_market({ticker}) failed: {e}")
            return {}

    def find_next_market(self) -> dict | None:
        """
        Find the NEXT 15-min BTC market (the one that hasn't started yet
        or just started). Waits until it exists on Kalshi.
        Returns the market dict or None if something goes wrong.
        """
        et = datetime.now(ET)

        # Find the next 15-min boundary
        current_slot_end_min = ((et.minute // 15) + 1) * 15
        # The NEXT market closes one slot further
        next_close_min = current_slot_end_min + 15
        next_close_hour = et.hour
        day_offset = 0

        while next_close_min >= 60:
            next_close_min -= 60
            next_close_hour += 1
            if next_close_hour >= 24:
                next_close_hour -= 24
                day_offset += 1

        next_close_et = et.replace(
            hour=next_close_hour, minute=next_close_min,
            second=0, microsecond=0
        )
        if day_offset:
            next_close_et += timedelta(days=day_offset)

        ticker = self._build_ticker(next_close_et)
        log.info(f"Next market: {ticker} (closes {next_close_et.strftime('%H:%M ET')})")
        return self._fetch_market_safe(ticker)

    def find_current_market(self) -> dict | None:
        """Find the currently active 15-min BTC market."""
        et = datetime.now(ET)

        close_minute = ((et.minute // 15) + 1) * 15
        close_hour = et.hour
        day_offset = 0

        if close_minute >= 60:
            close_minute -= 60
            close_hour += 1
            if close_hour >= 24:
                close_hour -= 24
                day_offset = 1

        close_et = et.replace(
            hour=close_hour, minute=close_minute,
            second=0, microsecond=0
        )
        if day_offset:
            close_et += timedelta(days=1)

        ticker = self._build_ticker(close_et)
        return self._fetch_market_safe(ticker)

    def _build_ticker(self, close_et: datetime) -> str:
        """Build the Kalshi ticker string from close time in ET."""
        # Use locale-independent month abbreviation (strftime %b is locale-dependent)
        _MONTHS = ["JAN","FEB","MAR","APR","MAY","JUN",
                    "JUL","AUG","SEP","OCT","NOV","DEC"]
        mon = _MONTHS[close_et.month - 1]
        event_ticker = f"KXBTC15M-{close_et.strftime('%y')}{mon}{close_et.strftime('%d%H%M')}"
        return f"{event_ticker}-{close_et.minute:02d}"

    def _fetch_market_safe(self, ticker: str) -> dict | None:
        """Fetch market, return None if not found or already closed."""
        market = self.get_market(ticker)
        if not market.get("ticker"):
            log.warning(f"Market not found: {ticker}")
            return None

        close_str = market.get("close_time", "")
        if close_str:
            close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
            if close_dt < datetime.now(timezone.utc):
                log.warning(f"Market {ticker} already closed")
                return None

        return market

    # ── Orders ─────────────────────────────────────────────────

    def place_limit_order(self, ticker: str, side: str, count: int,
                          price_cents: int, action: str = "buy") -> dict:
        """
        Place a limit order. Returns the full API response.
        side: "yes" or "no"
        action: "buy" or "sell"
        Uses _dollars/_fp fields only (legacy integers removed for API migration).
        """
        price_dollars = f"{price_cents / 100:.4f}"
        count_fp = f"{count:.2f}"
        price_dollars_key = "yes_price_dollars" if side == "yes" else "no_price_dollars"
        resp = self.post("/portfolio/orders", {
            "ticker":       ticker,
            "action":       action,
            "side":         side,
            "count_fp":     count_fp,
            "type":         "limit",
            price_dollars_key: price_dollars,
        })
        # Normalize the order in the response
        if "order" in resp:
            resp["order"] = _normalize_order(resp["order"])
        return resp

    def get_order(self, order_id: str) -> dict:
        """Fetch order status. Returns {} on error.
        Normalizes _fp/_dollars fields to legacy integers."""
        try:
            order = self.get(f"/portfolio/orders/{order_id}").get("order", {})
            return _normalize_order(order)
        except Exception:
            return {}

    def cancel_order(self, order_id: str) -> bool:
        """Cancel order via /decrease. Returns True if no error."""
        try:
            self.post(f"/portfolio/orders/{order_id}/decrease",
                      {"reduce_by_fp": "99999.00"})
            return True
        except Exception as e:
            log.warning(f"Cancel order failed: {e}")
            return False

    # ── Order Fill Parsing ─────────────────────────────────────

    @staticmethod
    def parse_fill(order: dict) -> dict:
        """
        Extract fill details from an order response.
        Returns dict with: fill_count, cost_dollars, avg_price_c, fees_dollars
        cost_dollars = total out-of-pocket including fees.

        Prefers _fp/_dollars fields (post-migration); falls back to legacy integers.
        """
        # Normalize first in case raw order passed in
        order = _normalize_order(order)

        fill_count = order.get("fill_count", 0)
        taker_cost = order.get("taker_fill_cost", 0) or 0
        maker_cost = order.get("maker_fill_cost", 0) or 0
        taker_fees = order.get("taker_fees", 0) or 0
        maker_fees = order.get("maker_fees", 0) or 0

        contract_cost = taker_cost + maker_cost
        fees = taker_fees + maker_fees
        cost_dollars = (contract_cost + fees) / 100
        fees_dollars = fees / 100

        avg_price_c = round(contract_cost / fill_count) if fill_count > 0 else 0

        return {
            "fill_count":   fill_count,
            "cost_dollars":  round(cost_dollars, 4),
            "avg_price_c":  avg_price_c,
            "fees_dollars":  round(fees_dollars, 4),
            "contract_cost_cents": contract_cost,
        }

    # ── Polling Helpers ────────────────────────────────────────

    def poll_until_filled(self, order_id: str, target_count: int,
                          deadline: float, interval: int = 3) -> dict:
        """
        Poll order status until filled, terminal, or deadline passed.
        Returns parse_fill result.
        """
        while time.time() < deadline:
            time.sleep(interval)
            order = self.get_order(order_id)
            status = order.get("status", "")
            fc = order.get("fill_count", 0)
            log.info(f"  Poll: {status} — {fc}/{target_count} filled")
            if status in ("executed", "canceled", "expired"):
                return self.parse_fill(order)
        return self.parse_fill(self.get_order(order_id))

    def get_market_result(self, ticker: str) -> str | None:
        """Return 'yes' or 'no' once resolved, else None."""
        result = (self.get_market(ticker).get("result") or "").lower()
        return result if result in ("yes", "no") else None

    # ── Helpers ────────────────────────────────────────────────

    @staticmethod
    def get_cheaper_side(market: dict) -> tuple:
        """Return (side, ask_price_c) for the lower-ask side."""
        yes_ask = market.get("yes_ask", 99) or 99
        no_ask = market.get("no_ask", 99) or 99
        if yes_ask <= no_ask:
            return "yes", yes_ask
        return "no", no_ask

    @staticmethod
    def minutes_until_close(close_time_str: str) -> float:
        close_dt = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
        return (close_dt - datetime.now(timezone.utc)).total_seconds() / 60

    @staticmethod
    def estimate_fees(shares: int, price_c: int) -> float:
        """Estimate buy fees in dollars. Fees = 7% of contract price per contract."""
        fee_per_contract_c = max(1, round(price_c * KALSHI_FEE_RATE))
        return shares * fee_per_contract_c / 100

    @staticmethod
    def calc_shares_for_dollars(dollars: float, price_c: int) -> int:
        """How many shares can we buy for ~dollars at price_c per share?
        Rounds UP to ensure we meet or slightly exceed the target bet."""
        if price_c <= 0:
            return 1
        price_per = price_c / 100
        return max(math.ceil(dollars / price_per), 1)

    @staticmethod
    def calc_sell_price(shares: int, cost_dollars: float,
                        target_pct: float) -> int:
        """
        Calculate sell price per share (cents) to achieve target profit.

        target_pct: desired profit as % of cost (100 = double your money)
        No sell fees on Kalshi, so proceeds = shares × sell_price_c / 100.
        """
        target_dollars = cost_dollars * target_pct / 100
        needed = cost_dollars + target_dollars
        sell_c = math.ceil((needed / shares) * 100)
        return max(sell_c, 2)  # minimum 2c (Kalshi floor)

    @staticmethod
    def calc_gross(buy_filled: int, sell_filled: int,
                   sell_price_c: int, won: bool) -> float:
        """
        Gross proceeds from a completed trade.
        Sold contracts pay sell_price. Unsold contracts pay $1 if won, $0 if lost.
        """
        sold = min(sell_filled, buy_filled)
        remaining = buy_filled - sold
        return sold * sell_price_c / 100 + (remaining * 1.0 if won else 0.0)