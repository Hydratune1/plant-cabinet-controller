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

1. Install Node.js and Homebridge on the Linux side:
   ```bash
   sudo apt install nodejs npm
   sudo npm install -g homebridge
   ```
2. Install the HTTP plugin:
   ```bash
   sudo npm install -g homebridge-http-temperature-sensor
   ```
3. Configure Homebridge to point at the Flask API endpoints
4. Pair with Apple Home using the setup code shown in Homebridge logs

## IFTTT

1. Create an IFTTT account and enable Webhooks
2. Set your webhook key in the server config
3. Configure applets for notifications (e.g., "Humidity below 55%")
