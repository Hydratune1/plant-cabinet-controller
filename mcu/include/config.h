/**
 * Plant Cabinet Controller — Configuration
 *
 * Hardware pin assignments and default setpoints.
 */

#ifndef CONFIG_H
#define CONFIG_H

// --- I2C Sensor Addresses ---
#define BME280_ADDR 0x76
#define SCD40_ADDR  0x62

// --- Relay Pin Assignments (UNO digital pins) ---
#define RELAY_HUMIDIFIER 4
#define RELAY_FAN        5
#define RELAY_HEATER     6
#define RELAY_SPARE      7

// --- Default PID Setpoints ---
#define SETPOINT_TEMP_C      22.0f   // Target temperature (°C)
#define SETPOINT_HUMIDITY_RH 70.0f   // Target relative humidity (%)
#define SETPOINT_CO2_PPM     600.0f  // Target CO2 (ppm)

// --- PID Setpoint Acceptable Ranges (spec §5.5) ---
#define TEMP_SP_MIN   18.0f
#define TEMP_SP_MAX   25.0f
#define HUM_SP_MIN    60.0f
#define HUM_SP_MAX    80.0f
#define CO2_SP_MIN   400.0f
#define CO2_SP_MAX  1000.0f

// --- PID Tuning (initial values — will need calibration) ---
#define PID_TEMP_KP 2.0f
#define PID_TEMP_KI 0.5f
#define PID_TEMP_KD 1.0f

#define PID_HUM_KP 2.0f
#define PID_HUM_KI 0.5f
#define PID_HUM_KD 1.0f

#define PID_CO2_KP 1.0f
#define PID_CO2_KI 0.3f
#define PID_CO2_KD 0.5f

// --- PID Sample Intervals (QuickPID compute period, microseconds) ---
#define PID_SAMPLE_TEMP_US  2000000UL   // 2 s — matches BME read rate
#define PID_SAMPLE_HUM_US   2000000UL   // 2 s
#define PID_SAMPLE_CO2_US   5000000UL   // 5 s — matches SCD measurement rate

// --- PID Time-Proportional Control (spec §5.2) ---
#define PID_CONTROL_CYCLE_MS    60000UL   // Time-proportional output window
#define PID_OUTPUT_DEADBAND_LOW  0.05f    // Below: always OFF
#define PID_OUTPUT_DEADBAND_HIGH 0.95f    // Above: always ON

// --- Fan Baseline Duty (continuous air circulation) ---
#define FAN_BASELINE_DUTY    0.20f

// --- Sensor Read Intervals ---
#define BME280_READ_INTERVAL_MS  2000   // BME280 polls fast; 2s is plenty
#define SCD40_READ_INTERVAL_MS   5000   // SCD40 produces a measurement every 5s

// --- Reporting Intervals ---
#define READING_EMIT_INTERVAL_MS  2000  // Cadence of "reading" JSON messages
#define SERIAL_REPORT_INTERVAL_MS 5000  // Cadence of PID status reports (Phase 3)

// --- Fault Tolerance ---
#define SENSOR_FAILURE_THRESHOLD 3      // Consecutive failures before SENSOR_FAULT

// --- Relay Minimum Cycle Times (ms) ---
#define HUMIDIFIER_MIN_ON_MS   10000UL
#define HUMIDIFIER_MIN_OFF_MS  10000UL
#define FAN_MIN_ON_MS           5000UL
#define FAN_MIN_OFF_MS          5000UL
#define HEATER_MIN_ON_MS       60000UL
#define HEATER_MIN_OFF_MS      60000UL
#define SPARE_MIN_ON_MS        10000UL
#define SPARE_MIN_OFF_MS       10000UL

// --- Heater Safety ---
#define HEATER_MAX_ON_MS       600000UL   // 10 min hard ceiling on continuous on-time
#define HEATER_COOLDOWN_MS     120000UL   // 2 min forced off after a max-on trip
#define HEATER_TEMP_CEILING_C   28.0f     // °C — heater cuts out regardless of PID

// --- Failsafe ---
#define FAILSAFE_NO_TEMP_MS    30000UL    // No valid temp reading for 30s -> failsafe

// --- Serial RX ---
#define RX_LINE_BUFFER_SIZE    128        // Max length of a single JSON command line

// --- EEPROM Persistence ---
#define EEPROM_BASE_ADDR        0
#define EEPROM_RESERVED_BYTES   64        // PersistedConfig fits in 52 bytes; pad for headroom
#define EEPROM_MAGIC            0xCAB13201UL  // Bump on PersistedConfig layout change

// --- Firmware Metadata ---
#define FIRMWARE_VERSION "0.3.0"

#endif // CONFIG_H
