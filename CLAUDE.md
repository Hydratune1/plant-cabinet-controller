# Plant Cabinet Controller — Codebase Guide

## Project Overview

Automated tropical plant cabinet environment controller using the Arduino Uno Q (dual-processor: Linux + STM32 MCU). Controls temperature, humidity, and CO2 via PID loops driving a humidifier, fan, and heater through relays.

## Architecture

Two independent codebases communicate via Arduino Bridge RPC:

1. **`mcu/`** — Arduino C++ sketch running on STM32U585 (real-time control)
2. **`server/`** — Python Flask app running on Debian Linux (Qualcomm QRB2210)

## Development Conventions

### MCU Side (`mcu/`)
- Language: C++ (Arduino framework)
- Build tool: Arduino CLI or Arduino IDE 2.0+
- Board FQBN: `arduino:stm32:uno_q`
- Style: snake_case for variables, UPPER_CASE for constants/defines
- All hardware config (pins, addresses, defaults) lives in `include/config.h`
- Keep `loop()` non-blocking — use millis()-based timing, not delay()
- Relay logic is active LOW (HIGH = off, LOW = on)

### Server Side (`server/`)
- Language: Python 3.10+
- Framework: Flask
- Database: SQLite (single file, stored in server/data/)
- Style: PEP 8, type hints encouraged
- API routes return JSON, prefixed with `/api/`
- Templates use Jinja2, dashboard is a responsive SPA
- Dependencies pinned in `requirements.txt`

### General
- Commits: conventional commits (`feat:`, `fix:`, `docs:`, `refactor:`)
- Branches: feature branches off `main`, merged via PR
- No secrets in code — use environment variables or `.env` (gitignored)

## Key Libraries

### MCU
- `Adafruit_BME280` — temperature/humidity/pressure sensor
- `SensirionI2CScd4x` — SCD40 CO2 sensor
- `QuickPID` or `Arduino-PID-Library` — PID control
- Arduino Bridge RPC — MCU ↔ Linux communication

### Server
- `flask` — web framework
- `pyserial` — serial communication with MCU (fallback if Bridge not used)
- `simple-pid` — PID reference (primary PID runs on MCU)

## Sensor Notes

- BME280 at I2C address 0x76
- SCD40 at I2C address 0x62
- Both on shared I2C bus via Qwiic connector
- SCD40 needs 5 seconds between measurements (built-in to sensor)
- BME280 can be polled much faster but 2s intervals are sufficient

## Safety Considerations

- Heater MUST have a max-on timer (failsafe: 10 minutes continuous max)
- If sensor read fails, default to SAFE state (heater off, fan on)
- Log all relay state changes with timestamps
- PID output should be clamped to prevent rapid cycling (minimum on/off time)
