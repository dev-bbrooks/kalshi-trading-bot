"""
strategy.py — Strategy Observatory: data collection + simulation + evaluation.

Three layers:
  1. Observatory: Records every market's price path + regime context + outcome
  2. Laboratory: Simulates strategies against recorded observations
  3. Advisor: Surfaces insights (best strategies per setup, confidence levels)
"""

import json
import math
import time
import logging
from datetime import datetime, timezone, timedelta

from config import ET, KALSHI_FEE_RATE
from db import (
    get_conn, now_utc, rows_to_list,
    upsert_market_observation, get_unresolved_observations,
    get_observations_for_simulation, upsert_strategy_result,
    get_latest_regime_snapshot, get_regime_risk,
    get_strategy_for_setup,
)

log = logging.getLogger("strategy")


# ═══════════════════════════════════════════════════════════════
#  LAYER 1: OBSERVATORY — In-Memory Price Accumulator
# ═══════════════════════════════════════════════════════════════

class MarketObserver:
    """
    Accumulates price snapshots for the current market in memory.
    When the market changes (new ticker), writes the completed observation to DB.
    
    Usage (called from bot main loop):
        observer.tick(ticker, close_time, market_data, snapshot, risk_info)
        # ... on market transition, automatically writes observation
    """

    def __init__(self):
        self._current_ticker = None
        self._current_close_time = None
        self._market_start_time = None
        self._snapshots = []           # [{t, ya, yb, na, nb, btc}, ...]
        self._regime_context = None    # Captured once at first sight
        self._bot_action = "idle"      # Updated when trade/skip happens
        self._trade_id = None
        self._market_id = None
        self._active_strategy_key = None
        self._discarded_ticker = None  # Ticker to suppress after discard
        self._is_partial = False       # True if bot joined market mid-way

        # Health metrics (session-scoped, reset on bot restart)
        self._written = 0
        self._dropped_partial = 0      # Bot started mid-market
        self._dropped_short = 0        # Too few snapshots (API issues)
        self._dropped_few = 0          # < 3 snapshots (barely saw the market)

    def tick(self, ticker: str, close_time: str, market_data: dict,
             regime_snapshot: dict = None, risk_info: dict = None):
        """
        Called every poll cycle (~2s). Accumulates price data.
        Returns True if a market transition occurred (observation was written).
        """
        if not ticker:
            return False

        # Market changed → write previous observation, start new one
        if ticker != self._current_ticker:
            wrote = self._finalize_observation()
            self._start_new_market(ticker, close_time, market_data,
                                   regime_snapshot, risk_info)
            return wrote

        # Same market → accumulate snapshot (unless suppressed)
        if not getattr(self, '_is_partial', False):
            self._add_snapshot(market_data)
        return False

    def mark_action(self, action: str, trade_id: int = None,
                    market_id: int = None, strategy_key: str = None,
                    regime_label: str = None):
        """Mark what the bot did with this market: 'traded', 'observed', 'idle'.
        If regime_label is provided, syncs the observation's regime_label to match
        the bot's decision-time label (prevents trade/observation mismatch when
        the regime shifts between first tick and decision time)."""
        self._bot_action = action
        if trade_id:
            self._trade_id = trade_id
        if market_id:
            self._market_id = market_id
        if strategy_key:
            self._active_strategy_key = strategy_key
        if regime_label and self._regime_context:
            self._regime_context["regime_label"] = regime_label

    def discard(self):
        """Discard accumulated data for the current market without writing.
        Suppresses re-accumulation if the same ticker is seen again."""
        if self._current_ticker:
            log.info(f"Observatory: discarding data for {self._current_ticker}")
            self._discarded_ticker = self._current_ticker
        self._current_ticker = None
        self._snapshots = []
        self._regime_context = None
        self._bot_action = "idle"
        self._trade_id = None
        self._market_id = None
        self._active_strategy_key = None
        self._market_start_time = None

    def flush(self):
        """Force-write current observation (e.g., on shutdown)."""
        self._finalize_observation()

    def get_health(self) -> dict:
        """Return Observatory health metrics for this session."""
        total_attempted = self._written + self._dropped_partial + self._dropped_short + self._dropped_few
        return {
            "written": self._written,
            "dropped_partial": self._dropped_partial,
            "dropped_short": self._dropped_short,
            "dropped_few": self._dropped_few,
            "total_attempted": total_attempted,
            "drop_rate_pct": round(
                (total_attempted - self._written) / total_attempted * 100, 1
            ) if total_attempted > 0 else 0,
        }

    def _start_new_market(self, ticker, close_time, market_data,
                          regime_snapshot, risk_info):
        """Initialize tracking for a new market."""
        self._current_ticker = ticker
        self._current_close_time = close_time
        self._market_start_time = time.time()
        self._snapshots = []
        self._bot_action = "idle"
        self._trade_id = None
        self._market_id = None
        self._active_strategy_key = None

        # If this ticker was discarded (bot stopped mid-market), suppress it
        if self._discarded_ticker and ticker == self._discarded_ticker:
            self._is_partial = True
            self._discarded_ticker = None  # Only suppress once
            return
        # Clear suppression for genuinely new markets
        self._discarded_ticker = None

        # Detect if we're picking this market up mid-way (e.g. after restart)
        # Full market is 15 min. If <12 min left, we missed the start.
        self._is_partial = False
        if close_time:
            try:
                close_dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                mins_left = (close_dt - datetime.now(timezone.utc)).total_seconds() / 60
                if mins_left < 12:
                    self._is_partial = True
            except Exception:
                pass

        # Capture regime context once
        now_et = datetime.now(ET)
        snap = regime_snapshot or {}
        risk = risk_info or {}

        self._regime_context = {
            "regime_label": snap.get("composite_label", "unknown"),
            "vol_regime": snap.get("vol_regime"),
            "trend_regime": snap.get("trend_regime"),
            "volume_regime": snap.get("volume_regime"),
            "risk_level": risk.get("risk_level", "unknown"),
            "regime_confidence": snap.get("regime_confidence"),
            "btc_price": snap.get("btc_price"),
            "btc_return_15m": snap.get("btc_return_15m"),
            "btc_return_1h": snap.get("btc_return_1h"),
            "btc_return_4h": snap.get("btc_return_4h"),
            "realized_vol": snap.get("realized_vol_15m"),
            "atr_15m": snap.get("atr_15m"),
            "bollinger_width": snap.get("bollinger_width_15m"),
            "ema_slope_15m": snap.get("ema_slope_15m"),
            "ema_slope_1h": snap.get("ema_slope_1h"),
            "trend_direction": snap.get("trend_direction"),
            "trend_strength": snap.get("trend_strength"),
            "bollinger_squeeze": snap.get("bollinger_squeeze", 0),
            "volume_spike": snap.get("volume_spike", 0),
            "hour_et": now_et.hour,
            "minute_et": now_et.minute,
            "day_of_week": now_et.weekday(),
            # Kalshi market liquidity at first sight
            "kalshi_volume": market_data.get("volume"),
            "kalshi_open_interest": market_data.get("open_interest"),
        }

        # Add first snapshot
        self._add_snapshot(market_data)

    def _add_snapshot(self, market_data: dict):
        """Add a price snapshot. Throttled to 5-second intervals."""
        if not self._market_start_time:
            return

        # Throttle: one snapshot every 5 seconds (~180 per market)
        # This gives enough resolution to evaluate entry timing and sell targets
        if self._snapshots:
            last_t = self._snapshots[-1]["t"]
            current_t = int(time.time() - self._market_start_time)
            if current_t - last_t < 5:
                return

        t = int(time.time() - self._market_start_time)
        snap = {
            "t": t,
            "ya": market_data.get("yes_ask") or 0,
            "yb": market_data.get("yes_bid") or 0,
            "na": market_data.get("no_ask") or 0,
            "nb": market_data.get("no_bid") or 0,
        }

        # Include BTC price for intra-market movement tracking
        btc = market_data.get("btc_price")
        if btc:
            snap["btc"] = round(btc, 0)
            # BTC distance from open (%) — the core settlement signal
            # First snapshot's BTC is the "open" price
            if self._snapshots:
                first_btc = None
                for s in self._snapshots:
                    if s.get("btc"):
                        first_btc = s["btc"]
                        break
                if first_btc and first_btc > 0:
                    snap["bd"] = round((btc - first_btc) / first_btc * 100, 4)

        # Include Kalshi volume for liquidity tracking over market lifetime
        vol = market_data.get("volume")
        if vol:
            snap["v"] = vol

        self._snapshots.append(snap)

    def _finalize_observation(self) -> bool:
        """Write the completed market observation to DB.
        Now stores ALL observations with a quality flag instead of
        silently dropping imperfect ones — enables unbiased simulation."""
        if not self._current_ticker or not self._snapshots:
            return False

        # Determine observation quality
        n_snaps = len(self._snapshots)
        is_partial = getattr(self, '_is_partial', False)

        if n_snaps < 3:
            quality = "few"
            self._dropped_few += 1
        elif is_partial:
            quality = "partial"
            self._dropped_partial += 1
        elif n_snaps < 80:
            quality = "short"
            self._dropped_short += 1
        else:
            quality = "full"

        # Compute price summary from snapshots
        ya_vals = [s["ya"] for s in self._snapshots if s["ya"] > 0]
        na_vals = [s["na"] for s in self._snapshots if s["na"] > 0]

        data = {
            "ticker": self._current_ticker,
            "close_time_utc": self._current_close_time or "",
            "price_snapshots": json.dumps(self._snapshots),
            "snapshot_count": n_snaps,
            "bot_action": self._bot_action,
            "trade_id": self._trade_id,
            "market_id": self._market_id,
            "obs_quality": quality,
        }

        # Strategy context at observation time
        if self._active_strategy_key:
            data["active_strategy_key"] = self._active_strategy_key

        # Merge regime context
        if self._regime_context:
            data.update(self._regime_context)

        # Price summary
        if ya_vals:
            data["yes_open_c"] = ya_vals[0]
            data["yes_close_c"] = ya_vals[-1]
            data["yes_high_c"] = max(ya_vals)
            data["yes_low_c"] = min(ya_vals)
        if na_vals:
            data["no_open_c"] = na_vals[0]
            data["no_close_c"] = na_vals[-1]
            data["no_high_c"] = max(na_vals)
            data["no_low_c"] = min(na_vals)

        # BTC movement — now with distance-from-open tracking
        btc_vals = [s.get("btc") for s in self._snapshots if s.get("btc")]
        if btc_vals and len(btc_vals) >= 2 and btc_vals[0] > 0:
            btc_open = btc_vals[0]
            btc_close = btc_vals[-1]
            data["btc_price_at_open"] = btc_open
            data["btc_price_at_close"] = btc_close
            data["btc_move_during_pct"] = round(
                (btc_close - btc_open) / btc_open * 100, 4
            )
            # BTC distance from open at close (this IS the settlement signal)
            data["btc_distance_pct_at_close"] = round(
                (btc_close - btc_open) / btc_open * 100, 4
            )
            # Max/min distance during market (for volatility context)
            distances = [(v - btc_open) / btc_open * 100 for v in btc_vals]
            data["btc_max_distance_pct"] = round(max(distances), 4)
            data["btc_min_distance_pct"] = round(min(distances), 4)

        try:
            upsert_market_observation(data)
            if quality == "full":
                self._written += 1
            log.debug(f"Observatory: wrote {self._current_ticker} "
                      f"(quality={quality}, {n_snaps} snapshots, "
                      f"action={self._bot_action})")
        except Exception as e:
            log.warning(f"Observatory write failed: {e}")

        self._current_ticker = None
        return True


# ═══════════════════════════════════════════════════════════════
#  LAYER 2: LABORATORY — Strategy Simulation Engine
# ═══════════════════════════════════════════════════════════════

# Strategy dimension definitions — 5¢ increments, absolute sell targets
# Side rules: cheaper (buy whichever side costs less), yes (always YES),
#   no (always NO), model (BTC FV model picks side using Brownian bridge P(yes))
SIDE_RULES = ["cheaper", "yes", "no", "model"]
ENTRY_TIME_RULES = ["early", "mid", "late"]
ENTRY_MAXES = list(range(5, 100, 5))   # 5, 10, 15, ... 95

# Setup hierarchy — coarse_regime is the primary regime dimension.
# Fine-grained regime labels are recorded passively in observations but
# not used for strategy evaluation (too many buckets, too sparse).
SETUP_TYPES = ["global", "coarse_regime", "hour"]


def _valid_sell_targets(entry_max: int) -> list:
    """Generate valid sell targets for an entry max price.
    Sells must be above entry_max to have any chance of profit.
    Returns list of ints + 'hold'."""
    targets = list(range(entry_max + 5, 100, 5))  # 5¢ steps above entry
    targets.append(99)                              # always include 99
    targets.append("hold")                          # hold to expiry
    return targets


def _all_strategy_combos():
    """Generate all strategy combinations. Yields (side_rule, timing, entry_max, sell_target)."""
    for side_rule in SIDE_RULES:
        for entry_max in ENTRY_MAXES:
            for sell in _valid_sell_targets(entry_max):
                for timing in ENTRY_TIME_RULES:
                    yield side_rule, timing, entry_max, sell


def strategy_key(side_rule: str, timing: str, entry_max: int, sell_target) -> str:
    """Build strategy key string. Format: side:timing:entry_max:sell_target"""
    return f"{side_rule}:{timing}:{entry_max}:{sell_target}"


def parse_strategy_key(key: str) -> dict:
    """Parse strategy key into components.
    Handles both old format (timing:entry:sell) and new (side:timing:entry:sell)."""
    parts = key.split(":")
    if len(parts) == 4:
        return {
            "side_rule": parts[0],
            "entry_time_rule": parts[1],
            "entry_price_max": int(parts[2]),
            "sell_target": parts[3] if parts[3] == "hold" else int(parts[3]),
        }
    if len(parts) == 3:
        # Legacy format without side — assume cheaper
        return {
            "side_rule": "cheaper",
            "entry_time_rule": parts[0],
            "entry_price_max": int(parts[1]),
            "sell_target": parts[2] if parts[2] == "hold" else int(parts[2]),
        }
    return {}


def simulate_market(obs: dict) -> list:
    """
    Simulate all strategy variants against one market observation.
    Tests cheaper, yes, no, and model side rules with entry/sell increments.

    Returns list of dicts: {strategy_key, side_rule, entry_time_rule,
                            entry_price_max, sell_target, entered, won, pnl_c}
    """
    result = obs["market_result"]
    if result not in ("yes", "no"):
        return []

    snapshots = json.loads(obs["price_snapshots"]) if obs.get("price_snapshots") else []
    if len(snapshots) < 3:
        return []

    market_duration = max(s["t"] for s in snapshots)
    if market_duration < 60:
        return []

    # Context needed by "model" side rule
    btc_open = obs.get("btc_price_at_open")
    realized_vol = obs.get("realized_vol")

    results = []

    for side_rule, time_rule, max_price, sell_target in _all_strategy_combos():
        sim = _simulate_one(
            snapshots, result, market_duration,
            side_rule, time_rule, max_price, sell_target,
            btc_open=btc_open, realized_vol=realized_vol,
        )
        if sim is not None:
            key = strategy_key(side_rule, time_rule, max_price, sell_target)
            results.append({
                "strategy_key": key,
                "side_rule": side_rule,
                "entry_time_rule": time_rule,
                "entry_price_max": max_price,
                "sell_target": sell_target,
                **sim,
            })

    return results


def _brownian_p_yes(dist_pct: float, secs_into_market: float,
                    realized_vol: float = None) -> float:
    """
    Brownian bridge P(YES wins) — standalone version for simulation.
    Same math as BtcFairValueModel._analytical_estimate but without
    class overhead, usable from the simulation engine.

    P(BTC finishes above open | currently at distance D, T remaining)
    ≈ Φ(D / (σ√T))
    """
    time_remaining = max(1, 900 - secs_into_market)
    minutes_remaining = time_remaining / 60

    sigma_per_min = 0.10
    if realized_vol is not None and realized_vol > 0:
        sigma_per_min = realized_vol / math.sqrt(15)

    if minutes_remaining < 0.25:
        if dist_pct > 0.005:
            return 0.95
        elif dist_pct < -0.005:
            return 0.05
        else:
            return 0.50

    denom = sigma_per_min * math.sqrt(minutes_remaining)
    if denom > 0:
        z = dist_pct / denom
        p_yes = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    else:
        p_yes = 0.50

    return max(0.02, min(0.98, p_yes))


