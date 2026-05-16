# Server & API Specification

Technical specification for the Flask web application running on the Uno Q's Linux side (Qualcomm QRB2210, Debian).

---

## 1. Overview

The server application is responsible for:
- Receiving sensor data and relay states from the MCU via serial
- Persisting readings in a SQLite database
- Serving a responsive web dashboard
- Exposing a REST API for the dashboard SPA and Homebridge
- Monitoring thresholds and firing IFTTT webhook alerts
- Running as a systemd service (auto-start on boot)

---

## 2. System Architecture

```
┌─────────────┐     Serial/Bridge     ┌───────────────────────────┐
│  STM32 MCU  │ ◄──────────────────►  │  Linux (Debian)           │
│             │    JSON lines          │                           │
└─────────────┘                        │  ┌─────────────────────┐  │
                                       │  │  serial_daemon.py   │  │
                                       │  │  (reads MCU serial)  │  │
                                       │  └──────────┬──────────┘  │
                                       │             │ writes       │
                                       │  ┌──────────▼──────────┐  │
                                       │  │  SQLite database     │  │
                                       │  └──────────┬──────────┘  │
                                       │             │ reads        │
                                       │  ┌──────────▼──────────┐  │
                                       │  │  Flask app (app.py)  │  │
                                       │  │  - REST API          │  │
                                       │  │  - Dashboard SPA     │  │
                                       │  │  - Alert monitor     │  │
                                       │  └──────────┬──────────┘  │
                                       │             │              │
                                       │      WiFi (port 5000)     │
                                       └─────────────┼─────────────┘
                                                     │
                          ┌──────────────────────────┼──────────────────────┐
                          │                          │                      │
                    ┌─────▼─────┐           ┌───────▼───────┐    ┌────────▼────────┐
                    │  Browser  │           │  Homebridge   │    │  IFTTT Webhooks │
                    │  (phone)  │           │  (HomeKit)    │    │  (notifications)│
                    └───────────┘           └───────────────┘    └─────────────────┘
```

---

## 3. Database Schema (SQLite)

### 3.1 File Location
```
server/data/cabinet.db
```

### 3.2 Tables

**readings** — Sensor data time series
```sql
CREATE TABLE readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    temperature_c REAL,
    humidity_rh REAL,
    pressure_hpa REAL,
    co2_ppm INTEGER,
    scd_temperature_c REAL,
    scd_humidity_rh REAL
);

CREATE INDEX idx_readings_timestamp ON readings(timestamp);
```

**relay_events** — Log of all relay state changes
```sql
CREATE TABLE relay_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    relay_name TEXT NOT NULL,       -- 'humidifier', 'fan', 'heater', 'spare'
    new_state INTEGER NOT NULL,     -- 1 = on, 0 = off
    trigger TEXT NOT NULL           -- 'pid', 'manual', 'safety', 'failsafe'
);

CREATE INDEX idx_relay_events_timestamp ON relay_events(timestamp);
```

**setpoints** — Current and historical setpoint values
```sql
CREATE TABLE setpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    parameter TEXT NOT NULL,        -- 'temperature', 'humidity', 'co2'
    value REAL NOT NULL
);
```

**alerts** — Alert history
```sql
CREATE TABLE alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    condition TEXT NOT NULL,         -- 'humidity_low', 'temp_high', etc.
    severity TEXT NOT NULL,          -- 'warning', 'critical'
    message TEXT NOT NULL,
    resolved_at TEXT                 -- NULL if still active
);
```

### 3.3 Data Retention

- **Raw readings:** Keep 30 days at full resolution (one row per 2s = ~1.3M rows/month)
- **Aggregated:** Roll up to 5-minute averages after 30 days
- **Relay events & alerts:** Keep indefinitely (small volume)
- **Pruning:** Run daily via scheduled task or cron

---

## 4. REST API

Base URL: `http://<uno-q-ip>:5000/api/`

All responses are JSON. All timestamps are ISO 8601 (UTC).

