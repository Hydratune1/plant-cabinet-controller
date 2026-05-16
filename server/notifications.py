"""IFTTT webhook notifier and threshold-based alert monitor.

Per docs/server-api-spec.md §8. Receives sensor readings from the serial
daemon, evaluates threshold rules with sustained-duration semantics, writes
transitions to the alerts table, and posts to IFTTT (with per-alert-type
cooldown) when a rule fires.

The alert state machine for each rule:

    NORMAL --[breach observed]--> RAISING --[sustained for sustained_s]--> ACTIVE
       ^                            |                                          |
       +-- [condition resolves -----+                                          |
            before sustained]                                                  |
                                                                               |
    ACTIVE --[normal observed]--> RESOLVING --[sustained for resolve_s]--> NORMAL
                                      |
                                      +--[breach again]--> ACTIVE
"""

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Callable

import models

logger = logging.getLogger(__name__)

IFTTT_EVENT_NAME = "plant_cabinet_alert"
IFTTT_URL_TEMPLATE = "https://maker.ifttt.com/trigger/{event}/json/with/key/{key}"
DEFAULT_COOLDOWN_S: float = 1800.0  # 30 minutes per spec §8.4 (configurable)
SENSOR_OFFLINE_AFTER_S: float = 120.0
TICK_INTERVAL_S: float = 30.0
HEATER_LOCKOUT_CODES: frozenset[str] = frozenset({"HEATER_MAX_ON", "HEATER_OVERTEMP"})


@dataclass
class AlertRule:
    """A threshold rule against a sensor reading field."""
    name: str
    severity: str          # "warning" | "critical"
    sustained_s: float     # breach must persist this long before firing (0 = immediate)
    resolve_s: float       # normality must persist this long before clearing
    reading_key: str       # field in the MCU reading JSON
    threshold_key: str     # key in app.config["alert_thresholds"]
    direction: str         # "below" | "above"
    unit: str              # human-readable unit suffix for notification text


# Threshold rules from spec §8.2. sustained_s and resolve_s are in seconds.
ALERT_RULES: tuple[AlertRule, ...] = (
    AlertRule("humidity_low",      "warning",  300, 300, "hum_rh",  "humidity_low_warning",  "below", "% RH"),
    AlertRule("humidity_critical", "critical",   0,  60, "hum_rh",  "humidity_low_critical", "below", "% RH"),
    AlertRule("temp_low",          "warning",  300, 300, "temp_c",  "temp_low_warning",      "below", "°C"),
    AlertRule("temp_high",         "warning",  300, 300, "temp_c",  "temp_high_warning",     "above", "°C"),
    AlertRule("co2_high",          "warning",  300, 300, "co2_ppm", "co2_high_warning",      "above", " ppm"),
)


@dataclass
class _AlertState:
    """Mutable per-rule tracking. Mutated under AlertMonitor._lock only."""
    name: str
    severity: str
    active: bool = False
    raising_since: float | None = None
    resolving_since: float | None = None
    db_id: int | None = None


# --- IFTTT client with per-type cooldown ---

class NotificationManager:
    """Posts IFTTT webhooks with a per-alert-type cooldown.

    Failed sends do NOT consume the cooldown — only successful 2xx responses
    start the clock, so a transient network blip can't suppress the next real
    alert for 30 minutes.
    """

    def __init__(
        self,
        webhook_key: str | None,
        cooldown_s: float = DEFAULT_COOLDOWN_S,
        timeout_s: float = 10.0,
    ) -> None:
        self.webhook_key = webhook_key
        self.cooldown_s = cooldown_s
        self.timeout_s = timeout_s
        self._last_fired: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.webhook_key)

    def fire(self, alert_type: str, value: str, threshold: str) -> bool:
        """POST a webhook. Returns True if sent, False if skipped or failed."""
        if not self.webhook_key:
            logger.debug("IFTTT not configured; skipping %s", alert_type)
            return False

        now = time.monotonic()
        with self._lock:
            last = self._last_fired.get(alert_type, 0.0)
            if last and (now - last) < self.cooldown_s:
                remaining = int(self.cooldown_s - (now - last))
                logger.info(
                    "IFTTT cooldown active for %s (%ds remaining); skipping",
                    alert_type, remaining,
                )
                return False

        url = IFTTT_URL_TEMPLATE.format(event=IFTTT_EVENT_NAME, key=self.webhook_key)
        body = json.dumps({"value1": alert_type, "value2": value, "value3": threshold}).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                status = getattr(resp, "status", resp.getcode())
                if 200 <= status < 300:
                    with self._lock:
                        self._last_fired[alert_type] = now
                    logger.info("Fired IFTTT alert %s (value=%s)", alert_type, value)
                    return True
                logger.warning("IFTTT returned HTTP %s for %s", status, alert_type)
                return False
        except (urllib.error.URLError, OSError) as e:
            logger.warning("IFTTT request failed for %s: %s", alert_type, e)
            return False


# --- Threshold + sustained-duration state machine ---

