# MCU Firmware Specification

Technical specification for the STM32U585 Arduino sketch running on the Uno Q.

---

## 1. Overview

The MCU firmware is responsible for all real-time control:
- Reading environmental sensors at fixed intervals
- Running PID control loops to maintain setpoints
- Driving relays to control actuators
- Communicating with the Linux side via serial (Bridge RPC)
- Enforcing safety invariants at all times

The firmware must be **non-blocking** — no `delay()` calls in production code. All timing uses `millis()`.

---

## 2. Hardware Interface

### 2.1 I2C Bus (Qwiic)

| Sensor | Address | Library | Read Interval |
|--------|---------|---------|---------------|
| BME280 | 0x76 | `Adafruit_BME280` | 2000 ms |
| SCD40 | 0x62 | `SensirionI2CScd4x` | 5000 ms |

Both sensors share the I2C bus. The SCD40 has a built-in 5-second measurement cycle — do not poll faster.

### 2.2 Relay Outputs (Active LOW)

| Pin | Channel | Actuator | Notes |
|-----|---------|----------|-------|
| D4 | CH1 | Humidifier | Ultrasonic, safe to cycle |
| D5 | CH2 | Fan | DC fan, safe to cycle |
| D6 | CH3 | Heater | Ceramic — 10 min max continuous |
| D7 | CH4 | Spare | Reserved for grow light or pump |

**Active LOW** means:
- `digitalWrite(pin, LOW)` → relay ON (closed, actuator powered)
- `digitalWrite(pin, HIGH)` → relay OFF (open, actuator unpowered)

### 2.3 LED Matrix (8x13)

The onboard LED matrix can be used for status indication:
- Startup animation → boot complete
- Steady pattern → normal operation
- Blinking → sensor error or safety fault
- Scrolling text → IP address on first connect

---

## 3. Sensor Reading

### 3.1 BME280

```cpp
struct BME280Reading {
    float temperature_c;    // °C, resolution 0.01
    float humidity_rh;      // %, resolution 0.01
    float pressure_hpa;     // hPa, resolution 0.01
    bool valid;             // false if read failed
    unsigned long timestamp_ms;
};
```

**Error handling:** If `bme.readTemperature()` returns NaN or the I2C bus fails, mark the reading as invalid. After 3 consecutive failures, trigger the sensor fault state.

### 3.2 SCD40

```cpp
struct SCD40Reading {
    uint16_t co2_ppm;       // ppm (400–5000 typical)
    float temperature_c;    // °C (lower accuracy than BME280)
    float humidity_rh;      // % (lower accuracy than BME280)
    bool valid;
    unsigned long timestamp_ms;
};
```

**Startup:** Call `scd4x.startPeriodicMeasurement()` in `setup()`. The sensor needs ~30 seconds to produce reliable readings after power-on.

**Error handling:** Same as BME280 — 3 consecutive failures trigger sensor fault.

### 3.3 Sensor Fusion

Use BME280 as the primary source for temperature and humidity (higher accuracy). Use SCD40 temp/humidity as a sanity check:
- If BME280 and SCD40 disagree by >5°C or >15% RH, log a warning (possible sensor drift or placement issue)
- CO2 only comes from the SCD40

---

## 4. Relay Management

### 4.1 State Machine

Each relay has an independent state:

```cpp
struct RelayState {
    bool requested;          // what PID/user wants
    bool actual;             // current hardware state
    unsigned long last_on;   // millis() when last turned on
    unsigned long last_off;  // millis() when last turned off
    unsigned long total_on;  // cumulative on-time since boot (ms)
};
```

### 4.2 Minimum Cycle Time

To protect actuators from rapid cycling:

| Actuator | Min ON time | Min OFF time |
|----------|-------------|--------------|
| Humidifier | 10 s | 10 s |
| Fan | 5 s | 5 s |
| Heater | 60 s | 60 s |

If a PID output requests a state change before the minimum time has elapsed, the change is **deferred** until the minimum time passes.

### 4.3 Heater Safety

The heater is the most dangerous actuator. Additional rules:
1. **Max continuous on-time:** 10 minutes. After 10 min, force OFF for at least 2 minutes.
2. **Max duty cycle:** 70% over any rolling 30-minute window.
3. **Temperature ceiling:** If temperature exceeds 28°C, heater OFF regardless of PID.
4. **Sensor fault:** If no valid temperature reading for 30 seconds, heater OFF immediately.

### 4.4 Failsafe State

Triggered when sensors are unavailable:

| Actuator | Failsafe State | Reason |
|----------|---------------|--------|
| Heater | OFF | Prevent overheating |
| Fan | ON | Prevent stagnation and heat buildup |
| Humidifier | OFF | Prevent over-humidification |

---

## 5. PID Control

### 5.1 Control Loops

Three independent PID loops running at the sensor read rate:

| Loop | Input | Output | Actuator | Direction |
|------|-------|--------|----------|-----------|
| Temperature | BME280 temp °C | 0.0–1.0 | Heater | Direct (higher output = more heating) |
| Humidity | BME280 humidity % | 0.0–1.0 | Humidifier | Direct (higher output = more mist) |
| CO2/Ventilation | SCD40 CO2 ppm | 0.0–1.0 | Fan | Reverse (higher CO2 = more fan) |

### 5.2 Output Interpretation

