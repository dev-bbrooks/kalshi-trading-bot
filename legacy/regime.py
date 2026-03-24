"""
regime.py — BTC regime analysis and classification.
Fetches candles from Binance, computes indicators, classifies market state.
Runs as a background thread within the main bot process.
"""

import time
import math
import logging
import statistics
import requests
from datetime import datetime, timezone, timedelta
from threading import Thread

from config import BINANCE_BASE_URL, ET
from db import (
    insert_btc_candles, get_btc_candles, get_latest_btc_candle,
    count_btc_candles, upsert_baseline, get_baseline,
    insert_regime_snapshot, get_latest_regime_snapshot,
    update_regime_stats, get_all_regime_stats, get_recent_trades,
    insert_log, now_utc, update_bot_state
)

log = logging.getLogger("regime")

BACKFILL_DAYS = 365       # 1 year of history
SNAPSHOT_INTERVAL = 300   # 5 minutes between snapshots
HISTORY_POLL = 60         # 1 minute between candle updates
BASELINE_INTERVAL = 86400 # 24 hours between baseline recomputes
STATS_INTERVAL = 900      # 15 minutes between regime stats refresh


# ═══════════════════════════════════════════════════════════════
#  BINANCE DATA
# ═══════════════════════════════════════════════════════════════

def fetch_binance_candles(symbol: str = "BTCUSDT", interval: str = "1m",
                          start_ms: int = None, limit: int = 1000) -> list:
    """Fetch OHLCV candles from Binance. Returns list of dicts."""
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if start_ms:
        params["startTime"] = start_ms
    try:
        r = requests.get(f"{BINANCE_BASE_URL}/api/v3/klines",
                         params=params, timeout=15)
        r.raise_for_status()
        return [{
            "ts": datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc).isoformat(),
            "open": float(c[1]), "high": float(c[2]),
            "low": float(c[3]), "close": float(c[4]),
            "volume": float(c[5]),
        } for c in r.json()]
    except Exception as e:
        log.warning(f"Binance fetch error: {e}")
        return []


def get_live_btc_price() -> float | None:
    """Fetch live BTC price from Binance ticker."""
    try:
        r = requests.get(f"{BINANCE_BASE_URL}/api/v3/ticker/price",
                         params={"symbol": "BTCUSDT"}, timeout=5)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception:
        latest = get_latest_btc_candle()
        return latest["close"] if latest else None


def backfill_history():
    """Backfill BTC candle history on first run."""
    existing = count_btc_candles()
    if existing > 100_000:
        log.info(f"BTC history: {existing:,} candles already present")
        return

    log.info(f"Backfilling {BACKFILL_DAYS} days of BTC candles...")
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - (BACKFILL_DAYS * 24 * 3600 * 1000)
    total = 0
    batch_start = start_ms

    while batch_start < now_ms:
        candles = fetch_binance_candles(start_ms=batch_start, limit=1000)
        if not candles:
            time.sleep(5)
            continue
        insert_btc_candles(candles)
        total += len(candles)
        last_dt = datetime.fromisoformat(candles[-1]["ts"])
        batch_start = int((last_dt.timestamp() + 60) * 1000)
        if total % 50_000 == 0:
            log.info(f"  Backfill: {total:,} candles")
        time.sleep(0.15)

    log.info(f"Backfill complete: {total:,} candles")


def update_history():
    """Fetch candles newer than our latest stored candle."""
    latest = get_latest_btc_candle()
    if not latest:
        return
    last_ts = datetime.fromisoformat(latest["ts"])
    start_ms = int((last_ts.timestamp() + 60) * 1000)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if start_ms >= now_ms:
        return
    candles = fetch_binance_candles(start_ms=start_ms, limit=1000)
    if candles:
        insert_btc_candles(candles)


# ═══════════════════════════════════════════════════════════════
#  TECHNICAL INDICATORS
# ═══════════════════════════════════════════════════════════════