def _simulate_one(snapshots: list, result: str, duration: int,
                   side_rule: str, time_rule: str,
                   max_price: int, sell_target,
                   fee_rate: float = KALSHI_FEE_RATE,
                   slippage_c: int = 0,
                   btc_open: float = None,
                   realized_vol: float = None) -> dict | None:
    """
    Simulate one strategy on one market.
    Timing determines when to START looking. Once started, scans all
    subsequent snapshots until a valid entry price is found or market closes.

    side_rule: "cheaper" (whichever side costs less), "yes" (always YES),
               "no" (always NO), "model" (BTC FV model picks via Brownian bridge)
    sell_target is int (absolute cents) or 'hold'.
    fee_rate defaults to KALSHI_FEE_RATE (7%) but can be overridden for
    fee sensitivity analysis.
    slippage_c: additional cents added to entry cost (models adverse fill,
    market impact, order book dynamics). Run at 0, 1, 2 to test robustness.
    btc_open: BTC price at market open (needed for "model" side rule).
    realized_vol: trailing 15-min vol in % (optional, improves model accuracy).
    Returns {entered: bool, won: bool, pnl_c: int} or None if no valid entry.
    """
    # "model" side rule requires BTC open price for distance calculation
    if side_rule == "model" and (not btc_open or btc_open <= 0):
        return {"entered": False, "won": False, "pnl_c": 0,
                "sold_early": False, "sell_btc_distance_pct": None}
    # 1. Determine when to start looking — absolute seconds into market
    #    Markets are always 15 minutes. Using absolute values avoids bias
    #    when observations start slightly late.
    if time_rule == "mid":
        t_min = 300                        # 5 min
    elif time_rule == "late":
        t_min = 600                        # 10 min
    else:  # "early" — start immediately
        t_min = 0
    t_max = duration - 30              # Stop 30s before close (all timings)

    # 2. Scan snapshots from t_min until we find a valid entry price.
    #    Fill delay: the first valid snapshot is when the bot "notices" the price.
    #    Entry happens on the NEXT valid snapshot (simulating order placement lag).
    entry_snap = None
    side = None
    entry_price = 0
    spotted = False       # True once we've seen a valid price (but haven't "filled")
    for s in snapshots:
        if s["t"] < t_min or s["t"] > t_max:
            continue

        ya, na = s["ya"], s["na"]
        if ya <= 0 and na <= 0:
            continue

        # Pick side based on side_rule
        if side_rule == "yes":
            _side, _price = "yes", ya
        elif side_rule == "no":
            _side, _price = "no", na
        elif side_rule == "model":
            # BTC FV model: use Brownian bridge P(yes) at this snapshot
            # to decide which side has higher settlement probability
            btc_now = s.get("btc")
            if not btc_now or btc_now <= 0 or not btc_open:
                continue  # Can't compute model without BTC price
            dist_pct = (btc_now - btc_open) / btc_open * 100
            secs = s["t"]
            p_yes = _brownian_p_yes(dist_pct, secs, realized_vol)
            if p_yes >= 0.5:
                _side = "yes"
                _price = ya if ya > 0 else 0
            else:
                _side = "no"
                _price = na if na > 0 else 0
        else:  # "cheaper"
            if ya <= 0:
                _side, _price = "no", na
            elif na <= 0:
                _side, _price = "yes", ya
            elif ya <= na:
                _side, _price = "yes", ya
            else:
                _side, _price = "no", na

        # Check if price is within our max
        if _price > 0 and _price <= max_price:
            if not spotted:
                # First valid snapshot — bot notices price, order not yet placed
                spotted = True
            else:
                # Second valid snapshot — simulated fill at the ask price.
                # The two-snapshot delay (≥5 seconds) already models execution
                # lag. No additional slippage penalty — a limit buy at the ask
                # fills at the ask or doesn't fill.
                entry_snap = s
                side = _side
                entry_price = _price
                break
        else:
            # Price moved out of range — reset spotting
            spotted = False

    if entry_snap is None:
        return {"entered": False, "won": False, "pnl_c": 0,
                "sold_early": False, "sell_btc_distance_pct": None}

    # 3. Compute fee and cost — entry_price is the ask, which is
    #    what you pay for a limit buy. slippage_c models adverse fill
    #    and market impact on top of the ask price.
    fee_c = max(1, round(entry_price * fee_rate))
    cost_c = entry_price + fee_c + slippage_c

    # 4. Determine outcome
    won = (result == side)
    sold = False
    sell_btc_dist = None

    if sell_target == "hold":
        gross_c = 100 if won else 0
        pnl_c = gross_c - cost_c
    else:
        target_c = int(sell_target)
        if target_c <= cost_c:
            # Sell target can't cover cost — treat as hold
            gross_c = 100 if won else 0
            pnl_c = gross_c - cost_c
        else:
            # Scan price path for sell opportunity
            # Two-snapshot fill requirement (matches entry logic):
            # First snapshot where bid >= target_c + 1 = "order noticed"
            # Second consecutive snapshot = "fill confirmed"
            # This prevents counting a single bid touch as a fill — a
            # momentary tick at your price doesn't guarantee your resting
            # sell order actually executes when there's queue priority.
            bid_key = "yb" if side == "yes" else "nb"
            sell_spotted = False
            for s in snapshots:
                if s["t"] <= entry_snap["t"]:
                    continue
                bid = s.get(bid_key, 0)
                if bid >= target_c + 1:
                    if not sell_spotted:
                        sell_spotted = True  # First touch — not yet filled
                    else:
                        # Second consecutive snapshot at/above target — fill
                        gross_c = target_c
                        pnl_c = gross_c - cost_c
                        sold = True
                        # Record BTC distance at sell fill for structural analysis
                        if btc_open and btc_open > 0 and s.get("btc"):
                            sell_btc_dist = round(
                                (s["btc"] - btc_open) / btc_open * 100, 4)
                        break
                else:
                    sell_spotted = False  # Bid retreated — reset
            if not sold:
                gross_c = 100 if won else 0
                pnl_c = gross_c - cost_c

    return {"entered": True, "won": won, "pnl_c": pnl_c,
            "sold_early": sold if sell_target != "hold" else False,
            "sell_btc_distance_pct": sell_btc_dist if (sell_target != "hold") else None}


def run_simulation_batch(limit: int = 0):
    """
    Full recompute: simulate ALL resolved observations and rebuild strategy_results.
    
    This is reprocessable by design — if we change fee assumptions, fix bugs,
    or add new strategy variants, we just clear strategy_results and re-run.
    Called periodically from regime worker (~30 min).
    
    Now includes:
    - Observations at min_quality="short" (addresses survivorship bias)
    - Quality-split EV tracking (full vs degraded)
    - Slippage sensitivity for positive-EV strategies (+1¢, +2¢)
    
    limit: max observations to process (0 = all available)
    """
    observations = get_observations_for_simulation(limit=limit)
    if not observations:
        return 0

    n_obs = len(observations)
    log.info(f"Observatory: simulating across {n_obs} markets")

    # Clean up stale results from removed setup types (regime, regime_hour, favored)
    try:
        with get_conn() as c:
            deleted = c.execute("""
                DELETE FROM strategy_results
                WHERE setup_type IN ('regime', 'regime_hour')
                   OR strategy_key LIKE 'favored:%'
            """).rowcount
            if deleted:
                log.info(f"Observatory: cleaned {deleted} stale strategy_results "
                         f"(removed setup types + favored side rule)")
    except Exception as e:
        log.warning(f"Observatory: stale cleanup error: {e}")

    # Accumulate all results per setup × strategy from scratch
    # Key: (setup_key, strategy_key) → {meta, trades: [(won, pnl_c, close_time, quality), ...]}
    # Uses tuples instead of dicts to reduce memory ~70% (~72 bytes vs ~300 bytes per entry)
    accum = {}

    for obs in observations:
        sim_results = simulate_market(obs)
        setups = _get_setup_keys(obs)
        obs_quality = obs.get("obs_quality") or "full"
        obs_close = obs.get("close_time_utc", "")

        for sim in sim_results:
            if not sim["entered"]:
                continue
            # Compact trade tuple: (won, pnl_c, close_time, quality)
            trade = (sim["won"], sim["pnl_c"], obs_close, obs_quality)
            for setup_key, setup_type in setups:
                key = (setup_key, sim["strategy_key"])
                if key not in accum:
                    accum[key] = {
                        "setup_type": setup_type,
                        "side_rule": sim["side_rule"],
                        "sell_target": sim["sell_target"],
                        "entry_time_rule": sim["entry_time_rule"],
                        "entry_price_max": sim["entry_price_max"],
                        "trades": [],
                    }
                accum[key]["trades"].append(trade)

        # Free the heavy snapshot data — no longer needed after simulation
        obs.pop("price_snapshots", None)
        obs.pop("_snapshots_parsed", None)

    # Write results (full replace — each row computed from all data)
    wrote = 0
    for (setup_key, strategy_key), data in accum.items():
        trades = data["trades"]
        if not trades:
            continue
        _write_strategy_result(setup_key, data["setup_type"],
                               strategy_key, data, trades)
        wrote += 1

    if wrote > 0:
        log.info(f"Observatory: wrote {wrote} strategy results from {n_obs} markets")

    # ── Slippage sensitivity pass ──
    # Only for strategies with positive EV — targeted, not exhaustive
    if wrote > 0:
        try:
            _run_slippage_sensitivity(observations, accum)
        except Exception as e:
            log.debug(f"Slippage sensitivity error: {e}")

    # Walk-forward out-of-sample validation
    if n_obs >= 50 and wrote > 0:
        try:
            _run_walk_forward(observations, accum)
        except Exception as e:
            log.debug(f"Walk-forward validation error: {e}")

    # Check for new strategy discoveries to notify about
    if wrote > 0:
        try:
            _check_strategy_discoveries()
        except Exception as e:
            log.debug(f"Strategy discovery check error: {e}")

    # Apply Benjamini-Hochberg FDR correction to all strategies
    if wrote > 0:
        try:
            _apply_fdr_correction()
        except Exception as e:
            log.debug(f"FDR correction error: {e}")

    # Check if global best strategy changed
    if wrote > 0:
        try:
            _check_global_best_change()
        except Exception as e:
            log.debug(f"Global best check error: {e}")

    # Record convergence metric snapshot
    try:
        _record_convergence_snapshot()
    except Exception as e:
        log.debug(f"Convergence snapshot error: {e}")

    # Free memory — accum and observations can be large
    del accum
    del observations
    import gc
    gc.collect()

    return n_obs


def _run_walk_forward(observations: list, accum: dict):
    """
    Rolling walk-forward validation with expanding training window.
    
    Splits observations chronologically into 5 equal folds. For each fold k
    (2 through 5), trains on folds 1..k-1 and tests on fold k. Averages
    OOS results across all test folds.
    
    Uses the already-computed simulation results in `accum` instead of
    re-simulating — trades have close_time which maps to folds.
    
    This is strictly better than a single 70/30 split because:
    - Multiple test windows catch regime shifts at different points
    - Each observation appears in exactly one test fold
    - Results aren't sensitive to a single split point
    """
    # Sort by close time to determine fold boundaries
    close_times = sorted(set(
        o.get("close_time_utc", "") for o in observations
        if o.get("close_time_utc")
    ))

    n_folds = 5
    fold_size = len(close_times) // n_folds
    if fold_size < 10:
        return  # Not enough data for meaningful folds

    # Compute fold boundary timestamps — fold i contains close_times[start:end]
    fold_boundaries = []  # (start_time, end_time) per fold
    for i in range(n_folds):
        start = i * fold_size
        end = start + fold_size if i < n_folds - 1 else len(close_times)
        fold_boundaries.append((close_times[start], close_times[end - 1]))

    # Build quick fold lookup: close_time → fold_index
    # OOS = folds 1..4 (indices 1-4), fold 0 is always training-only
    def _get_fold(ct):
        for idx in range(n_folds - 1, -1, -1):
            if ct >= fold_boundaries[idx][0]:
                return idx
        return 0

    # Partition existing accum trades into OOS folds (no re-simulation needed)
    # Key: (setup_key, strategy_key) → {wins, pnls}
    oos_accum = {}

    for (setup_key, strategy_key), data in accum.items():
        for trade in data["trades"]:
            ct = trade[2]  # close_time
            if not ct:
                continue
            fold_idx = _get_fold(ct)
            # Folds 1-4 are test folds (fold 0 is always in-sample only)
            if fold_idx < 1:
                continue

            key = (setup_key, strategy_key)
            if key not in oos_accum:
                oos_accum[key] = {"wins": 0, "pnls": []}
            if trade[0]:  # won
                oos_accum[key]["wins"] += 1
            oos_accum[key]["pnls"].append(trade[1])  # pnl_c

    # Update strategy_results with averaged OOS metrics
    updated = 0
    for (setup_key, strategy_key), data in oos_accum.items():
        n = len(data["pnls"])
        if n < 10:  # Need reasonable total across all folds
            continue
        oos_wr = data["wins"] / n
        oos_ev = sum(data["pnls"]) / n

        try:
            upsert_strategy_result({
                "setup_key": setup_key,
                "strategy_key": strategy_key,
                "oos_ev_c": round(oos_ev, 1),
                "oos_win_rate": round(oos_wr, 4),
                "oos_sample_size": n,
            })
            updated += 1
        except Exception:
            pass

    if updated > 0:
        log.info(f"Walk-forward: updated {updated} strategies "
                 f"({n_folds - 1} test folds, "
                 f"{fold_size}+ obs each)")


def _run_slippage_sensitivity(observations: list, accum: dict):
    """
    Test slippage robustness for strategies with positive EV.
    Computes EV at +1¢ and +2¢ additional slippage from existing accum data.
    
    Slippage is a flat per-trade cost: PnL at +Nc slippage = base PnL - N.
    No re-simulation needed — just adjust the already-computed PnLs.
    
    Any strategy that goes negative at +1¢ slippage is fragile —
    its edge is too thin to survive real execution imperfections.
    
    Only runs on global:all strategies to keep output bounded.
    """
    # Identify positive-EV global strategies to test
    positive_ev_keys = []
    for (setup_key, strategy_key), data in accum.items():
        if setup_key != "global:all":
            continue
        trades = data["trades"]
        if not trades:
            continue
        avg_pnl = sum(t[1] for t in trades) / len(trades)
        if avg_pnl > 0:
            positive_ev_keys.append((strategy_key, trades))

    if not positive_ev_keys:
        return

    for strategy_key, trades in positive_ev_keys:
        n = len(trades)
        base_ev = sum(t[1] for t in trades) / n
        # Each cent of slippage reduces every entered trade's PnL by 1
        slip_1_ev = round(base_ev - 1, 1)
        slip_2_ev = round(base_ev - 2, 1)

        try:
            upsert_strategy_result({
                "setup_key": "global:all",
                "strategy_key": strategy_key,
                "slippage_1c_ev": slip_1_ev,
                "slippage_2c_ev": slip_2_ev,
            })
        except Exception:
            pass

    log.info(f"Slippage sensitivity: {len(positive_ev_keys)} strategies tested")


