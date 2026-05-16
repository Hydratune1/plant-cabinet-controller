"""SQLite schema and query helpers (per docs/server-api-spec.md §3)."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import config

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    temperature_c REAL,
    humidity_rh REAL,
    pressure_hpa REAL,
    co2_ppm INTEGER,
    scd_temperature_c REAL,
    scd_humidity_rh REAL
);
CREATE INDEX IF NOT EXISTS idx_readings_timestamp ON readings(timestamp);

CREATE TABLE IF NOT EXISTS relay_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    relay_name TEXT NOT NULL,
    new_state INTEGER NOT NULL,
    trigger TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_relay_events_timestamp ON relay_events(timestamp);

CREATE TABLE IF NOT EXISTS setpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    parameter TEXT NOT NULL,
    value REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    condition TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    resolved_at TEXT
);
"""

PERIOD_DELTAS: dict[str, timedelta] = {
    "1h":  timedelta(hours=1),
    "6h":  timedelta(hours=6),
    "24h": timedelta(hours=24),
    "7d":  timedelta(days=7),
    "30d": timedelta(days=30),
}

RESOLUTION_SECONDS: dict[str, int] = {
    "raw": 0,
    "1m":  60,
    "5m":  300,
    "15m": 900,
    "1h":  3600,
}


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Yield a database connection with sqlite3.Row factory and FKs enabled."""
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create tables and indexes if they don't already exist."""
    with connect() as conn:
        # WAL allows dashboard reads to run while serial daemon writes — set
        # once per database file, persists across connections.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA_SQL)


def insert_reading(
    *,
    temperature_c: float | None,
    humidity_rh: float | None,
    pressure_hpa: float | None,
    co2_ppm: int | None,
    scd_temperature_c: float | None,
    scd_humidity_rh: float | None,
    timestamp: str | None = None,
) -> None:
    """Insert one sensor reading row. timestamp defaults to NOW (UTC)."""
    with connect() as conn:
        if timestamp is None:
            conn.execute(
                """INSERT INTO readings
                       (temperature_c, humidity_rh, pressure_hpa, co2_ppm,
                        scd_temperature_c, scd_humidity_rh)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (temperature_c, humidity_rh, pressure_hpa, co2_ppm,
                 scd_temperature_c, scd_humidity_rh),
            )
        else:
            conn.execute(
                """INSERT INTO readings
                       (timestamp, temperature_c, humidity_rh, pressure_hpa,
                        co2_ppm, scd_temperature_c, scd_humidity_rh)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (timestamp, temperature_c, humidity_rh, pressure_hpa, co2_ppm,
                 scd_temperature_c, scd_humidity_rh),
            )


def insert_relay_event(
    *,
    relay_name: str,
    new_state: bool,
    trigger: str,
    timestamp: str | None = None,
) -> None:
    """Log a relay state transition. trigger is 'pid' | 'manual' | 'safety' | 'failsafe'."""
    state_int = 1 if new_state else 0
    with connect() as conn:
        if timestamp is None:
            conn.execute(
                "INSERT INTO relay_events (relay_name, new_state, trigger) VALUES (?, ?, ?)",
                (relay_name, state_int, trigger),
            )
        else:
            conn.execute(
                """INSERT INTO relay_events (timestamp, relay_name, new_state, trigger)
                   VALUES (?, ?, ?, ?)""",
                (timestamp, relay_name, state_int, trigger),
            )


def insert_alert(
    *,
    condition: str,
    severity: str,
    message: str,
    timestamp: str | None = None,
) -> int:
    """Insert an alert row and return its primary key."""
    with connect() as conn:
        if timestamp is None:
            cur = conn.execute(
                "INSERT INTO alerts (condition, severity, message) VALUES (?, ?, ?)",
                (condition, severity, message),
            )
        else:
            cur = conn.execute(
                """INSERT INTO alerts (timestamp, condition, severity, message)
                   VALUES (?, ?, ?, ?)""",
                (timestamp, condition, severity, message),
            )
        return int(cur.lastrowid or 0)


def resolve_alert(alert_id: int) -> None:
    """Mark an alert as resolved (sets resolved_at to NOW; no-op if already resolved)."""
    with connect() as conn:
        conn.execute(
            "UPDATE alerts SET resolved_at = datetime('now') "
            "WHERE id = ? AND resolved_at IS NULL",
            (alert_id,),
        )


def get_latest_reading() -> dict[str, Any] | None:
    """Return the most recent reading row as a dict, or None if the table is empty."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM readings ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def auto_resolution(period: str) -> str:
    """Pick a sensible default resolution for the given period."""
    if period in ("1h", "6h"):
        return "raw"
    if period == "24h":
        return "5m"
    if period == "7d":
        return "15m"
    return "1h"  # 30d


def get_history(
    period: str = "24h",
    resolution: str | None = None,
) -> list[dict[str, Any]]:
    """Return time-series rows aggregated to the requested resolution.

    period:     '1h' | '6h' | '24h' | '7d' | '30d'
    resolution: 'raw' | '1m' | '5m' | '15m' | '1h' (default: auto-by-period)
    """
    if period not in PERIOD_DELTAS:
        raise ValueError(f"unknown period: {period!r}")
    if resolution is None:
        resolution = auto_resolution(period)
    if resolution not in RESOLUTION_SECONDS:
        raise ValueError(f"unknown resolution: {resolution!r}")

    since = datetime.now(timezone.utc) - PERIOD_DELTAS[period]
    since_sql = since.strftime("%Y-%m-%d %H:%M:%S")
    bucket_s = RESOLUTION_SECONDS[resolution]

    with connect() as conn:
        if bucket_s == 0:
            rows = conn.execute(
                """SELECT timestamp, temperature_c, humidity_rh, pressure_hpa,
                          co2_ppm, scd_temperature_c, scd_humidity_rh
                   FROM readings
                   WHERE timestamp >= ?
                   ORDER BY timestamp ASC""",
                (since_sql,),
            ).fetchall()
        else:
            # bucket_s is whitelisted by RESOLUTION_SECONDS — safe to interpolate.
            rows = conn.execute(
                f"""SELECT
                       datetime((strftime('%s', timestamp) / {bucket_s}) * {bucket_s},
                                'unixepoch') AS timestamp,
                       AVG(temperature_c)     AS temperature_c,
                       AVG(humidity_rh)       AS humidity_rh,
                       AVG(pressure_hpa)      AS pressure_hpa,
                       AVG(co2_ppm)           AS co2_ppm,
                       AVG(scd_temperature_c) AS scd_temperature_c,
                       AVG(scd_humidity_rh)   AS scd_humidity_rh
                   FROM readings
                   WHERE timestamp >= ?
                   GROUP BY (strftime('%s', timestamp) / {bucket_s})
                   ORDER BY timestamp ASC""",
                (since_sql,),
            ).fetchall()
        return [dict(r) for r in rows]
