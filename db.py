"""
db.py — Platform database layer.
SQLite with WAL mode. Handles platform tables + shared queries.
Plugin-specific tables are created by each plugin's init_db().
"""

import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager

from config import DB_PATH


# ═══════════════════════════════════════════════════════════════
#  CONNECTION
# ═══════════════════════════════════════════════════════════════

def _ensure_dir():
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)


@contextmanager
def get_conn():
    _ensure_dir()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def row_to_dict(row) -> dict | None:
    return dict(row) if row else None


def rows_to_list(rows) -> list:
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════
#  SCHEMA — Platform tables only
# ═══════════════════════════════════════════════════════════════

def init_db():
    """Create all platform-level tables. Plugin tables are separate."""
    with get_conn() as c:

        # ── Bot config (namespaced key-value store) ────────────
        # Keys are namespaced: "btc_15m.trading_mode", "platform.log_retention_days"
        c.execute("""
            CREATE TABLE IF NOT EXISTS bot_config (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)

        # ── Plugin state (one row per plugin, replaces single-row bot_state) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS plugin_state (
                plugin_id       TEXT PRIMARY KEY,
                status          TEXT DEFAULT 'stopped',
                status_detail   TEXT DEFAULT '',
                state_json      TEXT DEFAULT '{}',
                last_updated    TEXT
            )
        """)

        # ── Bot commands (dashboard → plugin, with plugin column) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS bot_commands (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                plugin_id       TEXT NOT NULL,
                command_type    TEXT NOT NULL,
                parameters      TEXT DEFAULT '{}',
                status          TEXT DEFAULT 'pending',
                created_at      TEXT NOT NULL,
                result          TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_cmd_plugin ON bot_commands(plugin_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_cmd_status ON bot_commands(status)")

        # ── Log entries (with source column) ───────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS log_entries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                level       TEXT NOT NULL,
                source      TEXT DEFAULT 'platform',
                message     TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts ON log_entries(ts)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_logs_source ON log_entries(source)")

        # ── Push notification subscriptions ────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint        TEXT NOT NULL UNIQUE,
                subscription_json TEXT NOT NULL,
                created_at      TEXT NOT NULL
            )
        """)

        # ── Push notification log ──────────────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS push_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT NOT NULL,
                body        TEXT,
                tag         TEXT,
                sent_at     TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_pushlog_ts ON push_log(sent_at)")

        # ── Bankroll snapshots ─────────────────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS bankroll_snapshots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at     TEXT NOT NULL,
                bankroll_cents  INTEGER NOT NULL,
                plugin_id       TEXT,
                trade_id        INTEGER
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_bs_time ON bankroll_snapshots(captured_at)")

        # ── Audit log ──────────────────────────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                action      TEXT NOT NULL,
                detail      TEXT DEFAULT '',
                ip          TEXT DEFAULT '',
                success     INTEGER DEFAULT 1
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts)")

        # ── Asset tables: BTC candles ──────────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS candles (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                asset   TEXT NOT NULL DEFAULT 'BTC',
                ts      TEXT NOT NULL,
                open    REAL NOT NULL,
                high    REAL NOT NULL,
                low     REAL NOT NULL,
                close   REAL NOT NULL,
                volume  REAL NOT NULL,
                UNIQUE(asset, ts)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_candles_asset_ts ON candles(asset, ts)")

        # ── Asset tables: baselines ────────────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS baselines (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                asset           TEXT NOT NULL DEFAULT 'BTC',
                computed_at     TEXT NOT NULL,
                hour_et         INTEGER,
                day_of_week     INTEGER,
                avg_vol_15m     REAL,
                p25_vol_15m     REAL,
                p75_vol_15m     REAL,
                p90_vol_15m     REAL,
                avg_atr_15m     REAL,
                avg_volume_15m  REAL,
                p25_volume_15m  REAL,
                p75_volume_15m  REAL,
                p90_volume_15m  REAL,
                avg_range_15m   REAL,
                avg_bollinger_width REAL,
                p10_bollinger_width REAL,
                sample_count    INTEGER,
                UNIQUE(asset, hour_et, day_of_week)
            )
        """)

        # ── Asset tables: regime snapshots ─────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS regime_snapshots (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                asset                   TEXT NOT NULL DEFAULT 'BTC',
                captured_at             TEXT NOT NULL,
                btc_price               REAL,
                btc_return_15m          REAL,
                btc_return_1h           REAL,
                btc_return_4h           REAL,
                atr_15m                 REAL,
                atr_1h                  REAL,
                bollinger_width_15m     REAL,
                realized_vol_15m        REAL,
                realized_vol_1h         REAL,
                ema_slope_15m           REAL,
                ema_slope_1h            REAL,
                vol_regime              INTEGER,
                trend_regime            INTEGER,
                trend_direction         INTEGER,
                trend_strength          REAL,
                volume_15m              REAL,
                volume_regime           INTEGER,
                volume_spike            INTEGER DEFAULT 0,
                post_spike              INTEGER DEFAULT 0,
                trend_exhaustion        INTEGER DEFAULT 0,
                composite_label         TEXT,
                regime_confidence       REAL,
                bollinger_squeeze       INTEGER DEFAULT 0,
                trend_acceleration      TEXT,
                thin_market             INTEGER DEFAULT 0
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_regime_asset_ts ON regime_snapshots(asset, captured_at)")

        # ── Asset tables: regime stability log ─────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS regime_stability_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                asset           TEXT NOT NULL DEFAULT 'BTC',
                captured_at     TEXT NOT NULL,
                prev_label      TEXT,
                curr_label      TEXT,
                prev_coarse     TEXT,
                curr_coarse     TEXT,
                label_changed   INTEGER DEFAULT 0,
                coarse_changed  INTEGER DEFAULT 0,
                btc_price       REAL,
                btc_change_pct  REAL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_stab_asset_ts ON regime_stability_log(asset, captured_at)")

        # ── Asset tables: regime heartbeat ─────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS regime_heartbeat (
                asset           TEXT PRIMARY KEY,
                pid             INTEGER,
                last_beat       TEXT NOT NULL,
                phase           TEXT DEFAULT 'starting'
            )
        """)


# ═══════════════════════════════════════════════════════════════
#  CONFIG — Namespaced key-value store
# ═══════════════════════════════════════════════════════════════

def get_config(key: str, default=None):
    """Get a config value. Keys are namespaced like 'btc_15m.trading_mode'."""
    with get_conn() as c:
        row = c.execute("SELECT value FROM bot_config WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        val = row["value"]
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return val


def set_config(key: str, value):
    """Set a config value."""
    with get_conn() as c:
        c.execute("""
            INSERT INTO bot_config (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """, (key, json.dumps(value) if not isinstance(value, str) else value, now_utc()))


def get_all_config(namespace: str = None) -> dict:
    """Get all config, optionally filtered by namespace prefix."""
    with get_conn() as c:
        if namespace:
            rows = c.execute("SELECT key, value FROM bot_config WHERE key LIKE ?",
                             (f"{namespace}.%",)).fetchall()
        else:
            rows = c.execute("SELECT key, value FROM bot_config").fetchall()
    result = {}
    for r in rows:
        try:
            result[r["key"]] = json.loads(r["value"])
        except (json.JSONDecodeError, TypeError):
            result[r["key"]] = r["value"]
    return result


# ═══════════════════════════════════════════════════════════════
#  PLUGIN STATE
# ═══════════════════════════════════════════════════════════════

def get_plugin_state(plugin_id: str) -> dict:
    """Get plugin state. Returns dict with status, status_detail, and decoded state_json."""
    with get_conn() as c:
        row = c.execute("SELECT * FROM plugin_state WHERE plugin_id = ?",
                         (plugin_id,)).fetchone()
    if not row:
        return {"plugin_id": plugin_id, "status": "stopped",
                "status_detail": "", "state": {}, "last_updated": None}
    d = dict(row)
    try:
        d["state"] = json.loads(d.get("state_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        d["state"] = {}
    return d


def update_plugin_state(plugin_id: str, data: dict):
    """Update plugin state. 'state' key merges into existing state_json (not replace)."""
    with get_conn() as c:
        # Ensure row exists
        c.execute("""
            INSERT OR IGNORE INTO plugin_state (plugin_id, last_updated)
            VALUES (?, ?)
        """, (plugin_id, now_utc()))

        updates = []
        values = []
        for k, v in data.items():
            if k == "state":
                # Merge into existing state_json
                row = c.execute(
                    "SELECT state_json FROM plugin_state WHERE plugin_id = ?",
                    (plugin_id,)
                ).fetchone()
                existing = {}
                if row and row["state_json"]:
                    try:
                        existing = json.loads(row["state_json"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                existing.update(v)
                updates.append("state_json = ?")
                values.append(json.dumps(existing))
            elif k in ("status", "status_detail"):
                updates.append(f"{k} = ?")
                values.append(v)
        updates.append("last_updated = ?")
        values.append(now_utc())
        values.append(plugin_id)

        if updates:
            c.execute(f"UPDATE plugin_state SET {', '.join(updates)} WHERE plugin_id = ?",
                      values)


def get_all_plugin_states() -> list:
    """Get state for all registered plugins."""
    with get_conn() as c:
        rows = c.execute("SELECT * FROM plugin_state ORDER BY plugin_id").fetchall()
    result = []
    for row in rows:
        d = dict(row)
        try:
            d["state"] = json.loads(d.get("state_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            d["state"] = {}
        result.append(d)
    return result


# ═══════════════════════════════════════════════════════════════
#  COMMANDS
# ═══════════════════════════════════════════════════════════════

def enqueue_command(plugin_id: str, command_type: str, parameters: dict = None) -> int:
    """Enqueue a command for a plugin. Returns command ID."""
    with get_conn() as c:
        cur = c.execute("""
            INSERT INTO bot_commands (plugin_id, command_type, parameters, created_at)
            VALUES (?, ?, ?, ?)
        """, (plugin_id, command_type, json.dumps(parameters or {}), now_utc()))
        return cur.lastrowid


def get_pending_commands(plugin_id: str) -> list:
    """Get pending commands for a plugin, oldest first."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT * FROM bot_commands
            WHERE plugin_id = ? AND status = 'pending'
            ORDER BY id ASC
        """, (plugin_id,)).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["parameters"] = json.loads(d.get("parameters") or "{}")
        except (json.JSONDecodeError, TypeError):
            d["parameters"] = {}
        result.append(d)
    return result


def complete_command(cmd_id: int, result: dict = None):
    """Mark command as completed."""
    with get_conn() as c:
        c.execute("UPDATE bot_commands SET status = 'completed', result = ? WHERE id = ?",
                  (json.dumps(result) if result else None, cmd_id))


def cancel_command(cmd_id: int, reason: str = ""):
    """Mark command as cancelled."""
    with get_conn() as c:
        c.execute("UPDATE bot_commands SET status = 'cancelled', result = ? WHERE id = ?",
                  (json.dumps({"reason": reason}), cmd_id))


def flush_pending_commands(plugin_id: str):
    """Cancel all pending commands for a plugin."""
    with get_conn() as c:
        c.execute("""
            UPDATE bot_commands SET status = 'cancelled',
                   result = '{"reason": "flushed"}'
            WHERE plugin_id = ? AND status = 'pending'
        """, (plugin_id,))


# ═══════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════

def insert_log(level: str, message: str, source: str = "platform"):
    """Insert a structured log entry."""
    with get_conn() as c:
        c.execute("INSERT INTO log_entries (ts, level, source, message) VALUES (?, ?, ?, ?)",
                  (now_utc(), level, source, message))


def get_logs(before_id: int = None, limit: int = 100, level: str = None,
             source: str = None) -> list:
    """Get log entries with optional filters."""
    with get_conn() as c:
        sql = "SELECT * FROM log_entries WHERE 1=1"
        params = []
        if before_id:
            sql += " AND id < ?"
            params.append(before_id)
        if level:
            sql += " AND level = ?"
            params.append(level)
        if source:
            sql += " AND source = ?"
            params.append(source)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return rows_to_list(c.execute(sql, params).fetchall())


# ═══════════════════════════════════════════════════════════════
#  PUSH SUBSCRIPTIONS
# ═══════════════════════════════════════════════════════════════

def save_push_subscription(endpoint: str, subscription_json: str):
    with get_conn() as c:
        c.execute("""
            INSERT INTO push_subscriptions (endpoint, subscription_json, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET subscription_json = excluded.subscription_json
        """, (endpoint, subscription_json, now_utc()))


def get_push_subscriptions() -> list:
    with get_conn() as c:
        return rows_to_list(c.execute("SELECT * FROM push_subscriptions").fetchall())


def remove_push_subscription(sub_id: int):
    with get_conn() as c:
        c.execute("DELETE FROM push_subscriptions WHERE id = ?", (sub_id,))


def remove_push_subscription_by_endpoint(endpoint: str):
    with get_conn() as c:
        c.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))


def insert_push_log(title: str, body: str, tag: str = ""):
    with get_conn() as c:
        c.execute("INSERT INTO push_log (title, body, tag, sent_at) VALUES (?, ?, ?, ?)",
                  (title, body, tag, now_utc()))


def get_push_log(limit: int = 200, tag: str = None) -> list:
    with get_conn() as c:
        if tag:
            rows = c.execute(
                "SELECT * FROM push_log WHERE tag = ? ORDER BY id DESC LIMIT ?",
                (tag, limit)).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM push_log ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
    return rows_to_list(rows)


# ═══════════════════════════════════════════════════════════════
#  BANKROLL SNAPSHOTS
# ═══════════════════════════════════════════════════════════════

def insert_bankroll_snapshot(bankroll_cents: int, plugin_id: str = None,
                              trade_id: int = None):
    with get_conn() as c:
        c.execute("""
            INSERT INTO bankroll_snapshots (captured_at, bankroll_cents, plugin_id, trade_id)
            VALUES (?, ?, ?, ?)
        """, (now_utc(), bankroll_cents, plugin_id, trade_id))


def get_bankroll_chart_data(hours: int = None) -> list:
    with get_conn() as c:
        if hours:
            since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            rows = c.execute(
                "SELECT captured_at, bankroll_cents FROM bankroll_snapshots "
                "WHERE captured_at >= ? ORDER BY captured_at",
                (since,)).fetchall()
        else:
            rows = c.execute(
                "SELECT captured_at, bankroll_cents FROM bankroll_snapshots "
                "ORDER BY captured_at").fetchall()
    return rows_to_list(rows)


# ═══════════════════════════════════════════════════════════════
#  AUDIT LOG
# ═══════════════════════════════════════════════════════════════

def insert_audit_log(action: str, detail: str = "", ip: str = "",
                      success: bool = True):
    with get_conn() as c:
        c.execute("""
            INSERT INTO audit_log (ts, action, detail, ip, success)
            VALUES (?, ?, ?, ?, ?)
        """, (now_utc(), action, detail, ip, int(success)))


# ═══════════════════════════════════════════════════════════════
#  CANDLES (asset-parameterized)
# ═══════════════════════════════════════════════════════════════

def insert_candles(candles: list, asset: str = "BTC"):
    """Insert candles, ignoring duplicates."""
    with get_conn() as c:
        c.executemany("""
            INSERT OR IGNORE INTO candles (asset, ts, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, [(asset, cd["ts"], cd["open"], cd["high"], cd["low"],
               cd["close"], cd["volume"]) for cd in candles])


def get_candles(asset: str = "BTC", since: str = None, limit: int = 1500) -> list:
    """Get candles for an asset since a given timestamp."""
    with get_conn() as c:
        if since:
            rows = c.execute(
                "SELECT * FROM candles WHERE asset = ? AND ts >= ? ORDER BY ts LIMIT ?",
                (asset, since, limit)).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM candles WHERE asset = ? ORDER BY ts DESC LIMIT ?",
                (asset, limit)).fetchall()
            rows = list(reversed(rows))
    return rows_to_list(rows)


def get_latest_candle(asset: str = "BTC") -> dict | None:
    with get_conn() as c:
        row = c.execute(
            "SELECT * FROM candles WHERE asset = ? ORDER BY ts DESC LIMIT 1",
            (asset,)).fetchone()
    return row_to_dict(row)


def count_candles(asset: str = "BTC") -> int:
    with get_conn() as c:
        row = c.execute("SELECT COUNT(*) as cnt FROM candles WHERE asset = ?",
                         (asset,)).fetchone()
    return row["cnt"] if row else 0


# ═══════════════════════════════════════════════════════════════
#  BASELINES (asset-parameterized)
# ═══════════════════════════════════════════════════════════════

def upsert_baseline(asset: str, hour_et: int | None, day_of_week: int | None,
                     data: dict):
    """Upsert a baseline row for a given asset/hour/dow combo."""
    with get_conn() as c:
        c.execute("""
            INSERT INTO baselines (asset, computed_at, hour_et, day_of_week,
                avg_vol_15m, p25_vol_15m, p75_vol_15m, p90_vol_15m,
                avg_atr_15m, avg_volume_15m, p25_volume_15m, p75_volume_15m,
                p90_volume_15m, avg_range_15m, avg_bollinger_width,
                p10_bollinger_width, sample_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset, hour_et, day_of_week) DO UPDATE SET
                computed_at = excluded.computed_at,
                avg_vol_15m = excluded.avg_vol_15m,
                p25_vol_15m = excluded.p25_vol_15m,
                p75_vol_15m = excluded.p75_vol_15m,
                p90_vol_15m = excluded.p90_vol_15m,
                avg_atr_15m = excluded.avg_atr_15m,
                avg_volume_15m = excluded.avg_volume_15m,
                p25_volume_15m = excluded.p25_volume_15m,
                p75_volume_15m = excluded.p75_volume_15m,
                p90_volume_15m = excluded.p90_volume_15m,
                avg_range_15m = excluded.avg_range_15m,
                avg_bollinger_width = excluded.avg_bollinger_width,
                p10_bollinger_width = excluded.p10_bollinger_width,
                sample_count = excluded.sample_count
        """, (asset, now_utc(), hour_et, day_of_week,
              data.get("avg_vol_15m"), data.get("p25_vol_15m"),
              data.get("p75_vol_15m"), data.get("p90_vol_15m"),
              data.get("avg_atr_15m"), data.get("avg_volume_15m"),
              data.get("p25_volume_15m"), data.get("p75_volume_15m"),
              data.get("p90_volume_15m"), data.get("avg_range_15m"),
              data.get("avg_bollinger_width"), data.get("p10_bollinger_width"),
              data.get("sample_count")))


def get_baseline(asset: str = "BTC", hour_et: int = None,
                  day_of_week: int = None) -> dict | None:
    """Get baseline, with fallback: hour+dow → hour-only → global."""
    with get_conn() as c:
        if hour_et is not None and day_of_week is not None:
            row = c.execute(
                "SELECT * FROM baselines WHERE asset = ? AND hour_et = ? AND day_of_week = ?",
                (asset, hour_et, day_of_week)).fetchone()
            if row:
                return row_to_dict(row)
        if hour_et is not None:
            row = c.execute(
                "SELECT * FROM baselines WHERE asset = ? AND hour_et = ? AND day_of_week IS NULL",
                (asset, hour_et)).fetchone()
            if row:
                return row_to_dict(row)
        row = c.execute(
            "SELECT * FROM baselines WHERE asset = ? AND hour_et IS NULL AND day_of_week IS NULL",
            (asset,)).fetchone()
        return row_to_dict(row)


# ═══════════════════════════════════════════════════════════════
#  REGIME SNAPSHOTS (asset-parameterized)
# ═══════════════════════════════════════════════════════════════

def insert_regime_snapshot(asset: str, data: dict) -> int:
    """Insert a regime snapshot. Returns the new row ID."""
    with get_conn() as c:
        cur = c.execute("""
            INSERT INTO regime_snapshots (
                asset, captured_at, btc_price,
                btc_return_15m, btc_return_1h, btc_return_4h,
                atr_15m, atr_1h, bollinger_width_15m,
                realized_vol_15m, realized_vol_1h,
                ema_slope_15m, ema_slope_1h,
                vol_regime, trend_regime, trend_direction, trend_strength,
                volume_15m, volume_regime, volume_spike,
                post_spike, trend_exhaustion,
                composite_label, regime_confidence,
                bollinger_squeeze, trend_acceleration, thin_market
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            asset, now_utc(), data.get("btc_price"),
            data.get("btc_return_15m"), data.get("btc_return_1h"), data.get("btc_return_4h"),
            data.get("atr_15m"), data.get("atr_1h"), data.get("bollinger_width_15m"),
            data.get("realized_vol_15m"), data.get("realized_vol_1h"),
            data.get("ema_slope_15m"), data.get("ema_slope_1h"),
            data.get("vol_regime"), data.get("trend_regime"),
            data.get("trend_direction"), data.get("trend_strength"),
            data.get("volume_15m"), data.get("volume_regime"),
            data.get("volume_spike", 0),
            data.get("post_spike", 0), data.get("trend_exhaustion", 0),
            data.get("composite_label"), data.get("regime_confidence"),
            data.get("bollinger_squeeze", 0), data.get("trend_acceleration"),
            data.get("thin_market", 0),
        ))
        return cur.lastrowid


def get_latest_regime_snapshot(asset: str = "BTC") -> dict | None:
    with get_conn() as c:
        row = c.execute(
            "SELECT * FROM regime_snapshots WHERE asset = ? ORDER BY id DESC LIMIT 1",
            (asset,)).fetchone()
    return row_to_dict(row)


# ═══════════════════════════════════════════════════════════════
#  REGIME STABILITY
# ═══════════════════════════════════════════════════════════════

def insert_regime_stability(asset: str, data: dict):
    with get_conn() as c:
        c.execute("""
            INSERT INTO regime_stability_log (
                asset, captured_at, prev_label, curr_label,
                prev_coarse, curr_coarse, label_changed, coarse_changed,
                btc_price, btc_change_pct
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            asset, now_utc(),
            data.get("prev_label"), data.get("curr_label"),
            data.get("prev_coarse"), data.get("curr_coarse"),
            data.get("label_changed", 0), data.get("coarse_changed", 0),
            data.get("btc_price"), data.get("btc_change_pct"),
        ))


# ═══════════════════════════════════════════════════════════════
#  REGIME HEARTBEAT
# ═══════════════════════════════════════════════════════════════

def update_regime_heartbeat(asset: str, phase: str = "running"):
    """Update heartbeat for cross-process coordination."""
    import os
    with get_conn() as c:
        c.execute("""
            INSERT INTO regime_heartbeat (asset, pid, last_beat, phase)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(asset) DO UPDATE SET
                pid = excluded.pid, last_beat = excluded.last_beat, phase = excluded.phase
        """, (asset, os.getpid(), now_utc(), phase))


def get_regime_heartbeat(asset: str) -> dict | None:
    with get_conn() as c:
        row = c.execute("SELECT * FROM regime_heartbeat WHERE asset = ?",
                         (asset,)).fetchone()
    return row_to_dict(row)


def is_regime_worker_running(asset: str, max_stale_minutes: float = 10) -> bool:
    """Check if another regime worker is alive for this asset."""
    beat = get_regime_heartbeat(asset)
    if not beat:
        return False
    try:
        last = datetime.fromisoformat(beat["last_beat"])
        age_min = (datetime.now(timezone.utc) - last).total_seconds() / 60
        if age_min > max_stale_minutes:
            return False
        # Check if PID is still alive
        import os, signal
        os.kill(beat["pid"], 0)
        return True
    except (OSError, ValueError, KeyError):
        return False