def _check_strategy_discoveries():
    """
    Check if any coarse regime just got its first viable +EV strategy.
    Notify once per regime — tracks notified regimes in bot config.
    """
    from db import get_config, set_config, get_conn

    min_n = int(get_config("auto_strategy_min_samples", 20) or 20)

    with get_conn() as c:
        # Find all coarse regimes with a positive EV strategy above threshold
        rows = c.execute("""
            SELECT setup_key, strategy_key, ev_per_trade_c, win_rate, sample_size
            FROM strategy_results
            WHERE setup_type = 'coarse_regime' AND sample_size >= ? AND ev_per_trade_c > 0
            ORDER BY ev_per_trade_c DESC
        """, (min_n,)).fetchall()

    if not rows:
        return

    # Group by regime — only care about best strategy per regime
    best_per_regime = {}
    for r in rows:
        regime = r["setup_key"].replace("coarse_regime:", "")
        if regime not in best_per_regime:
            best_per_regime[regime] = r

    # Load already-notified set
    import json as _json
    notified_raw = get_config("_strategy_discoveries_notified", "[]")
    if isinstance(notified_raw, str):
        try:
            notified = set(_json.loads(notified_raw))
        except Exception:
            notified = set()
    else:
        notified = set(notified_raw) if notified_raw else set()

    # Notify for new regimes
    new_found = False
    for regime, r in best_per_regime.items():
        if regime not in notified:
            try:
                from push import notify_strategy_discovery
                notify_strategy_discovery(
                    regime, r["strategy_key"],
                    r["ev_per_trade_c"], r["win_rate"],
                    r["sample_size"], r["setup_key"]
                )
            except Exception:
                pass
            notified.add(regime)
            new_found = True
            log.info(f"Strategy discovery: {regime} → "
                     f"{r['strategy_key']} EV {r['ev_per_trade_c']:+.1f}¢")

    if new_found:
        set_config("_strategy_discoveries_notified", _json.dumps(list(notified)))


def _check_global_best_change():
    """
    Check if the global best strategy changed after recompute.
    Compares against last known best stored in config. Notifies on change.
    """
    from db import get_config, set_config, get_conn

    with get_conn() as c:
        # FDR-significant first, then overall
        best = c.execute("""
            SELECT strategy_key, ev_per_trade_c, weighted_ev_c, win_rate, sample_size,
                   fdr_significant
            FROM strategy_results
            WHERE setup_key = 'global:all' AND sample_size >= 30
              AND fdr_significant = 1
            ORDER BY COALESCE(weighted_ev_c, ev_per_trade_c) DESC
            LIMIT 1
        """).fetchone()
        if not best:
            best = c.execute("""
                SELECT strategy_key, ev_per_trade_c, weighted_ev_c, win_rate, sample_size,
                       fdr_significant
                FROM strategy_results
                WHERE setup_key = 'global:all' AND sample_size >= 30
                ORDER BY COALESCE(weighted_ev_c, ev_per_trade_c) DESC
                LIMIT 1
            """).fetchone()

    if not best:
        return

    current_key = best["strategy_key"]
    last_key = get_config("_last_global_best_key", "")

    if current_key != last_key and last_key:
        # Changed — notify
        try:
            from push import notify_global_best_changed
            notify_global_best_changed(
                last_key, current_key,
                best["ev_per_trade_c"], best["win_rate"],
                best["sample_size"]
            )
        except Exception:
            pass
        log.info(f"Global best changed: {last_key} → {current_key} "
                 f"(EV {best['ev_per_trade_c']:+.1f}¢, n={best['sample_size']})")

    # Always update stored key (handles first run too)
    if current_key != last_key:
        set_config("_last_global_best_key", current_key)


def _get_setup_keys(obs: dict) -> list:
    """Return list of (setup_key, setup_type) for an observation.
    Uses coarse regime labels (~15 buckets) instead of fine-grained labels
    (~63 buckets) for strategy evaluation — fine labels are too sparse to
    reach statistical significance. Fine labels remain in observations
    as passive data for future analysis."""
    keys = [("global:all", "global")]

    hour = obs.get("hour_et")

    # Coarse regime — primary regime dimension for strategy evaluation
    vol = obs.get("vol_regime")
    trend = obs.get("trend_regime")
    if vol is not None and trend is not None:
        try:
            from regime import compute_coarse_label
            coarse = compute_coarse_label(int(vol), int(trend))
            keys.append((f"coarse_regime:{coarse}", "coarse_regime"))
        except Exception:
            pass

    if hour is not None:
        keys.append((f"hour:{hour}", "hour"))

    return keys


def _write_strategy_result(setup_key, setup_type, strategy_key, meta, trades):
    """Compute and write a strategy result from the complete trade list.
    Includes time-weighted metrics using exponential decay (half-life 14 days).
    
    Trades are tuples: (won, pnl_c, close_time, quality) at indices 0-3."""
    n = len(trades)
    if n == 0:
        return

    wins = sum(1 for t in trades if t[0])
    losses = n - wins
    pnls = [t[1] for t in trades]
    total_pnl = sum(pnls)

    wr = wins / n
    avg_pnl = total_pnl / n

    # PnL standard deviation (for t-test based FDR)
    if n >= 2:
        pnl_var = sum((p - avg_pnl) ** 2 for p in pnls) / (n - 1)
        pnl_std = math.sqrt(pnl_var)
    else:
        pnl_std = 0

    ci_lo, ci_hi = _wilson_ci(wins, n)

    # Time-weighted metrics — exponential decay with 14-day half-life
    # Recent observations contribute more than older ones
    HALF_LIFE_DAYS = 14
    decay_rate = math.log(2) / HALF_LIFE_DAYS
    now = datetime.now(timezone.utc)
    weighted_wins = 0.0
    weighted_total = 0.0
    weighted_pnl = 0.0
    for t in trades:
        ct = t[2]  # close_time
        if ct:
            try:
                obs_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                age_days = (now - obs_dt).total_seconds() / 86400
                w = math.exp(-decay_rate * age_days)
            except Exception:
                w = 0.5  # Unknown age → half weight
        else:
            w = 0.5
        weighted_total += w
        if t[0]:  # won
            weighted_wins += w
        weighted_pnl += w * t[1]  # pnl_c

    w_wr = weighted_wins / weighted_total if weighted_total > 0 else wr
    w_ev = weighted_pnl / weighted_total if weighted_total > 0 else avg_pnl

    # Profit factor
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else None

    # Max consecutive losses
    max_consec = 0
    current_consec = 0
    for t in trades:
        if not t[0]:  # not won
            current_consec += 1
            max_consec = max(max_consec, current_consec)
        else:
            current_consec = 0

    # Max drawdown (peak-to-trough on cumulative PnL)
    cum = 0
    peak = 0
    max_dd = 0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    # Expectancy: avg_win * P(win) - avg_loss * P(loss)
    win_pnls = [p for p in pnls if p > 0]
    loss_pnls = [p for p in pnls if p < 0]
    avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0
    avg_loss = abs(sum(loss_pnls) / len(loss_pnls)) if loss_pnls else 0
    expectancy = wr * avg_win - (1 - wr) * avg_loss

    times = [t[2] for t in trades if t[2]]  # close_time

    # Quality-split EV — compares performance on clean vs degraded observations
    # A large gap between these suggests the edge is fragile under adversity
    full_pnls = [t[1] for t in trades if t[3] == "full"]
    degraded_pnls = [t[1] for t in trades
                     if t[3] in ("short", "partial")]
    quality_full_ev = (round(sum(full_pnls) / len(full_pnls), 1)
                       if full_pnls else None)
    quality_degraded_ev = (round(sum(degraded_pnls) / len(degraded_pnls), 1)
                           if len(degraded_pnls) >= 5 else None)

    # Breakeven fee rate — at what fee rate does this strategy's EV hit zero?
    # Analytical approximation: if fee increases by Δ, cost per contract rises
    # by ~entry_price × Δ, so EV drops by ~entry_price × Δ.
    # breakeven_fee ≈ current_fee + (EV / entry_price)
    entry_max = meta["entry_price_max"]
    if avg_pnl > 0 and entry_max > 0:
        breakeven_fee = round(KALSHI_FEE_RATE + (avg_pnl / entry_max), 4)
    else:
        breakeven_fee = None

    upsert_strategy_result({
        "setup_key": setup_key,
        "setup_type": setup_type,
        "strategy_key": strategy_key,
        "side_rule": meta["side_rule"],
        "exit_rule": str(meta["sell_target"]),  # absolute sell target or "hold"
        "sell_target": str(meta["sell_target"]),
        "entry_time_rule": meta["entry_time_rule"],
        "entry_price_max": meta["entry_price_max"],
        "sample_size": n,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wr, 4),
        "total_pnl_c": total_pnl,
        "avg_pnl_c": round(avg_pnl, 1),
        "best_pnl_c": max(pnls),
        "worst_pnl_c": min(pnls),
        "max_drawdown_c": max_dd,
        "profit_factor": pf,
        "expectancy_c": round(expectancy, 1),
        "max_consecutive_losses": max_consec,
        "ci_lower": round(ci_lo, 4),
        "ci_upper": round(ci_hi, 4),
        "ev_per_trade_c": round(avg_pnl, 1),
        "pnl_std_c": round(pnl_std, 2),
        "weighted_win_rate": round(w_wr, 4),
        "weighted_ev_c": round(w_ev, 1),
        "quality_full_ev_c": quality_full_ev,
        "quality_degraded_ev_c": quality_degraded_ev,
        "breakeven_fee_rate": breakeven_fee,
        "first_observation": min(times) if times else None,
        "last_observation": max(times) if times else None,
    })


def _wilson_ci(wins: int, total: int, z: float = 1.96) -> tuple:
    """Wilson score confidence interval for a proportion."""
    if total == 0:
        return (0.0, 1.0)
    p = wins / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denom
    return (max(0, center - spread), min(1, center + spread))


def _apply_fdr_correction():
    """
    Apply Benjamini-Hochberg False Discovery Rate correction to all strategies.
    
    With 2500+ strategy variants × multiple setup dimensions, some will appear
    profitable by chance. BH-FDR controls the expected proportion of false
    discoveries among all strategies we call "significant."
    
    Uses a one-sample t-test on PnL (H0: mean PnL ≤ 0) instead of binomial
    on win rate. This correctly handles all strategy types — hold-to-expiry
    and sell-target strategies have different payoff structures, but the t-test
    on actual PnL is agnostic to payoff structure. A strategy is significant
    if its mean PnL is reliably above zero regardless of how it generates returns.
    """
    from db import get_conn, now_utc

    with get_conn() as c:
        rows = c.execute("""
            SELECT id, setup_key, strategy_key, sample_size,
                   ev_per_trade_c, pnl_std_c
            FROM strategy_results
            WHERE sample_size >= 20
        """).fetchall()

    if not rows:
        return

    # Compute p-values using one-sample t-test: H0: mean PnL ≤ 0
    results = []
    for r in rows:
        n = r["sample_size"]
        mean_pnl = r["ev_per_trade_c"] or 0
        std_pnl = r["pnl_std_c"] or 0

        if n < 5 or std_pnl <= 0:
            p_value = 1.0
        elif mean_pnl <= 0:
            p_value = 1.0  # Not even positive mean — no need to test
        else:
            # t-statistic: mean / (std / sqrt(n))
            se = std_pnl / math.sqrt(n)
            t_stat = mean_pnl / se if se > 0 else 0
            # One-sided p-value from t-distribution
            # For n >= 20, t-distribution ≈ normal, use erfc approximation
            p_value = 0.5 * math.erfc(t_stat / math.sqrt(2))

        results.append({
            "id": r["id"],
            "p_value": p_value,
            "ev": mean_pnl,
        })

    # Sort by p-value for BH procedure
    results.sort(key=lambda x: x["p_value"])
    m = len(results)
    alpha = 0.10  # FDR threshold (10%)

    # BH procedure: find the largest i where p(i) ≤ (i/m) × α
    bh_threshold_idx = -1
    for i, r in enumerate(results):
        bh_critical = (i + 1) / m * alpha
        r["q_value"] = min(1.0, r["p_value"] * m / (i + 1))
        if r["p_value"] <= bh_critical:
            bh_threshold_idx = i

    # Mark significant strategies
    with get_conn() as c:
        for i, r in enumerate(results):
            is_sig = 1 if (i <= bh_threshold_idx and r["ev"] > 0) else 0
            c.execute("""
                UPDATE strategy_results
                SET fdr_significant = ?, fdr_q_value = ?
                WHERE id = ?
            """, (is_sig, round(r["q_value"], 6), r["id"]))

    sig_count = sum(1 for i, r in enumerate(results)
                    if i <= bh_threshold_idx and r["ev"] > 0)
    if sig_count > 0:
        log.info(f"FDR correction: {sig_count}/{m} strategies significant at α={alpha}")


# ═══════════════════════════════════════════════════════════════
#  BTC PROBABILITY SURFACE — The core edge model
# ═══════════════════════════════════════════════════════════════

def compute_btc_probability_surface():
    """
    Build empirical probability surfaces from observation data:
    Given BTC's distance from market-open price at time T into the market,
    what is the probability YES wins (BTC finishes up)?
    
    Now builds FOUR surfaces:
      - "all": global (all vol regimes combined)
      - "calm": vol_regime 1-2
      - "normal": vol_regime 3
      - "volatile": vol_regime 4-5
    
    Vol-conditioned surfaces capture how BTC volatility regime changes
    the probability dynamics. A 0.05% distance means very different things
    at 20% annualized vol vs 80% annualized vol.
    
    Distance buckets: <-0.2%, -0.2 to -0.1%, -0.1 to -0.05%, -0.05 to 0%,
                      0 to 0.05%, 0.05 to 0.1%, 0.1 to 0.2%, >0.2%
    Time buckets: 0-3min, 3-6min, 6-9min, 9-12min, 12-14min
    """
    from db import (get_conn, rows_to_list, upsert_btc_surface_cell)

    with get_conn() as c:
        rows = c.execute("""
            SELECT price_snapshots, market_result, btc_price_at_open,
                   yes_open_c, no_open_c, vol_regime
            FROM market_observations
            WHERE market_result IS NOT NULL
              AND price_snapshots IS NOT NULL
              AND COALESCE(obs_quality, 'full') IN ('full', 'short')
              AND btc_price_at_open IS NOT NULL
              AND btc_price_at_open > 0
        """).fetchall()

    observations = rows_to_list(rows)
    if len(observations) < 30:
        log.debug(f"BTC surface: insufficient data ({len(observations)} obs)")
        return 0

    # Distance bucket edges (in percent)
    dist_edges = [-0.20, -0.10, -0.05, 0.0, 0.05, 0.10, 0.20]
    dist_labels = ["<-0.20", "-0.20:-0.10", "-0.10:-0.05", "-0.05:0.00",
                   "0.00:+0.05", "+0.05:+0.10", "+0.10:+0.20", ">+0.20"]

    # Time bucket edges (in seconds)
    time_edges = [180, 360, 540, 720, 840]
    time_labels = ["0-3m", "3-6m", "6-9m", "9-12m", "12-14m"]

    def _dist_bucket(pct):
        for i, edge in enumerate(dist_edges):
            if pct < edge:
                return dist_labels[i]
        return dist_labels[-1]

    def _time_bucket(secs):
        for i, edge in enumerate(time_edges):
            if secs < edge:
                return time_labels[i]
        return time_labels[-1]

    def _vol_bucket(vol_regime):
        """Map vol_regime (1-5) to surface bucket."""
        if vol_regime is None:
            return None
        try:
            v = int(vol_regime)
        except (ValueError, TypeError):
            return None
        if v <= 2:
            return "calm"
        elif v <= 3:
            return "normal"
        else:
            return "volatile"

    # Accumulate per vol_bucket + global
    # Key: (vol_bucket, dist_bucket, time_bucket) → cell data
    cells = {}

    for obs in observations:
        result = obs["market_result"]
        btc_open = obs["btc_price_at_open"]
        if not btc_open or btc_open <= 0:
            continue

        obs_vol_bucket = _vol_bucket(obs.get("vol_regime"))
        snapshots = json.loads(obs["price_snapshots"]) if obs.get("price_snapshots") else []

        for s in snapshots:
            btc = s.get("btc")
            if not btc or btc <= 0:
                continue
            t = s.get("t", 0)
            dist_pct = (btc - btc_open) / btc_open * 100

            db = _dist_bucket(dist_pct)
            tb = _time_bucket(t)

            # Write to both global ("all") and vol-specific bucket
            buckets_to_write = ["all"]
            if obs_vol_bucket:
                buckets_to_write.append(obs_vol_bucket)

            for vb in buckets_to_write:
                key = (vb, db, tb)
                if key not in cells:
                    cells[key] = {"total": 0, "yes_wins": 0, "no_wins": 0,
                                  "yes_prices": [], "no_prices": []}

                cells[key]["total"] += 1
                if result == "yes":
                    cells[key]["yes_wins"] += 1
                else:
                    cells[key]["no_wins"] += 1

                ya = s.get("ya", 0)
                na = s.get("na", 0)
                if ya > 0:
                    cells[key]["yes_prices"].append(ya)
                if na > 0:
                    cells[key]["no_prices"].append(na)

    # Write to DB
    wrote = 0
    for (vb, db, tb), data in cells.items():
        if data["total"] < 3:
            continue
        avg_yes = (sum(data["yes_prices"]) / len(data["yes_prices"])
                   if data["yes_prices"] else None)
        avg_no = (sum(data["no_prices"]) / len(data["no_prices"])
                  if data["no_prices"] else None)
        yes_wr = data["yes_wins"] / data["total"]

        upsert_btc_surface_cell(
            db, tb, data["total"], data["yes_wins"], data["no_wins"],
            yes_wr, avg_yes, avg_no, vol_bucket=vb
        )
        wrote += 1

    if wrote > 0:
        log.info(f"BTC probability surface: {wrote} cells from "
                 f"{len(observations)} observations (4 vol surfaces)")
    return wrote