PID output (0.0–1.0) maps to relay on/off using **time-proportional control**:
- Each control cycle = 60 seconds (configurable)
- Output 0.7 → relay ON for 42s, OFF for 18s within that cycle
- Output below 0.05 → always OFF (deadband)
- Output above 0.95 → always ON

This provides proportional control using simple on/off relays.

### 5.3 Default Tuning Parameters

Starting conservative — these need real-world calibration:

| Loop | Kp | Ki | Kd | Notes |
|------|----|----|-----|-------|
| Temperature | 2.0 | 0.5 | 1.0 | Slow thermal mass, careful with integral |
| Humidity | 2.0 | 0.5 | 1.0 | Humidifier effect is fast |
| CO2 | 1.0 | 0.3 | 0.5 | Fan response is immediate |

### 5.4 Anti-Windup

Use integral clamping to prevent windup:
- Clamp integral term when output is saturated (at 0 or 1)
- Reset integral on setpoint change to prevent overshoot

### 5.5 Setpoints (Defaults)

| Parameter | Setpoint | Acceptable Range |
|-----------|----------|-----------------|
| Temperature | 22.0°C | 18–25°C |
| Humidity | 70.0% RH | 60–80% |
| CO2 | 600 ppm | 400–1000 ppm |

Setpoints are adjustable at runtime via serial commands. Persist last-known setpoints in EEPROM/flash so they survive power cycles.

---

## 6. Serial Communication Protocol

### 6.1 Physical Layer

- UART over internal Bridge connection (MCU ↔ Linux)
- Baud rate: 115200
- Format: newline-delimited JSON (`\n` terminated)

### 6.2 MCU → Linux Messages

**Sensor Reading (every 2s):**
```json
{"type":"reading","ts":123456,"temp_c":22.3,"hum_rh":68.5,"pres_hpa":1013.2,"co2_ppm":485,"scd_temp_c":22.1,"scd_hum_rh":67.8}
```

**PID Status (every 5s):**
```json
{"type":"pid","ts":123456,"temp":{"sp":22.0,"pv":21.8,"out":0.45},"hum":{"sp":70.0,"pv":68.2,"out":0.72},"co2":{"sp":600,"pv":520,"out":0.15}}
```

**Relay State Change (on every change):**
```json
{"type":"relay","ts":123456,"humidifier":true,"fan":false,"heater":false,"spare":false}
```

**Error/Warning:**
```json
{"type":"error","ts":123456,"code":"SENSOR_FAULT","msg":"BME280 read failed 3 consecutive times"}
```

**Boot/Status:**
```json
{"type":"status","ts":0,"state":"running","uptime_s":3600,"version":"1.0.0"}
```

### 6.3 Linux → MCU Commands

**Set Relay (manual override):**
```json
{"cmd":"relay","target":"humidifier","state":true}
```

**Set PID Setpoint:**
```json
{"cmd":"setpoint","param":"temperature","value":23.0}
```

**Set PID Mode:**
```json
{"cmd":"pid_mode","mode":"auto"}
{"cmd":"pid_mode","mode":"manual"}
```

**Request Status:**
```json
{"cmd":"status"}
```

**Set PID Tuning (advanced):**
```json
{"cmd":"pid_tune","loop":"temperature","kp":2.5,"ki":0.6,"kd":1.2}
```

### 6.4 Acknowledgement

Every command from Linux receives an ack:
```json
{"type":"ack","cmd":"setpoint","success":true}
{"type":"ack","cmd":"relay","success":false,"reason":"heater safety lockout active"}
```

---

## 7. Timing Budget

All operations must complete within the main loop cycle (2000ms):

| Operation | Budget | Frequency |
|-----------|--------|-----------|
| BME280 read | ~10 ms | Every 2s |
| SCD40 read | ~5 ms | Every 5s |
| PID compute (x3) | ~1 ms | Every 2s |
| Relay update | ~1 ms | Every cycle |
| Serial TX | ~5 ms | Every 2–5s |
| Serial RX parse | ~2 ms | On receive |
| Safety checks | ~1 ms | Every cycle |
| **Total worst case** | **~25 ms** | — |

Plenty of headroom. The loop should complete in well under 50ms.

---

## 8. State Machine Overview

```
BOOT → SENSOR_INIT → WARMUP (30s for SCD40) → RUNNING
                                                  ↓
                                            SENSOR_FAULT → FAILSAFE
                                                  ↑
                                            (sensors recover)
```

- **BOOT:** Initialise pins, serial, I2C
- **SENSOR_INIT:** Scan I2C bus, initialise BME280 and SCD40
- **WARMUP:** Wait for SCD40 first valid reading (~30s)
- **RUNNING:** Normal operation with PID control
- **SENSOR_FAULT:** One or more sensors offline, log warnings
- **FAILSAFE:** No valid readings for 30s, safe relay states enforced

---

## 9. Memory & Storage

- **RAM:** 786 KB available — more than sufficient
- **Flash:** 2 MB — ample for firmware
- **Persistent storage:** Use EEPROM emulation for:
  - Current setpoints (12 bytes)
  - PID tuning parameters (36 bytes)
  - Relay state preferences (4 bytes)
  - Boot counter (4 bytes)

---

## 10. Future Considerations

- **OTA updates:** Linux side can compile and flash MCU via Bridge
- **Grow light control:** Use spare relay (CH4) with timer/schedule
- **Multi-zone:** Support additional sensor nodes via I2C multiplexer
- **Data buffering:** If Linux side is offline, buffer readings in MCU RAM (circular buffer, ~1000 readings)
