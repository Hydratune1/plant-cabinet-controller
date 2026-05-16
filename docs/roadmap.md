# Implementation Roadmap

Phased development plan for the plant cabinet controller. Each phase is self-contained and testable — the system is usable at the end of every phase.

---

## Phase 1 — Sensor Reading & Serial Output

**Goal:** Prove the hardware works. Read both sensors and output values to serial.

### Tasks
- [ ] Wire BME280 and SCD40 via Qwiic to the Uno Q
- [ ] Install Arduino libraries: `Adafruit_BME280`, `SensirionI2CScd4x`
- [ ] Implement I2C scan to verify sensor addresses (0x76, 0x62)
- [ ] Read BME280 (temperature, humidity, pressure) every 2 seconds
- [ ] Read SCD40 (CO2, temperature, humidity) every 5 seconds
- [ ] Output formatted JSON over Serial for the Linux side to consume
- [ ] Handle sensor read failures gracefully (log error, continue)

### Serial Output Format
```json
{"type":"reading","ts":12345,"temp_c":22.3,"hum_rh":68.5,"pres_hpa":1013.2,"co2_ppm":485,"scd_temp_c":22.1,"scd_hum_rh":67.8}
```

### Success Criteria
- Both sensors report plausible values on serial monitor
- SCD40 temperature/humidity roughly agrees with BME280 (±2°C, ±5% RH)
- No crashes or lockups over 1 hour continuous operation

### CLI Commands
```
"Implement Phase 1: BME280 and SCD40 sensor reading with JSON serial output. Follow the spec in docs/mcu-firmware-spec.md"
```

---

## Phase 2 — Relay Control & Safety Logic

**Goal:** Drive relays safely with manual on/off commands and implement failsafes.

### Tasks
- [ ] Wire 4-channel relay board to GPIO pins D4–D7
- [ ] Implement relay driver with active-LOW logic
- [ ] Add minimum on/off time enforcement (prevent rapid cycling, minimum 30s)
- [ ] Implement heater safety: max continuous on-time of 10 minutes
- [ ] Implement failsafe: if no sensor reading for 30s → heater OFF, fan ON
- [ ] Accept relay commands via Serial from Linux side
- [ ] Log all relay state changes with timestamps

### Command Format (Linux → MCU)
```json
{"cmd":"relay","target":"humidifier","state":true}
{"cmd":"relay","target":"fan","state":false}
```

### Response Format (MCU → Linux)
```json
{"type":"relay_state","humidifier":true,"fan":false,"heater":false,"spare":false,"ts":12345}
```

### Success Criteria
- Relays toggle on command
- Heater auto-shuts-off after 10 minutes continuous
- Removing a sensor wire triggers failsafe within 30 seconds
- No relay chattering (minimum cycle time enforced)

### CLI Commands
```
"Implement Phase 2: relay control with safety logic. Follow docs/mcu-firmware-spec.md sections on relay management and safety."
```

---

## Phase 3 — PID Control Loops

**Goal:** Automatically maintain target temperature, humidity, and CO2 levels.

### Tasks
- [ ] Install PID library (`QuickPID` or `Arduino-PID-Library`)
- [ ] Implement temperature PID → controls heater relay
- [ ] Implement humidity PID → controls humidifier relay
- [ ] Implement CO2 PID → controls fan relay (ventilation)
- [ ] Fan also runs on a duty cycle for baseline circulation
- [ ] Clamp PID outputs to respect minimum on/off times
- [ ] Allow setpoint adjustment via Serial commands from Linux side
- [ ] Tune PID gains (start conservative, tighten over time)

### Setpoint Command Format
```json
{"cmd":"setpoint","param":"temperature","value":23.0}
{"cmd":"setpoint","param":"humidity","value":72.0}
{"cmd":"setpoint","param":"co2","value":550.0}
```

### PID Status Report (MCU → Linux, every 5 seconds)
```json
{"type":"pid_status","temp":{"setpoint":22.0,"actual":21.8,"output":0.45},"hum":{"setpoint":70.0,"actual":68.2,"output":0.72},"co2":{"setpoint":600,"actual":520,"output":0.15},"ts":12345}
```

### Success Criteria
- Temperature holds within ±1°C of setpoint over 1 hour
- Humidity holds within ±5% RH of setpoint
- No overshooting that causes relay rapid-cycling
- Setpoints adjustable in real-time without restart

### CLI Commands
```
"Implement Phase 3: PID control loops for temperature, humidity, and CO2. Follow docs/mcu-firmware-spec.md PID section."
```