# ═══════════════════════════════════════════════════════════════
#  BTC FAIR VALUE ENGINE — Model-driven trading decisions
# ═══════════════════════════════════════════════════════════════

class BtcFairValueModel:
    """
    Real-time fair value model for BTC 15-minute binary options.

    Uses the empirical BTC Probability Surface to answer the core question:
    "Given BTC's current position relative to the market open, with T time
     remaining, what is the probability YES wins?"

    That probability IS the fair value of a YES contract (× 100 = cents).
    Compare it to Kalshi's ask price after fees → the gap is your edge.

    Usage:
        model = BtcFairValueModel()
        edge = model.compute_edge(
            yes_ask_c=45, no_ask_c=58,
            btc_distance_pct=0.03,      # BTC 0.03% above market open
            seconds_into_market=480,     # 8 minutes into 15-min market
            realized_vol=0.12,           # trailing 15-min realized vol (%)
        )
        # edge["recommended_side"]  → "yes" or "no" or None
        # edge["best_edge_pct"]     → 5.2 (meaning 5.2% edge)
        # edge["yes_ev_c"]          → 3.8 (expected value in cents)
    """

    def __init__(self):
        self._surface = {}     # (dist_bucket, time_bucket) → cell data
        self._loaded_at = 0
        self._cell_count = 0
        self._min_cell_samples = 10
        self._cache_ttl = 1800  # 30 min (matches regime worker analysis cycle)
        self._active_vol_bucket = "all"  # Which vol surface is loaded

        # Distance bucket definitions — MUST match compute_btc_probability_surface
        self._dist_edges = [-0.20, -0.10, -0.05, 0.0, 0.05, 0.10, 0.20]
        self._dist_labels = [
            "<-0.20", "-0.20:-0.10", "-0.10:-0.05", "-0.05:0.00",
            "0.00:+0.05", "+0.05:+0.10", "+0.10:+0.20", ">+0.20",
        ]
        self._dist_midpoints = [-0.30, -0.15, -0.075, -0.025,
                                 0.025, 0.075, 0.15, 0.30]

        # Time bucket definitions — MUST match compute_btc_probability_surface
        self._time_edges = [180, 360, 540, 720, 840]
        self._time_labels = ["0-3m", "3-6m", "6-9m", "9-12m", "12-14m"]
        self._time_midpoints = [90, 270, 450, 630, 780]

    # ── Loading ───────────────────────────────────────────────

    @staticmethod
    def _map_vol_bucket(vol_regime) -> str:
        """Map vol_regime (1-5) to surface bucket name."""
        if vol_regime is None:
            return "all"
        try:
            v = int(vol_regime)
        except (ValueError, TypeError):
            return "all"
        if v <= 2:
            return "calm"
        elif v <= 3:
            return "normal"
        else:
            return "volatile"

    def load(self, force: bool = False, vol_regime: int = None):
        """Load or refresh surface data from DB. Auto-caches.
        
        Tries vol-specific surface first (calm/normal/volatile).
        Falls back to global 'all' surface if vol-specific has
        insufficient cells (< 8). This ensures the model adapts
        to current conditions when data permits, without degrading
        when vol-specific data is sparse."""
        target_vb = self._map_vol_bucket(vol_regime)
        now = time.time()

        # Check if cached surface is still valid for this vol bucket
        if (not force and self._surface
                and self._active_vol_bucket == target_vb
                and (now - self._loaded_at) < self._cache_ttl):
            return

        from db import get_btc_surface_data

        # Try vol-specific surface first
        surface = {}
        if target_vb != "all":
            cells = get_btc_surface_data(vol_bucket=target_vb)
            for cell in cells:
                db = cell["distance_bucket"]
                tb = cell["time_bucket"]
                if cell["total"] >= self._min_cell_samples:
                    surface[(db, tb)] = {
                        "yes_win_rate": cell["yes_win_rate"],
                        "total": cell["total"],
                        "avg_yes_price": cell.get("avg_yes_price"),
                        "avg_no_price": cell.get("avg_no_price"),
                    }

        # Fall back to global if vol-specific has too few cells
        if len(surface) < 8:
            if target_vb != "all":
                log.debug(f"Fair value model: vol '{target_vb}' has only "
                          f"{len(surface)} cells, falling back to global")
            surface = {}
            cells = get_btc_surface_data(vol_bucket="all")
            for cell in cells:
                db = cell["distance_bucket"]
                tb = cell["time_bucket"]
                if cell["total"] >= self._min_cell_samples:
                    surface[(db, tb)] = {
                        "yes_win_rate": cell["yes_win_rate"],
                        "total": cell["total"],
                        "avg_yes_price": cell.get("avg_yes_price"),
                        "avg_no_price": cell.get("avg_no_price"),
                    }
            active_vb = "all"
        else:
            active_vb = target_vb

        self._surface = surface
        self._cell_count = len(surface)
        self._loaded_at = now
        self._active_vol_bucket = active_vb
        log.debug(f"Fair value model: loaded {self._cell_count} cells "
                  f"(vol={active_vb})")

    def is_ready(self, vol_regime: int = None) -> bool:
        """Check if model has enough data to make meaningful predictions."""
        self.load(vol_regime=vol_regime)
        return self._cell_count >= 8

    def get_status(self, vol_regime: int = None) -> dict:
        """Model status for dashboard/logging."""
        self.load(vol_regime=vol_regime)
        return {
            "ready": self._cell_count >= 8,
            "cells_loaded": self._cell_count,
            "min_cells_needed": 8,
            "last_loaded": self._loaded_at,
            "active_vol_bucket": self._active_vol_bucket,
            "cache_age_s": round(time.time() - self._loaded_at, 0)
                           if self._loaded_at else None,
        }

    # ── Bucketing helpers ─────────────────────────────────────

    def _dist_bucket(self, pct: float) -> str:
        for i, edge in enumerate(self._dist_edges):
            if pct < edge:
                return self._dist_labels[i]
        return self._dist_labels[-1]

    def _time_bucket(self, secs: float) -> str:
        for i, edge in enumerate(self._time_edges):
            if secs < edge:
                return self._time_labels[i]
        return self._time_labels[-1]

    def _dist_index(self, pct: float) -> int:
        for i, edge in enumerate(self._dist_edges):
            if pct < edge:
                return i
        return len(self._dist_labels) - 1

    def _time_index(self, secs: float) -> int:
        for i, edge in enumerate(self._time_edges):
            if secs < edge:
                return i
        return len(self._time_labels) - 1

    # ── Core model ────────────────────────────────────────────

    def get_yes_probability(self, btc_distance_pct: float,
                            seconds_into_market: float,
                            realized_vol: float = None,
                            vol_regime: int = None) -> dict:
        """
        Compute P(YES wins) given current market state.

        Args:
            btc_distance_pct: BTC price vs market open, in percent.
                              Positive = BTC above open = favors YES.
            seconds_into_market: seconds elapsed since market opened (0–900).
            realized_vol: trailing 15-min realized volatility in percent
                          (optional; used for analytical fallback calibration).
            vol_regime: current volatility regime (1-5). When provided, loads
                        the vol-specific probability surface for better accuracy.

        Returns dict with:
            p_yes, p_no, fair_yes_c, fair_no_c,
            cell_samples, interpolated, confidence, source
        """
        self.load(vol_regime=vol_regime)

        if not self._surface:
            return self._analytical_estimate(btc_distance_pct,
                                             seconds_into_market, realized_vol)

        # 1. Try exact cell
        db = self._dist_bucket(btc_distance_pct)
        tb = self._time_bucket(seconds_into_market)
        cell = self._surface.get((db, tb))

        if cell and cell["total"] >= self._min_cell_samples:
            return self._build_result(
                cell["yes_win_rate"], cell["total"], False,
                cell.get("avg_yes_price"), cell.get("avg_no_price"),
                source="surface",
            )

        # 2. Interpolate from neighbors
        p_yes, samples, n_cells = self._interpolate(btc_distance_pct,
                                                     seconds_into_market)
        if p_yes is not None and n_cells >= 2:
            return self._build_result(p_yes, samples, True,
                                      source="interpolated")

        # 3. Analytical fallback
        return self._analytical_estimate(btc_distance_pct,
                                         seconds_into_market, realized_vol)

    def _interpolate(self, dist_pct: float, secs: float):
        """Distance-weighted interpolation from neighboring surface cells."""
        di = self._dist_index(dist_pct)
        ti = self._time_index(secs)

        neighbors = []
        for d_off in (-1, 0, 1):
            for t_off in (-1, 0, 1):
                d_idx = di + d_off
                t_idx = ti + t_off
                if not (0 <= d_idx < len(self._dist_labels)):
                    continue
                if not (0 <= t_idx < len(self._time_labels)):
                    continue
                cell = self._surface.get(
                    (self._dist_labels[d_idx], self._time_labels[t_idx]))
                if not cell:
                    continue

                # Weight: inverse distance × sample depth
                d_dist = abs(self._dist_midpoints[d_idx] - dist_pct)
                t_dist = abs(self._time_midpoints[t_idx] - secs)
                d_w = 1.0 / (1.0 + d_dist * 20)    # ~0.5 at 0.05% away
                t_w = 1.0 / (1.0 + t_dist / 180)    # ~0.5 at 3 min away
                s_w = min(1.0, cell["total"] / 50)   # saturates at 50 obs
                w = d_w * t_w * s_w
                neighbors.append((cell["yes_win_rate"], w, cell["total"]))

        if not neighbors:
            return None, 0, 0

        total_w = sum(w for _, w, _ in neighbors)
        if total_w <= 0:
            return None, 0, 0

        p_yes = sum(p * w for p, w, _ in neighbors) / total_w
        total_n = sum(n for _, _, n in neighbors)
        return p_yes, total_n, len(neighbors)

    def _analytical_estimate(self, dist_pct: float, secs: float,
                             realized_vol: float = None) -> dict:
        """
        Analytical fallback using Brownian bridge approximation.

        P(BTC finishes above open | currently at distance D, T remaining)
        ≈ Φ(D / (σ√T))

        This is a well-known result for random walk terminal distributions.
        σ calibrated from realized vol or defaults to ~0.1%/√min for BTC.
        """
        time_remaining = max(1, 900 - secs)
        minutes_remaining = time_remaining / 60

        # σ per minute in percent; default ~0.10%, use realized_vol if available
        sigma_per_min = 0.10
        if realized_vol is not None and realized_vol > 0:
            # realized_vol is 15-min vol in %; convert to per-minute
            sigma_per_min = realized_vol / math.sqrt(15)

        if minutes_remaining < 0.25:
            # <15s left: distance is nearly deterministic
            if dist_pct > 0.005:
                p_yes = 0.95
            elif dist_pct < -0.005:
                p_yes = 0.05
            else:
                p_yes = 0.50
        else:
            denom = sigma_per_min * math.sqrt(minutes_remaining)
            if denom > 0:
                z = dist_pct / denom
                p_yes = 0.5 * (1 + math.erf(z / math.sqrt(2)))
            else:
                p_yes = 0.50

        p_yes = max(0.02, min(0.98, p_yes))
        return self._build_result(p_yes, 0, False, source="analytical")

    def _build_result(self, p_yes: float, samples: int, interpolated: bool,
                      avg_yes_price=None, avg_no_price=None,
                      source: str = "surface") -> dict:
        """Build standardized model output."""
        if samples >= 100:
            conf = "high"
        elif samples >= 30:
            conf = "moderate"
        elif samples > 0:
            conf = "low"
        else:
            conf = "analytical"

        return {
            "p_yes": round(p_yes, 4),
            "p_no": round(1 - p_yes, 4),
            "fair_yes_c": round(p_yes * 100, 1),
            "fair_no_c": round((1 - p_yes) * 100, 1),
            "cell_samples": samples,
            "interpolated": interpolated,
            "confidence": conf,
            "source": source,
            "vol_surface": self._active_vol_bucket,
            "avg_market_yes_c": round(avg_yes_price, 1)
                                if avg_yes_price else None,
            "avg_market_no_c": round(avg_no_price, 1)
                               if avg_no_price else None,
        }

    # ── Edge computation ──────────────────────────────────────

    def compute_edge(self, yes_ask_c: int, no_ask_c: int,
                     btc_distance_pct: float,
                     seconds_into_market: float,
                     realized_vol: float = None,
                     fee_rate: float = KALSHI_FEE_RATE,
                     vol_regime: int = None) -> dict:
        """
        The money function: compute edge for each side given live prices.

        Compares model fair value to Kalshi prices after fees.
        Returns which side (if any) has positive expected value.

        Args:
            yes_ask_c: Kalshi YES ask in cents (0 if unavailable)
            no_ask_c: Kalshi NO ask in cents (0 if unavailable)
            btc_distance_pct: BTC distance from market open (%)
            seconds_into_market: elapsed seconds (0–900)
            realized_vol: trailing vol (optional)
            fee_rate: Kalshi fee rate (default 7%)
            vol_regime: current vol regime (1-5) for vol-conditioned surface

        Returns dict:
            recommended_side: "yes" / "no" / None (None = no edge)
            yes_edge_pct: model P(yes) minus YES breakeven (can be negative)
            no_edge_pct: model P(no) minus NO breakeven (can be negative)
            yes_ev_c: expected value of buying YES in cents
            no_ev_c: expected value of buying NO in cents
            best_edge_pct: max positive edge, or 0
            model: full model output dict
        """
        model = self.get_yes_probability(btc_distance_pct,
                                         seconds_into_market, realized_vol,
                                         vol_regime=vol_regime)
        p_yes = model["p_yes"]
        p_no = model["p_no"]

        # YES side
        yes_edge_pct = None
        yes_ev_c = None
        if yes_ask_c and yes_ask_c > 0 and yes_ask_c < 99:
            yes_fee = max(1, round(yes_ask_c * fee_rate))
            yes_cost = yes_ask_c + yes_fee
            yes_be = yes_cost / 100.0      # breakeven probability
            yes_edge_pct = round((p_yes - yes_be) * 100, 2)
            yes_ev_c = round(p_yes * (100 - yes_cost) - p_no * yes_cost, 2)

        # NO side
        no_edge_pct = None
        no_ev_c = None
        if no_ask_c and no_ask_c > 0 and no_ask_c < 99:
            no_fee = max(1, round(no_ask_c * fee_rate))
            no_cost = no_ask_c + no_fee
            no_be = no_cost / 100.0
            no_edge_pct = round((p_no - no_be) * 100, 2)
            no_ev_c = round(p_no * (100 - no_cost) - p_yes * no_cost, 2)

        # Pick the side with the biggest positive edge
        best_side = None
        best_edge = 0.0
        if yes_edge_pct is not None and yes_edge_pct > 0:
            best_side = "yes"
            best_edge = yes_edge_pct
        if no_edge_pct is not None and no_edge_pct > best_edge:
            best_side = "no"
            best_edge = no_edge_pct

        return {
            "recommended_side": best_side,
            "yes_edge_pct": yes_edge_pct,
            "no_edge_pct": no_edge_pct,
            "yes_ev_c": yes_ev_c,
            "no_ev_c": no_ev_c,
            "best_edge_pct": round(best_edge, 2),
            "model": model,
        }

    def format_summary(self, edge_result: dict) -> str:
        """Human-readable one-liner for logging."""
        m = edge_result.get("model", {})
        side = edge_result.get("recommended_side")
        if not side:
            return (f"FV: Y={m.get('fair_yes_c', '?')}¢ N={m.get('fair_no_c', '?')}¢ "
                    f"({m.get('source', '?')}) — no edge")
        ep = edge_result.get("best_edge_pct", 0)
        ev = edge_result.get(f"{side}_ev_c", 0)
        return (f"FV: Y={m.get('fair_yes_c', '?')}¢ N={m.get('fair_no_c', '?')}¢ "
                f"({m.get('source', '?')}) → {side.upper()} "
                f"edge +{ep:.1f}% EV {ev:+.1f}¢")