class AlertMonitor:
    """Tracks per-rule alert state, persists transitions, fires notifications.

    Pushes from the serial daemon happen via `check_reading()` (per-reading
    rules) and `on_mcu_error()` (immediate heater_lockout). A background
    thread ticks `_tick_offline()` every TICK_INTERVAL_S to catch the
    sensor_offline case independent of incoming data.
    """

    def __init__(
        self,
        notifications: NotificationManager,
        thresholds_provider: Callable[[], dict[str, float]],
        rules: tuple[AlertRule, ...] = ALERT_RULES,
        offline_after_s: float = SENSOR_OFFLINE_AFTER_S,
        tick_interval_s: float = TICK_INTERVAL_S,
    ) -> None:
        self.notifications = notifications
        self.thresholds_provider = thresholds_provider
        self.rules = {r.name: r for r in rules}
        self.offline_after_s = offline_after_s
        self.tick_interval_s = tick_interval_s

        self._states: dict[str, _AlertState] = {
            r.name: _AlertState(name=r.name, severity=r.severity) for r in rules
        }
        # Non-reading-based alerts get their own state entries.
        self._states["sensor_offline"] = _AlertState(name="sensor_offline", severity="critical")
        self._states["heater_lockout"] = _AlertState(name="heater_lockout", severity="critical")

        self._lock = threading.Lock()
        self._last_reading_at: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- Lifecycle ---

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="alert-monitor", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    # --- Inputs from the serial daemon ---

    def check_reading(self, reading: dict[str, Any]) -> None:
        """Evaluate every reading-based rule against `reading`."""
        thresholds = self.thresholds_provider()
        now = time.monotonic()
        notify_calls: list[tuple[str, str, str]] = []
        with self._lock:
            self._last_reading_at = now
            # Sensor is back; resolve sensor_offline immediately.
            self._maybe_resolve(self._states["sensor_offline"])
            for rule in self.rules.values():
                args = self._evaluate(self._states[rule.name], rule, reading, thresholds, now)
                if args is not None:
                    notify_calls.append(args)
        # Network calls outside the lock — IFTTT can take seconds.
        for args in notify_calls:
            self.notifications.fire(*args)

    def on_mcu_error(self, code: str, msg: str) -> None:
        """Fire heater_lockout when the MCU emits a heater safety error."""
        if code not in HEATER_LOCKOUT_CODES:
            return
        notify: tuple[str, str, str] | None = None
        with self._lock:
            state = self._states["heater_lockout"]
            if state.active:
                return
            args = self._fire(state, value_str=msg, threshold_str=code,
                              message=f"Heater safety: {msg}")
            if args is not None:
                notify = args
        if notify is not None:
            self.notifications.fire(*notify)

    # --- Internal ---

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick_offline()
            except Exception:
                logger.exception("AlertMonitor tick failed")
            if self._stop.wait(self.tick_interval_s):
                return

    def _tick_offline(self) -> None:
        notify: tuple[str, str, str] | None = None
        with self._lock:
            if self._last_reading_at is None:
                return  # haven't seen any reading yet — daemon may still be coming up
            state = self._states["sensor_offline"]
            now = time.monotonic()
            offline_for = now - self._last_reading_at
            if offline_for < self.offline_after_s:
                return
            if state.active:
                return
            notify = self._fire(
                state,
                value_str=f"{int(offline_for)}s",
                threshold_str=f">{int(self.offline_after_s)}s",
                message=f"No MCU reading for {int(offline_for)}s",
            )
        if notify is not None:
            self.notifications.fire(*notify)

    def _evaluate(
        self,
        state: _AlertState,
        rule: AlertRule,
        reading: dict[str, Any],
        thresholds: dict[str, float],
        now: float,
    ) -> tuple[str, str, str] | None:
        value = reading.get(rule.reading_key)
        threshold = thresholds.get(rule.threshold_key)
        if value is None or threshold is None:
            return None

        breached = (value < threshold) if rule.direction == "below" else (value > threshold)

        if breached:
            state.resolving_since = None
            if state.active:
                return None
            if state.raising_since is None:
                state.raising_since = now
            if (now - state.raising_since) >= rule.sustained_s:
                return self._fire(
                    state,
                    value_str=self._format_value(rule, value),
                    threshold_str=self._format_threshold(rule, threshold),
                    message=self._format_message(rule, value, threshold),
                )
            return None

        # Not breached
        state.raising_since = None
        if not state.active:
            return None
        if state.resolving_since is None:
            state.resolving_since = now
        elif (now - state.resolving_since) >= rule.resolve_s:
            self._maybe_resolve(state)
        return None

    def _fire(
        self,
        state: _AlertState,
        *,
        value_str: str,
        threshold_str: str,
        message: str,
    ) -> tuple[str, str, str] | None:
        """Transition state to ACTIVE; persist; return notification args."""
        state.active = True
        state.raising_since = None
        state.resolving_since = None
        try:
            state.db_id = models.insert_alert(
                condition=state.name, severity=state.severity, message=message,
            )
        except Exception:
            logger.exception("Failed to persist alert %s", state.name)
            state.db_id = None
        return (state.name, value_str, threshold_str)

    def _maybe_resolve(self, state: _AlertState) -> None:
        if not state.active:
            return
        state.active = False
        state.resolving_since = None
        if state.db_id is not None:
            try:
                models.resolve_alert(state.db_id)
            except Exception:
                logger.exception("Failed to resolve alert %s", state.name)
            state.db_id = None

    @staticmethod
    def _format_value(rule: AlertRule, value: float) -> str:
        if rule.reading_key == "co2_ppm":
            return f"{int(value)}{rule.unit}"
        return f"{value:.1f}{rule.unit}"

    @staticmethod
    def _format_threshold(rule: AlertRule, threshold: float) -> str:
        arrow = "below" if rule.direction == "below" else "above"
        if rule.reading_key == "co2_ppm":
            return f"{arrow} {int(threshold)}{rule.unit}"
        return f"{arrow} {threshold}{rule.unit}"

    @classmethod
    def _format_message(cls, rule: AlertRule, value: float, threshold: float) -> str:
        return (
            f"{rule.name}: {cls._format_value(rule, value)} "
            f"({cls._format_threshold(rule, threshold)})"
        )


def rule_descriptors() -> list[dict[str, Any]]:
    """JSON-serialisable view of ALERT_RULES for /api/alerts/config."""
    return [asdict(r) for r in ALERT_RULES]