---

## Phase 4 — Flask Web Server & Dashboard

**Goal:** Live web dashboard accessible from any device on the local network.

### Tasks
- [ ] Set up Bridge RPC or serial communication between MCU and Linux
- [ ] Create SQLite database schema (readings, relay_events, setpoints)
- [ ] Implement serial reader daemon that ingests MCU JSON and writes to SQLite
- [ ] Build REST API endpoints (readings, history, setpoints, relays)
- [ ] Build responsive dashboard with live-updating sensor cards
- [ ] Add historical charts (last 24h, 7d, 30d) using Chart.js or similar
- [ ] Add relay control toggles (manual override)
- [ ] Add setpoint adjustment sliders
- [ ] Mobile-optimised layout (phone-first design)

### Success Criteria
- Dashboard loads on phone browser via `http://<uno-q-ip>:5000`
- Charts show real sensor history
- Changing a setpoint on the dashboard reflects on the MCU within 2 seconds
- Manual relay override works from the UI

### CLI Commands
```
"Implement Phase 4: Flask server with SQLite storage, REST API, and responsive dashboard. Follow docs/server-api-spec.md."
```

---

## Phase 5 — Apple HomeKit via Homebridge

**Goal:** See plant cabinet data in Apple Home and control relays via Siri.

### Tasks
- [ ] Install Node.js and Homebridge on the Uno Q Linux side
- [ ] Install homebridge-http plugins for temperature, humidity, and switch
- [ ] Configure Homebridge to poll Flask API endpoints
- [ ] Expose sensors: temperature (°C), humidity (%), CO2 (ppm as AirQuality)
- [ ] Expose switches: humidifier, fan, heater
- [ ] Test pairing with Apple Home via Apple TV 4K hub
- [ ] Verify Siri commands work ("What's the humidity in the plant cabinet?")
- [ ] Set up Home automations (e.g., notify if temp < 18°C)

### Success Criteria
- Plant cabinet appears as a room in Apple Home with all accessories
- Sensor values update within 30 seconds
- Siri responds to queries about cabinet conditions
- Home automations trigger correctly

### CLI Commands
```
"Implement Phase 5: Homebridge configuration for HomeKit. Follow docs/server-api-spec.md HomeKit section."
```

---

## Phase 6 — IFTTT & Notifications

**Goal:** Get phone alerts for out-of-range conditions and enable smart home integration.

### Tasks
- [ ] Set up IFTTT Webhooks service
- [ ] Implement alert thresholds on the server side
- [ ] Fire webhooks when conditions breach thresholds for >5 minutes
- [ ] Notifications: humidity too low, temperature too low/high, CO2 too high
- [ ] Implement cooldown (don't spam — max 1 alert per condition per hour)
- [ ] Optional: integrate with other IFTTT services (Google Sheets logging, smart plugs)

### Alert Thresholds (defaults, configurable via API)
| Condition | Trigger | Severity |
|-----------|---------|----------|
| Humidity < 55% | 5 min sustained | Warning |
| Humidity < 45% | Immediate | Critical |
| Temperature < 16°C | 5 min sustained | Warning |
| Temperature > 28°C | 5 min sustained | Warning |
| CO2 > 1200 ppm | 5 min sustained | Warning |
| Sensor offline | 2 min no data | Critical |

### Success Criteria
- Phone notification received within 1 minute of threshold breach
- No duplicate alerts during cooldown period
- Alerts clear when conditions return to normal

### CLI Commands
```
"Implement Phase 6: IFTTT webhook alerts with threshold monitoring. Follow docs/server-api-spec.md alerts section."
```

---

## Phase 7 — Polish & Hardening

**Goal:** Production-ready system that runs unattended for months.

### Tasks
- [ ] Implement watchdog timer on MCU (auto-restart if hung)
- [ ] Add systemd service files for Flask server and serial daemon
- [ ] Add Homebridge auto-start on boot
- [ ] Implement log rotation (don't fill the 16GB eMMC)
- [ ] Add database pruning (keep 30 days detailed, aggregate older data)
- [ ] Add OTA firmware update support via Linux side
- [ ] Stress test: run 7 days unattended, verify stability
- [ ] Document final wiring with photos/diagrams

### Success Criteria
- System recovers from power outage automatically
- No manual intervention needed for 30+ days
- Storage usage stays bounded
- All services start on boot within 60 seconds

### CLI Commands
```
"Implement Phase 7: production hardening — systemd services, watchdog, log rotation, DB pruning."
```