### 4.1 Sensor Readings

**GET /api/readings**
Returns the most recent sensor reading.

```json
{
    "temperature_c": 22.3,
    "humidity_rh": 68.5,
    "pressure_hpa": 1013.2,
    "co2_ppm": 485,
    "scd_temperature_c": 22.1,
    "scd_humidity_rh": 67.8,
    "timestamp": "2026-05-16T20:30:00Z"
}
```

**GET /api/history?period=24h&resolution=5m**
Returns time-series data for charting.

Parameters:
- `period`: `1h`, `6h`, `24h`, `7d`, `30d` (default: `24h`)
- `resolution`: `raw`, `1m`, `5m`, `15m`, `1h` (default: auto based on period)

```json
{
    "period": "24h",
    "resolution": "5m",
    "data": [
        {"timestamp": "2026-05-16T20:00:00Z", "temperature_c": 22.1, "humidity_rh": 67.3, "co2_ppm": 490},
        {"timestamp": "2026-05-16T20:05:00Z", "temperature_c": 22.2, "humidity_rh": 68.1, "co2_ppm": 485}
    ]
}
```

### 4.2 Setpoints

**GET /api/setpoints**
```json
{
    "temperature_c": 22.0,
    "humidity_rh": 70.0,
    "co2_ppm": 600
}
```

**POST /api/setpoints**
```json
{
    "temperature_c": 23.0
}
```
Response: `{"success": true, "applied": {"temperature_c": 23.0}}`

Partial updates are supported — only include the fields you want to change.

### 4.3 Relay Control

**GET /api/relays**
```json
{
    "humidifier": {"state": true, "mode": "auto", "on_since": "2026-05-16T20:25:00Z"},
    "fan": {"state": false, "mode": "auto", "off_since": "2026-05-16T20:28:00Z"},
    "heater": {"state": false, "mode": "auto", "off_since": "2026-05-16T20:20:00Z"},
    "spare": {"state": false, "mode": "off", "off_since": null}
}
```

**POST /api/relays**
```json
{
    "target": "humidifier",
    "mode": "manual_on"
}
```

Modes: `auto` (PID controls), `manual_on`, `manual_off`

Response: `{"success": true}` or `{"success": false, "reason": "heater safety lockout"}`

### 4.4 PID Status

**GET /api/pid**
```json
{
    "temperature": {"setpoint": 22.0, "actual": 21.8, "output": 0.45, "mode": "auto"},
    "humidity": {"setpoint": 70.0, "actual": 68.2, "output": 0.72, "mode": "auto"},
    "co2": {"setpoint": 600, "actual": 520, "output": 0.15, "mode": "auto"}
}
```

### 4.5 System Status

**GET /api/status**
```json
{
    "mcu_connected": true,
    "mcu_uptime_s": 86400,
    "mcu_state": "running",
    "server_uptime_s": 86350,
    "db_size_mb": 42.5,
    "last_reading_age_s": 2,
    "firmware_version": "1.0.0",
    "alerts_active": 0
}
```

### 4.6 Alerts

**GET /api/alerts?active=true**
```json
{
    "alerts": [
        {"id": 12, "condition": "humidity_low", "severity": "warning", "message": "Humidity at 54% (below 55% threshold)", "timestamp": "2026-05-16T19:45:00Z", "resolved_at": null}
    ]
}
```

### 4.7 Configuration

**GET /api/config**
```json
{
    "alert_thresholds": {
        "humidity_low_warning": 55,
        "humidity_low_critical": 45,
        "temp_low_warning": 16,
        "temp_high_warning": 28,
        "co2_high_warning": 1200
    },
    "ifttt_webhook_key": "***configured***",
    "pid_tuning": {
        "temperature": {"kp": 2.0, "ki": 0.5, "kd": 1.0},
        "humidity": {"kp": 2.0, "ki": 0.5, "kd": 1.0},
        "co2": {"kp": 1.0, "ki": 0.3, "kd": 0.5}
    }
}
```