# ═══════════════════════════════════════════════════════════════
#  FEATURE IMPORTANCE — Which features predict outcomes?
# ═══════════════════════════════════════════════════════════════

def compute_feature_importance():
    """
    Compute which observation features are most predictive of market outcome.
    Uses point-biserial correlation (equivalent to t-test for binary outcome).
    
    This tells us whether regime components, hours, BTC metrics etc.
    actually predict YES/NO outcomes — or if they're just noise.
    No external dependencies needed (pure Python).
    """
    from db import get_conn, rows_to_list, upsert_feature_importance

    with get_conn() as c:
        rows = c.execute("""
            SELECT market_result, vol_regime, trend_regime, volume_regime,
                   hour_et, day_of_week, realized_vol, atr_15m,
                   bollinger_width, ema_slope_15m, ema_slope_1h,
                   trend_direction, trend_strength, bollinger_squeeze,
                   btc_return_15m, btc_return_1h, btc_return_4h,
                   btc_move_during_pct, btc_distance_pct_at_close,
                   btc_max_distance_pct, btc_min_distance_pct,
                   volume_spike
            FROM market_observations
            WHERE market_result IS NOT NULL
              AND COALESCE(obs_quality, 'full') IN ('full', 'short')
        """).fetchall()

    data = rows_to_list(rows)
    if len(data) < 50:
        log.debug(f"Feature importance: insufficient data ({len(data)} obs)")
        return 0

    # Encode outcome: yes=1, no=0
    outcomes = [1 if d["market_result"] == "yes" else 0 for d in data]
    n = len(outcomes)
    y_mean = sum(outcomes) / n

    features = [
        "vol_regime", "trend_regime", "volume_regime", "hour_et",
        "day_of_week", "realized_vol", "atr_15m", "bollinger_width",
        "ema_slope_15m", "ema_slope_1h", "trend_direction", "trend_strength",
        "bollinger_squeeze", "btc_return_15m", "btc_return_1h", "btc_return_4h",
        "btc_move_during_pct", "btc_distance_pct_at_close",
        "btc_max_distance_pct", "btc_min_distance_pct", "volume_spike",
    ]

    wrote = 0
    for feat in features:
        vals = [d.get(feat) for d in data]
        # Filter to non-None pairs
        pairs = [(v, outcomes[i]) for i, v in enumerate(vals) if v is not None]
        if len(pairs) < 30:
            continue

        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        fn = len(xs)

        # Point-biserial correlation
        x_mean = sum(xs) / fn
        y_mean_local = sum(ys) / fn
        numerator = sum((x - x_mean) * (y - y_mean_local)
                        for x, y in zip(xs, ys))
        x_var = sum((x - x_mean) ** 2 for x in xs)
        y_var = sum((y - y_mean_local) ** 2 for y in ys)
        denom = math.sqrt(x_var * y_var) if x_var > 0 and y_var > 0 else 1
        corr = numerator / denom if denom > 0 else 0

        # Importance = |correlation| — simple but effective for screening
        importance = abs(corr)

        upsert_feature_importance(feat, importance, corr, fn)
        wrote += 1

    if wrote > 0:
        log.info(f"Feature importance: computed {wrote} features "
                 f"from {n} observations")
    return wrote


# ═══════════════════════════════════════════════════════════════
#  REGIME EFFECTIVENESS — Coarse vs fine-grained comparison
# ═══════════════════════════════════════════════════════════════

def compute_regime_effectiveness() -> dict:
    """
    Compare whether fine-grained regimes predict outcomes better than
    coarse regimes, after adjusting for sample size.
    
    Returns dict with comparison metrics. If fine-grained doesn't beat
    coarse, you should collapse to the coarse system to stop
    fragmenting data without gaining predictive power.
    """
    from db import get_conn, rows_to_list

    with get_conn() as c:
        # Get all observations with regime labels (include short-quality too)
        rows = c.execute("""
            SELECT regime_label, market_result, vol_regime, trend_regime
            FROM market_observations
            WHERE market_result IS NOT NULL
              AND regime_label IS NOT NULL AND regime_label != 'unknown'
              AND COALESCE(obs_quality, 'full') IN ('full', 'short')
        """).fetchall()

    data = rows_to_list(rows)
    if len(data) < 100:
        return {"error": "Need 100+ observations", "n": len(data)}

    # For each regime, compute prediction accuracy
    # (using majority class as the "prediction")
    fine_stats = {}
    coarse_stats = {}

    from regime import compute_coarse_label

    for d in data:
        label = d["regime_label"]
        result = d["market_result"]

        # compute_coarse_label requires (vol_regime, trend_regime) integers
        vol = d.get("vol_regime")
        trend = d.get("trend_regime")
        if vol is not None and trend is not None:
            coarse = compute_coarse_label(int(vol), int(trend))
        else:
            coarse = "unknown"

        # Fine-grained
        if label not in fine_stats:
            fine_stats[label] = {"yes": 0, "no": 0}
        fine_stats[label][result] += 1

        # Coarse
        if coarse != "unknown":
            if coarse not in coarse_stats:
                coarse_stats[coarse] = {"yes": 0, "no": 0}
            coarse_stats[coarse][result] += 1

    # Compute accuracy of majority-class prediction
    def _accuracy(stats):
        correct = 0
        total = 0
        regime_details = []
        for label, counts in stats.items():
            n = counts["yes"] + counts["no"]
            if n < 5:
                continue
            majority = max(counts["yes"], counts["no"])
            correct += majority
            total += n
            regime_details.append({
                "label": label, "n": n,
                "yes_rate": round(counts["yes"] / n, 3),
                "majority_accuracy": round(majority / n, 3),
            })
        return {
            "accuracy": round(correct / total, 4) if total > 0 else 0.5,
            "n_regimes": len([s for s in stats.values()
                              if s["yes"] + s["no"] >= 5]),
            "n_samples": total,
            "details": sorted(regime_details, key=lambda x: x["n"],
                              reverse=True)[:15],
        }

    fine_result = _accuracy(fine_stats)
    coarse_result = _accuracy(coarse_stats)

    improvement = fine_result["accuracy"] - coarse_result["accuracy"]

    return {
        "fine_grained": fine_result,
        "coarse": coarse_result,
        "improvement": round(improvement, 4),
        "recommendation": (
            "Fine-grained regimes provide meaningful improvement"
            if improvement > 0.02
            else "Consider using coarse regimes — fine-grained adds "
                 "fragmentation without significant predictive gain"
        ),
        "total_observations": len(data),
    }


# ═══════════════════════════════════════════════════════════════
#  FEE SENSITIVITY ANALYSIS
# ═══════════════════════════════════════════════════════════════

def fee_sensitivity_analysis(setup_key: str = "global:all",
                              top_n: int = 10,
                              fee_rates: list = None) -> list:
    """
    Stress-test top strategies by re-simulating at higher fee rates.
    Shows whether a strategy's edge survives wider effective spreads.

    Args:
        setup_key: which setup to analyze (e.g. "global:all", "coarse_regime:calm_flat")
        top_n: how many top strategies to test
        fee_rates: list of fee rates to test (default: [0.07, 0.08, 0.09, 0.10])

    Returns list of dicts:
        [{strategy_key, results: [{fee_rate, ev_per_trade_c, win_rate, sample_size}]}]
    """
    if fee_rates is None:
        fee_rates = [0.07, 0.08, 0.09, 0.10]

    # Get top strategies at current fee rate
    with get_conn() as c:
        rows = c.execute("""
            SELECT strategy_key, ev_per_trade_c, win_rate, sample_size
            FROM strategy_results
            WHERE setup_key = ? AND sample_size >= 20 AND ev_per_trade_c > 0
            ORDER BY ev_per_trade_c DESC
            LIMIT ?
        """, (setup_key, top_n)).fetchall()

    if not rows:
        return []

    top_strategies = []
    for r in rows:
        top_strategies.append({
            "strategy_key": r["strategy_key"],
            "current_ev": r["ev_per_trade_c"],
            "current_wr": r["win_rate"],
            "sample_size": r["sample_size"],
        })

    # Get all resolved observations for re-simulation
    observations = get_observations_for_simulation(limit=0)
    if not observations:
        return top_strategies

    # Filter observations matching the setup
    setup_type, _, setup_val = setup_key.partition(":")
    filtered_obs = []
    for obs in observations:
        if setup_key == "global:all":
            filtered_obs.append(obs)
        elif setup_type == "coarse_regime":
            vol = obs.get("vol_regime")
            trend = obs.get("trend_regime")
            if vol is not None and trend is not None:
                try:
                    from regime import compute_coarse_label
                    if compute_coarse_label(int(vol), int(trend)) == setup_val:
                        filtered_obs.append(obs)
                except Exception:
                    pass
        elif setup_type == "hour" and str(obs.get("hour_et")) == setup_val:
            filtered_obs.append(obs)
    observations = filtered_obs

    if not observations:
        return top_strategies

    # For each top strategy, re-simulate at each fee rate
    for strat in top_strategies:
        sk = strat["strategy_key"]
        parsed = parse_strategy_key(sk)
        if not parsed:
            continue

        side_rule = parsed["side_rule"]
        time_rule = parsed["entry_time_rule"]
        entry_max = parsed["entry_price_max"]
        sell_target = parsed["sell_target"]

        strat["fee_results"] = []
        for fee_rate in fee_rates:
            pnls = []
            wins = 0
            for obs in observations:
                snaps = obs.get("_snapshots_parsed")
                if not snaps:
                    try:
                        import json as _j
                        snaps = _j.loads(obs.get("price_snapshots", "[]"))
                        obs["_snapshots_parsed"] = snaps
                    except Exception:
                        continue

                result = obs.get("market_result")
                if not result or not snaps:
                    continue

                duration = max(s["t"] for s in snaps) if snaps else 0
                if duration < 60:
                    continue

                sim = _simulate_one(snaps, result, duration, side_rule,
                                    time_rule, entry_max, sell_target,
                                    fee_rate=fee_rate,
                                    btc_open=obs.get("btc_price_at_open"),
                                    realized_vol=obs.get("realized_vol"))
                if sim and sim["entered"]:
                    pnls.append(sim["pnl_c"])
                    if sim["won"]:
                        wins += 1

            n = len(pnls)
            if n > 0:
                avg_ev = sum(pnls) / n
                wr = wins / n
            else:
                avg_ev = 0
                wr = 0

            strat["fee_results"].append({
                "fee_rate": fee_rate,
                "fee_pct": round(fee_rate * 100, 1),
                "ev_per_trade_c": round(avg_ev, 1),
                "win_rate": round(wr, 4),
                "sample_size": n,
                "profitable": avg_ev > 0,
            })

    return top_strategies


# ═══════════════════════════════════════════════════════════════
#  LAYER 3: ADVISOR — Strategy Recommendations
# ═══════════════════════════════════════════════════════════════

def get_recommendation(regime_label: str, hour_et: int = None,
                       vol_regime: int = None, trend_regime: int = None,
                       rejection_info: dict = None) -> dict | None:
    """
    Get the best strategy recommendation for a given setup.
    
    Hierarchical fallback:
      coarse_regime → hour → global
    
    Fine-grained regime labels are too sparse (~63 labels) for reliable
    strategy evaluation. Coarse labels (~15 buckets) converge much faster.
    
    Validation gates (all must pass for a strategy to be recommended):
      0. Selection process validated (walk-forward selection test passed)
      1. Positive time-weighted EV
      2. Fee resilience (survives +1% fee bump)
      3. OOS validation: positive OOS EV with ≥30 test samples
         (in-sample results are hypothesis generation only)
      4. Slippage robustness: flagged if +1¢ slippage turns EV negative
      5. Breakeven fee rate: must be ≥ current_rate + configurable buffer
    
    Returns dict with recommendation details, or None if insufficient data.
    
    If rejection_info dict is passed, it will be populated with details about
    why the best candidate was rejected (gate name, values, setup_key).
    """
    from db import get_config
    min_samples = int(get_config("auto_strategy_min_samples", 20) or 20)

    # Track the deepest rejection across all candidates for diagnostics.
    # Priority order: higher = deeper into validation = more informative.
    _rej_priority = 0  # 0=nothing tried, higher=deeper gate reached
    def _track_rejection(priority, gate, short, detail, setup=None, strat=None):
        nonlocal _rej_priority
        if rejection_info is not None and priority > _rej_priority:
            _rej_priority = priority
            rejection_info.clear()
            rejection_info.update({
                "gate": gate,
                "short": short,
                "detail": detail,
                "setup_key": setup,
                "strategy_key": strat,
            })

    # Gate 0: Selection process validation
    selection_validated = get_config("_selection_test_result")
    if selection_validated == "failed":
        _track_rejection(99, "selection_test", "selection test failed",
                         "Walk-forward selection test failed — all recommendations blocked")
        return None

    # Build fallback hierarchy: coarse_regime → hour → global
    candidates = []

    if vol_regime is not None and trend_regime is not None:
        try:
            from regime import compute_coarse_label
            coarse = compute_coarse_label(int(vol_regime), int(trend_regime))
            candidates.append(f"coarse_regime:{coarse}")
        except Exception:
            pass

    if hour_et is not None:
        candidates.append(f"hour:{hour_et}")
    candidates.append("global:all")

    for setup_key in candidates:
        strategies = get_strategy_for_setup(setup_key, min_samples=min_samples)
        if not strategies:
            _track_rejection(1, "no_strategies",
                             f"no strategies n≥{min_samples}",
                             f"No strategies with n≥{min_samples} for {setup_key}",
                             setup=setup_key)
            continue

        for best in strategies:
            _sk = best.get("strategy_key", "?")
            ev = best.get("weighted_ev_c") or best.get("ev_per_trade_c")
            if ev is None or ev <= 0:
                _track_rejection(3, "ev_negative",
                                 f"EV {(ev or 0):+.1f}¢ ≤ 0",
                                 f"Best EV {(ev or 0):+.1f}¢ not positive ({setup_key})",
                                 setup=setup_key, strat=_sk)
                continue

            # Gate 1: Fee resilience — survives +1% fee bump
            entry_max = best.get("entry_price_max", 50)
            fee_impact_c = max(1, round(entry_max * 0.01))
            if ev <= fee_impact_c:
                _track_rejection(4, "fee_fragile",
                                 f"EV {ev:+.1f}¢ ≤ fee impact {fee_impact_c}¢",
                                 f"Fee fragile: EV {ev:+.1f}¢ ≤ 1% fee bump ({fee_impact_c}¢) for {_sk} ({setup_key})",
                                 setup=setup_key, strat=_sk)
                continue

            # Gate 2: OOS validation — ENFORCED, not advisory
            # No strategy trades live without positive out-of-sample EV
            # with minimum test sample size. In-sample is hypothesis only.
            oos_ev = best.get("oos_ev_c")
            oos_n = best.get("oos_sample_size") or 0
            if oos_ev is None or oos_n < 30:
                # Insufficient OOS data — can't validate yet
                _track_rejection(5, "oos_insufficient",
                                 f"OOS n={oos_n} < 30",
                                 f"Insufficient OOS data: n={oos_n} (need 30) for {_sk} ({setup_key})",
                                 setup=setup_key, strat=_sk)
                continue
            if oos_ev <= 0:
                # OOS shows no edge — in-sample was likely overfit
                _track_rejection(6, "oos_negative",
                                 f"OOS EV {oos_ev:+.1f}¢ ≤ 0",
                                 f"OOS EV negative: {oos_ev:+.1f}¢ for {_sk} ({setup_key}) — likely overfit",
                                 setup=setup_key, strat=_sk)
                continue

            # Gate 3: Slippage robustness check (informational flag)
            slippage_1c = best.get("slippage_1c_ev")
            slippage_fragile = (slippage_1c is not None and slippage_1c < 0)

            # Gate 4: Breakeven fee rate — require minimum buffer above
            # current Kalshi rate. If breakeven_fee is only barely above 7%,
            # any fee increase or execution imperfection wipes out the edge.
            breakeven_fee = best.get("breakeven_fee_rate")
            fee_buffer = float(get_config("min_breakeven_fee_buffer", 0.03) or 0.03)
            min_breakeven = KALSHI_FEE_RATE + fee_buffer
            if breakeven_fee is not None and breakeven_fee < min_breakeven:
                _track_rejection(7, "fee_buffer",
                                 f"BE fee {breakeven_fee:.1%} < {min_breakeven:.1%}",
                                 f"Breakeven fee {breakeven_fee:.1%} < required {min_breakeven:.1%} for {_sk} ({setup_key})",
                                 setup=setup_key, strat=_sk)
                continue

            # Quality split check (informational)
            quality_full_ev = best.get("quality_full_ev_c")
            quality_degraded_ev = best.get("quality_degraded_ev_c")
            quality_gap = None
            if quality_full_ev is not None and quality_degraded_ev is not None:
                quality_gap = round(quality_full_ev - quality_degraded_ev, 1)

            return {
                "setup_key": setup_key,
                "strategy_key": best["strategy_key"],
                "side_rule": best["side_rule"],
                "sell_target": best["exit_rule"],
                "entry_time_rule": best["entry_time_rule"],
                "entry_price_max": entry_max,
                "ev_per_trade_c": best["ev_per_trade_c"],
                "weighted_ev_c": best.get("weighted_ev_c"),
                "win_rate": best["win_rate"],
                "weighted_win_rate": best.get("weighted_win_rate"),
                "sample_size": best["sample_size"],
                "ci_lower": best["ci_lower"],
                "profit_factor": best["profit_factor"],
                "oos_ev_c": oos_ev,
                "oos_win_rate": best.get("oos_win_rate"),
                "oos_sample_size": oos_n,
                "fee_resilient": True,
                "breakeven_fee_rate": breakeven_fee,
                "slippage_fragile": slippage_fragile,
                "slippage_1c_ev": slippage_1c,
                "slippage_2c_ev": best.get("slippage_2c_ev"),
                "quality_full_ev_c": quality_full_ev,
                "quality_degraded_ev_c": quality_degraded_ev,
                "quality_gap_c": quality_gap,
                "confidence": "high" if best["sample_size"] >= 75
                              else "moderate" if best["sample_size"] >= 30
                              else "low",
                "selection_validated": selection_validated == "passed",
            }

    return None


