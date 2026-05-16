# Plant Cabinet Controller

Automated environment controller for a tropical plant cabinet, keeping
Alocasia, Calathea, and other humidity-loving plants happy with precise
temperature, humidity, and CO2 management.

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | MCU sensor reading + JSON serial output | Implemented |
| 2 | Relay control, heater safety, failsafe state | Implemented |
| 3 | PID loops with EEPROM persistence | Implemented |
| 4 | Flask backend, REST API, responsive dashboard | Implemented |
| 5 | Homebridge / HomeKit integration | Configured |
| 6 | IFTTT webhook alerts | Planned |
| 7 | Systemd, watchdog, log rotation, OTA | Planned |

Phases 1–5 are written but pending hardware bring-up on the Uno Q.

## Architecture

Built on the **Arduino Uno Q**, which pairs a Linux computer (Qualcomm
QRB2210) with a real-time microcontroller (STM32U585) on a single board:

- **MCU (STM32U585)** — Reads sensors, runs PID loops, drives relays, and
  enforces safety invariants. Communicates with Linux via newline-delimited
  JSON over the internal serial bridge.
- **Linux (Debian)** — Hosts the Flask dashboard, persists readings in
  SQLite, runs Homebridge for HomeKit, and (Phase 6) will fire IFTTT alerts.

## Features

- **Three PID loops** for temperature, humidity, and CO2, driving heater /
  humidifier / fan relays via time-proportional output (60s cycle, deadband
  and saturation thresholds, anti-windup).
- **Hardware safety:** per-actuator minimum cycle times (humidifier 10s,
  fan 5s, heater 60s), 10-minute heater max-on with 2-minute cooldown,
  28°C temperature ceiling, and a `FAILSAFE` state that forces
  {heater off, fan on, humidifier off} after 30s without a valid temperature
  reading.
- **EEPROM persistence** for setpoints and PID tunings — survives power
  cycles, falls back to compile-time defaults on layout change.
- **REST API** (`/api/readings`, `/api/history`, `/api/setpoints`,
  `/api/relays`, `/api/pid`, `/api/status`, `/api/alerts`, `/api/config`)
  fed by a serial daemon that bridges MCU traffic into SQLite and an
  in-memory state cache.
- **Mobile-first dashboard:** live sensor cards, three-way relay toggles,
  debounced setpoint sliders, tabbed Chart.js history (1h / 6h / 24h / 7d),
  and a dismissible alerts banner.
- **HomeKit** exposure of temperature, humidity, and three switches via
  community HTTP plugins; CO2 → AirQuality mapping helper included for
  the optional air-quality accessory.

## Hardware

| Component | Role |
|---|---|
| Arduino Uno Q | Main controller (dual-processor) |
| BME280 (I2C 0x76) | Temperature, humidity, pressure |
| SCD40 (I2C 0x62) | CO2 + temp/humidity cross-check |
| 4-channel relay board (active LOW) | Humidifier / fan / heater / spare |
| Ultrasonic humidifier | Raises cabinet humidity |
| DC fan | Air circulation, CO2 venting |
| Ceramic PTC heater | Maintains temperature |

## Target Environment

| Parameter | Setpoint | Acceptable Range |
|---|---|---|
| Temperature | 22°C | 18–25°C |
| Humidity | 70% RH | 60–80% |
| CO2 | 600 ppm | 400–1000 ppm |

## Project Structure

```
plant-cabinet-controller/
├── CLAUDE.md            # Codebase conventions
├── mcu/                 # STM32 firmware (Arduino sketch)
│   ├── include/config.h # Pins, addresses, timings, safety constants
│   ├── src/main.cpp     # State machine, sensors, relays, PID, serial
│   └── sketch.yaml      # Arduino CLI / IDE configuration
├── server/              # Flask app (Linux side)
│   ├── app.py           # Application factory + REST endpoints
│   ├── config.py        # .env loading (python-dotenv)
│   ├── models.py        # SQLite schema + query helpers
│   ├── serial_daemon.py # MCU bridge thread + command queue
│   ├── requirements.txt
│   ├── data/            # SQLite database (gitignored)
│   ├── homebridge/      # HomeKit bridge config + CO2 mapping
│   ├── static/          # Dashboard CSS + JS
│   └── templates/       # Dashboard HTML
└── docs/                # Roadmap, specs, setup guides
```

## Getting Started

- **Hardware wiring:** [docs/wiring.md](docs/wiring.md)
- **Install & run:** [docs/setup.md](docs/setup.md)
- **Apple HomeKit:** [docs/homebridge-setup.md](docs/homebridge-setup.md)
- **Architecture details:** [docs/mcu-firmware-spec.md](docs/mcu-firmware-spec.md),
  [docs/server-api-spec.md](docs/server-api-spec.md)
- **Roadmap:** [docs/roadmap.md](docs/roadmap.md)

## Development

- **MCU:** Arduino CLI — `arduino-cli compile --fqbn arduino:stm32:uno_q mcu/`.
  Required libraries: `Adafruit_BME280`, `Sensirion I2C SCD4x`, `QuickPID`.
- **Server:** Python 3.10+ — `pip install -r server/requirements.txt`,
  then `python server/app.py`. Dashboard at `http://<uno-q-ip>:5000`.
- **Dashboard:** Vanilla JS + Chart.js via CDN. No build step.

## License

See [LICENSE](LICENSE).
