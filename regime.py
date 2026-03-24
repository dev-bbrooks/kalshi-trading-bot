"""
regime.py — Asset regime analysis and classification.
Parameterized by asset name. Uses DB heartbeat for cross-process coordination.
Multiple plugins sharing the same asset share one regime worker.
"""

import os
import time
import math
import logging
import statistics
import requests
from datetime import datetime, timezone, timedelta
from threading import Thread

from config import BINANCE_BASE_URL, ET
from db import (
    insert_candles, get_candles, get_latest_candle,
    count_candles, upsert_baseline, get_baseline,
    insert_regime_snapshot, get_latest_regime_snapshot,
    insert_log, now_utc, update_plugin_state,
    update_regime_heartbeat, is_regime_worker_running,
    insert_regime_stability,
)

log = logging.getLogger("regime")

BACKFILL_DAYS = 365       # 1 year of history
SNAPSHOT_INTERVAL = 300   # 5 minutes between snapshots
HISTORY_POLL = 60         # 1 minute between candle updates
BASELINE_INTERVAL = 86400 # 24 hours between baseline recomputes


# ═══════════════════════════════════════════════════════════════
#  BINANCE DATA (asset-parameterized)
# ═══════════════════════════════════════════════════════════════

_ASSET_SYMBOLS = {
    "BTC": "BTCUSDT",
}


def _get_symbol(asset: str) -> str:
    return _ASSET_SYMBOLS.get(asset, f"{asset}USDT")


def fetch_binance_candles(asset: str = "BTC", interval: str = "1m",
                          start_ms: int = None, limit: int = 1000) -> list:
    symbol = _get_symbol(asset)
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
        log.warning(f"Binance fetch error ({asset}): {e}")
        return []


def get_live_price(asset: str = "BTC") -> float | None:
    symbol = _get_symbol(asset)
    try:
        r = requests.get(f"{BINANCE_BASE_URL}/api/v3/ticker/price",
                         params={"symbol": symbol}, timeout=5)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception:
        latest = get_latest_candle(asset)
        return latest["close"] if latest else None


def backfill_history(asset: str):
    existing = count_candles(asset)
    if existing > 100_000:
        log.info(f"{asset} history: {existing:,} candles already present")
        return

    log.info(f"Backfilling {BACKFILL_DAYS} days of {asset} candles...")
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - (BACKFILL_DAYS * 24 * 3600 * 1000)
    total = 0
    batch_start = start_ms

    while batch_start < now_ms:
        candles = fetch_binance_candles(asset=asset, start_ms=batch_start, limit=1000)
        if not candles:
            time.sleep(5)
            continue
        insert_candles(candles, asset)
        total += len(candles)
        last_dt = datetime.fromisoformat(candles[-1]["ts"])
        batch_start = int((last_dt.timestamp() + 60) * 1000)
        if total % 50_000 == 0:
            log.info(f"  Backfill {asset}: {total:,} candles")
        time.sleep(0.15)

    log.info(f"Backfill complete ({asset}): {total:,} candles")


def update_history(asset: str):
    latest = get_latest_candle(asset)
    if not latest:
        return
    last_ts = datetime.fromisoformat(latest["ts"])
    start_ms = int((last_ts.timestamp() + 60) * 1000)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if start_ms >= now_ms:
        return
    candles = fetch_binance_candles(asset=asset, start_ms=start_ms, limit=1000)
    if candles:
        insert_candles(candles, asset)


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
    if ema_slope_15m is None:
        return 0, 0, 0.0

    WEAK, MEDIUM, STRONG = 0.0005, 0.0015, 0.003
    slope = ema_slope_15m
    abs_slope = abs(slope)
    direction = 1 if slope > 0 else (-1 if slope < 0 else 0)
    strength = min(abs_slope / STRONG, 1.0)

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
    confidence = 1.0

    if post_spike:
        return "post_spike_settling", 0.7
    if vol_regime == 5:
        return "volatile_explosive", 0.8

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

    if trend_accel and abs(trend_regime) >= 1:
        label = f"{label}_{trend_accel}"
        if trend_accel == "accel":
            confidence = min(confidence * 1.1, 0.95)
        elif trend_accel == "decel":
            confidence *= 0.85

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
    if bollinger_width is None:
        return False
    p10_boll = baseline.get("p10_bollinger_width", 0) if baseline else 0
    if p10_boll > 0:
        return bollinger_width < p10_boll
    avg_boll = baseline.get("avg_bollinger_width", 0) if baseline else 0
    if avg_boll > 0:
        return bollinger_width < avg_boll * 0.2
    return bollinger_width < 0.03