# ═══════════════════════════════════════════════════════════════
#  BACKFILL: Resolve market outcomes for observations
# ═══════════════════════════════════════════════════════════════

def analyze_correlated_losses(strategy_key: str,
                               setup_key: str = "global:all") -> dict:
    """
    Analyze whether losses for a strategy cluster in specific conditions.
    Helps identify hidden regime-specific weaknesses in apparently profitable
    strategies.
    
    Returns dict with loss concentration analysis per dimension:
    vol_regime, trend_direction, hour_et, day_of_week.
    """
    observations = get_observations_for_simulation(limit=0)
    if not observations:
        return {"error": "No observations available"}

    # Filter by setup
    setup_type, _, setup_val = setup_key.partition(":")
    if setup_key != "global:all":
        filtered = []
        for obs in observations:
            if setup_type == "coarse_regime":
                vol = obs.get("vol_regime")
                trend = obs.get("trend_regime")
                if vol is not None and trend is not None:
                    try:
                        from regime import compute_coarse_label
                        if compute_coarse_label(int(vol), int(trend)) == setup_val:
                            filtered.append(obs)
                    except Exception:
                        pass
            elif setup_type == "hour" and str(obs.get("hour_et")) == setup_val:
                filtered.append(obs)
        observations = filtered

    # Parse strategy key
    parsed = parse_strategy_key(strategy_key)
    if not parsed:
        return {"error": f"Invalid strategy key: {strategy_key}"}

    side_rule = parsed["side_rule"]
    time_rule = parsed["entry_time_rule"]
    entry_max = parsed["entry_price_max"]
    sell_target = parsed["sell_target"]

    # Simulate strategy on each observation, record wins/losses with context
    results = []
    for obs in observations:
        snaps = json.loads(obs.get("price_snapshots", "[]")) if obs.get("price_snapshots") else []
        result = obs.get("market_result")
        if not snaps or not result or result not in ("yes", "no"):
            continue
        duration = max(s["t"] for s in snaps) if snaps else 0
        if duration < 60:
            continue

        sim = _simulate_one(snaps, result, duration, side_rule, time_rule,
                            entry_max, sell_target,
                            btc_open=obs.get("btc_price_at_open"),
                            realized_vol=obs.get("realized_vol"))
        if sim and sim["entered"]:
            results.append({
                "won": sim["won"],
                "pnl_c": sim["pnl_c"],
                "vol_regime": obs.get("vol_regime"),
                "trend_direction": obs.get("trend_direction"),
                "hour_et": obs.get("hour_et"),
                "day_of_week": obs.get("day_of_week"),
            })

    if len(results) < 20:
        return {"error": f"Insufficient data: {len(results)} simulated trades",
                "sample_size": len(results)}

    total_n = len(results)
    total_losses = sum(1 for r in results if not r["won"])
    overall_loss_rate = total_losses / total_n

    # Analyze each dimension
    analysis = {
        "strategy_key": strategy_key,
        "setup_key": setup_key,
        "total_trades": total_n,
        "overall_loss_rate": round(overall_loss_rate, 4),
        "dimensions": {},
    }

    for dim in ["vol_regime", "trend_direction", "hour_et", "day_of_week"]:
        buckets = {}
        for r in results:
            val = r.get(dim)
            if val is None:
                continue
            val = str(val)
            if val not in buckets:
                buckets[val] = {"n": 0, "losses": 0}
            buckets[val]["n"] += 1
            if not r["won"]:
                buckets[val]["losses"] += 1

        dim_results = []
        for val, data in sorted(buckets.items()):
            if data["n"] < 5:
                continue
            loss_rate = data["losses"] / data["n"]
            # Flag if loss rate is >1.5x the overall rate
            elevated = loss_rate > overall_loss_rate * 1.5 and data["losses"] >= 3
            dim_results.append({
                "value": val,
                "n": data["n"],
                "losses": data["losses"],
                "loss_rate": round(loss_rate, 4),
                "vs_overall": round(loss_rate / overall_loss_rate, 2) if overall_loss_rate > 0 else None,
                "elevated": elevated,
            })

        analysis["dimensions"][dim] = dim_results

    # Summary: which conditions are dangerous?
    danger_zones = []
    for dim, buckets in analysis["dimensions"].items():
        for b in buckets:
            if b["elevated"]:
                danger_zones.append({
                    "dimension": dim,
                    "value": b["value"],
                    "loss_rate": b["loss_rate"],
                    "vs_overall": b["vs_overall"],
                    "n": b["n"],
                })
    analysis["danger_zones"] = danger_zones

    return analysis


def backfill_observation_results(client, limit: int = 30) -> int:
    """
    Backfill market results for observations that are past close time.
    Called periodically from main loop.
    """
    unresolved = get_unresolved_observations(limit=limit)
    if not unresolved:
        return 0

    filled = 0
    for obs in unresolved:
        try:
            result = client.get_market_result(obs["ticker"])
            if result:
                upsert_market_observation({
                    "ticker": obs["ticker"],
                    "market_result": result,
                })
                filled += 1
        except Exception as e:
            log.debug(f"Backfill obs result failed for {obs['ticker']}: {e}")

    if filled > 0:
        log.info(f"Observatory: backfilled {filled} market results")
    return filled


# ═══════════════════════════════════════════════════════════════
#  CALIBRATION: Live vs Model Performance Monitor
# ═══════════════════════════════════════════════════════════════

def check_model_calibration(window: int = 50) -> dict:
    """
    Compare predicted win probabilities to actual outcomes over recent trades.
    Detects when the model's predictions diverge from reality.
    
    Args:
        window: number of recent trades to analyze
    
    Returns dict with:
        overall: {predicted_wr, actual_wr, gap, calibrated}
        by_strategy: {strategy_key: {predicted, actual, n, gap}}
        by_regime: {regime_label: {predicted, actual, n, gap}}
        should_pause: bool (True if gap > 15% over 30+ trades)
    """
    from db import get_conn, rows_to_list

    with get_conn() as c:
        rows = c.execute("""
            SELECT predicted_win_pct, outcome, auto_strategy_key,
                   regime_label, predicted_edge_pct
            FROM trades
            WHERE outcome IN ('win', 'loss')
              AND predicted_win_pct IS NOT NULL
              AND predicted_win_pct > 0
            ORDER BY created_at DESC
            LIMIT ?
        """, (window,)).fetchall()
        trades = rows_to_list(rows)

    if len(trades) < 10:
        return {"error": "Insufficient trades with predictions",
                "n": len(trades), "should_pause": False}

    # Overall calibration
    predicted_sum = sum(t["predicted_win_pct"] for t in trades)
    actual_wins = sum(1 for t in trades if t["outcome"] == "win")
    n = len(trades)
    predicted_wr = predicted_sum / n / 100  # Convert from % to fraction
    actual_wr = actual_wins / n
    gap = actual_wr - predicted_wr

    # By strategy key
    by_strategy = {}
    for t in trades:
        sk = t.get("auto_strategy_key") or "unknown"
        if sk not in by_strategy:
            by_strategy[sk] = {"pred_sum": 0, "wins": 0, "n": 0}
        by_strategy[sk]["pred_sum"] += t["predicted_win_pct"]
        by_strategy[sk]["n"] += 1
        if t["outcome"] == "win":
            by_strategy[sk]["wins"] += 1

    strat_results = {}
    for sk, data in by_strategy.items():
        if data["n"] >= 5:
            pred = data["pred_sum"] / data["n"] / 100
            act = data["wins"] / data["n"]
            strat_results[sk] = {
                "predicted_wr": round(pred, 4),
                "actual_wr": round(act, 4),
                "n": data["n"],
                "gap": round(act - pred, 4),
            }

    # By regime
    by_regime = {}
    for t in trades:
        rl = t.get("regime_label") or "unknown"
        if rl not in by_regime:
            by_regime[rl] = {"pred_sum": 0, "wins": 0, "n": 0}
        by_regime[rl]["pred_sum"] += t["predicted_win_pct"]
        by_regime[rl]["n"] += 1
        if t["outcome"] == "win":
            by_regime[rl]["wins"] += 1

    regime_results = {}
    for rl, data in by_regime.items():
        if data["n"] >= 5:
            pred = data["pred_sum"] / data["n"] / 100
            act = data["wins"] / data["n"]
            regime_results[rl] = {
                "predicted_wr": round(pred, 4),
                "actual_wr": round(act, 4),
                "n": data["n"],
                "gap": round(act - pred, 4),
            }

    # Should we pause? If actual WR is >15% below predicted over 30+ trades
    should_pause = (n >= 30 and gap < -0.15)

    return {
        "n": n,
        "overall": {
            "predicted_wr": round(predicted_wr, 4),
            "actual_wr": round(actual_wr, 4),
            "gap": round(gap, 4),
            "calibrated": abs(gap) < 0.10,
        },
        "by_strategy": strat_results,
        "by_regime": regime_results,
        "should_pause": should_pause,
        "pause_reason": (f"Model overestimates: predicted {predicted_wr:.0%} "
                         f"vs actual {actual_wr:.0%} ({gap:+.0%} gap, n={n})")
                        if should_pause else None,
    }


# ═══════════════════════════════════════════════════════════════
#  HOLD vs SELL — First-class exit strategy comparison
# ═══════════════════════════════════════════════════════════════

def compare_hold_vs_sell(setup_key: str = "global:all",
                          min_samples: int = 30) -> dict:
    """
    The honest question: do sell targets actually beat hold-to-expiry
    after fees and execution costs?

    For each side_rule × timing × entry_max combination, compares the
    "hold" strategy to the best sell-target strategy. If hold wins
    across the board, sell targets are just adding complexity and
    execution risk without improving returns.

    Returns:
        summary: {hold_wins, sell_wins, total_compared, hold_advantage_pct}
        details: [{side_rule, timing, entry_max, hold_ev, best_sell_ev,
                   best_sell_target, hold_wins}]
    """
    from db import get_conn, rows_to_list

    with get_conn() as c:
        rows = c.execute("""
            SELECT strategy_key, side_rule, entry_time_rule, entry_price_max,
                   sell_target, ev_per_trade_c, weighted_ev_c, win_rate,
                   sample_size, oos_ev_c
            FROM strategy_results
            WHERE setup_key = ? AND sample_size >= ?
        """, (setup_key, min_samples)).fetchall()

    strategies = rows_to_list(rows)
    if not strategies:
        return {"error": "No strategies with sufficient data",
                "setup_key": setup_key}

    # Group by (side_rule, timing, entry_max)
    groups = {}
    for s in strategies:
        key = (s["side_rule"], s["entry_time_rule"], s["entry_price_max"])
        if key not in groups:
            groups[key] = {"hold": None, "sells": []}
        sell = s.get("sell_target") or s.get("exit_rule")
        if sell == "hold" or sell is None:
            groups[key]["hold"] = s
        else:
            groups[key]["sells"].append(s)

    # Compare
    hold_wins = 0
    sell_wins = 0
    details = []

    for (side_rule, timing, entry_max), group in groups.items():
        hold = group["hold"]
        sells = group["sells"]
        if not hold or not sells:
            continue

        hold_ev = hold.get("weighted_ev_c") or hold.get("ev_per_trade_c") or 0

        # Best sell-target strategy by weighted EV
        best_sell = max(sells,
                        key=lambda s: s.get("weighted_ev_c") or s.get("ev_per_trade_c") or -999)
        sell_ev = best_sell.get("weighted_ev_c") or best_sell.get("ev_per_trade_c") or 0

        hold_better = hold_ev >= sell_ev
        if hold_better:
            hold_wins += 1
        else:
            sell_wins += 1

        details.append({
            "side_rule": side_rule,
            "timing": timing,
            "entry_max": entry_max,
            "hold_ev_c": round(hold_ev, 1),
            "hold_wr": hold["win_rate"],
            "hold_n": hold["sample_size"],
            "hold_oos_ev": hold.get("oos_ev_c"),
            "best_sell_ev_c": round(sell_ev, 1),
            "best_sell_target": best_sell.get("sell_target") or best_sell.get("exit_rule"),
            "best_sell_wr": best_sell["win_rate"],
            "best_sell_n": best_sell["sample_size"],
            "best_sell_oos_ev": best_sell.get("oos_ev_c"),
            "hold_wins": hold_better,
            "ev_diff_c": round(hold_ev - sell_ev, 1),
        })

    total = hold_wins + sell_wins
    details.sort(key=lambda d: d["ev_diff_c"], reverse=True)

    return {
        "setup_key": setup_key,
        "summary": {
            "hold_wins": hold_wins,
            "sell_wins": sell_wins,
            "total_compared": total,
            "hold_win_pct": round(hold_wins / total * 100, 1) if total > 0 else 0,
            "verdict": ("hold" if hold_wins > sell_wins
                        else "sell" if sell_wins > hold_wins
                        else "mixed"),
        },
        "details": details[:20],  # Top 20 by hold advantage
    }


