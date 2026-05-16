"""Plant Cabinet Controller — Flask web server.

Wires the serial daemon to the REST API and dashboard SPA per
docs/server-api-spec.md §4. Run directly for the dev server, or under
systemd in production (see spec §10.1).
"""

import atexit
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from flask import Blueprint, Flask, current_app, jsonify, render_template, request

import config
import models
from notifications import (
    DEFAULT_COOLDOWN_S,
    AlertMonitor,
    NotificationManager,
    rule_descriptors,
)
from serial_daemon import SerialDaemon

logger = logging.getLogger(__name__)

# --- Static configuration ---

RELAY_NAMES: tuple[str, ...] = ("humidifier", "fan", "heater", "spare")
PID_LOOP_NAMES: tuple[str, ...] = ("temperature", "humidity", "co2")

SETPOINT_TO_MCU_PARAM: dict[str, str] = {
    "temperature_c": "temperature",
    "humidity_rh":   "humidity",
    "co2_ppm":       "co2",
}

SETPOINT_RANGES: dict[str, tuple[float, float]] = {
    "temperature_c": (18.0, 25.0),
    "humidity_rh":   (60.0, 80.0),
    "co2_ppm":       (400.0, 1000.0),
}

DEFAULT_SETPOINTS: dict[str, float] = {
    "temperature_c": 22.0,
    "humidity_rh":   70.0,
    "co2_ppm":       600.0,
}

DEFAULT_ALERT_THRESHOLDS: dict[str, float] = {
    "humidity_low_warning":  55,
    "humidity_low_critical": 45,
    "temp_low_warning":      16,
    "temp_high_warning":     28,
    "co2_high_warning":      1200,
}

# Mirrors mcu/include/config.h compile-time defaults. The MCU is authoritative
# at runtime; this server-side copy is updated on every successful pid_tune POST.
DEFAULT_PID_TUNING: dict[str, dict[str, float]] = {
    "temperature": {"kp": 2.0, "ki": 0.5, "kd": 1.0},
    "humidity":    {"kp": 2.0, "ki": 0.5, "kd": 1.0},
    "co2":         {"kp": 1.0, "ki": 0.3, "kd": 0.5},
}

# Maps each PID loop name -> the relay it drives. Used to derive per-loop
# "mode" in /api/pid from the per-relay mode tracked by the server.
LOOP_TO_RELAY: dict[str, str] = {
    "temperature": "heater",
    "humidity":    "humidifier",
    "co2":         "fan",
}

# --- Helpers ---

def _to_iso8601(ts: str | None) -> str | None:
    """SQLite 'YYYY-MM-DD HH:MM:SS' (UTC) -> 'YYYY-MM-DDTHH:MM:SSZ'."""
    if not ts:
        return None
    s = ts.replace(" ", "T")
    return s if s.endswith("Z") else s + "Z"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _daemon() -> SerialDaemon:
    return current_app.config["serial_daemon"]


