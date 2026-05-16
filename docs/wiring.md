# Wiring Guide

## Overview

All sensors connect to the Arduino Uno Q via I2C (Qwiic connector or SDA/SCL pins).
Relay board connects via digital GPIO pins.

## Sensor Connections (I2C Bus)

| Sensor | Address | Connection         |
|--------|---------|-------------------|
| BME280 | 0x76   | Qwiic daisy-chain |
| SCD40  | 0x62   | Qwiic daisy-chain |

## Relay Board Connections

| Relay Channel | GPIO Pin | Controls    |
|--------------|----------|-------------|
| CH1          | D4       | Humidifier  |
| CH2          | D5       | Fan         |
| CH3          | D6       | Heater      |
| CH4          | D7       | Spare       |

## Power

- Arduino Uno Q: USB-C power (5V)
- Relay board: powered from Arduino 5V rail
- Actuators: powered independently via relay-switched mains/DC supply

## Wiring Diagram

TODO: Add fritzing/schematic diagram