def detect_thin_market(volume_15m: float, volume_regime: int,
                       baseline: dict) -> bool:
    if volume_regime != 1:
        return False
    if volume_15m is None or not baseline:
        return False
    p25 = baseline.get("p25_volume_15m", 0)
    if p25 <= 0:
        return False
    return volume_15m < p25 * 0.5


def detect_trend_acceleration(closes: list, trend_regime: int) -> str | None:
    if abs(trend_regime) < 1 or len(closes) < 30:
        return None

    recent = calc_ema_slope(closes[-12:], ema_period=8, slope_lookback=4)
    prior = calc_ema_slope(closes[-22:-8], ema_period=8, slope_lookback=4)

    if recent is None or prior is None or abs(prior) < 0.00005:
        return None

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
    if vol_regime <= 2:
        vol_bucket = "calm"
    elif vol_regime <= 3:
        vol_bucket = "normal"
    else:
        vol_bucket = "volatile"

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

def compute_snapshot(asset: str) -> dict | None:
    """Compute a full regime snapshot from current asset data."""
    now = datetime.now(timezone.utc)
    since_24h = (now - timedelta(hours=25)).isoformat()
    candles = get_candles(asset, since=since_24h, limit=1500)

    if len(candles) < 30:
        log.warning(f"Not enough candles for regime ({asset}: {len(candles)})")
        return None

    cutoff_15m = (now - timedelta(minutes=15)).isoformat()
    cutoff_1h = (now - timedelta(hours=1)).isoformat()

    candles_15m = [c for c in candles if c["ts"] >= cutoff_15m]
    candles_1h = [c for c in candles if c["ts"] >= cutoff_1h]

    closes_all = [c["close"] for c in candles]
    closes_1h = [c["close"] for c in candles_1h]

    price = candles[-1]["close"]

    def safe_return(now_p, old_p):
        if now_p is None or old_p is None or old_p == 0:
            return None
        return round((now_p - old_p) / old_p * 100, 4)

    ago_15m = candles[-16]["close"] if len(candles) >= 16 else None
    ago_1h = candles[-61]["close"] if len(candles) >= 61 else None
    ago_4h = candles[-241]["close"] if len(candles) >= 241 else None

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

    now_et = now.astimezone(ET)
    baseline = get_baseline(asset, hour_et=now_et.hour, day_of_week=now_et.weekday())

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
        "btc_price": price,
        "btc_return_15m": safe_return(price, ago_15m),
        "btc_return_1h": safe_return(price, ago_1h),
        "btc_return_4h": safe_return(price, ago_4h),
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

    snap_id = insert_regime_snapshot(asset, snapshot)
    snapshot["id"] = snap_id
    log.debug(f"Regime ({asset}): {composite} (vol={vol_regime} trend={trend_regime:+d} "
              f"volume={volume_regime}) ${price:,.0f}")
    return snapshot


# ═══════════════════════════════════════════════════════════════
#  BASELINES
# ═══════════════════════════════════════════════════════════════

def compute_baselines(asset: str):
    log.info(f"Computing baselines ({asset})...")
    since = (datetime.now(timezone.utc) - timedelta(days=BACKFILL_DAYS)).isoformat()
    candles = get_candles(asset, since=since, limit=999_999)

    if len(candles) < 1000:
        log.warning(f"Not enough candles for baselines ({asset}: {len(candles)})")
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
        upsert_baseline(asset, None, None, global_stats)

    hour_count = 0
    for hour, hcandles in by_hour.items():
        stats = compute_stats(hcandles)
        if stats:
            upsert_baseline(asset, hour, None, stats)
            hour_count += 1

    hour_day_count = 0
    for (hour, dow), hdcandles in by_hour_day.items():
        stats = compute_stats(hdcandles)
        if stats:
            upsert_baseline(asset, hour, dow, stats)
            hour_day_count += 1

    log.info(f"Baselines ({asset}): global + {hour_count} hours + {hour_day_count} hour×day")


# ═══════════════════════════════════════════════════════════════
#  REGIME STABILITY TRACKING
# ═══════════════════════════════════════════════════════════════

