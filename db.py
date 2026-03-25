"""
db.py — Database schema and connection for the trading platform.
SQLite with WAL mode for concurrent dashboard + plugin access.
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
#  SCHEMA
# ═══════════════════════════════════════════════════════════════

def init_db():
    with get_conn() as c:

        # ── Platform tables ────────────────────────────────────

        # ── Bot config (key-value store) ──────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS bot_config (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)

        # ── Plugin state (one row per plugin — live status, session tracking) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS plugin_state (
                plugin_id           TEXT PRIMARY KEY,
                status              TEXT DEFAULT 'stopped',
                status_detail       TEXT DEFAULT '',
                auto_trading        INTEGER DEFAULT 0,
                trades_remaining    INTEGER DEFAULT 0,

                -- Consecutive loss tracking
                loss_streak         INTEGER DEFAULT 0,
                cooldown_remaining  INTEGER DEFAULT 0,

                -- Live info
                bankroll_cents      INTEGER DEFAULT 0,
                session_pnl         REAL DEFAULT 0,
                session_wins        INTEGER DEFAULT 0,
                session_losses      INTEGER DEFAULT 0,
                session_skips       INTEGER DEFAULT 0,
                lifetime_pnl        REAL DEFAULT 0,
                lifetime_wins       INTEGER DEFAULT 0,
                lifetime_losses     INTEGER DEFAULT 0,

                -- Active trade snapshot (JSON)
                active_trade        TEXT,
                -- Live market info (JSON, updated even when stopped)
                live_market         TEXT,
                last_ticker         TEXT,
                last_updated        TEXT,

                -- Extended state
                last_completed_trade TEXT,
                _delay_end_iso      TEXT,
                cashing_out         INTEGER,
                cancel_cash_out     INTEGER,
                pending_trade       TEXT,
                session_data_bets   INTEGER DEFAULT 0,
                session_stopped_at  TEXT DEFAULT '',
                _prev_session       TEXT DEFAULT '',
                auto_trading_since  TEXT DEFAULT '',
                active_skip         TEXT,
                regime_engine_phase TEXT,
                observatory_health  TEXT,
                active_shadow       TEXT
            )
        """)

        # ── Bot commands (dashboard → plugin) ────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS bot_commands (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                plugin_id       TEXT,
                command_type    TEXT NOT NULL,
                parameters      TEXT DEFAULT '{}',
                status          TEXT DEFAULT 'pending',
                created_at      TEXT NOT NULL,
                result          TEXT
            )
        """)

        # ── Log entries (structured, for dashboard display) ───
        c.execute("""
            CREATE TABLE IF NOT EXISTS log_entries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                level       TEXT NOT NULL,
                category    TEXT DEFAULT 'bot',
                source      TEXT DEFAULT 'platform',
                message     TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts ON log_entries(ts)")

        # ── Push notification subscriptions ──────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint        TEXT NOT NULL UNIQUE,
                subscription_json TEXT NOT NULL,
                created_at      TEXT NOT NULL
            )
        """)

        # ── Push notification log ─────────────────────────────
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

        # ── Bankroll snapshots (recorded after each trade settles) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS bankroll_snapshots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at     TEXT NOT NULL,
                bankroll_cents  INTEGER NOT NULL,
                trade_id        INTEGER
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_bs_time ON bankroll_snapshots(captured_at)")

        # ── Audit log (security — tracks destructive operations) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT NOT NULL,
                action      TEXT NOT NULL,
                detail      TEXT,
                ip          TEXT,
                success     INTEGER DEFAULT 1
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(created_at)")

        # ── Asset tables (shared across plugins, partitioned by asset) ──

        # ── Candle history (1-minute from Binance) ────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS candles (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                asset   TEXT DEFAULT 'BTC',
                ts      TEXT NOT NULL,
                open    REAL NOT NULL,
                high    REAL NOT NULL,
                low     REAL NOT NULL,
                close   REAL NOT NULL,
                volume  REAL NOT NULL,
                UNIQUE(asset, ts)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_candles_ts ON candles(ts)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_candles_asset_ts ON candles(asset, ts)")

        # ── Market baselines (statistical norms per hour/dow) ─
        c.execute("""
            CREATE TABLE IF NOT EXISTS baselines (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                asset           TEXT DEFAULT 'BTC',
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
                sample_count    INTEGER,
                avg_bollinger_width REAL,
                p10_bollinger_width REAL
            )
        """)

        # ── Regime snapshots (written every ~5 min) ───────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS regime_snapshots (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                asset                   TEXT DEFAULT 'BTC',
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
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_regime_captured
            ON regime_snapshots(captured_at)
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_regime_asset
            ON regime_snapshots(asset, captured_at)
        """)

        # ── Regime stability log (tracks label persistence) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS regime_stability_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                asset           TEXT DEFAULT 'BTC',
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
        c.execute("CREATE INDEX IF NOT EXISTS idx_rstab_ts ON regime_stability_log(captured_at)")

        # ── Regime heartbeat (cross-process coordination) ────
        c.execute("""
            CREATE TABLE IF NOT EXISTS regime_heartbeat (
                asset           TEXT PRIMARY KEY,
                updated_at      TEXT NOT NULL,
                composite_label TEXT,
                regime_confidence REAL,
                vol_regime      INTEGER,
                trend_regime    INTEGER,
                volume_regime   INTEGER
            )
        """)


