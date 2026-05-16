# Setup Guide

## Prerequisites

- Arduino Uno Q with USB-C cable
- Sensors: BME280 + SCD40 (Qwiic-compatible breakouts)
- 4-channel relay module
- Actuators: humidifier, fan, heater
- Qwiic cables

## MCU Firmware (STM32 side)

1. Install [PlatformIO](https://platformio.org/) in VS Code or Cursor
2. Open the `mcu/` folder as a PlatformIO project
3. Connect the Uno Q via USB-C
4. Build and upload:
   ```
   pio run --target upload
   ```

## Web Server (Linux side)

1. SSH into the Uno Q's Linux side (or connect via USB-C terminal)
2. Navigate to the server directory:
   ```bash
   cd /home/arduino/plant-cabinet-controller/server
   ```
3. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Run the server:
   ```bash
   python app.py
   ```
5. Access the dashboard at `http://<uno-q-ip>:5000`

## Homebridge (HomeKit)

See [`homebridge-setup.md`](homebridge-setup.md) for the full step-by-step:
Node.js install, plugin install, config deployment, pairing, and the systemd
unit. The ready-to-use Homebridge config lives at
`server/homebridge/config.json`, and `server/homebridge/co2_quality.py`
holds the CO2 -> HomeKit AirQuality (1-5) mapping for the optional air-quality
accessory.

No new Python dependencies are required — `co2_quality.py` only uses the
standard library (`urllib`, `argparse`, `json`).

## IFTTT

1. Create an IFTTT account and enable Webhooks
2. Set your webhook key in the server config
3. Configure applets for notifications (e.g., "Humidity below 55%")