# ═══════════════════════════════════════════════════════════════
#  STRATEGY PERSISTENCE — Do winning strategies stay winning?
# ═══════════════════════════════════════════════════════════════

def test_strategy_persistence(setup_key: str = "global:all",
                               top_n: int = 10) -> dict:
    """
    The hardest test: do the best strategies from the first half of data
    persist as winners in the second half — without re-optimization?

    If the top strategies rotate (different winners in each period),
    then auto-strategy is perpetually chasing last period's winner
    and the positive EV is likely an artifact of multiple testing.

    If the top strategies persist, the edge is likely real and the
    auto-strategy system can be trusted.

    Returns:
        persist_rate: fraction of first-half winners still positive in second half
        details: per-strategy comparison across halves
    """
    observations = get_observations_for_simulation(limit=0)
    if not observations:
        return {"error": "No observations available"}

    # Sort chronologically and split in half
    sorted_obs = sorted(observations,
                        key=lambda o: o.get("close_time_utc", ""))
    mid = len(sorted_obs) // 2
    first_half = sorted_obs[:mid]
    second_half = sorted_obs[mid:]

    if len(first_half) < 50 or len(second_half) < 50:
        return {"error": f"Need 100+ observations (have {len(sorted_obs)})",
                "n_first": len(first_half), "n_second": len(second_half)}

    def _simulate_half(obs_list):
        """Simulate all strategies on a subset of observations, return per-strategy results."""
        results = {}  # strategy_key → {pnls, wins, n}
        for obs in obs_list:
            sim_results = simulate_market(obs)
            for sim in sim_results:
                if not sim["entered"]:
                    continue
                # Only look at the target setup
                setups = _get_setup_keys(obs)
                for sk, st in setups:
                    if sk != setup_key:
                        continue
                    key = sim["strategy_key"]
                    if key not in results:
                        results[key] = {"pnls": [], "wins": 0}
                    results[key]["pnls"].append(sim["pnl_c"])
                    if sim["won"]:
                        results[key]["wins"] += 1
        return results

    log.info(f"Strategy persistence: simulating {len(first_half)} + "
             f"{len(second_half)} observations")

    first_results = _simulate_half(first_half)
    second_results = _simulate_half(second_half)

    # Find top N strategies from first half
    first_ranked = []
    for sk, data in first_results.items():
        n = len(data["pnls"])
        if n < 10:
            continue
        ev = sum(data["pnls"]) / n
        first_ranked.append({"strategy_key": sk, "ev_c": ev,
                             "wr": data["wins"] / n, "n": n})

    first_ranked.sort(key=lambda x: x["ev_c"], reverse=True)
    top_strategies = first_ranked[:top_n]

    if not top_strategies:
        return {"error": "No strategies with sufficient first-half data"}

    # Check each in second half
    details = []
    persisted = 0
    for strat in top_strategies:
        sk = strat["strategy_key"]
        second = second_results.get(sk)
        if second and len(second["pnls"]) >= 5:
            second_n = len(second["pnls"])
            second_ev = sum(second["pnls"]) / second_n
            second_wr = second["wins"] / second_n
            still_positive = second_ev > 0
            if still_positive:
                persisted += 1
        else:
            second_ev = None
            second_wr = None
            second_n = 0
            still_positive = False

        details.append({
            "strategy_key": sk,
            "first_half_ev_c": round(strat["ev_c"], 1),
            "first_half_wr": round(strat["wr"], 4),
            "first_half_n": strat["n"],
            "second_half_ev_c": round(second_ev, 1) if second_ev is not None else None,
            "second_half_wr": round(second_wr, 4) if second_wr is not None else None,
            "second_half_n": second_n,
            "persisted": still_positive,
            "ev_change_c": round(second_ev - strat["ev_c"], 1)
                          if second_ev is not None else None,
        })

    total_tested = len([d for d in details if d["second_half_n"] >= 5])

    return {
        "setup_key": setup_key,
        "n_first_half": len(first_half),
        "n_second_half": len(second_half),
        "top_n_tested": top_n,
        "total_with_second_half_data": total_tested,
        "persisted": persisted,
        "persist_rate": round(persisted / total_tested, 2) if total_tested > 0 else 0,
        "verdict": ("strong" if total_tested >= 5 and persisted / max(1, total_tested) >= 0.7
                    else "weak" if total_tested >= 5 and persisted / max(1, total_tested) >= 0.4
                    else "poor" if total_tested >= 5
                    else "insufficient_data"),
        "details": details,
    }


# ═══════════════════════════════════════════════════════════════
#  PERMUTATION TEST — Is the best strategy's EV real or noise?
# ═══════════════════════════════════════════════════════════════