# ═══════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════

def get_config(key: str, default=None):
    with get_conn() as c:
        row = c.execute("SELECT value FROM bot_config WHERE key = ?",
                        (key,)).fetchone()
        if row:
            return json.loads(row["value"])
        return default


def set_config(key: str, value):
    with get_conn() as c:
        c.execute("""
            INSERT INTO bot_config (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                           updated_at = excluded.updated_at
        """, (key, json.dumps(value), now_utc()))


def get_all_config(namespace: str = None) -> dict:
    """Get all config keys. If namespace given, filter keys starting with that prefix."""
    with get_conn() as c:
        if namespace:
            rows = c.execute(
                "SELECT key, value FROM bot_config WHERE key LIKE ?",
                (namespace + "%",)
            ).fetchall()
        else:
            rows = c.execute("SELECT key, value FROM bot_config").fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}


# ═══════════════════════════════════════════════════════════════
#  PLUGIN STATE
# ═══════════════════════════════════════════════════════════════

_PLUGIN_STATE_JSON_FIELDS = (
    "active_trade", "active_skip", "active_shadow", "live_market",
    "last_completed_trade", "pending_trade", "observatory_health",
)


def get_plugin_state(plugin_id: str) -> dict:
    """Get state for a plugin, with JSON fields decoded."""
    with get_conn() as c:
        row = c.execute(
            "SELECT * FROM plugin_state WHERE plugin_id = ?",
            (plugin_id,)
        ).fetchone()
        state = row_to_dict(row) or {}
        for key in _PLUGIN_STATE_JSON_FIELDS:
            if state.get(key):
                try:
                    state[key] = json.loads(state[key])
                except (json.JSONDecodeError, TypeError):
                    state[key] = None
        return state


def update_plugin_state(plugin_id: str, data: dict):
    """Upsert plugin state. Merges data into existing row (INSERT OR UPDATE)."""
    # Serialize any dict/list fields to JSON
    for key in _PLUGIN_STATE_JSON_FIELDS:
        if key in data and isinstance(data[key], (dict, list)):
            data = {**data, key: json.dumps(data[key])}
    data["last_updated"] = now_utc()
    data["plugin_id"] = plugin_id

    cols = ", ".join(data.keys())
    placeholders = ", ".join(["?"] * len(data))
    updates = ", ".join(f"{k} = excluded.{k}" for k in data if k != "plugin_id")

    with get_conn() as c:
        c.execute(f"""
            INSERT INTO plugin_state ({cols}) VALUES ({placeholders})
            ON CONFLICT(plugin_id) DO UPDATE SET {updates}
        """, list(data.values()))


def get_all_plugin_states() -> list:
    """Get state for all plugins."""
    with get_conn() as c:
        rows = c.execute("SELECT * FROM plugin_state").fetchall()
        results = []
        for row in rows:
            state = dict(row)
            for key in _PLUGIN_STATE_JSON_FIELDS:
                if state.get(key):
                    try:
                        state[key] = json.loads(state[key])
                    except (json.JSONDecodeError, TypeError):
                        state[key] = None
            results.append(state)
        return results


# ═══════════════════════════════════════════════════════════════
#  COMMANDS
# ═══════════════════════════════════════════════════════════════

def enqueue_command(plugin_id: str, command_type: str, parameters: dict = None) -> int:
    with get_conn() as c:
        cur = c.execute("""
            INSERT INTO bot_commands (plugin_id, command_type, parameters, created_at)
            VALUES (?, ?, ?, ?)
        """, (plugin_id, command_type, json.dumps(parameters or {}), now_utc()))
        return cur.lastrowid