**POST /api/config**
Partial updates supported. Sensitive fields (webhook key) accepted but never returned in full.

---

## 5. Serial Daemon

### 5.1 Purpose

A background process (`serial_daemon.py`) that:
- Opens the serial port to the MCU
- Reads newline-delimited JSON messages
- Writes readings to SQLite
- Logs relay events
- Forwards commands from the Flask app to the MCU

### 5.2 Architecture Options

**Option A — Thread within Flask app:**
Simpler deployment (one process), uses a shared queue for commands.

**Option B — Separate process with IPC:**
More robust (daemon can restart independently), uses a Unix socket or Redis for message passing.

**Recommended: Option A** for initial implementation. Migrate to Option B in Phase 7 if stability requires it.

### 5.3 Command Queue

The Flask app pushes commands to a thread-safe queue. The serial daemon pops commands and writes them to the MCU serial port. Responses are matched by command type.

---

## 6. Dashboard (Frontend)

### 6.1 Design Principles

- **Mobile-first:** Primary use is checking from phone
- **Dark theme:** Matches typical monitoring dashboards
- **Real-time:** Sensor cards update every 5 seconds via polling (WebSocket upgrade in future)
- **Single-page app:** No page reloads, vanilla JS (no framework needed for this scope)

### 6.2 Dashboard Sections

1. **Header:** "Plant Cabinet" title, connection status indicator
2. **Sensor Cards:** Temperature, Humidity, CO2, Pressure — large values, colour-coded (green=good, amber=warning, red=critical)
3. **Relay Status:** Visual indicators for each relay (on/off/auto), with manual override toggles
4. **Charts:** Tabbed view — 1h / 6h / 24h / 7d — line charts for temp, humidity, CO2
5. **Setpoints:** Sliders or number inputs to adjust PID targets
6. **Alerts:** Recent alerts banner (dismissible)
7. **System:** MCU uptime, server uptime, last reading age, firmware version

### 6.3 Colour Coding