def _is_number(v: Any) -> bool:
    """True for int/float but not bool (since bool is an int subclass in Python)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# --- API blueprint ---

api_bp = Blueprint("api", __name__, url_prefix="/api")


@api_bp.get("/readings")
def api_readings():
    state = _daemon().get_latest_state()
    if state.reading is not None:
        r = state.reading
        return jsonify({
            "temperature_c":     r.get("temp_c"),
            "humidity_rh":       r.get("hum_rh"),
            "pressure_hpa":      r.get("pres_hpa"),
            "co2_ppm":           r.get("co2_ppm"),
            "scd_temperature_c": r.get("scd_temp_c"),
            "scd_humidity_rh":   r.get("scd_hum_rh"),
            "timestamp":         _utcnow_iso(),
        })
    db_row = models.get_latest_reading()
    if db_row is not None:
        return jsonify({
            "temperature_c":     db_row.get("temperature_c"),
            "humidity_rh":       db_row.get("humidity_rh"),
            "pressure_hpa":      db_row.get("pressure_hpa"),
            "co2_ppm":           db_row.get("co2_ppm"),
            "scd_temperature_c": db_row.get("scd_temperature_c"),
            "scd_humidity_rh":   db_row.get("scd_humidity_rh"),
            "timestamp":         _to_iso8601(db_row.get("timestamp")),
        })
    return jsonify({"error": "no readings available"}), 503


@api_bp.get("/history")
def api_history():
    period = request.args.get("period", "24h")
    resolution = request.args.get("resolution")
    try:
        rows = models.get_history(period=period, resolution=resolution)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    for r in rows:
        if r.get("timestamp"):
            r["timestamp"] = _to_iso8601(r["timestamp"])

    return jsonify({
        "period":     period,
        "resolution": resolution or models.auto_resolution(period),
        "data":       rows,
    })


@api_bp.get("/setpoints")
def api_setpoints_get():
    state = _daemon().get_latest_state()
    pid = state.pid or {}
    if pid:
        return jsonify({
            "temperature_c": pid.get("temp", {}).get("sp"),
            "humidity_rh":   pid.get("hum",  {}).get("sp"),
            "co2_ppm":       pid.get("co2",  {}).get("sp"),
        })
    return jsonify(dict(DEFAULT_SETPOINTS))


@api_bp.post("/setpoints")
def api_setpoints_post():
    data = request.get_json(silent=True) or {}

    # Validate everything first, then send — atomic semantics.
    to_send: list[tuple[str, str, float]] = []
    for field, mcu_param in SETPOINT_TO_MCU_PARAM.items():
        if field not in data:
            continue
        value = data[field]
        if not _is_number(value):
            return jsonify({"success": False, "reason": f"{field}: not a number"}), 400
        lo, hi = SETPOINT_RANGES[field]
        if value < lo or value > hi:
            return jsonify({
                "success": False,
                "reason":  f"{field}: out of range [{lo}, {hi}]",
            }), 400
        to_send.append((field, mcu_param, float(value)))

    if not to_send:
        return jsonify({"success": False, "reason": "no setpoint fields provided"}), 400

    daemon = _daemon()
    applied: dict[str, float] = {}
    for field, mcu_param, value in to_send:
        daemon.send_command({"cmd": "setpoint", "param": mcu_param, "value": value})
        applied[field] = value
    return jsonify({"success": True, "applied": applied})


@api_bp.get("/relays")
def api_relays_get():
    state = _daemon().get_latest_state()
    relays_state = state.relays or {}
    modes = current_app.config["relay_modes"]

    result: dict[str, dict[str, Any]] = {}
    with models.connect() as conn:
        for name in RELAY_NAMES:
            on = bool(relays_state.get(name, False))
            # Most-recent event matching the current state tells us when it began.
            row = conn.execute(
                "SELECT timestamp FROM relay_events "
                "WHERE relay_name = ? AND new_state = ? "
                "ORDER BY id DESC LIMIT 1",
                (name, 1 if on else 0),
            ).fetchone()
            ts = _to_iso8601(row["timestamp"]) if row else None
            entry: dict[str, Any] = {
                "state": on,
                "mode":  modes.get(name, "auto"),
            }
            entry["on_since" if on else "off_since"] = ts
            result[name] = entry
    return jsonify(result)


@api_bp.post("/relays")
def api_relays_post():
    data = request.get_json(silent=True) or {}
    target = data.get("target")
    mode = data.get("mode")

    if target not in RELAY_NAMES:
        return jsonify({"success": False, "reason": "invalid target"}), 400
    if mode not in ("auto", "manual_on", "manual_off"):
        return jsonify({"success": False, "reason": "invalid mode"}), 400

    daemon = _daemon()
    if mode == "manual_on":
        daemon.send_command({"cmd": "relay", "target": target, "state": True})
    elif mode == "manual_off":
        daemon.send_command({"cmd": "relay", "target": target, "state": False})
    # mode == "auto": server-side preference only. The MCU's PID continues
    # to drive PID-controlled relays; future cycles will overwrite any
    # manual state that was set previously.

    current_app.config["relay_modes"][target] = mode
    return jsonify({"success": True})


@api_bp.get("/pid")
def api_pid_get():
    state = _daemon().get_latest_state()
    pid = state.pid or {}
    modes = current_app.config["relay_modes"]

    def loop_response(mcu_key: str, loop_name: str) -> dict[str, Any]:
        loop = pid.get(mcu_key, {}) if isinstance(pid, dict) else {}
        relay = LOOP_TO_RELAY[loop_name]
        return {
            "setpoint": loop.get("sp"),
            "actual":   loop.get("pv"),
            "output":   loop.get("out"),
            "mode":     "auto" if modes.get(relay) == "auto" else "manual",
        }

    return jsonify({
        "temperature": loop_response("temp", "temperature"),
        "humidity":    loop_response("hum",  "humidity"),
        "co2":         loop_response("co2",  "co2"),
    })


@api_bp.get("/status")
def api_status_get():
    state = _daemon().get_latest_state()
    server_uptime_s = int(time.monotonic() - current_app.config["server_started_at"])

    last_age_s: int | None = None
    if state.last_message_at is not None:
        last_age_s = int(time.monotonic() - state.last_message_at)

    db_size_mb: float | None = None
    try:
        if config.DB_PATH.exists():
            db_size_mb = round(config.DB_PATH.stat().st_size / 1_000_000, 2)
    except OSError:
        logger.exception("Failed to stat database file")

    active_alerts = 0
    try:
        with models.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM alerts WHERE resolved_at IS NULL"
            ).fetchone()
            if row is not None:
                active_alerts = row["n"]
    except Exception:
        logger.exception("Failed to count active alerts")

    mcu_status = state.status or {}
    return jsonify({
        "mcu_connected":      state.connected,
        "mcu_uptime_s":       mcu_status.get("uptime_s"),
        "mcu_state":          mcu_status.get("state"),
        "server_uptime_s":    server_uptime_s,
        "db_size_mb":         db_size_mb,
        "last_reading_age_s": last_age_s,
        "firmware_version":   mcu_status.get("version"),
        "alerts_active":      active_alerts,
    })


@api_bp.get("/alerts")
def api_alerts_get():
    active_only = request.args.get("active", "false").lower() in ("true", "1", "yes")
    with models.connect() as conn:
        if active_only:
            rows = conn.execute(
                "SELECT * FROM alerts WHERE resolved_at IS NULL ORDER BY id DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM alerts ORDER BY id DESC LIMIT 100"
            ).fetchall()
    alerts = []
    for r in rows:
        d = dict(r)
        d["timestamp"] = _to_iso8601(d.get("timestamp"))
        if d.get("resolved_at"):
            d["resolved_at"] = _to_iso8601(d["resolved_at"])
        alerts.append(d)
    return jsonify({"alerts": alerts})


@api_bp.get("/alerts/config")
def api_alerts_config():
    notifications: NotificationManager = current_app.config["notifications"]
    return jsonify({
        "thresholds":       dict(current_app.config["alert_thresholds"]),
        "cooldown_seconds": int(notifications.cooldown_s),
        "ifttt_configured": notifications.configured,
        "rules":            rule_descriptors(),
    })


@api_bp.get("/config")
def api_config_get():
    webhook = "***configured***" if config.IFTTT_WEBHOOK_KEY else "***not configured***"
    return jsonify({
        "alert_thresholds":  dict(current_app.config["alert_thresholds"]),
        "ifttt_webhook_key": webhook,
        "pid_tuning":        {k: dict(v) for k, v in current_app.config["pid_tuning"].items()},
    })


@api_bp.post("/config")
def api_config_post():
    data = request.get_json(silent=True) or {}
    daemon = _daemon()
    applied: dict[str, Any] = {}

    thresholds = data.get("alert_thresholds")
    if isinstance(thresholds, dict):
        applied["alert_thresholds"] = {}
        for key, value in thresholds.items():
            if key not in current_app.config["alert_thresholds"]:
                continue
            if not _is_number(value):
                continue
            current_app.config["alert_thresholds"][key] = value
            applied["alert_thresholds"][key] = value

    tuning = data.get("pid_tuning")
    if isinstance(tuning, dict):
        applied["pid_tuning"] = {}
        for loop_name, gains in tuning.items():
            if loop_name not in PID_LOOP_NAMES or not isinstance(gains, dict):
                continue
            kp, ki, kd = gains.get("kp"), gains.get("ki"), gains.get("kd")
            if not all(_is_number(g) and g >= 0 for g in (kp, ki, kd)):
                continue
            daemon.send_command({
                "cmd":  "pid_tune",
                "loop": loop_name,
                "kp":   float(kp),
                "ki":   float(ki),
                "kd":   float(kd),
            })
            new_gains = {"kp": float(kp), "ki": float(ki), "kd": float(kd)}
            current_app.config["pid_tuning"][loop_name] = new_gains
            applied["pid_tuning"][loop_name] = new_gains

    # IFTTT webhook key is intentionally not editable via this endpoint —
    # it lives in .env so secrets don't flow through HTTP.
    return jsonify({"success": True, "applied": applied})


# --- App factory ---

def create_app() -> Flask:
    """Build and configure the Flask application."""
    app = Flask(__name__)
    app.config["SECRET_KEY"] = config.FLASK_SECRET_KEY
    app.config["server_started_at"] = time.monotonic()
    app.config["alert_thresholds"] = dict(DEFAULT_ALERT_THRESHOLDS)
    app.config["pid_tuning"] = {k: dict(v) for k, v in DEFAULT_PID_TUNING.items()}

    # Per-relay user-facing mode. 'spare' has no PID so it's marked 'off' for
    # display; POST /api/relays still requires 'manual_on' / 'manual_off' to
    # change its hardware state.
    app.config["relay_modes"] = {name: "auto" for name in RELAY_NAMES}
    app.config["relay_modes"]["spare"] = "off"

    models.init_db()

    notifications = NotificationManager(
        webhook_key=config.IFTTT_WEBHOOK_KEY,
        cooldown_s=DEFAULT_COOLDOWN_S,
    )
    alert_monitor = AlertMonitor(
        notifications=notifications,
        thresholds_provider=lambda: app.config["alert_thresholds"],
    )
    alert_monitor.start()

    daemon = SerialDaemon(alert_monitor=alert_monitor)
    daemon.start()

    app.config["notifications"] = notifications
    app.config["alert_monitor"] = alert_monitor
    app.config["serial_daemon"] = daemon

    # Register reverse-order: daemon stops first, monitor second.
    atexit.register(alert_monitor.stop)
    atexit.register(daemon.stop)

    @app.route("/")
    def dashboard():
        return render_template("index.html")

    app.register_blueprint(api_bp)
    return app


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    debug = os.environ.get("FLASK_DEBUG", "0").lower() in ("1", "true", "yes")
    # use_reloader disabled — the reloader spawns a child process that would
    # try to open the same serial port and orphan the daemon thread.
    create_app().run(host="0.0.0.0", port=5000, debug=debug, use_reloader=False)