def _track_regime_stability(asset: str, snap: dict, prev_label: str,
                             prev_coarse: str, prev_price: float = None):
    try:
        curr_label = snap.get("composite_label", "unknown")
        curr_coarse = compute_coarse_label(
            snap.get("vol_regime", 3),
            snap.get("trend_regime", 0),
            snap.get("volume_regime")
        )
        price = snap.get("btc_price")
        btc_change = None
        if price and prev_price and prev_price > 0:
            btc_change = round((price - prev_price) / prev_price * 100, 4)

        label_changed = int(curr_label != prev_label) if prev_label else 0
        coarse_changed = int(curr_coarse != prev_coarse) if prev_coarse else 0

        insert_regime_stability(asset, {
            "prev_label": prev_label,
            "curr_label": curr_label,
            "prev_coarse": prev_coarse,
            "curr_coarse": curr_coarse,
            "label_changed": label_changed,
            "coarse_changed": coarse_changed,
            "btc_price": price,
            "btc_change_pct": btc_change,
        })
    except Exception as e:
        log.debug(f"Regime stability tracking error: {e}")


# ═══════════════════════════════════════════════════════════════
#  BACKGROUND WORKER
# ═══════════════════════════════════════════════════════════════

def regime_worker(asset: str, stop_event, plugin_id: str = None):
    """
    Background thread: keeps asset data and regime snapshots current.
    Uses DB heartbeat so multiple plugins sharing an asset don't duplicate work.
    """
    log.info(f"Regime worker starting ({asset})...")

    # Check if another worker is already running for this asset
    if is_regime_worker_running(asset):
        log.info(f"Regime worker for {asset} already running — skipping")
        return

    update_regime_heartbeat(asset, "backfilling")

    try:
        backfill_history(asset)
    except Exception as e:
        log.error(f"Backfill failed ({asset}): {e}")
        try:
            insert_log("ERROR", f"Backfill failed: {e}", f"regime.{asset}")
        except Exception:
            pass

    update_regime_heartbeat(asset, "computing_baselines")

    try:
        compute_baselines(asset)
    except Exception as e:
        log.error(f"Baselines failed ({asset}): {e}")

    update_regime_heartbeat(asset, "updating_history")

    try:
        update_history(asset)
    except Exception as e:
        log.error(f"History update failed ({asset}): {e}")

    update_regime_heartbeat(asset, "first_snapshot")

    snap = None
    try:
        snap = compute_snapshot(asset)
    except Exception as e:
        log.error(f"First snapshot failed ({asset}): {e}")

    update_regime_heartbeat(asset, "running")

    if snap:
        label = snap.get("composite_label", "unknown")
        price = snap.get("btc_price", 0)
        candle_count = count_candles(asset)
        try:
            from push import send_to_all
            send_to_all(
                "Regime Engine Ready",
                f"Backfill complete ({candle_count:,} candles). "
                f"Current: {label.replace('_',' ')} · {asset} ${price:,.0f}",
                tag="regime-ready",
            )
        except Exception:
            pass
        log.info(f"Regime engine ready ({asset}): {label} ({candle_count:,} candles)")
    else:
        log.warning(f"Regime engine started but first snapshot failed ({asset})")

    last_history = time.time()
    last_snapshot = time.time()
    last_baseline = time.time()

    _prev_regime_label = snap.get("composite_label", "unknown") if snap else "unknown"
    _prev_coarse_label = None
    _prev_price = snap.get("btc_price") if snap else None

    while not stop_event.is_set():
        try:
            now = time.time()

            update_regime_heartbeat(asset, "running")

            if now - last_history >= HISTORY_POLL:
                update_history(asset)
                last_history = now

            if now - last_snapshot >= SNAPSHOT_INTERVAL:
                new_snap = compute_snapshot(asset)
                if new_snap:
                    _track_regime_stability(
                        asset, new_snap, _prev_regime_label, _prev_coarse_label,
                        _prev_price
                    )
                    _prev_regime_label = new_snap.get("composite_label", "unknown")
                    _prev_coarse_label = compute_coarse_label(
                        new_snap.get("vol_regime", 3),
                        new_snap.get("trend_regime", 0),
                        new_snap.get("volume_regime"))
                    _prev_price = new_snap.get("btc_price")
                last_snapshot = now

            if now - last_baseline >= BASELINE_INTERVAL:
                compute_baselines(asset)
                last_baseline = now

            stop_event.wait(10)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            log.error(f"Regime worker error ({asset}): {e}", exc_info=True)
            try:
                insert_log("ERROR", f"Regime worker ({asset}): {e}", f"regime.{asset}")
            except Exception:
                pass
            stop_event.wait(30)

    log.info(f"Regime worker stopped ({asset})")