| Condition | Colour | CSS Variable |
|-----------|--------|-------------|
| Normal (in range) | Green (#10b981) | `--status-ok` |
| Warning (approaching limit) | Amber (#f59e0b) | `--status-warn` |
| Critical (out of range) | Red (#ef4444) | `--status-crit` |
| Unknown/offline | Grey (#6b7280) | `--status-unknown` |

### 6.4 Chart Library

Use **Chart.js** via CDN — lightweight, responsive, good time-series support. No build step needed.

---

## 7. Homebridge Integration

### 7.1 Approach

Run Homebridge on the Uno Q's Linux side. Use HTTP-based plugins that poll the Flask API.

### 7.2 Accessories Exposed

| Accessory | HomeKit Type | Data Source |
|-----------|-------------|-------------|
| Cabinet Temperature | TemperatureSensor | `/api/readings` → `temperature_c` |
| Cabinet Humidity | HumiditySensor | `/api/readings` → `humidity_rh` |
| Cabinet Air Quality | AirQualitySensor | `/api/readings` → `co2_ppm` mapped to 1–5 scale |
| Humidifier | Switch | `/api/relays` → humidifier |
| Fan | Switch | `/api/relays` → fan |
| Heater | Switch | `/api/relays` → heater |

### 7.3 CO2 → AirQuality Mapping

HomeKit AirQualitySensor uses a 1–5 scale:

| CO2 Range | HomeKit Value | Label |
|-----------|--------------|-------|
| < 600 ppm | 1 | Excellent |
| 600–800 ppm | 2 | Good |
| 800–1000 ppm | 3 | Fair |
| 1000–1200 ppm | 4 | Inferior |
| > 1200 ppm | 5 | Poor |

### 7.4 Homebridge Config (example)

```json
{
    "bridge": {
        "name": "Plant Cabinet",
        "port": 51826,
        "pin": "031-45-154"
    },
    "accessories": [
        {
            "accessory": "HttpTemperature",
            "name": "Cabinet Temperature",
            "url": "http://localhost:5000/api/readings",
            "http_method": "GET",
            "field_name": "temperature_c",
            "update_interval": 30000
        },
        {
            "accessory": "HttpHumidity",
            "name": "Cabinet Humidity",
            "url": "http://localhost:5000/api/readings",
            "http_method": "GET",
            "field_name": "humidity_rh",
            "update_interval": 30000
        }
    ]
}
```

### 7.5 Homebridge Plugins Required

- `homebridge-http-temperature`
- `homebridge-http-humidity`
- `homebridge-http-switch`
- `homebridge-http-air-quality` (or custom plugin)

---

## 8. IFTTT Integration

### 8.1 Setup

1. Enable IFTTT Webhooks service at `https://ifttt.com/maker_webhooks`
2. Get webhook key
3. Store in server config (environment variable or `.env` file)

### 8.2 Alert Thresholds

| Condition | Threshold | Sustained | Severity | Event Name |
|-----------|-----------|-----------|----------|------------|
| Humidity low | < 55% | 5 min | warning | `cabinet_humidity_low` |
| Humidity critical | < 45% | immediate | critical | `cabinet_humidity_critical` |
| Temp low | < 16°C | 5 min | warning | `cabinet_temp_low` |
| Temp high | > 28°C | 5 min | warning | `cabinet_temp_high` |
| CO2 high | > 1200 ppm | 5 min | warning | `cabinet_co2_high` |
| Sensor offline | no data | 2 min | critical | `cabinet_sensor_offline` |
| Heater safety | lockout triggered | immediate | critical | `cabinet_heater_lockout` |

### 8.3 Webhook Payload

```
POST https://maker.ifttt.com/trigger/{event_name}/with/key/{webhook_key}

Body:
{
    "value1": "Humidity dropped to 52%",
    "value2": "warning",
    "value3": "2026-05-16T20:30:00Z"
}
```

### 8.4 Cooldown Rules

- Maximum 1 webhook per condition per hour
- Alert clears when condition returns to normal for 5 minutes
- On clear, optionally fire a `_resolved` event (e.g., `cabinet_humidity_low_resolved`)

---

## 9. Server Configuration

### 9.1 Environment Variables

```bash
# .env file (gitignored)
FLASK_SECRET_KEY=<random-string>
IFTTT_WEBHOOK_KEY=<your-key>
MCU_SERIAL_PORT=/dev/ttyACM0    # or Bridge RPC path
MCU_BAUD_RATE=115200
DB_PATH=data/cabinet.db
```

### 9.2 File Structure

```
server/
├── app.py                  # Flask application factory and routes
├── serial_daemon.py        # MCU serial communication thread
├── models.py               # Database models and queries
├── alerts.py               # Threshold monitoring and IFTTT webhooks
├── config.py               # Configuration loading (.env)
├── requirements.txt        # Python dependencies
├── data/                   # SQLite database (gitignored)
│   └── cabinet.db
├── templates/
│   └── index.html          # Dashboard SPA
└── static/
    ├── style.css           # Dashboard styles
    └── app.js              # Dashboard JavaScript
```

---

## 10. Deployment

### 10.1 Systemd Services

**plant-cabinet-server.service:**
```ini
[Unit]
Description=Plant Cabinet Web Server
After=network.target

[Service]
Type=simple
User=arduino
WorkingDirectory=/home/arduino/plant-cabinet-controller/server
ExecStart=/usr/bin/python3 app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**homebridge.service:**
```ini
[Unit]
Description=Homebridge (Plant Cabinet HomeKit)
After=plant-cabinet-server.service

[Service]
Type=simple
User=arduino
ExecStart=/usr/bin/homebridge -U /home/arduino/.homebridge
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### 10.2 Boot Sequence

1. Linux boots (~30s)
2. `plant-cabinet-server.service` starts → opens serial, begins receiving MCU data
3. `homebridge.service` starts → connects to Flask API
4. Dashboard accessible within ~60s of power-on