def calc_ema(values: list, period: int) -> list:
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    ema = [sum(values[:period]) / period]
    for v in values[period:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema


def calc_atr(candles: list, period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period if len(trs) >= period else None


def calc_bollinger_width(closes: list, period: int = 20) -> float | None:
    if len(closes) < period:
        return None
    recent = closes[-period:]
    mid = sum(recent) / period
    if mid == 0:
        return None
    std = statistics.stdev(recent)
    return ((mid + 2 * std) - (mid - 2 * std)) / mid * 100


def calc_realized_vol(closes: list, period: int = 15) -> float | None:
    if len(closes) < period + 1:
        return None
    recent = closes[-(period + 1):]
    returns = []
    for i in range(1, len(recent)):
        if recent[i - 1] > 0:
            returns.append(math.log(recent[i] / recent[i - 1]))
    return statistics.stdev(returns) if len(returns) >= 2 else None


def calc_ema_slope(closes: list, ema_period: int = 20,
                   slope_lookback: int = 5) -> float | None:
    ema = calc_ema(closes, ema_period)
    if len(ema) < slope_lookback + 1:
        return None
    recent = ema[-slope_lookback:]
    slope = (recent[-1] - recent[0]) / slope_lookback
    price = closes[-1]
    return (slope / price) * 100 if price != 0 else None


# ═══════════════════════════════════════════════════════════════
#  REGIME SCORING
# ═══════════════════════════════════════════════════════════════

def score_volatility(realized_vol: float, baseline: dict) -> int:
    """Score 1 (compressed) to 5 (explosive) using baseline percentiles."""
    if realized_vol is None or not baseline:
        return 3
    p25 = baseline.get("p25_vol_15m", 0)
    avg = baseline.get("avg_vol_15m", 0)
    p75 = baseline.get("p75_vol_15m", 0)
    p90 = baseline.get("p90_vol_15m", 0)
    if avg == 0:
        return 3
    if realized_vol <= p25:
        return 1
    elif realized_vol <= avg:
        return 2
    elif realized_vol <= p75:
        return 3
    elif realized_vol <= p90:
        return 4
    return 5


def score_trend(ema_slope_15m: float, ema_slope_1h: float) -> tuple:
    """Score -3 (strong down) to +3 (strong up). Returns (regime, direction, strength)."""
    if ema_slope_15m is None:
        return 0, 0, 0.0

    WEAK, MEDIUM, STRONG = 0.0005, 0.0015, 0.003
    slope = ema_slope_15m
    abs_slope = abs(slope)
    direction = 1 if slope > 0 else (-1 if slope < 0 else 0)
    strength = min(abs_slope / STRONG, 1.0)

    # Dampen if 15m and 1h disagree
    if ema_slope_1h is not None and slope * ema_slope_1h < 0:
        strength *= 0.5

    if abs_slope < WEAK:
        regime = 0
    elif abs_slope < MEDIUM:
        regime = direction * 1
    elif abs_slope < STRONG:
        regime = direction * 2
    else:
        regime = direction * 3

    return regime, direction, round(strength, 3)


def score_volume(volume_15m: float, baseline: dict) -> tuple:
    """Score 1 (thin) to 5 (spike). Returns (regime, is_spike)."""
    if volume_15m is None or not baseline:
        return 3, False
    avg = baseline.get("avg_volume_15m", 0)
    p25 = baseline.get("p25_volume_15m", 0)
    p75 = baseline.get("p75_volume_15m", 0)
    p90 = baseline.get("p90_volume_15m", 0)
    if avg == 0:
        return 3, False
    if volume_15m <= p25:
        return 1, False
    elif volume_15m <= avg:
        return 2, False
    elif volume_15m <= p75:
        return 3, False
    elif volume_15m <= p90:
        return 4, False
    return 5, True


def classify_composite(vol_regime: int, trend_regime: int,
                       volume_regime: int, post_spike: bool,
                       trend_exhaustion: bool,
                       squeeze: bool = False,
                       trend_accel: str | None = None,
                       thin_market: bool = False) -> tuple:
    """
    Derive composite regime label and confidence.
    Returns (label, confidence).

    Priority:
      1. Overrides: post_spike_settling, volatile_explosive (rare, exceptional)
      2. Base label from trend/ranging classification
      3. Modifiers prepended: squeeze_, thin_ (preserves trend info)
    """
    confidence = 1.0

    # ── True overrides — exceptional conditions that dominate everything ──
    if post_spike:
        return "post_spike_settling", 0.7
    if vol_regime == 5:
        return "volatile_explosive", 0.8

    # ── Compute base label from trend/ranging axes ──
    if trend_exhaustion and abs(trend_regime) >= 2:
        d = "up" if trend_regime > 0 else "down"
        label = f"trend_exhaustion_{d}"
        confidence = 0.75
    elif trend_regime == 0:
        vol_labels = {1: "compressed", 2: "low_vol", 3: "normal",
                      4: "elevated_vol", 5: "volatile"}
        label = f"ranging_{vol_labels.get(vol_regime, 'normal')}"
        confidence = {1: 0.9, 2: 0.9, 3: 0.85, 4: 0.7, 5: 0.6}.get(vol_regime, 0.8)
    elif abs(trend_regime) == 1:
        d = "up" if trend_regime > 0 else "down"
        label = f"trending_{d}_weak"
        confidence = 0.75
    elif abs(trend_regime) == 2:
        d = "up" if trend_regime > 0 else "down"
        label = f"trending_{d}_moderate"
        confidence = 0.85
    else:
        d = "up" if trend_regime > 0 else "down"
        label = f"trending_{d}_strong"
        confidence = 0.9

    # ── Append acceleration/deceleration modifier to trending labels ──
    if trend_accel and abs(trend_regime) >= 1:
        label = f"{label}_{trend_accel}"
        if trend_accel == "accel":
            confidence = min(confidence * 1.1, 0.95)
        elif trend_accel == "decel":
            confidence *= 0.85

    # ── Prepend condition modifiers (squeeze > thin priority) ──
    if squeeze:
        label = f"squeeze_{label}"
        confidence *= 0.85
    elif thin_market:
        label = f"thin_{label}"
        confidence *= 0.7

    if vol_regime >= 4:
        confidence *= 0.8
    return label, round(confidence, 2)


def detect_post_spike(candles_1h: list, vol_regime: int) -> bool:
    if len(candles_1h) < 30:
        return False
    ranges = [(c["high"] - c["low"]) / max(c["close"], 1) * 100 for c in candles_1h]
    max_range = max(ranges)
    tail_avg = sum(ranges[-5:]) / 5
    return max_range > 0.5 and tail_avg < max_range * 0.4 and vol_regime <= 3


def detect_trend_exhaustion(candles: list, trend_regime: int) -> bool:
    if abs(trend_regime) < 2 or len(candles) < 20:
        return False
    closes = [c["close"] for c in candles]
    recent = calc_ema_slope(closes[-10:], ema_period=5, slope_lookback=5)
    prior = calc_ema_slope(closes[-20:-5], ema_period=5, slope_lookback=5)
    if recent is None or prior is None or abs(prior) == 0:
        return False
    ratio = abs(recent) / abs(prior)
    same_dir = (recent * prior) > 0
    return ratio < 0.4 and same_dir


def detect_squeeze(bollinger_width: float, baseline: dict) -> bool:
    """
    Detect Bollinger Band squeeze — bands compressed below the empirical
    10th percentile of historical width. Squeezes often precede large moves.
    Returns True if squeeze detected.
    """
    if bollinger_width is None:
        return False
    # Use actual p10 from baseline data (computed from 1yr of candles)
    p10_boll = baseline.get("p10_bollinger_width", 0) if baseline else 0
    if p10_boll > 0:
        return bollinger_width < p10_boll
    # Fallback if p10 not yet computed: use 20% of average (conservative)
    avg_boll = baseline.get("avg_bollinger_width", 0) if baseline else 0
    if avg_boll > 0:
        return bollinger_width < avg_boll * 0.2
    return bollinger_width < 0.03


def detect_thin_market(volume_15m: float, volume_regime: int,
                       baseline: dict) -> bool:
    """
    Detect genuinely thin market — not just below-average volume, but
    significantly below the 25th percentile. volume_regime == 1 alone
    catches ~25% of observations; this requires volume < 50% of p25,
    targeting only the truly illiquid bottom ~5%.
    """
    if volume_regime != 1:
        return False
    if volume_15m is None or not baseline:
        return False
    p25 = baseline.get("p25_volume_15m", 0)
    if p25 <= 0:
        return False
    return volume_15m < p25 * 0.5


def detect_trend_acceleration(closes: list, trend_regime: int) -> str | None:
    """
    Detect whether a trend is accelerating or decelerating.
    Compares recent EMA slope to slightly older EMA slope.

    Returns:
      "accel" — trend strengthening (slope magnitude increasing)
      "decel" — trend weakening (slope magnitude decreasing)
      None    — no trend or insufficient data
    """
    if abs(trend_regime) < 1 or len(closes) < 30:
        return None

    # Recent slope (last 10 candles) vs prior slope (10 before that)
    recent = calc_ema_slope(closes[-12:], ema_period=8, slope_lookback=4)
    prior = calc_ema_slope(closes[-22:-8], ema_period=8, slope_lookback=4)

    if recent is None or prior is None or abs(prior) < 0.00005:
        return None

    # Both must be in the same direction as the trend
    if trend_regime > 0 and (recent <= 0 or prior <= 0):
        return None
    if trend_regime < 0 and (recent >= 0 or prior >= 0):
        return None

    ratio = abs(recent) / abs(prior)

    if ratio >= 1.4:
        return "accel"
    elif ratio <= 0.6:
        return "decel"
    return None


def score_spread(spread_c: int | None) -> str:
    """
    Classify Kalshi market spread into regime buckets.
    Spread is the gap between best ask and best bid on the cheaper side.

    Returns: "tight" | "normal" | "wide" | "very_wide" | "unknown"
    """
    if spread_c is None:
        return "unknown"
    if spread_c <= 3:
        return "tight"
    elif spread_c <= 6:
        return "normal"
    elif spread_c <= 10:
        return "wide"
    return "very_wide"


def compute_coarse_label(vol_regime: int, trend_regime: int,
                         volume_regime: int = None) -> str:
    """
    Compute a simplified regime label with fewer buckets for faster
    statistical convergence. Groups fine-grained regimes into ~15 categories.

    Axes:
      Volatility: calm (1-2), normal (3), volatile (4-5)
      Trend: flat (0), trending_up/down (±1-2), strong_trend_up/down (±3)
    """
    # Volatility bucket
    if vol_regime <= 2:
        vol_bucket = "calm"
    elif vol_regime <= 3:
        vol_bucket = "normal"
    else:
        vol_bucket = "volatile"

    # Trend bucket — includes direction for non-flat
    abs_trend = abs(trend_regime)
    if abs_trend == 0:
        trend_bucket = "flat"
    elif abs_trend <= 2:
        direction = "up" if trend_regime > 0 else "down"
        trend_bucket = f"trending_{direction}"
    else:
        direction = "up" if trend_regime > 0 else "down"
        trend_bucket = f"strong_trend_{direction}"

    return f"{vol_bucket}_{trend_bucket}"


# ═══════════════════════════════════════════════════════════════
#  SNAPSHOT COMPUTATION
# ═══════════════════════════════════════════════════════════════

def compute_snapshot() -> dict | None:
    """Compute a full regime snapshot from current BTC data."""
    now = datetime.now(timezone.utc)
    since_24h = (now - timedelta(hours=25)).isoformat()
    candles = get_btc_candles(since=since_24h, limit=1500)

    if len(candles) < 30:
        log.warning(f"Not enough candles for regime ({len(candles)})")
        return None

    cutoff_15m = (now - timedelta(minutes=15)).isoformat()
    cutoff_1h = (now - timedelta(hours=1)).isoformat()
    cutoff_4h = (now - timedelta(hours=4)).isoformat()

    candles_15m = [c for c in candles if c["ts"] >= cutoff_15m]
    candles_1h = [c for c in candles if c["ts"] >= cutoff_1h]
    candles_4h = [c for c in candles if c["ts"] >= cutoff_4h]

    closes_all = [c["close"] for c in candles]
    closes_1h = [c["close"] for c in candles_1h]

    btc_price = candles[-1]["close"]

    # Returns
    def safe_return(now_p, old_p):
        if now_p is None or old_p is None or old_p == 0:
            return None
        return round((now_p - old_p) / old_p * 100, 4)

    ago_15m = candles[-16]["close"] if len(candles) >= 16 else None
    ago_1h = candles[-61]["close"] if len(candles) >= 61 else None
    ago_4h = candles[-241]["close"] if len(candles) >= 241 else None

    # Indicators
    atr_15m = calc_atr(candles_15m, period=min(14, max(len(candles_15m) - 1, 1)))
    atr_1h = calc_atr(candles_1h, period=min(14, max(len(candles_1h) - 1, 1)))
    boll_15m = calc_bollinger_width(closes_all[-21:])
    rvol_15m = calc_realized_vol(closes_all, period=15)
    rvol_1h = calc_realized_vol(closes_1h, period=min(59, max(len(closes_1h) - 1, 2)))
    ema_slope_15m = calc_ema_slope(closes_all[-26:], ema_period=20, slope_lookback=5)
    ema_slope_1h = (calc_ema_slope(closes_1h, ema_period=min(20, len(closes_1h) // 2),
                                   slope_lookback=5)
                    if len(closes_1h) >= 15 else None)

    volume_15m = sum(c["volume"] for c in candles_15m) if candles_15m else None

    # Baseline
    now_et = now.astimezone(ET)
    baseline = get_baseline(hour_et=now_et.hour, day_of_week=now_et.weekday())

    # Score all axes
    vol_regime = score_volatility(rvol_15m, baseline)
    trend_regime, trend_dir, trend_str = score_trend(ema_slope_15m, ema_slope_1h)
    volume_regime, vol_spike = score_volume(volume_15m, baseline)
    post_spike = detect_post_spike(candles_1h, vol_regime)
    trend_exhaust = detect_trend_exhaustion(candles, trend_regime)
    squeeze = detect_squeeze(boll_15m, baseline)
    thin_market = detect_thin_market(volume_15m, volume_regime, baseline)
    trend_accel = detect_trend_acceleration(closes_all, trend_regime)
    composite, confidence = classify_composite(
        vol_regime, trend_regime, volume_regime, post_spike, trend_exhaust,
        squeeze=squeeze, trend_accel=trend_accel, thin_market=thin_market
    )

    snapshot = {
        "btc_price": btc_price,
        "btc_return_15m": safe_return(btc_price, ago_15m),
        "btc_return_1h": safe_return(btc_price, ago_1h),
        "btc_return_4h": safe_return(btc_price, ago_4h),
        "atr_15m": round(atr_15m, 2) if atr_15m else None,
        "atr_1h": round(atr_1h, 2) if atr_1h else None,
        "bollinger_width_15m": round(boll_15m, 4) if boll_15m else None,
        "realized_vol_15m": round(rvol_15m, 6) if rvol_15m else None,
        "realized_vol_1h": round(rvol_1h, 6) if rvol_1h else None,
        "ema_slope_15m": round(ema_slope_15m, 6) if ema_slope_15m else None,
        "ema_slope_1h": round(ema_slope_1h, 6) if ema_slope_1h else None,
        "vol_regime": vol_regime,
        "trend_regime": trend_regime,
        "trend_direction": trend_dir,
        "trend_strength": trend_str,
        "volume_15m": round(volume_15m, 2) if volume_15m else None,
        "volume_regime": volume_regime,
        "volume_spike": int(vol_spike),
        "post_spike": int(post_spike),
        "trend_exhaustion": int(trend_exhaust),
        "bollinger_squeeze": int(squeeze),
        "thin_market": int(thin_market),
        "trend_acceleration": trend_accel,
        "composite_label": composite,
        "regime_confidence": confidence,
    }

    snap_id = insert_regime_snapshot(snapshot)
    snapshot["id"] = snap_id
    extras = []
    if squeeze:
        extras.append("SQUEEZE")
    if thin_market:
        extras.append("THIN")
    if trend_accel:
        extras.append(trend_accel)
    extra_str = f" [{','.join(extras)}]" if extras else ""
    log.debug(f"Regime: {composite} (vol={vol_regime} trend={trend_regime:+d} "
              f"volume={volume_regime}){extra_str} BTC=${btc_price:,.0f}")
    return snapshot


# ═══════════════════════════════════════════════════════════════
#  BASELINES
# ═══════════════════════════════════════════════════════════════

def compute_baselines():
    """Compute statistical baselines from all candle history."""
    log.info("Computing baselines...")
    since = (datetime.now(timezone.utc) - timedelta(days=BACKFILL_DAYS)).isoformat()
    candles = get_btc_candles(since=since, limit=999_999)

    if len(candles) < 1000:
        log.warning(f"Not enough candles for baselines ({len(candles)})")
        return

    from collections import defaultdict
    by_hour = defaultdict(list)
    by_hour_day = defaultdict(list)
    global_candles = []

    for c in candles:
        ts = datetime.fromisoformat(c["ts"])
        et = ts.astimezone(ET)
        by_hour[et.hour].append(c)
        by_hour_day[(et.hour, et.weekday())].append(c)
        global_candles.append(c)

    def compute_stats(group):
        if len(group) < 100:
            return None
        closes = [c["close"] for c in group]
        vols, volumes = [], []
        for i in range(15, len(closes)):
            v = calc_realized_vol(closes[i - 15:i + 1], period=15)
            if v is not None:
                vols.append(v)
        for i in range(0, len(group) - 14, 15):
            chunk = group[i:i + 15]
            volumes.append(sum(c["volume"] for c in chunk))

        if not vols or not volumes:
            return None
        vols.sort()
        volumes_s = sorted(volumes)

        def pct(lst, p):
            idx = int(len(lst) * p / 100)
            return lst[min(idx, len(lst) - 1)]

        atrs = []
        for i in range(14, len(group)):
            a = calc_atr(group[i - 14:i + 1], period=14)
            if a is not None:
                atrs.append(a)

        # Bollinger widths for squeeze baseline
        boll_widths = []
        for i in range(20, len(closes)):
            bw = calc_bollinger_width(closes[i - 20:i + 1], period=20)
            if bw is not None:
                boll_widths.append(bw)

        boll_widths_sorted = sorted(boll_widths) if boll_widths else []

        return {
            "avg_vol_15m": sum(vols) / len(vols),
            "p25_vol_15m": pct(vols, 25),
            "p75_vol_15m": pct(vols, 75),
            "p90_vol_15m": pct(vols, 90),
            "avg_atr_15m": sum(atrs) / len(atrs) if atrs else 0,
            "avg_volume_15m": sum(volumes) / len(volumes),
            "p25_volume_15m": pct(volumes_s, 25),
            "p75_volume_15m": pct(volumes_s, 75),
            "p90_volume_15m": pct(volumes_s, 90),
            "avg_bollinger_width": sum(boll_widths) / len(boll_widths) if boll_widths else 0,
            "p10_bollinger_width": pct(boll_widths_sorted, 10) if boll_widths_sorted else 0,
            "avg_range_15m": 0,
            "sample_count": len(group),
        }

    global_stats = compute_stats(global_candles)
    if global_stats:
        upsert_baseline(None, None, global_stats)

    hour_count = 0
    for hour, hcandles in by_hour.items():
        stats = compute_stats(hcandles)
        if stats:
            upsert_baseline(hour, None, stats)
            hour_count += 1

    # Hour × day-of-week baselines (weekend vs weekday differences)
    hour_day_count = 0
    for (hour, dow), hdcandles in by_hour_day.items():
        stats = compute_stats(hdcandles)
        if stats:
            upsert_baseline(hour, dow, stats)
            hour_day_count += 1

    log.info(f"Baselines: global + {hour_count} hours + {hour_day_count} hour×day")


def refresh_regime_stats():
    """Recompute risk levels for all regime labels from trade data."""
    with __import__("db").get_conn() as c:
        # Include ALL regime labels — even those with only unresolved observations
        # update_regime_stats handles the counting correctly
        rows = c.execute("""
            SELECT DISTINCT regime_label FROM trades
            WHERE regime_label IS NOT NULL AND regime_label != 'unknown'
        """).fetchall()
    for row in rows:
        result = update_regime_stats(row["regime_label"])
        # Send notifications for new regimes and risk level changes
        if result:
            try:
                if result.get("is_new") and result["total"] > 0:
                    from push import notify_new_regime
                    notify_new_regime(row["regime_label"], result["total"])
                old_risk = result.get("old_risk")
                new_risk = result.get("new_risk")
                if old_risk and new_risk and old_risk != new_risk:
                    from push import notify_regime_classified
                    notify_regime_classified(
                        row["regime_label"], new_risk,
                        total=result["total"],
                        win_rate=result["win_rate"],
                        old_risk=old_risk,
                    )
            except Exception:
                pass
    log.debug(f"Regime stats refreshed for {len(rows)} labels")

    # Also refresh coarse regime and hourly stats
    try:
        from db import refresh_all_coarse_regime_stats, refresh_all_hourly_stats
        refresh_all_coarse_regime_stats()
        refresh_all_hourly_stats()
        log.debug("Coarse regime + hourly stats refreshed")
    except Exception as e:
        log.warning(f"Error refreshing extended stats: {e}")


# ═══════════════════════════════════════════════════════════════
#  REGIME STABILITY TRACKING
# ═══════════════════════════════════════════════════════════════

def _track_regime_stability(snap: dict, prev_label: str, prev_coarse: str,
                             prev_btc: float = None):
    """Track whether the regime label changed between consecutive snapshots.
    High churn = the label is adding noise rather than signal."""
    try:
        curr_label = snap.get("composite_label", "unknown")
        curr_coarse = compute_coarse_label(
            snap.get("vol_regime", 3),
            snap.get("trend_regime", 0),
            snap.get("volume_regime")
        )
        btc = snap.get("btc_price")
        btc_change = None
        if btc and prev_btc and prev_btc > 0:
            btc_change = round((btc - prev_btc) / prev_btc * 100, 4)

        label_changed = int(curr_label != prev_label) if prev_label else 0
        coarse_changed = int(curr_coarse != prev_coarse) if prev_coarse else 0

        from db import insert_regime_stability
        insert_regime_stability({
            "prev_label": prev_label,
            "curr_label": curr_label,
            "prev_coarse": prev_coarse,
            "curr_coarse": curr_coarse,
            "label_changed": label_changed,
            "coarse_changed": coarse_changed,
            "btc_price": btc,
            "btc_change_pct": btc_change,
        })
    except Exception as e:
        log.debug(f"Regime stability tracking error: {e}")


# ═══════════════════════════════════════════════════════════════
#  BACKGROUND THREAD
# ═══════════════════════════════════════════════════════════════

def regime_worker(stop_event):
    """Background thread: keeps BTC data and regime snapshots current."""
    log.info("Regime worker starting...")

    try:
        update_bot_state({"regime_engine_phase": "backfilling"})
        backfill_history()
    except Exception as e:
        log.error(f"Backfill failed: {e}")
        try:
            insert_log("ERROR", f"Backfill failed: {e}", "regime")
        except Exception:
            pass

    try:
        update_bot_state({"regime_engine_phase": "computing_baselines"})
        compute_baselines()
    except Exception as e:
        log.error(f"Baselines failed: {e}")
        try:
            insert_log("ERROR", f"Baselines failed: {e}", "regime")
        except Exception:
            pass

    try:
        update_bot_state({"regime_engine_phase": "updating_history"})
        update_history()
    except Exception as e:
        log.error(f"History update failed: {e}")

    snap = None
    try:
        update_bot_state({"regime_engine_phase": "first_snapshot"})
        snap = compute_snapshot()
    except Exception as e:
        log.error(f"First snapshot failed: {e}")
        try:
            insert_log("ERROR", f"First snapshot failed: {e}", "regime")
        except Exception:
            pass

    update_bot_state({"regime_engine_phase": "running"})

    # Notify that regime engine is ready for trading
    if snap:
        label = snap.get("composite_label", "unknown")
        btc = snap.get("btc_price", 0)
        candle_count = count_btc_candles()
        try:
            from push import send_to_all
            send_to_all(
                "Regime Engine Ready",
                f"Backfill complete ({candle_count:,} candles). "
                f"Current: {label.replace('_',' ')} · BTC ${btc:,.0f}",
                tag="regime-ready",
            )
        except Exception:
            pass
        try:
            insert_log("INFO", f"Regime engine ready: {label} | {candle_count:,} candles", "regime")
        except Exception:
            pass
        log.info(f"Regime engine ready: {label} ({candle_count:,} candles)")
    else:
        log.warning("Regime engine started but first snapshot failed")

    last_history = time.time()
    last_snapshot = time.time()
    last_baseline = time.time()
    last_stats = time.time()
    last_strategy_sim = time.time()
    last_analysis = time.time()  # New: feature importance + BTC surface

    STRATEGY_SIM_INTERVAL = 1800  # 30 minutes
    ANALYSIS_INTERVAL = 3600      # 1 hour — heavier computations

    # Background thread for heavy computation (sim batch, surface, features)
    # so snapshots never get blocked
    _heavy_thread = None

    # Track previous regime label for stability monitoring
    _prev_regime_label = snap.get("composite_label", "unknown") if snap else "unknown"
    _prev_coarse_label = None
    _prev_btc_price = snap.get("btc_price") if snap else None

    while not stop_event.is_set():
        try:
            now = time.time()

            if now - last_history >= HISTORY_POLL:
                update_history()
                last_history = now

            if now - last_snapshot >= SNAPSHOT_INTERVAL:
                new_snap = compute_snapshot()
                # Regime stability tracking
                if new_snap:
                    _track_regime_stability(
                        new_snap, _prev_regime_label, _prev_coarse_label,
                        _prev_btc_price
                    )
                    _prev_regime_label = new_snap.get("composite_label", "unknown")
                    _prev_coarse_label = compute_coarse_label(
                        new_snap.get("vol_regime", 3),
                        new_snap.get("trend_regime", 0),
                        new_snap.get("volume_regime"))
                    _prev_btc_price = new_snap.get("btc_price")
                last_snapshot = now

            if now - last_baseline >= BASELINE_INTERVAL:
                compute_baselines()
                last_baseline = now

            if now - last_stats >= STATS_INTERVAL:
                refresh_regime_stats()
                last_stats = now

            # Strategy Observatory: run simulations on new observations
            # Runs in background thread so snapshots never get blocked
            run_sim = now - last_strategy_sim >= STRATEGY_SIM_INTERVAL
            run_analysis = now - last_analysis >= ANALYSIS_INTERVAL
            if (run_sim or run_analysis) and (_heavy_thread is None or not _heavy_thread.is_alive()):
                def _heavy_work(do_sim, do_analysis):
                    if do_sim:
                        try:
                            from strategy import run_simulation_batch
                            processed = run_simulation_batch()
                            if processed > 0:
                                log.info(f"Strategy sim: processed {processed} observations")
                        except Exception as e:
                            log.warning(f"Strategy simulation error: {e}")
                    if do_analysis:
                        try:
                            from strategy import compute_btc_probability_surface
                            compute_btc_probability_surface()
                        except Exception as e:
                            log.debug(f"BTC surface error: {e}")
                        try:
                            from strategy import compute_feature_importance
                            compute_feature_importance()
                        except Exception as e:
                            log.debug(f"Feature importance error: {e}")
                _heavy_thread = Thread(target=_heavy_work, args=(run_sim, run_analysis), daemon=True)
                _heavy_thread.start()
                log.debug(f"Heavy computation started in background (sim={run_sim}, analysis={run_analysis})")
                if run_sim:
                    last_strategy_sim = now
                if run_analysis:
                    last_analysis = now

            stop_event.wait(10)  # Check every 10s
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            log.error(f"Regime worker error: {e}", exc_info=True)
            try:
                insert_log("ERROR", f"Regime worker: {e}", "regime")
                insert_log("ERROR", f"Regime traceback:\n{tb}", "regime")
            except Exception:
                pass
            stop_event.wait(30)

    log.info("Regime worker stopped")