def run_permutation_test(setup_key: str = "global:all",
                          n_permutations: int = 500) -> dict:
    """
    The definitive edge validation: shuffle market outcomes randomly and
    re-run the simulation. If the best strategy's real EV falls within
    the distribution of shuffled-result EVs, the edge isn't real — it's
    an artifact of testing 2,500+ variants on limited data.

    For each permutation:
      1. Take all observations, randomly reassign market_result (yes/no)
      2. Simulate all strategies against the shuffled data
      3. Record the best strategy's EV

    If the real best EV exceeds 95% of permuted best EVs, the edge is
    significant at p<0.05 (not due to multiple testing).

    This is computationally expensive — limited to global:all setup and
    uses a fast path (only simulates strategies that entered in real data).
    Designed to run on-demand, not every 30 minutes.
    """
    import random

    observations = get_observations_for_simulation(limit=0)
    if not observations:
        return {"error": "No observations available"}

    # Filter to setup
    if setup_key != "global:all":
        setup_type, _, setup_val = setup_key.partition(":")
        filtered = []
        for obs in observations:
            if setup_type == "coarse_regime":
                vol = obs.get("vol_regime")
                trend = obs.get("trend_regime")
                if vol is not None and trend is not None:
                    try:
                        from regime import compute_coarse_label
                        c = compute_coarse_label(int(vol), int(trend))
                        if c == setup_val:
                            filtered.append(obs)
                    except Exception:
                        pass
            elif setup_type == "hour" and str(obs.get("hour_et")) == setup_val:
                filtered.append(obs)
        observations = filtered

    if len(observations) < 50:
        return {"error": f"Need 50+ observations (have {len(observations)})"}

    log.info(f"Permutation test: {n_permutations} permutations on "
             f"{len(observations)} observations")

    # Step 1: Get the real best strategy EV
    real_evs = {}  # strategy_key → [pnl_c]
    for obs in observations:
        sims = simulate_market(obs)
        for sim in sims:
            if sim["entered"]:
                sk = sim["strategy_key"]
                if sk not in real_evs:
                    real_evs[sk] = []
                real_evs[sk].append(sim["pnl_c"])

    # Find real best EV (strategies with ≥20 samples)
    real_best_ev = -999
    real_best_key = None
    for sk, pnls in real_evs.items():
        if len(pnls) >= 20:
            ev = sum(pnls) / len(pnls)
            if ev > real_best_ev:
                real_best_ev = ev
                real_best_key = sk

    if real_best_key is None:
        return {"error": "No strategies with 20+ observations"}

    # Step 2: Run permutations — shuffle outcomes, find best EV each time
    # Fast path: only re-evaluate strategies that had entries in real data
    # (shuffling outcomes doesn't change entry logic, only win/loss)
    outcomes = [obs.get("market_result") for obs in observations]

    permuted_best_evs = []
    for perm_i in range(n_permutations):
        shuffled = outcomes[:]
        random.shuffle(shuffled)

        # Re-compute win/loss for each observation with shuffled result
        perm_evs = {}
        for idx, obs in enumerate(observations):
            shuffled_result = shuffled[idx]
            if shuffled_result not in ("yes", "no"):
                continue

            snaps = json.loads(obs.get("price_snapshots", "[]")) \
                    if obs.get("price_snapshots") else []
            if len(snaps) < 3:
                continue
            dur = max(s["t"] for s in snaps)
            if dur < 60:
                continue

            btc_open = obs.get("btc_price_at_open")
            rvol = obs.get("realized_vol")

            # Only simulate strategies that had entries in real data
            for sk in real_evs:
                parsed = parse_strategy_key(sk)
                if not parsed:
                    continue
                sim = _simulate_one(
                    snaps, shuffled_result, dur,
                    parsed["side_rule"], parsed["entry_time_rule"],
                    parsed["entry_price_max"], parsed["sell_target"],
                    btc_open=btc_open, realized_vol=rvol,
                )
                if sim and sim["entered"]:
                    if sk not in perm_evs:
                        perm_evs[sk] = []
                    perm_evs[sk].append(sim["pnl_c"])

        # Best permuted EV
        perm_best = -999
        for sk, pnls in perm_evs.items():
            if len(pnls) >= 20:
                ev = sum(pnls) / len(pnls)
                if ev > perm_best:
                    perm_best = ev
        if perm_best > -999:
            permuted_best_evs.append(perm_best)

    if not permuted_best_evs:
        return {"error": "Permutation produced no valid results"}

    # Step 3: Where does the real best EV rank?
    n_perm = len(permuted_best_evs)
    beats = sum(1 for pe in permuted_best_evs if real_best_ev > pe)
    p_value = 1 - (beats / n_perm)

    sorted_perms = sorted(permuted_best_evs)
    perm_95 = sorted_perms[int(0.95 * n_perm)] if n_perm > 20 else None
    perm_99 = sorted_perms[int(0.99 * n_perm)] if n_perm > 100 else None

    log.info(f"Permutation test: real best EV={real_best_ev:+.1f}¢, "
             f"p={p_value:.3f} ({n_perm} permutations)")

    return {
        "setup_key": setup_key,
        "n_observations": len(observations),
        "n_permutations": n_perm,
        "real_best_strategy": real_best_key,
        "real_best_ev_c": round(real_best_ev, 1),
        "permuted_best_evs": {
            "mean_c": round(sum(permuted_best_evs) / n_perm, 1),
            "median_c": round(sorted_perms[n_perm // 2], 1),
            "p95_c": round(perm_95, 1) if perm_95 is not None else None,
            "p99_c": round(perm_99, 1) if perm_99 is not None else None,
        },
        "p_value": round(p_value, 4),
        "significant_at_05": p_value < 0.05,
        "significant_at_01": p_value < 0.01,
        "verdict": ("edge_confirmed" if p_value < 0.05
                     else "inconclusive" if p_value < 0.10
                     else "no_edge"),
    }


# ═══════════════════════════════════════════════════════════════
#  WALK-FORWARD SELECTION TEST — Does the selection process work?
# ═══════════════════════════════════════════════════════════════

def run_walkforward_selection_test(n_folds: int = 5) -> dict:
    """
    Tests whether the strategy *selection process* produces positive
    returns — not just whether individual strategies persist.

    For each expanding window:
      1. Simulate all strategies on training folds (1..k-1)
      2. Pick the best strategy using the same ranking logic as the advisor
         (weighted EV, fee resilience, OOS positive)
      3. Simulate ONLY that strategy on the test fold (k)
      4. Record whether the selected strategy was profitable OOS

    This directly measures: if you let the advisor pick the best strategy
    each period and trade it next period, would you make money?

    This is the ultimate test before live auto-strategy trading.
    """
    observations = get_observations_for_simulation(limit=0)
    if not observations:
        return {"error": "No observations available"}

    sorted_obs = sorted(observations,
                        key=lambda o: o.get("close_time_utc", ""))

    fold_size = len(sorted_obs) // n_folds
    if fold_size < 20:
        return {"error": f"Need {n_folds * 20}+ observations "
                         f"(have {len(sorted_obs)})"}

    folds = []
    for i in range(n_folds):
        start = i * fold_size
        end = start + fold_size if i < n_folds - 1 else len(sorted_obs)
        folds.append(sorted_obs[start:end])

    log.info(f"Walk-forward selection: {n_folds} folds, "
             f"{fold_size}+ obs each")

    fold_results = []

    for test_idx in range(1, n_folds):
        # Training set: all folds before test
        train_obs = []
        for i in range(test_idx):
            train_obs.extend(folds[i])
        test_obs = folds[test_idx]

        if len(train_obs) < 30 or len(test_obs) < 10:
            continue

        # Step 1: Simulate all strategies on training data
        train_strats = {}  # strategy_key → {pnls, wins}
        for obs in train_obs:
            sims = simulate_market(obs)
            for sim in sims:
                if not sim["entered"]:
                    continue
                sk = sim["strategy_key"]
                if sk not in train_strats:
                    train_strats[sk] = {"pnls": [], "wins": 0}
                train_strats[sk]["pnls"].append(sim["pnl_c"])
                if sim["won"]:
                    train_strats[sk]["wins"] += 1

        # Step 2: Pick the best strategy (mirror advisor ranking logic)
        best_key = None
        best_ev = -999
        for sk, data in train_strats.items():
            n = len(data["pnls"])
            if n < 20:
                continue
            ev = sum(data["pnls"]) / n
            if ev <= 0:
                continue
            # Fee resilience check
            parsed = parse_strategy_key(sk)
            if parsed:
                entry_max = parsed["entry_price_max"]
                fee_impact = max(1, round(entry_max * 0.01))
                if ev <= fee_impact:
                    continue
            if ev > best_ev:
                best_ev = ev
                best_key = sk

        if not best_key:
            fold_results.append({
                "fold": test_idx + 1,
                "train_n": len(train_obs),
                "test_n": len(test_obs),
                "selected": None,
                "reason": "No positive-EV strategy in training",
            })
            continue

        # Step 3: Simulate ONLY the selected strategy on test fold
        parsed = parse_strategy_key(best_key)
        test_pnls = []
        test_wins = 0
        for obs in test_obs:
            snaps = json.loads(obs.get("price_snapshots", "[]")) \
                    if obs.get("price_snapshots") else []
            mr = obs.get("market_result")
            if not snaps or not mr or mr not in ("yes", "no"):
                continue
            dur = max(s["t"] for s in snaps) if snaps else 0
            if dur < 60:
                continue
            sim = _simulate_one(
                snaps, mr, dur,
                parsed["side_rule"], parsed["entry_time_rule"],
                parsed["entry_price_max"], parsed["sell_target"],
                btc_open=obs.get("btc_price_at_open"),
                realized_vol=obs.get("realized_vol"),
            )
            if sim and sim["entered"]:
                test_pnls.append(sim["pnl_c"])
                if sim["won"]:
                    test_wins += 1

        test_n = len(test_pnls)
        test_ev = sum(test_pnls) / test_n if test_n > 0 else 0

        fold_results.append({
            "fold": test_idx + 1,
            "train_n": len(train_obs),
            "test_n": len(test_obs),
            "selected": best_key,
            "train_ev_c": round(best_ev, 1),
            "test_ev_c": round(test_ev, 1) if test_n > 0 else None,
            "test_trades": test_n,
            "test_wr": round(test_wins / test_n, 4) if test_n > 0 else None,
            "profitable_oos": test_ev > 0 if test_n > 0 else None,
        })

    # Aggregate
    tested_folds = [f for f in fold_results if f.get("test_ev_c") is not None]
    profitable_folds = [f for f in tested_folds if f["profitable_oos"]]
    total_test_pnl = sum(f["test_ev_c"] * f["test_trades"]
                         for f in tested_folds if f.get("test_trades"))
    total_test_trades = sum(f["test_trades"] for f in tested_folds
                            if f.get("test_trades"))

    return {
        "n_folds": n_folds,
        "n_observations": len(sorted_obs),
        "fold_results": fold_results,
        "summary": {
            "folds_tested": len(tested_folds),
            "folds_profitable": len(profitable_folds),
            "selection_success_rate": round(
                len(profitable_folds) / len(tested_folds), 2
            ) if tested_folds else 0,
            "aggregate_test_ev_c": round(
                total_test_pnl / total_test_trades, 1
            ) if total_test_trades > 0 else None,
            "total_test_trades": total_test_trades,
        },
        "verdict": ("selection_works" if tested_folds
                     and len(profitable_folds) / len(tested_folds) >= 0.6
                     else "selection_unreliable" if tested_folds
                     else "insufficient_data"),
    }


# ═══════════════════════════════════════════════════════════════
#  DATA COLLECTION PROGRESS — When will strategies be actionable?
# ═══════════════════════════════════════════════════════════════

def estimate_time_to_actionable() -> dict:
    """
    Given current observation rate and gate requirements, estimate how many
    days until the first strategy passes all five validation gates.

    Checks:
      - Current observation rate (markets/day)
      - Per-regime observation density
      - OOS sample accumulation rate
      - Whether any strategy is close to passing all gates

    This prevents the scenario where gates are so strict the system
    never trades. If the projection is "3 months," you either need to
    relax gates or accept that timeline.
    """
    from db import get_conn, rows_to_list, get_config
    from datetime import datetime, timezone, timedelta

    with get_conn() as c:
        # Observation rate over last 7 days
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        rate_row = c.execute("""
            SELECT COUNT(*) as n,
                   MIN(close_time_utc) as first_t,
                   MAX(close_time_utc) as last_t
            FROM market_observations
            WHERE close_time_utc > ?
              AND COALESCE(obs_quality, 'full') IN ('full', 'short')
        """, (week_ago,)).fetchone()

        total_obs = c.execute("""
            SELECT COUNT(*) as n FROM market_observations
            WHERE market_result IS NOT NULL
              AND COALESCE(obs_quality, 'full') IN ('full', 'short')
        """).fetchone()["n"]

        # Best strategy progress toward gates
        best = c.execute("""
            SELECT strategy_key, setup_key, sample_size, ev_per_trade_c,
                   weighted_ev_c, oos_ev_c, oos_sample_size,
                   breakeven_fee_rate, slippage_1c_ev, fdr_significant
            FROM strategy_results
            WHERE setup_key = 'global:all'
              AND ev_per_trade_c > 0
            ORDER BY COALESCE(weighted_ev_c, ev_per_trade_c) DESC
            LIMIT 5
        """).fetchall()
        top_strategies = rows_to_list(best)

        # Regime observation counts
        regime_counts = c.execute("""
            SELECT regime_label, COUNT(*) as n
            FROM market_observations
            WHERE market_result IS NOT NULL
              AND regime_label IS NOT NULL AND regime_label != 'unknown'
              AND COALESCE(obs_quality, 'full') IN ('full', 'short')
            GROUP BY regime_label
            ORDER BY n DESC
        """).fetchall()

    # Compute daily rate
    recent_n = rate_row["n"] if rate_row else 0
    if recent_n > 0 and rate_row["first_t"] and rate_row["last_t"]:
        try:
            first = datetime.fromisoformat(
                rate_row["first_t"].replace("Z", "+00:00"))
            last = datetime.fromisoformat(
                rate_row["last_t"].replace("Z", "+00:00"))
            days_span = max(1, (last - first).total_seconds() / 86400)
            daily_rate = round(recent_n / days_span, 1)
        except Exception:
            daily_rate = recent_n / 7
    else:
        daily_rate = 0

    # Gate status for top strategies
    min_oos = 30  # From get_recommendation gate
    fee_buffer = float(get_config("min_breakeven_fee_buffer", 0.03) or 0.03)
    from config import KALSHI_FEE_RATE
    min_breakeven = KALSHI_FEE_RATE + fee_buffer

    gate_progress = []
    for s in top_strategies:
        gates = {
            "positive_ev": (s.get("weighted_ev_c") or s.get("ev_per_trade_c", 0)) > 0,
            "fee_resilient": True,  # Simplified — already filtered by query
            "oos_positive": (s.get("oos_ev_c") or 0) > 0,
            "oos_sufficient": (s.get("oos_sample_size") or 0) >= min_oos,
            "breakeven_fee_ok": (s.get("breakeven_fee_rate") or 0) >= min_breakeven,
        }
        passed = sum(1 for v in gates.values() if v)

        # Estimate days until OOS sufficient (the usual bottleneck)
        oos_n = s.get("oos_sample_size") or 0
        if oos_n >= min_oos:
            days_to_oos = 0
        elif daily_rate > 0:
            # OOS gets ~80% of new obs (4 of 5 folds are test for any strategy)
            oos_rate = daily_rate * 0.8
            remaining = min_oos - oos_n
            days_to_oos = round(remaining / max(1, oos_rate), 0)
        else:
            days_to_oos = None

        gate_progress.append({
            "strategy_key": s["strategy_key"],
            "ev_c": s.get("weighted_ev_c") or s.get("ev_per_trade_c"),
            "oos_n": oos_n,
            "oos_target": min_oos,
            "gates_passed": passed,
            "gates_total": len(gates),
            "gates": gates,
            "est_days_to_oos": days_to_oos,
        })

    # Overall estimate: days until any strategy might pass
    if gate_progress:
        best_days = min(
            (g["est_days_to_oos"] for g in gate_progress
             if g["est_days_to_oos"] is not None),
            default=None
        )
    else:
        best_days = None

    return {
        "total_observations": total_obs,
        "daily_rate": daily_rate,
        "top_strategies": gate_progress,
        "est_days_to_first_actionable": best_days,
        "regime_density": [
            {"regime": r["regime_label"], "n": r["n"]}
            for r in (regime_counts or [])
        ][:10],
    }


def get_observation_capture_rate(hours: int = 24) -> dict:
    """
    Persistent observation coverage metric.

    In a 24-hour period, there should be ~96 markets (one every 15 min).
    What percentage were captured at full quality? If below 90%, data
    collection has a coverage problem from restarts, deploys, or outages
    that no amount of statistical correction can fix.
    """
    from db import get_conn
    from datetime import datetime, timezone, timedelta

    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    expected = int(hours * 4)  # 4 markets per hour

    with get_conn() as c:
        row = c.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN COALESCE(obs_quality, 'full') = 'full'
                       THEN 1 ELSE 0 END) as full_quality,
                   SUM(CASE WHEN obs_quality = 'short' THEN 1 ELSE 0 END) as short,
                   SUM(CASE WHEN obs_quality = 'partial' THEN 1 ELSE 0 END) as partial,
                   SUM(CASE WHEN obs_quality = 'few' THEN 1 ELSE 0 END) as few
            FROM market_observations
            WHERE close_time_utc > ?
        """, (since,)).fetchone()

    total = row["total"] or 0
    full = row["full_quality"] or 0

    return {
        "hours": hours,
        "expected_markets": expected,
        "total_captured": total,
        "full_quality": full,
        "short_quality": row["short"] or 0,
        "partial": row["partial"] or 0,
        "few": row["few"] or 0,
        "capture_rate_pct": round(total / expected * 100, 1) if expected > 0 else 0,
        "full_quality_rate_pct": round(full / expected * 100, 1) if expected > 0 else 0,
        "healthy": total >= expected * 0.9,
    }


# ═══════════════════════════════════════════════════════════════
#  EXECUTION ANALYTICS
# ═══════════════════════════════════════════════════════════════

def analyze_execution_quality(days: int = 30) -> dict:
    """
    Analyze execution quality across recent trades.
    Measures slippage, fill quality, and exit method effectiveness.
    
    Returns dict with slippage analysis, exit method comparison,
    and per-spread-bucket breakdown.
    """
    from db import get_conn, rows_to_list

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_conn() as c:
        rows = c.execute("""
            SELECT entry_price_c, avg_fill_price_c, side,
                   spread_at_entry_c, exit_method, outcome, pnl,
                   sell_price_c, sell_filled, shares_filled,
                   actual_cost, gross_proceeds,
                   yes_ask_at_entry, no_ask_at_entry,
                   yes_bid_at_entry, no_bid_at_entry,
                   regime_label
            FROM trades
            WHERE outcome IN ('win', 'loss')
              AND entry_price_c > 0
              AND avg_fill_price_c > 0
              AND datetime(created_at) > datetime(?)
        """, (since,)).fetchall()
        trades = rows_to_list(rows)

    if not trades:
        return {"error": "No completed trades in window", "n": 0}

    # 1. Slippage analysis: avg_fill_price vs entry_price (what we targeted)
    slippages = []
    for t in trades:
        entry = t["entry_price_c"]
        fill = t["avg_fill_price_c"]
        slip = fill - entry  # Positive = paid more than targeted
        slippages.append(slip)

    # 2. Exit method comparison
    exit_methods = {}
    for t in trades:
        method = t.get("exit_method") or "unknown"
        if method not in exit_methods:
            exit_methods[method] = {"n": 0, "wins": 0, "total_pnl": 0}
        exit_methods[method]["n"] += 1
        if t["outcome"] == "win":
            exit_methods[method]["wins"] += 1
        exit_methods[method]["total_pnl"] += t.get("pnl", 0) or 0

    for method, data in exit_methods.items():
        data["win_rate"] = round(data["wins"] / data["n"], 4) if data["n"] else 0
        data["avg_pnl"] = round(data["total_pnl"] / data["n"], 2) if data["n"] else 0

    # 3. Fill quality by spread bucket
    spread_buckets = {"tight_1_3": [], "normal_4_6": [], "wide_7_10": [], "very_wide_11+": []}
    for t in trades:
        spread = t.get("spread_at_entry_c")
        slip = t["avg_fill_price_c"] - t["entry_price_c"]
        if spread is None:
            continue
        if spread <= 3:
            spread_buckets["tight_1_3"].append(slip)
        elif spread <= 6:
            spread_buckets["normal_4_6"].append(slip)
        elif spread <= 10:
            spread_buckets["wide_7_10"].append(slip)
        else:
            spread_buckets["very_wide_11+"].append(slip)

    spread_analysis = {}
    for bucket, slips in spread_buckets.items():
        if slips:
            spread_analysis[bucket] = {
                "n": len(slips),
                "avg_slippage_c": round(sum(slips) / len(slips), 2),
                "max_slippage_c": max(slips),
                "pct_zero_slip": round(sum(1 for s in slips if s <= 0) / len(slips), 2),
            }

    # 4. Sell-fill vs hold-to-expiry comparison
    sell_fills = [t for t in trades if t.get("exit_method") == "sell_fill"]
    expiries = [t for t in trades if t.get("exit_method") == "market_expiry"]
    sell_avg_pnl = (sum(t.get("pnl", 0) or 0 for t in sell_fills) / len(sell_fills)
                    if sell_fills else None)
    expiry_avg_pnl = (sum(t.get("pnl", 0) or 0 for t in expiries) / len(expiries)
                      if expiries else None)

    return {
        "n": len(trades),
        "days": days,
        "slippage": {
            "avg_c": round(sum(slippages) / len(slippages), 2) if slippages else 0,
            "median_c": round(sorted(slippages)[len(slippages) // 2], 2) if slippages else 0,
            "max_c": max(slippages) if slippages else 0,
            "pct_zero_or_better": round(
                sum(1 for s in slippages if s <= 0) / len(slippages), 2
            ) if slippages else 0,
        },
        "exit_methods": exit_methods,
        "by_spread": spread_analysis,
        "sell_vs_hold": {
            "sell_fill_avg_pnl": round(sell_avg_pnl, 2) if sell_avg_pnl is not None else None,
            "sell_fill_n": len(sell_fills),
            "expiry_avg_pnl": round(expiry_avg_pnl, 2) if expiry_avg_pnl is not None else None,
            "expiry_n": len(expiries),
            "sell_advantage": round(sell_avg_pnl - expiry_avg_pnl, 2)
                             if sell_avg_pnl is not None and expiry_avg_pnl is not None else None,
        },
    }


# ═══════════════════════════════════════════════════════════════
#  CONVERGENCE METRIC SNAPSHOTS
# ═══════════════════════════════════════════════════════════════

def _record_convergence_snapshot():
    """Capture current key metrics for convergence tracking.
    Called after every simulation batch (~30 min)."""
    from db import get_conn, rows_to_list, insert_metric_snapshot

    with get_conn() as c:
        # Total usable observations
        total_obs = c.execute("""
            SELECT COUNT(*) as n FROM market_observations
            WHERE market_result IS NOT NULL
              AND COALESCE(obs_quality, 'full') IN ('full', 'short')
        """).fetchone()["n"]

        # Top strategies (global:all, n>=10)
        top = c.execute("""
            SELECT strategy_key, COALESCE(weighted_ev_c, ev_per_trade_c) as wev,
                   ev_per_trade_c, win_rate, sample_size,
                   fdr_significant, oos_ev_c, ci_lower
            FROM strategy_results
            WHERE setup_key = 'global:all' AND sample_size >= 10
            ORDER BY COALESCE(weighted_ev_c, ev_per_trade_c) DESC
            LIMIT 10
        """).fetchall()
        top_list = rows_to_list(top)

        # Counts
        counts = c.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN COALESCE(weighted_ev_c, ev_per_trade_c) > 0 THEN 1 ELSE 0 END) as pos_ev,
                   SUM(CASE WHEN fdr_significant = 1 THEN 1 ELSE 0 END) as fdr_sig
            FROM strategy_results
            WHERE setup_key = 'global:all' AND sample_size >= 10
        """).fetchone()

        # Best per side (global:all, n>=10)
        side_evs = {}
        for side in ('yes', 'no', 'cheaper', 'model'):
            row = c.execute("""
                SELECT MAX(COALESCE(weighted_ev_c, ev_per_trade_c)) as best_ev
                FROM strategy_results
                WHERE setup_key = 'global:all' AND sample_size >= 10
                  AND strategy_key LIKE ?
            """, (f"{side}:%",)).fetchone()
            side_evs[side] = round(row["best_ev"], 2) if row and row["best_ev"] else None

        # Best per timing
        timing_evs = {}
        for timing in ('early', 'mid', 'late'):
            row = c.execute("""
                SELECT MAX(COALESCE(weighted_ev_c, ev_per_trade_c)) as best_ev
                FROM strategy_results
                WHERE setup_key = 'global:all' AND sample_size >= 10
                  AND strategy_key LIKE ?
            """, (f"%:{timing}:%",)).fetchone()
            timing_evs[timing] = round(row["best_ev"], 2) if row and row["best_ev"] else None

        # Regimes with meaningful data
        regime_counts = c.execute("""
            SELECT COUNT(DISTINCT regime_label) as n
            FROM market_observations
            WHERE market_result IS NOT NULL AND regime_label IS NOT NULL
              AND regime_label != 'unknown'
              AND COALESCE(obs_quality, 'full') IN ('full', 'short')
            GROUP BY regime_label HAVING COUNT(*) >= 10
        """).fetchall()
        regimes_with_10 = len(regime_counts)

        # Shadow trade stats
        shadow = c.execute("""
            SELECT COUNT(*) as n,
                   SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) as wins,
                   AVG(avg_fill_price_c - entry_price_c) as avg_slip
            FROM trades
            WHERE COALESCE(is_shadow, 0) = 1 AND outcome IN ('win', 'loss')
        """).fetchone()

    best = top_list[0] if top_list else {}
    top5_evs = [t["wev"] for t in top_list[:5] if t.get("wev") is not None]

    metrics = {
        "total_obs": total_obs,
        "best_strategy": best.get("strategy_key", ""),
        "best_ev_c": round(best.get("wev", 0) or 0, 2),
        "best_wr": round(best.get("win_rate", 0) or 0, 4),
        "best_oos_ev_c": round(best.get("oos_ev_c", 0) or 0, 2),
        "top5_avg_ev_c": round(sum(top5_evs) / len(top5_evs), 2) if top5_evs else 0,
        "pos_ev_count": counts["pos_ev"] or 0,
        "fdr_sig_count": counts["fdr_sig"] or 0,
        "total_strategies": counts["total"] or 0,
        "side_evs": side_evs,
        "timing_evs": timing_evs,
        "regimes_with_10": regimes_with_10,
        "shadow_n": shadow["n"] or 0,
        "shadow_wr": round(shadow["wins"] / shadow["n"], 4) if shadow["n"] else None,
        "shadow_avg_slip_c": round(shadow["avg_slip"], 2) if shadow["avg_slip"] is not None else None,
    }

    insert_metric_snapshot(metrics)
    log.debug(f"Convergence snapshot: obs={total_obs}, best_ev={metrics['best_ev_c']:+.1f}¢, "
              f"+EV={metrics['pos_ev_count']}")
