# Plant Cabinet Controller

Automated environment controller for a tropical plant cabinet, keeping Alocasia, Calathea, and other humidity-loving plants happy with precise temperature, humidity, and CO2 management.

## Architecture

Built on the **Arduino Uno Q**, which pairs a Linux computer (Qualcomm QRB2210) with a real-time microcontroller (STM32U585) on a single board:

- **MCU (STM32U585)** — Reads sensors, runs PID control loops, drives relays
- **Linux (Debian)** — Hosts web dashboard, logs data, runs Homebridge for Apple HomeKit

## Hardware

| Component | Role |
|-----------|------|
| Arduino Uno Q | Main controller (dual-processor) |
| BME280 | Temperature, humidity, pressure sensor (I2C) |
| SCD40 | CO2 sensor with temp/humidity cross-check (I2C) |
| 4-channel relay board | Switches actuators on/off |
| Ultrasonic humidifier | Raises cabinet humidity |
| Fan | Air circulation |
| Ceramic heater | Maintains temperature |

## Software Stack

- **MCU firmware** (C++/Arduino) — Sensor polling, PID control, relay switching
- **Web server** (Python/Flask) — Dashboard, REST API, data logging (SQLite)
- **Homebridge** (Node.js) — Apple HomeKit integration
- **IFTTT webhooks** — Phone notifications and smart home triggers

## Target Environment

| Parameter | Setpoint | Range |
|-----------|----------|-------|
| Temperature | 22°C | 18–25°C |
| Humidity | 70% RH | 60–80% |
| CO2 | 600 ppm | 400–1000 ppm |

## Project Structure

```
plant-cabinet-controller/
├── mcu/                 # Arduino sketch for STM32 MCU
│   ├── src/main.cpp     # Main firmware
│   ├── include/config.h # Pin assignments and PID defaults
│   └── sketch.yaml      # Arduino CLI configuration
├── server/              # Flask web application (Linux side)
│   ├── app.py           # Main server
│   ├── requirements.txt # Python dependencies
│   ├── templates/       # HTML dashboard
│   └── static/          # CSS, JS, assets
├── docs/                # Documentation
│   ├── wiring.md        # Wiring guide
│   └── setup.md         # Installation and setup
└── README.md
```

## Getting Started

See [docs/setup.md](docs/setup.md) for full installation instructions.

## Development

- **MCU side:** Arduino IDE 2.0+, Arduino CLI, or Arduino App Lab
- **Linux side:** Python 3.x with Flask, developed via SSH or App Lab
- **IDE:** Cursor (recommended for collaborative development)

## License

See [LICENSE](LICENSE) file.
