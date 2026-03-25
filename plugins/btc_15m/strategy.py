"""
strategy.py — Strategy Observatory for BTC 15-min markets.

Three layers:
  1. Observatory: Records every market's price path + regime context + outcome
  2. Laboratory: Simulates strategies against recorded observations
  3. Advisor: Surfaces insights (best strategies per setup, confidence levels)
"""

import json
import math
import time
import logging
from datetime import datetime, timezone

from config import ET, KALSHI_FEE_RATE
from db import get_conn, now_utc, rows_to_list
from plugins.btc_15m.market_db import (
    upsert_observation, get_unresolved_observations,
    get_observations_for_simulation, upsert_strategy_result,
    insert_metric_snapshot,
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
        the bot's decision-time label."""
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
        Stores ALL observations with a quality flag instead of
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

        # BTC movement — with distance-from-open tracking
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
            upsert_observation(data)
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
#  OBSERVATION BACKFILL
# ═══════════════════════════════════════════════════════════════

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
                upsert_observation({
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
#  LAYER 2: LABORATORY — Strategy Simulation Engine
# ═══════════════════════════════════════════════════════════════

# Strategy dimension definitions — 5¢ increments, absolute sell targets
# Side rules: cheaper (buy whichever side costs less), yes (always YES),
#   no (always NO), model (BTC FV model picks side using Brownian bridge P(yes))
SIDE_RULES = ["cheaper", "yes", "no", "model"]
ENTRY_TIME_RULES = ["early", "mid", "late"]
ENTRY_MAXES = list(range(5, 100, 5))   # 5, 10, 15, ... 95

# Setup hierarchy — coarse_regime is the primary regime dimension.
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
    """Parse strategy key into components."""
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
    """Simulate all strategy variants against one market observation."""
    result = obs["market_result"]
    if result not in ("yes", "no"):
        return []

    snapshots = json.loads(obs["price_snapshots"]) if obs.get("price_snapshots") else []
    if len(snapshots) < 3:
        return []

    market_duration = max(s["t"] for s in snapshots)
    if market_duration < 60:
        return []

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

    side_rule: "cheaper", "yes", "no", "model"
    sell_target is int (absolute cents) or 'hold'.
    fee_rate defaults to KALSHI_FEE_RATE (7%).
    slippage_c: additional cents added to entry cost.
    Returns {entered: bool, won: bool, pnl_c: int} or None if no valid entry.
    """
    # "model" side rule requires BTC open price
    if side_rule == "model" and (not btc_open or btc_open <= 0):
        return {"entered": False, "won": False, "pnl_c": 0,
                "sold_early": False, "sell_btc_distance_pct": None}

    # 1. Determine when to start looking — absolute seconds into market
    if time_rule == "mid":
        t_min = 300                        # 5 min
    elif time_rule == "late":
        t_min = 600                        # 10 min
    else:  # "early"
        t_min = 0
    t_max = duration - 30              # Stop 30s before close

    # 2. Scan snapshots for valid entry price with two-snapshot fill delay
    entry_snap = None
    side = None
    entry_price = 0
    spotted = False
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
            btc_now = s.get("btc")
            if not btc_now or btc_now <= 0 or not btc_open:
                continue
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

        if _price > 0 and _price <= max_price:
            if not spotted:
                spotted = True
            else:
                entry_snap = s
                side = _side
                entry_price = _price
                break
        else:
            spotted = False

    if entry_snap is None:
        return {"entered": False, "won": False, "pnl_c": 0,
                "sold_early": False, "sell_btc_distance_pct": None}

    # 3. Compute fee and cost
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
            gross_c = 100 if won else 0
            pnl_c = gross_c - cost_c
        else:
            bid_key = "yb" if side == "yes" else "nb"
            sell_spotted = False
            for s in snapshots:
                if s["t"] <= entry_snap["t"]:
                    continue
                bid = s.get(bid_key, 0)
                if bid >= target_c + 1:
                    if not sell_spotted:
                        sell_spotted = True
                    else:
                        gross_c = target_c
                        pnl_c = gross_c - cost_c
                        sold = True
                        if btc_open and btc_open > 0 and s.get("btc"):
                            sell_btc_dist = round(
                                (s["btc"] - btc_open) / btc_open * 100, 4)
                        break
                else:
                    sell_spotted = False
            if not sold:
                gross_c = 100 if won else 0
                pnl_c = gross_c - cost_c

    return {"entered": True, "won": won, "pnl_c": pnl_c,
            "sold_early": sold if sell_target != "hold" else False,
            "sell_btc_distance_pct": sell_btc_dist if (sell_target != "hold") else None}


# ═══════════════════════════════════════════════════════════════
#  SIMULATION BATCH ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════

def run_simulation_batch(limit: int = 0):
    """
    Full recompute: simulate ALL resolved observations and rebuild strategy_results.

    Reprocessable by design — clear strategy_results and re-run if needed.
    Called periodically from regime worker (~30 min).

    limit: max observations to process (0 = all available)
    """
    observations = get_observations_for_simulation(limit=limit)
    if not observations:
        return 0

    n_obs = len(observations)
    log.info(f"Observatory: simulating across {n_obs} markets")

    # Clean up stale results from removed setup types
    try:
        with get_conn() as c:
            deleted = c.execute("""
                DELETE FROM btc15m_strategy_results
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
    accum = {}

    for obs in observations:
        sim_results = simulate_market(obs)
        setups = _get_setup_keys(obs)
        obs_quality = obs.get("obs_quality") or "full"
        obs_close = obs.get("close_time_utc", "")

        for sim in sim_results:
            if not sim["entered"]:
                continue
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

        # Free heavy snapshot data
        obs.pop("price_snapshots", None)
        obs.pop("_snapshots_parsed", None)

    # Write results
    wrote = 0
    for (setup_key, strat_key), data in accum.items():
        trades = data["trades"]
        if not trades:
            continue
        _write_strategy_result(setup_key, data["setup_type"],
                               strat_key, data, trades)
        wrote += 1

    if wrote > 0:
        log.info(f"Observatory: wrote {wrote} strategy results from {n_obs} markets")

    # Slippage sensitivity pass
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

    # Check for new strategy discoveries
    if wrote > 0:
        try:
            _check_strategy_discoveries()
        except Exception as e:
            log.debug(f"Strategy discovery check error: {e}")

    # Apply FDR correction
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

    # Free memory
    del accum
    del observations
    import gc
    gc.collect()

    return n_obs


# ═══════════════════════════════════════════════════════════════
#  WALK-FORWARD VALIDATION
# ═══════════════════════════════════════════════════════════════

def _run_walk_forward(observations: list, accum: dict):
    """Rolling walk-forward validation with expanding training window.
    5 folds, trains on folds 1..k-1, tests on fold k."""
    close_times = sorted(set(
        o.get("close_time_utc", "") for o in observations
        if o.get("close_time_utc")
    ))

    n_folds = 5
    fold_size = len(close_times) // n_folds
    if fold_size < 10:
        return

    fold_boundaries = []
    for i in range(n_folds):
        start = i * fold_size
        end = start + fold_size if i < n_folds - 1 else len(close_times)
        fold_boundaries.append((close_times[start], close_times[end - 1]))

    def _get_fold(ct):
        for idx in range(n_folds - 1, -1, -1):
            if ct >= fold_boundaries[idx][0]:
                return idx
        return 0

    oos_accum = {}

    for (setup_key, strat_key), data in accum.items():
        for trade in data["trades"]:
            ct = trade[2]
            if not ct:
                continue
            fold_idx = _get_fold(ct)
            if fold_idx < 1:
                continue

            key = (setup_key, strat_key)
            if key not in oos_accum:
                oos_accum[key] = {"wins": 0, "pnls": []}
            if trade[0]:
                oos_accum[key]["wins"] += 1
            oos_accum[key]["pnls"].append(trade[1])

    updated = 0
    for (setup_key, strat_key), data in oos_accum.items():
        n = len(data["pnls"])
        if n < 10:
            continue
        oos_wr = data["wins"] / n
        oos_ev = sum(data["pnls"]) / n

        try:
            upsert_strategy_result({
                "setup_key": setup_key,
                "strategy_key": strat_key,
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


# ═══════════════════════════════════════════════════════════════
#  SLIPPAGE SENSITIVITY
# ═══════════════════════════════════════════════════════════════

def _run_slippage_sensitivity(observations: list, accum: dict):
    """Test slippage robustness for positive-EV global strategies.
    PnL at +Nc slippage = base PnL - N (no re-simulation needed)."""
    positive_ev_keys = []
    for (setup_key, strat_key), data in accum.items():
        if setup_key != "global:all":
            continue
        trades = data["trades"]
        if not trades:
            continue
        avg_pnl = sum(t[1] for t in trades) / len(trades)
        if avg_pnl > 0:
            positive_ev_keys.append((strat_key, trades))

    if not positive_ev_keys:
        return

    for strat_key, trades in positive_ev_keys:
        n = len(trades)
        base_ev = sum(t[1] for t in trades) / n
        slip_1_ev = round(base_ev - 1, 1)
        slip_2_ev = round(base_ev - 2, 1)

        try:
            upsert_strategy_result({
                "setup_key": "global:all",
                "strategy_key": strat_key,
                "slippage_1c_ev": slip_1_ev,
                "slippage_2c_ev": slip_2_ev,
            })
        except Exception:
            pass

    log.info(f"Slippage sensitivity: {len(positive_ev_keys)} strategies tested")


# ═══════════════════════════════════════════════════════════════
#  STRATEGY DISCOVERY NOTIFICATIONS
# ═══════════════════════════════════════════════════════════════

def _check_strategy_discoveries():
    """Check if any coarse regime just got its first viable +EV strategy."""
    from db import get_config, set_config

    min_n = int(get_config("auto_strategy_min_samples", 20) or 20)

    with get_conn() as c:
        rows = c.execute("""
            SELECT setup_key, strategy_key, ev_per_trade_c, win_rate, sample_size
            FROM btc15m_strategy_results
            WHERE setup_type = 'coarse_regime' AND sample_size >= ? AND ev_per_trade_c > 0
            ORDER BY ev_per_trade_c DESC
        """, (min_n,)).fetchall()

    if not rows:
        return

    best_per_regime = {}
    for r in rows:
        regime = r["setup_key"].replace("coarse_regime:", "")
        if regime not in best_per_regime:
            best_per_regime[regime] = r

    import json as _json
    notified_raw = get_config("_strategy_discoveries_notified", "[]")
    if isinstance(notified_raw, str):
        try:
            notified = set(_json.loads(notified_raw))
        except Exception:
            notified = set()
    else:
        notified = set(notified_raw) if notified_raw else set()

    new_found = False
    for regime, r in best_per_regime.items():
        if regime not in notified:
            try:
                from push import send_to_all
                send_to_all(
                    "Strategy Discovery",
                    f"New +EV strategy for {regime.replace('_',' ')}: "
                    f"{r['strategy_key']} EV {r['ev_per_trade_c']:+.1f}¢ "
                    f"(n={r['sample_size']})",
                    tag="strategy-discovery",
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
    """Check if the global best strategy changed after recompute."""
    from db import get_config, set_config

    with get_conn() as c:
        best = c.execute("""
            SELECT strategy_key, ev_per_trade_c, weighted_ev_c, win_rate, sample_size,
                   fdr_significant
            FROM btc15m_strategy_results
            WHERE setup_key = 'global:all' AND sample_size >= 30
              AND fdr_significant = 1
            ORDER BY COALESCE(weighted_ev_c, ev_per_trade_c) DESC
            LIMIT 1
        """).fetchone()
        if not best:
            best = c.execute("""
                SELECT strategy_key, ev_per_trade_c, weighted_ev_c, win_rate, sample_size,
                       fdr_significant
                FROM btc15m_strategy_results
                WHERE setup_key = 'global:all' AND sample_size >= 30
                ORDER BY COALESCE(weighted_ev_c, ev_per_trade_c) DESC
                LIMIT 1
            """).fetchone()

    if not best:
        return

    current_key = best["strategy_key"]
    last_key = get_config("_last_global_best_key", "")

    if current_key != last_key and last_key:
        try:
            from push import send_to_all
            send_to_all(
                "Global Best Changed",
                f"{last_key} → {current_key} "
                f"(EV {best['ev_per_trade_c']:+.1f}¢, n={best['sample_size']})",
                tag="global-best-changed",
            )
        except Exception:
            pass
        log.info(f"Global best changed: {last_key} → {current_key} "
                 f"(EV {best['ev_per_trade_c']:+.1f}¢, n={best['sample_size']})")

    if current_key != last_key:
        set_config("_last_global_best_key", current_key)


# ═══════════════════════════════════════════════════════════════
#  SETUP KEY GENERATION
# ═══════════════════════════════════════════════════════════════

def _get_setup_keys(obs: dict) -> list:
    """Return list of (setup_key, setup_type) for an observation."""
    keys = [("global:all", "global")]

    hour = obs.get("hour_et")

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


# ═══════════════════════════════════════════════════════════════
#  WRITE STRATEGY RESULT
# ═══════════════════════════════════════════════════════════════

def _write_strategy_result(setup_key, setup_type, strat_key, meta, trades):
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
    HALF_LIFE_DAYS = 14
    decay_rate = math.log(2) / HALF_LIFE_DAYS
    now = datetime.now(timezone.utc)
    weighted_wins = 0.0
    weighted_total = 0.0
    weighted_pnl = 0.0
    for t in trades:
        ct = t[2]
        if ct:
            try:
                obs_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                age_days = (now - obs_dt).total_seconds() / 86400
                w = math.exp(-decay_rate * age_days)
            except Exception:
                w = 0.5
        else:
            w = 0.5
        weighted_total += w
        if t[0]:
            weighted_wins += w
        weighted_pnl += w * t[1]

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
        if not t[0]:
            current_consec += 1
            max_consec = max(max_consec, current_consec)
        else:
            current_consec = 0

    # Max drawdown
    cum = 0
    peak = 0
    max_dd = 0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    # Expectancy
    win_pnls = [p for p in pnls if p > 0]
    loss_pnls = [p for p in pnls if p < 0]
    avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0
    avg_loss = abs(sum(loss_pnls) / len(loss_pnls)) if loss_pnls else 0
    expectancy = wr * avg_win - (1 - wr) * avg_loss

    times = [t[2] for t in trades if t[2]]

    # Quality-split EV
    full_pnls = [t[1] for t in trades if t[3] == "full"]
    degraded_pnls = [t[1] for t in trades
                     if t[3] in ("short", "partial")]
    quality_full_ev = (round(sum(full_pnls) / len(full_pnls), 1)
                       if full_pnls else None)
    quality_degraded_ev = (round(sum(degraded_pnls) / len(degraded_pnls), 1)
                           if len(degraded_pnls) >= 5 else None)

    # Breakeven fee rate
    entry_max = meta["entry_price_max"]
    if avg_pnl > 0 and entry_max > 0:
        breakeven_fee = round(KALSHI_FEE_RATE + (avg_pnl / entry_max), 4)
    else:
        breakeven_fee = None

    upsert_strategy_result({
        "setup_key": setup_key,
        "setup_type": setup_type,
        "strategy_key": strat_key,
        "side_rule": meta["side_rule"],
        "exit_rule": str(meta["sell_target"]),
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


# ═══════════════════════════════════════════════════════════════
#  WILSON CI (local copy for simulation)
# ═══════════════════════════════════════════════════════════════

def _wilson_ci(wins: int, total: int, z: float = 1.96) -> tuple:
    """Wilson score confidence interval for a proportion."""
    if total == 0:
        return (0.0, 1.0)
    p = wins / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denom
    return (max(0, center - spread), min(1, center + spread))


# ═══════════════════════════════════════════════════════════════
#  FDR CORRECTION (Benjamini-Hochberg with t-test p-values)
# ═══════════════════════════════════════════════════════════════

def _apply_fdr_correction():
    """Apply Benjamini-Hochberg FDR correction to all strategies.
    Uses one-sample t-test on PnL (H0: mean PnL ≤ 0)."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT id, setup_key, strategy_key, sample_size,
                   ev_per_trade_c, pnl_std_c
            FROM btc15m_strategy_results
            WHERE sample_size >= 20
        """).fetchall()

    if not rows:
        return

    results = []
    for r in rows:
        n = r["sample_size"]
        mean_pnl = r["ev_per_trade_c"] or 0
        std_pnl = r["pnl_std_c"] or 0

        if n < 5 or std_pnl <= 0:
            p_value = 1.0
        elif mean_pnl <= 0:
            p_value = 1.0
        else:
            se = std_pnl / math.sqrt(n)
            t_stat = mean_pnl / se if se > 0 else 0
            p_value = 0.5 * math.erfc(t_stat / math.sqrt(2))

        results.append({
            "id": r["id"],
            "p_value": p_value,
            "ev": mean_pnl,
        })

    results.sort(key=lambda x: x["p_value"])
    m = len(results)
    alpha = 0.10

    bh_threshold_idx = -1
    for i, r in enumerate(results):
        bh_critical = (i + 1) / m * alpha
        r["q_value"] = min(1.0, r["p_value"] * m / (i + 1))
        if r["p_value"] <= bh_critical:
            bh_threshold_idx = i

    with get_conn() as c:
        for i, r in enumerate(results):
            is_sig = 1 if (i <= bh_threshold_idx and r["ev"] > 0) else 0
            c.execute("""
                UPDATE btc15m_strategy_results
                SET fdr_significant = ?, fdr_q_value = ?
                WHERE id = ?
            """, (is_sig, round(r["q_value"], 6), r["id"]))

    sig_count = sum(1 for i, r in enumerate(results)
                    if i <= bh_threshold_idx and r["ev"] > 0)
    if sig_count > 0:
        log.info(f"FDR correction: {sig_count}/{m} strategies significant at α={alpha}")


# ═══════════════════════════════════════════════════════════════
#  CONVERGENCE SNAPSHOT
# ═══════════════════════════════════════════════════════════════

def _record_convergence_snapshot():
    """Capture current key metrics for convergence tracking."""
    with get_conn() as c:
        total_obs = c.execute("""
            SELECT COUNT(*) as n FROM btc15m_observations
            WHERE market_result IS NOT NULL
              AND COALESCE(obs_quality, 'full') IN ('full', 'short')
        """).fetchone()["n"]

        top = c.execute("""
            SELECT strategy_key, COALESCE(weighted_ev_c, ev_per_trade_c) as wev,
                   ev_per_trade_c, win_rate, sample_size,
                   fdr_significant, oos_ev_c, ci_lower
            FROM btc15m_strategy_results
            WHERE setup_key = 'global:all' AND sample_size >= 10
            ORDER BY COALESCE(weighted_ev_c, ev_per_trade_c) DESC
            LIMIT 10
        """).fetchall()
        top_list = rows_to_list(top)

        counts = c.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN COALESCE(weighted_ev_c, ev_per_trade_c) > 0 THEN 1 ELSE 0 END) as pos_ev,
                   SUM(CASE WHEN fdr_significant = 1 THEN 1 ELSE 0 END) as fdr_sig
            FROM btc15m_strategy_results
            WHERE setup_key = 'global:all' AND sample_size >= 10
        """).fetchone()

        side_evs = {}
        for side in ('yes', 'no', 'cheaper', 'model'):
            row = c.execute("""
                SELECT MAX(COALESCE(weighted_ev_c, ev_per_trade_c)) as best_ev
                FROM btc15m_strategy_results
                WHERE setup_key = 'global:all' AND sample_size >= 10
                  AND strategy_key LIKE ?
            """, (f"{side}:%",)).fetchone()
            side_evs[side] = round(row["best_ev"], 2) if row and row["best_ev"] else None

        timing_evs = {}
        for timing in ('early', 'mid', 'late'):
            row = c.execute("""
                SELECT MAX(COALESCE(weighted_ev_c, ev_per_trade_c)) as best_ev
                FROM btc15m_strategy_results
                WHERE setup_key = 'global:all' AND sample_size >= 10
                  AND strategy_key LIKE ?
            """, (f"%:{timing}:%",)).fetchone()
            timing_evs[timing] = round(row["best_ev"], 2) if row and row["best_ev"] else None

        regime_counts = c.execute("""
            SELECT COUNT(DISTINCT regime_label) as n
            FROM btc15m_observations
            WHERE market_result IS NOT NULL AND regime_label IS NOT NULL
              AND regime_label != 'unknown'
              AND COALESCE(obs_quality, 'full') IN ('full', 'short')
            GROUP BY regime_label HAVING COUNT(*) >= 10
        """).fetchall()
        regimes_with_10 = len(regime_counts)

        shadow = c.execute("""
            SELECT COUNT(*) as n,
                   SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) as wins,
                   AVG(avg_fill_price_c - entry_price_c) as avg_slip
            FROM btc15m_trades
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
