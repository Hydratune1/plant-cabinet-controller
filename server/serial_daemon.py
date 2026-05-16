"""Background thread that bridges the MCU serial line to SQLite + in-memory state.

Per docs/server-api-spec.md §5 (Option A — thread within Flask app).

Reads newline-delimited JSON messages from the MCU and dispatches by `type`:
  - reading -> models.insert_reading + cache latest
  - relay   -> models.insert_relay_event (on diff) + cache latest
  - pid     -> cache latest in memory
  - status  -> cache latest in memory
  - error   -> log
  - ack     -> log

Outbound commands are submitted via send_command() into a thread-safe queue
and flushed to the MCU between reads.
"""

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any

import serial  # pyserial

import config
import models
from notifications import AlertMonitor

logger = logging.getLogger(__name__)

RELAY_CHANNELS: tuple[str, ...] = ("humidifier", "fan", "heater", "spare")


@dataclass
class LatestState:
    """In-memory snapshot of the most recent MCU state. Each field is a copy."""
    reading: dict[str, Any] | None = None
    relays:  dict[str, Any] | None = None
    pid:     dict[str, Any] | None = None
    status:  dict[str, Any] | None = None
    last_message_at: float | None = None  # time.monotonic()
    connected: bool = False


class SerialDaemon:
    """Owns the MCU serial connection and reading-ingestion thread."""

    def __init__(
        self,
        port: str = config.MCU_SERIAL_PORT,
        baud: int = config.MCU_BAUD_RATE,
        reconnect_delay_s: float = 5.0,
        alert_monitor: AlertMonitor | None = None,
    ) -> None:
        self.port = port
        self.baud = baud
        self.reconnect_delay_s = reconnect_delay_s

        self._cmd_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._state = LatestState()
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._serial: serial.Serial | None = None
        self._alert_monitor = alert_monitor

    # --- Public API ---

    def start(self) -> None:
        """Spawn the background thread if not already running."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="serial-daemon", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the thread to exit and wait briefly for it to do so."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def send_command(self, command: dict[str, Any]) -> None:
        """Queue a JSON command to be written to the MCU on the next tick."""
        self._cmd_queue.put(command)

    def get_latest_state(self) -> LatestState:
        """Return a snapshot of the latest MCU state. Safe to mutate."""
        with self._state_lock:
            s = self._state
            return LatestState(
                reading=dict(s.reading) if s.reading else None,
                relays=dict(s.relays) if s.relays else None,
                pid=dict(s.pid) if s.pid else None,
                status=dict(s.status) if s.status else None,
                last_message_at=s.last_message_at,
                connected=s.connected,
            )

    # --- Thread main ---

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._open_serial()
                self._serve_loop()
            except serial.SerialException as e:
                logger.warning("Serial error on %s: %s", self.port, e)
            except Exception:
                logger.exception("Unexpected error in serial daemon loop")
            finally:
                self._close_serial()
                self._set_connected(False)
            # Sleep before reconnecting, but wake up immediately on stop().
            if self._stop.wait(self.reconnect_delay_s):
                return

    def _open_serial(self) -> None:
        # timeout=0.1 lets the read loop check the stop flag and command queue
        # roughly 10 times per second when the MCU is idle.
        self._serial = serial.Serial(self.port, self.baud, timeout=0.1)
        self._set_connected(True)
        logger.info("Opened serial port %s @ %d baud", self.port, self.baud)

    def _close_serial(self) -> None:
        if self._serial is None:
            return
        try:
            self._serial.close()
        except Exception:
            logger.exception("Error closing serial port")
        self._serial = None

    def _set_connected(self, connected: bool) -> None:
        with self._state_lock:
            self._state.connected = connected

    def _serve_loop(self) -> None:
        ser = self._serial
        assert ser is not None
        buf = bytearray()
        while not self._stop.is_set():
            chunk = ser.read(256)
            if chunk:
                buf.extend(chunk)
                while b"\n" in buf:
                    line, _, rest = buf.partition(b"\n")
                    buf = bytearray(rest)
                    self._handle_line(line.decode("utf-8", errors="replace").strip())
            self._flush_commands()

    def _flush_commands(self) -> None:
        ser = self._serial
        if ser is None:
            return
        while True:
            try:
                cmd = self._cmd_queue.get_nowait()
            except queue.Empty:
                return
            payload = json.dumps(cmd, separators=(",", ":")) + "\n"
            # Let SerialException propagate so the outer loop reconnects.
            ser.write(payload.encode("utf-8"))

    # --- Message dispatch ---

    def _handle_line(self, line: str) -> None:
        if not line:
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("Non-JSON line from MCU: %r", line[:200])
            return

        msg_type = msg.get("type") if isinstance(msg, dict) else None
        if not msg_type:
            logger.debug("Untyped message from MCU: %r", msg)
            return

        with self._state_lock:
            self._state.last_message_at = time.monotonic()

        handler = self._dispatch.get(msg_type)
        if handler is None:
            logger.debug("Unknown MCU message type: %s", msg_type)
            return
        handler(self, msg)

    def _handle_reading(self, msg: dict[str, Any]) -> None:
        try:
            models.insert_reading(
                temperature_c=msg.get("temp_c"),
                humidity_rh=msg.get("hum_rh"),
                pressure_hpa=msg.get("pres_hpa"),
                co2_ppm=msg.get("co2_ppm"),
                scd_temperature_c=msg.get("scd_temp_c"),
                scd_humidity_rh=msg.get("scd_hum_rh"),
            )
        except Exception:
            logger.exception("Failed to persist reading")
        with self._state_lock:
            self._state.reading = msg
        if self._alert_monitor is not None:
            try:
                self._alert_monitor.check_reading(msg)
            except Exception:
                logger.exception("AlertMonitor.check_reading raised")

    def _handle_relay(self, msg: dict[str, Any]) -> None:
        # Diff against the previous snapshot so we only log true transitions.
        # MCU emits relay messages on every change, but each message echoes
        # all 4 channels — we'd over-count without diffing.
        with self._state_lock:
            previous = self._state.relays or {}
            self._state.relays = msg

        if not previous:
            # First relay message of the session — establishes baseline only.
            return

        # Phase 3 MCU doesn't tag the trigger source; default to 'pid'.
        # When Phase 4 wires up manual commands, the daemon can correlate
        # acks to attribute manual / safety triggers.
        for name in RELAY_CHANNELS:
            new_state = msg.get(name)
            if new_state is None:
                continue
            if previous.get(name) == new_state:
                continue
            try:
                models.insert_relay_event(
                    relay_name=name, new_state=bool(new_state), trigger="pid"
                )
            except Exception:
                logger.exception("Failed to persist relay event for %s", name)

    def _handle_pid(self, msg: dict[str, Any]) -> None:
        with self._state_lock:
            self._state.pid = msg

    def _handle_status(self, msg: dict[str, Any]) -> None:
        with self._state_lock:
            self._state.status = msg

    def _handle_error(self, msg: dict[str, Any]) -> None:
        code = msg.get("code", "")
        text = msg.get("msg", "")
        logger.warning("MCU error: %s — %s", code, text)
        if self._alert_monitor is not None:
            try:
                self._alert_monitor.on_mcu_error(code, text)
            except Exception:
                logger.exception("AlertMonitor.on_mcu_error raised")

    def _handle_ack(self, msg: dict[str, Any]) -> None:
        if msg.get("success"):
            logger.info("MCU ack: cmd=%s ok", msg.get("cmd"))
        else:
            logger.warning(
                "MCU ack rejected: cmd=%s reason=%s",
                msg.get("cmd"), msg.get("reason"),
            )

    # Dispatch table built after methods are defined.
    _dispatch: dict[str, Any] = {}


SerialDaemon._dispatch = {
    "reading": SerialDaemon._handle_reading,
    "relay":   SerialDaemon._handle_relay,
    "pid":     SerialDaemon._handle_pid,
    "status":  SerialDaemon._handle_status,
    "error":   SerialDaemon._handle_error,
    "ack":     SerialDaemon._handle_ack,
}