def get_pending_commands(plugin_id: str) -> list:
    """Atomically claim all pending commands for a plugin (pending → executing)."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT * FROM bot_commands
            WHERE plugin_id = ? AND status = 'pending'
            ORDER BY created_at ASC
        """, (plugin_id,)).fetchall()
        cmds = rows_to_list(rows)
        for cmd in cmds:
            c.execute("""
                UPDATE bot_commands SET status = 'executing'
                WHERE id = ? AND status = 'pending'
            """, (cmd["id"],))
        return cmds


def complete_command(cmd_id: int, result: dict = None):
    with get_conn() as c:
        c.execute("""
            UPDATE bot_commands SET status = 'completed', result = ?
            WHERE id = ?
        """, (json.dumps(result or {}), cmd_id))


def cancel_command(cmd_id: int, reason: str = ""):
    with get_conn() as c:
        c.execute("""
            UPDATE bot_commands SET status = 'cancelled',
                result = ? WHERE id = ?
        """, (json.dumps({"reason": reason}), cmd_id))


def flush_pending_commands(plugin_id: str):
    """Cancel all pending/executing commands for a plugin. Called on startup."""
    with get_conn() as c:
        c.execute("""
            UPDATE bot_commands SET status = 'cancelled',
                result = '{"reason": "flushed on startup"}'
            WHERE plugin_id = ? AND status IN ('pending', 'executing')
        """, (plugin_id,))


# ═══════════════════════════════════════════════════════════════
#  LOGS
# ═══════════════════════════════════════════════════════════════

def insert_log(level: str, message: str, category: str = "bot",
               source: str = "platform"):
    with get_conn() as c:
        c.execute("""
            INSERT INTO log_entries (ts, level, category, source, message)
            VALUES (?, ?, ?, ?, ?)
        """, (now_utc(), level, category, source, message))


def get_logs(before_id: int = None, limit: int = 100, level: str = None,
             source: str = None) -> list:
    with get_conn() as c:
        conditions = []
        params = []
        if before_id:
            conditions.append("id < ?")
            params.append(before_id)
        if level:
            conditions.append("level = ?")
            params.append(level)
        if source:
            conditions.append("source = ?")
            params.append(source)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)
        rows = c.execute(f"""
            SELECT * FROM log_entries {where}
            ORDER BY id DESC LIMIT ?
        """, params).fetchall()
        return rows_to_list(rows)


def get_logs_after(after_id: int) -> list:
    with get_conn() as c:
        rows = c.execute("""
            SELECT * FROM log_entries WHERE id > ?
            ORDER BY id ASC
        """, (after_id,)).fetchall()
        return rows_to_list(rows)


# ═══════════════════════════════════════════════════════════════
#  PUSH SUBSCRIPTIONS
# ═══════════════════════════════════════════════════════════════

def save_push_subscription(endpoint: str, subscription_json: str):
    with get_conn() as c:
        c.execute("""
            INSERT OR REPLACE INTO push_subscriptions
            (endpoint, subscription_json, created_at)
            VALUES (?, ?, ?)
        """, (endpoint, subscription_json, now_utc()))


def get_push_subscriptions() -> list:
    with get_conn() as c:
        rows = c.execute("SELECT * FROM push_subscriptions").fetchall()
        return rows_to_list(rows)


def remove_push_subscription(sub_id: int):
    with get_conn() as c:
        c.execute("DELETE FROM push_subscriptions WHERE id = ?", (sub_id,))


def remove_push_subscription_by_endpoint(endpoint: str):
    with get_conn() as c:
        c.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))


# ═══════════════════════════════════════════════════════════════
#  PUSH LOG
# ═══════════════════════════════════════════════════════════════

def insert_push_log(title: str, body: str, tag: str = ""):
    with get_conn() as c:
        c.execute("""
            INSERT INTO push_log (title, body, tag, sent_at) VALUES (?, ?, ?, ?)
        """, (title, body or "", tag or "", now_utc()))
        # Auto-clean: keep last 500
        c.execute("""
            DELETE FROM push_log WHERE id NOT IN (
                SELECT id FROM push_log ORDER BY sent_at DESC LIMIT 500
            )
        """)


def get_push_log(limit: int = 200, tag: str = None) -> list:
    with get_conn() as c:
        if tag:
            rows = c.execute("""
                SELECT * FROM push_log WHERE tag = ?
                ORDER BY sent_at DESC LIMIT ?
            """, (tag, limit)).fetchall()
        else:
            rows = c.execute("""
                SELECT * FROM push_log ORDER BY sent_at DESC LIMIT ?
            """, (limit,)).fetchall()
        return rows_to_list(rows)


# ═══════════════════════════════════════════════════════════════
#  BANKROLL SNAPSHOTS
# ═══════════════════════════════════════════════════════════════

def insert_bankroll_snapshot(bankroll_cents: int, trade_id: int = None,
                             plugin_id: str = None):
    with get_conn() as c:
        c.execute("""
            INSERT INTO bankroll_snapshots (captured_at, bankroll_cents, trade_id)
            VALUES (?, ?, ?)
        """, (now_utc(), bankroll_cents, trade_id))


def get_bankroll_chart_data(hours: int = None) -> list:
    """Get bankroll snapshots for charting. hours=None means all."""
    with get_conn() as c:
        if hours:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            rows = c.execute("""
                SELECT captured_at, bankroll_cents FROM bankroll_snapshots
                WHERE captured_at >= ?
                ORDER BY captured_at ASC
            """, (cutoff,)).fetchall()
        else:
            rows = c.execute("""
                SELECT captured_at, bankroll_cents FROM bankroll_snapshots
                ORDER BY captured_at ASC
            """).fetchall()
        return rows_to_list(rows)


def get_pnl_chart_data(hours: int = None) -> list:
    """Get cumulative PnL over time from trades.

    NOTE: This queries the trades table which lives in plugin databases.
    For the platform DB, this returns an empty list. Plugins should
    override or provide their own PnL chart data.
    """
    # Trades table is plugin-specific — return empty from platform db.
    # Plugins call this on their own market_db or override it.
    return []


# ═══════════════════════════════════════════════════════════════
#  AUDIT LOG
# ═══════════════════════════════════════════════════════════════

def insert_audit_log(action: str, detail: str = "", ip: str = "",
                     success: bool = True):
    """Log a security-relevant action to the audit trail."""
    try:
        with get_conn() as c:
            c.execute(
                "INSERT INTO audit_log (created_at, action, detail, ip, success) "
                "VALUES (?,?,?,?,?)",
                (now_utc(), action, detail, ip, 1 if success else 0)
            )
    except Exception:
        pass  # Never block on audit failure


# ═══════════════════════════════════════════════════════════════
#  CANDLES
# ═══════════════════════════════════════════════════════════════

def insert_candles(candles: list, asset: str = "BTC"):
    with get_conn() as c:
        rows = [{"asset": asset, **candle} for candle in candles]
        c.executemany("""
            INSERT OR IGNORE INTO candles (asset, ts, open, high, low, close, volume)
            VALUES (:asset, :ts, :open, :high, :low, :close, :volume)
        """, rows)


def get_candles(since: str, asset: str = "BTC", limit: int = 1500) -> list:
    with get_conn() as c:
        rows = c.execute("""
            SELECT * FROM candles WHERE asset = ? AND ts >= ?
            ORDER BY ts ASC LIMIT ?
        """, (asset, since, limit)).fetchall()
        return rows_to_list(rows)


def get_latest_candle(asset: str = "BTC") -> dict | None:
    with get_conn() as c:
        row = c.execute(
            "SELECT * FROM candles WHERE asset = ? ORDER BY ts DESC LIMIT 1",
            (asset,)
        ).fetchone()
        return row_to_dict(row)


def count_candles(asset: str = "BTC") -> int:
    with get_conn() as c:
        return c.execute(
            "SELECT COUNT(*) as n FROM candles WHERE asset = ?", (asset,)
        ).fetchone()["n"]


# ═══════════════════════════════════════════════════════════════
#  BASELINES
# ═══════════════════════════════════════════════════════════════

def upsert_baseline(hour_et: int | None, day_of_week: int | None,
                    data: dict, asset: str = "BTC"):
    with get_conn() as c:
        c.execute("""
            DELETE FROM baselines
            WHERE asset = ?
              AND (hour_et IS ? OR (hour_et IS NULL AND ? IS NULL))
              AND (day_of_week IS ? OR (day_of_week IS NULL AND ? IS NULL))
        """, (asset, hour_et, hour_et, day_of_week, day_of_week))

        fields = {"asset": asset, "computed_at": now_utc(),
                  "hour_et": hour_et, "day_of_week": day_of_week, **data}
        cols = ", ".join(fields.keys())
        placeholders = ", ".join(["?"] * len(fields))
        c.execute(f"INSERT INTO baselines ({cols}) VALUES ({placeholders})",
                  list(fields.values()))


def get_baseline(hour_et: int = None, day_of_week: int = None,
                 asset: str = "BTC") -> dict | None:
    """Get baseline with fallback: specific → hour-only → global."""
    with get_conn() as c:
        for h, d in [(hour_et, day_of_week), (hour_et, None), (None, None)]:
            row = c.execute("""
                SELECT * FROM baselines
                WHERE asset = ?
                  AND (hour_et IS ? OR (hour_et IS NULL AND ? IS NULL))
                  AND (day_of_week IS ? OR (day_of_week IS NULL AND ? IS NULL))
                ORDER BY computed_at DESC LIMIT 1
            """, (asset, h, h, d, d)).fetchone()
            if row:
                return row_to_dict(row)
        return None


# ═══════════════════════════════════════════════════════════════
#  REGIME SNAPSHOTS
# ═══════════════════════════════════════════════════════════════

def insert_regime_snapshot(data: dict, asset: str = "BTC") -> int:
    data["asset"] = asset
    data["captured_at"] = now_utc()
    cols = ", ".join(data.keys())
    placeholders = ", ".join(["?"] * len(data))
    with get_conn() as c:
        cur = c.execute(
            f"INSERT INTO regime_snapshots ({cols}) VALUES ({placeholders})",
            list(data.values()))
        return cur.lastrowid


def get_latest_regime_snapshot(asset: str = "BTC") -> dict | None:
    with get_conn() as c:
        row = c.execute("""
            SELECT * FROM regime_snapshots
            WHERE asset = ?
            ORDER BY captured_at DESC LIMIT 1
        """, (asset,)).fetchone()
        return row_to_dict(row)


# ═══════════════════════════════════════════════════════════════
#  REGIME STABILITY
# ═══════════════════════════════════════════════════════════════

def insert_regime_stability(data: dict, asset: str = "BTC"):
    """Insert a regime stability comparison record."""
    data["asset"] = asset
    data.setdefault("captured_at", now_utc())
    with get_conn() as c:
        keys = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        c.execute(f"INSERT INTO regime_stability_log ({keys}) VALUES ({placeholders})",
                  list(data.values()))


# ═══════════════════════════════════════════════════════════════
#  REGIME HEARTBEAT
# ═══════════════════════════════════════════════════════════════

def update_regime_heartbeat(asset: str, data: dict):
    """Upsert the regime heartbeat row for an asset."""
    data["asset"] = asset
    data["updated_at"] = now_utc()
    cols = ", ".join(data.keys())
    placeholders = ", ".join(["?"] * len(data))
    updates = ", ".join(f"{k} = excluded.{k}" for k in data if k != "asset")
    with get_conn() as c:
        c.execute(f"""
            INSERT INTO regime_heartbeat ({cols}) VALUES ({placeholders})
            ON CONFLICT(asset) DO UPDATE SET {updates}
        """, list(data.values()))


def get_regime_heartbeat(asset: str = "BTC") -> dict | None:
    with get_conn() as c:
        row = c.execute(
            "SELECT * FROM regime_heartbeat WHERE asset = ?", (asset,)
        ).fetchone()
        return row_to_dict(row)


def is_regime_worker_running(asset: str = "BTC",
                             stale_seconds: int = 600) -> bool:
    """Check if a regime worker is actively updating for this asset.
    Returns False if no heartbeat or heartbeat older than stale_seconds."""
    hb = get_regime_heartbeat(asset)
    if not hb or not hb.get("updated_at"):
        return False
    try:
        updated = datetime.fromisoformat(
            hb["updated_at"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        return age < stale_seconds
    except (ValueError, TypeError):
        return False


# ═══════════════════════════════════════════════════════════════
#  BACKUP
# ═══════════════════════════════════════════════════════════════

def backup_database(reason: str = "manual") -> str | None:
    """Create a timestamped backup of the database. Returns backup path or None."""
    import shutil
    try:
        backup_dir = os.path.join(os.path.dirname(DB_PATH), "_db_backups")
        os.makedirs(backup_dir, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(backup_dir, f"platform_{reason}_{ts}.db")
        # WAL-safe: checkpoint flushes WAL into main DB first
        src_conn = sqlite3.connect(DB_PATH, timeout=10)
        src_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        src_conn.close()
        shutil.copy2(DB_PATH, backup_path)
        # Keep only last 10 backups
        backups = sorted(
            [f for f in os.listdir(backup_dir) if f.endswith('.db')],
            reverse=True
        )
        for old in backups[10:]:
            try:
                os.remove(os.path.join(backup_dir, old))
            except Exception:
                pass
        return backup_path
    except Exception as e:
        print(f"[db] Backup failed: {e}")
        return None
