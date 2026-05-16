/**
 * Plant Cabinet Controller — MCU Firmware
 *
 * Runs on the Arduino Uno Q's STM32U585 microcontroller.
 *
 * Phase 1: BME280 + SCD40 reading, newline-delimited JSON over Serial.
 * Phase 2: Active-LOW relay driver with minimum cycle times, heater safety
 *          (max-on + cooldown + temp ceiling), failsafe state, JSON commands.
 * Phase 3: QuickPID control loops (temperature, humidity, CO2) with
 *          time-proportional output, anti-windup, fan baseline circulation,
 *          setpoint / tune / mode commands, and EEPROM persistence.
 *
 * State machine:
 *   BOOT -> SENSOR_INIT -> WARMUP -> RUNNING
 *                                       |
 *                                       v
 *                                 SENSOR_FAULT -> FAILSAFE
 *                                       ^             |
 *                                       +-------------+ (sensors recover)
 *
 * Required Arduino libraries:
 *   - Adafruit_BME280 (+ Adafruit_BusIO, Adafruit_Unified_Sensor)
 *   - Sensirion I2C SCD4x (+ Sensirion Core)
 *   - QuickPID (Dlloydev, version 3.x)
 *
 * Build:  arduino-cli compile --fqbn arduino:stm32:uno_q mcu/
 * Upload: arduino-cli upload  --fqbn arduino:stm32:uno_q -p <port> mcu/
 */

#include <Arduino.h>
#include <Wire.h>
#include <math.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <ctype.h>
#include <EEPROM.h>
#include <Adafruit_BME280.h>
#include <SensirionI2CScd4x.h>
#include <QuickPID.h>

#include "config.h"

// --- Type definitions ---

enum SystemState {
    STATE_BOOT,
    STATE_SENSOR_INIT,
    STATE_WARMUP,
    STATE_RUNNING,
    STATE_SENSOR_FAULT,
    STATE_FAILSAFE
};

enum PIDMode { PID_MODE_AUTO, PID_MODE_MANUAL };

struct BME280Reading {
    float temperature_c;
    float humidity_rh;
    float pressure_hpa;
    bool valid;
    unsigned long timestamp_ms;
};

struct SCD40Reading {
    uint16_t co2_ppm;
    float temperature_c;
    float humidity_rh;
    bool valid;
    unsigned long timestamp_ms;
};

enum RelayChannel {
    RELAY_CH_HUMIDIFIER = 0,
    RELAY_CH_FAN,
    RELAY_CH_HEATER,
    RELAY_CH_SPARE,
    RELAY_COUNT
};

struct RelayState {
    bool requested;
    bool actual;
    unsigned long last_on;
    unsigned long last_off;
    unsigned long total_on;
};

enum PIDLoopIndex {
    PID_LOOP_TEMP = 0,
    PID_LOOP_HUM,
    PID_LOOP_CO2,
    PID_LOOP_COUNT
};

struct PIDLoopInfo {
    QuickPID* pid;
    float* setpoint;
    float* input;
    float* output;
    float* kp;
    float* ki;
    float* kd;
    uint8_t relay_channel;
    float sp_min;
    float sp_max;
    const char* name;       // "temperature" | "humidity" | "co2"
    const char* json_key;   // "temp" | "hum" | "co2"
    bool is_fan;            // apply FAN_BASELINE_DUTY when locking output
    unsigned long cycle_start_ms;
    float locked_output;
    bool cycle_active;
};

struct PersistedConfig {
    uint32_t magic;
    float temp_sp, hum_sp, co2_sp;
    float temp_kp, temp_ki, temp_kd;
    float hum_kp,  hum_ki,  hum_kd;
    float co2_kp,  co2_ki,  co2_kd;
};

// --- Module state ---

static Adafruit_BME280 bme;
static SensirionI2CScd4x scd4x;

static SystemState system_state = STATE_BOOT;
static unsigned long state_entered_ms = 0;

static BME280Reading bme_reading = {0.0f, 0.0f, 0.0f, false, 0};
static SCD40Reading  scd_reading = {0,    0.0f, 0.0f, false, 0};

static uint8_t bme_consecutive_failures = 0;
static uint8_t scd_consecutive_failures = 0;

static unsigned long last_bme_read_ms     = 0;
static unsigned long last_scd_read_ms     = 0;
static unsigned long last_reading_emit_ms = 0;
static unsigned long last_valid_temp_ms   = 0;
static unsigned long last_pid_emit_ms     = 0;

// Per-channel relay metadata, indexed by RelayChannel
static const uint8_t relay_pins[RELAY_COUNT] = {
    RELAY_HUMIDIFIER, RELAY_FAN, RELAY_HEATER, RELAY_SPARE
};
static const char* const relay_names[RELAY_COUNT] = {
    "humidifier", "fan", "heater", "spare"
};
static const unsigned long relay_min_on_ms[RELAY_COUNT] = {
    HUMIDIFIER_MIN_ON_MS, FAN_MIN_ON_MS, HEATER_MIN_ON_MS, SPARE_MIN_ON_MS
};
static const unsigned long relay_min_off_ms[RELAY_COUNT] = {
    HUMIDIFIER_MIN_OFF_MS, FAN_MIN_OFF_MS, HEATER_MIN_OFF_MS, SPARE_MIN_OFF_MS
};

static RelayState relays[RELAY_COUNT];
static unsigned long heater_cooldown_until_ms = 0;

// PID variables declared at file scope so QuickPID constructors can store
// stable pointers to them. EEPROM may overwrite during setup().
static float sp_temp = SETPOINT_TEMP_C;
static float sp_hum  = SETPOINT_HUMIDITY_RH;
static float sp_co2  = SETPOINT_CO2_PPM;
static float pv_temp = 0.0f, pv_hum = 0.0f, pv_co2 = 0.0f;
static float out_temp = 0.0f, out_hum = 0.0f, out_co2 = 0.0f;
static float kp_temp = PID_TEMP_KP, ki_temp = PID_TEMP_KI, kd_temp = PID_TEMP_KD;
static float kp_hum  = PID_HUM_KP,  ki_hum  = PID_HUM_KI,  kd_hum  = PID_HUM_KD;
static float kp_co2  = PID_CO2_KP,  ki_co2  = PID_CO2_KI,  kd_co2  = PID_CO2_KD;

static QuickPID pid_temp(&pv_temp, &out_temp, &sp_temp);
static QuickPID pid_hum (&pv_hum,  &out_hum,  &sp_hum);
static QuickPID pid_co2 (&pv_co2,  &out_co2,  &sp_co2);

static PIDLoopInfo loops[PID_LOOP_COUNT];
static PIDMode pid_mode = PID_MODE_AUTO;

// Serial RX line buffer
static char rx_buffer[RX_LINE_BUFFER_SIZE];
static size_t rx_len = 0;

// --- EEPROM portability helpers (SFINAE-driven) ---

// commit() exists on STM32/ESP flash-EEPROM cores; AVR auto-commits.
template<typename T>
static auto eeprom_commit_impl(T& e, int) -> decltype(e.commit(), void()) {
    e.commit();
}
template<typename T>
static void eeprom_commit_impl(T&, long) {}

static void eeprom_flush() { eeprom_commit_impl(EEPROM, 0); }

// begin(size) on ESP, begin() on STM32, neither on AVR.
template<typename T>
static auto eeprom_begin_impl(T& e, int)
    -> decltype(e.begin((size_t)EEPROM_RESERVED_BYTES), void()) {
    e.begin((size_t)EEPROM_RESERVED_BYTES);
}
template<typename T>
static auto eeprom_begin_impl(T& e, long) -> decltype(e.begin(), void()) {
    e.begin();
}
template<typename T>
static void eeprom_begin_impl(T&, ...) {}

static void eeprom_init() { eeprom_begin_impl(EEPROM, 0); }

// --- Persisted-config load / save ---

static void load_persisted() {
    PersistedConfig cfg;
    EEPROM.get(EEPROM_BASE_ADDR, cfg);
    if (cfg.magic != EEPROM_MAGIC) return;  // first boot or layout change

    if (cfg.temp_sp >= TEMP_SP_MIN && cfg.temp_sp <= TEMP_SP_MAX) sp_temp = cfg.temp_sp;
    if (cfg.hum_sp  >= HUM_SP_MIN  && cfg.hum_sp  <= HUM_SP_MAX)  sp_hum  = cfg.hum_sp;
    if (cfg.co2_sp  >= CO2_SP_MIN  && cfg.co2_sp  <= CO2_SP_MAX)  sp_co2  = cfg.co2_sp;

    if (cfg.temp_kp >= 0.0f) { kp_temp = cfg.temp_kp; ki_temp = cfg.temp_ki; kd_temp = cfg.temp_kd; }
    if (cfg.hum_kp  >= 0.0f) { kp_hum  = cfg.hum_kp;  ki_hum  = cfg.hum_ki;  kd_hum  = cfg.hum_kd; }
    if (cfg.co2_kp  >= 0.0f) { kp_co2  = cfg.co2_kp;  ki_co2  = cfg.co2_ki;  kd_co2  = cfg.co2_kd; }
}

static void save_persisted() {
    PersistedConfig cfg;
    cfg.magic = EEPROM_MAGIC;
    cfg.temp_sp = sp_temp;
    cfg.hum_sp  = sp_hum;
    cfg.co2_sp  = sp_co2;
    cfg.temp_kp = kp_temp; cfg.temp_ki = ki_temp; cfg.temp_kd = kd_temp;
    cfg.hum_kp  = kp_hum;  cfg.hum_ki  = ki_hum;  cfg.hum_kd  = kd_hum;
    cfg.co2_kp  = kp_co2;  cfg.co2_ki  = ki_co2;  cfg.co2_kd  = kd_co2;
    EEPROM.put(EEPROM_BASE_ADDR, cfg);
    eeprom_flush();
}

// --- Emission helpers (F() keeps literals in flash) ---

static const char* state_name(SystemState s) {
    switch (s) {
        case STATE_BOOT:         return "boot";
        case STATE_SENSOR_INIT:  return "sensor_init";
        case STATE_WARMUP:       return "warmup";
        case STATE_RUNNING:      return "running";
        case STATE_SENSOR_FAULT: return "sensor_fault";
        case STATE_FAILSAFE:     return "failsafe";
        default:                 return "unknown";
    }
}

static void emit_status() {
    Serial.print(F("{\"type\":\"status\",\"ts\":"));
    Serial.print(millis());
    Serial.print(F(",\"state\":\""));
    Serial.print(state_name(system_state));
    Serial.print(F("\",\"uptime_s\":"));
    Serial.print(millis() / 1000UL);
    Serial.print(F(",\"pid_mode\":\""));
    Serial.print(pid_mode == PID_MODE_AUTO ? F("auto") : F("manual"));
    Serial.print(F("\",\"version\":\""));
    Serial.print(F(FIRMWARE_VERSION));
    Serial.println(F("\"}"));
}

static void emit_error(const char* code, const char* msg) {
    Serial.print(F("{\"type\":\"error\",\"ts\":"));
    Serial.print(millis());
    Serial.print(F(",\"code\":\""));
    Serial.print(code);
    Serial.print(F("\",\"msg\":\""));
    Serial.print(msg);
    Serial.println(F("\"}"));
}

static void emit_float_or_null(float v, bool valid, uint8_t decimals) {
    if (valid && !isnan(v)) {
        Serial.print(v, decimals);
    } else {
        Serial.print(F("null"));
    }
}

static void emit_uint_or_null(uint16_t v, bool valid) {
    if (valid) {
        Serial.print(v);
    } else {
        Serial.print(F("null"));
    }
}

static void emit_reading() {
    Serial.print(F("{\"type\":\"reading\",\"ts\":"));
    Serial.print(millis());
    Serial.print(F(",\"temp_c\":"));
    emit_float_or_null(bme_reading.temperature_c, bme_reading.valid, 2);
    Serial.print(F(",\"hum_rh\":"));
    emit_float_or_null(bme_reading.humidity_rh, bme_reading.valid, 2);
    Serial.print(F(",\"pres_hpa\":"));
    emit_float_or_null(bme_reading.pressure_hpa, bme_reading.valid, 2);
    Serial.print(F(",\"co2_ppm\":"));
    emit_uint_or_null(scd_reading.co2_ppm, scd_reading.valid);
    Serial.print(F(",\"scd_temp_c\":"));
    emit_float_or_null(scd_reading.temperature_c, scd_reading.valid, 2);
    Serial.print(F(",\"scd_hum_rh\":"));
    emit_float_or_null(scd_reading.humidity_rh, scd_reading.valid, 2);
    Serial.println(F("}"));
}

static void emit_relay_state(unsigned long now) {
    Serial.print(F("{\"type\":\"relay\",\"ts\":"));
    Serial.print(now);
    Serial.print(F(",\"humidifier\":"));
    Serial.print(relays[RELAY_CH_HUMIDIFIER].actual ? F("true") : F("false"));
    Serial.print(F(",\"fan\":"));
    Serial.print(relays[RELAY_CH_FAN].actual ? F("true") : F("false"));
    Serial.print(F(",\"heater\":"));
    Serial.print(relays[RELAY_CH_HEATER].actual ? F("true") : F("false"));
    Serial.print(F(",\"spare\":"));
    Serial.print(relays[RELAY_CH_SPARE].actual ? F("true") : F("false"));
    Serial.println(F("}"));
}

static void emit_ack(const char* cmd, bool success, const char* reason) {
    Serial.print(F("{\"type\":\"ack\",\"cmd\":\""));
    Serial.print(cmd ? cmd : "");
    Serial.print(F("\",\"success\":"));
    Serial.print(success ? F("true") : F("false"));
    if (!success && reason) {
        Serial.print(F(",\"reason\":\""));
        Serial.print(reason);
        Serial.print(F("\""));
    }
    Serial.println(F("}"));
}

static void emit_pid_loop_field(const char* key, float sp, float pv, float out, uint8_t pv_decimals) {
    Serial.print(F("\""));
    Serial.print(key);
    Serial.print(F("\":{\"sp\":"));
    Serial.print(sp, pv_decimals);
    Serial.print(F(",\"pv\":"));
    Serial.print(pv, pv_decimals);
    Serial.print(F(",\"out\":"));
    Serial.print(out, 2);
    Serial.print(F("}"));
}

static void emit_pid_status() {
    Serial.print(F("{\"type\":\"pid\",\"ts\":"));
    Serial.print(millis());
    Serial.print(F(","));
    emit_pid_loop_field("temp", sp_temp, pv_temp, out_temp, 1);
    Serial.print(F(","));
    emit_pid_loop_field("hum",  sp_hum,  pv_hum,  out_hum,  1);
    Serial.print(F(","));
    emit_pid_loop_field("co2",  sp_co2,  pv_co2,  out_co2,  0);
    Serial.println(F("}"));
}

// --- PID lifecycle ---

static void pid_init_on_running() {
    if (bme_reading.valid) {
        pv_temp = bme_reading.temperature_c;
        pv_hum  = bme_reading.humidity_rh;
    }
    if (scd_reading.valid) {
        pv_co2  = (float)scd_reading.co2_ppm;
    }
    // Bumpless restart — reset integrator anchor to current output/input.
    pid_temp.Initialize();
    pid_hum.Initialize();
    pid_co2.Initialize();
    for (uint8_t i = 0; i < PID_LOOP_COUNT; i++) {
        loops[i].cycle_active = false;
    }
    last_pid_emit_ms = 0;
}

static void transition_to(SystemState next) {
    const bool entering_running = (next == STATE_RUNNING && system_state != STATE_RUNNING);
    system_state = next;
    state_entered_ms = millis();
    emit_status();
    if (entering_running) {
        pid_init_on_running();
    }
}

// --- Sensor I/O ---

static bool read_bme(unsigned long now) {
    const float t = bme.readTemperature();
    const float h = bme.readHumidity();
    const float p = bme.readPressure() / 100.0f;  // Pa -> hPa

    if (isnan(t) || isnan(h) || isnan(p)) {
        bme_reading.valid = false;
        bme_consecutive_failures++;
        return false;
    }

    bme_reading.temperature_c = t;
    bme_reading.humidity_rh   = h;
    bme_reading.pressure_hpa  = p;
    bme_reading.valid         = true;
    bme_reading.timestamp_ms  = now;
    bme_consecutive_failures  = 0;
    last_valid_temp_ms        = now;
    return true;
}

static bool read_scd(unsigned long now) {
    bool data_ready = false;
    uint16_t err = scd4x.getDataReadyFlag(data_ready);
    if (err != 0) {
        scd_reading.valid = false;
        scd_consecutive_failures++;
        return false;
    }
    if (!data_ready) return false;  // normal between cycles

    uint16_t co2 = 0;
    float t = 0.0f, h = 0.0f;
    err = scd4x.readMeasurement(co2, t, h);
    if (err != 0) {
        scd_reading.valid = false;
        scd_consecutive_failures++;
        return false;
    }
    // Datasheet: co2 == 0 means the sample is still warming up.
    if (co2 == 0) return false;

    scd_reading.co2_ppm       = co2;
    scd_reading.temperature_c = t;
    scd_reading.humidity_rh   = h;
    scd_reading.valid         = true;
    scd_reading.timestamp_ms  = now;
    scd_consecutive_failures  = 0;
    return true;
}

static void tick_bme(unsigned long now) {
    if (now - last_bme_read_ms >= BME280_READ_INTERVAL_MS) {
        last_bme_read_ms = now;
        read_bme(now);
    }
}

static void tick_scd(unsigned long now) {
    if (now - last_scd_read_ms >= SCD40_READ_INTERVAL_MS) {
        last_scd_read_ms = now;
        read_scd(now);
    }
}

static void tick_emit(unsigned long now) {
    if (now - last_reading_emit_ms >= READING_EMIT_INTERVAL_MS) {
        last_reading_emit_ms = now;
        emit_reading();
    }
}

static void tick_pid_emit(unsigned long now) {
    if (now - last_pid_emit_ms >= SERIAL_REPORT_INTERVAL_MS) {
        last_pid_emit_ms = now;
        emit_pid_status();
    }
}

static bool check_sensor_fault() {
    if (bme_consecutive_failures >= SENSOR_FAILURE_THRESHOLD) {
        emit_error("SENSOR_FAULT", "BME280 read failed 3 consecutive times");
        transition_to(STATE_SENSOR_FAULT);
        return true;
    }
    if (scd_consecutive_failures >= SENSOR_FAILURE_THRESHOLD) {
        emit_error("SENSOR_FAULT", "SCD40 read failed 3 consecutive times");
        transition_to(STATE_SENSOR_FAULT);
        return true;
    }
    return false;
}

// --- Relay control ---

static bool can_change_now(uint8_t ch, bool to_state, unsigned long now) {
    const RelayState& r = relays[ch];
    if (r.actual == to_state) return true;
    if (to_state) return (now - r.last_off) >= relay_min_off_ms[ch];
    return (now - r.last_on) >= relay_min_on_ms[ch];
}

static void apply_relay_change(uint8_t ch, bool to_state, unsigned long now) {
    RelayState& r = relays[ch];
    if (r.actual == to_state) return;
    if (r.actual) {
        r.total_on += (now - r.last_on);
        r.last_off = now;
    } else {
        r.last_on = now;
    }
    r.actual = to_state;
    // Active LOW: HIGH = relay open (off), LOW = relay closed (on)
    digitalWrite(relay_pins[ch], to_state ? LOW : HIGH);
    emit_relay_state(now);
}

// Hard override — bypasses min cycle time. Reserved for safety / failsafe.
static void relay_force(uint8_t ch, bool to_state, unsigned long now) {
    relays[ch].requested = to_state;
    apply_relay_change(ch, to_state, now);
}

static int8_t channel_for_name(const char* name) {
    for (uint8_t i = 0; i < RELAY_COUNT; i++) {
        if (strcmp(name, relay_names[i]) == 0) return (int8_t)i;
    }
    return -1;
}

static bool heater_in_cooldown(unsigned long now) {
    return heater_cooldown_until_ms != 0 && now < heater_cooldown_until_ms;
}

// Manual / PID request — respects all safety checks. On rejection,
// relays[ch].requested is left untouched so prior intent survives.
static bool relay_request(uint8_t ch, bool state, const char** out_reason) {
    const unsigned long now = millis();
    if (system_state == STATE_FAILSAFE) {
        *out_reason = "failsafe active";
        return false;
    }
    if (ch == RELAY_CH_HEATER && state) {
        if (heater_in_cooldown(now)) {
            *out_reason = "heater cooldown active";
            return false;
        }
        if (bme_reading.valid && bme_reading.temperature_c >= HEATER_TEMP_CEILING_C) {
            *out_reason = "temperature ceiling exceeded";
            return false;
        }
        if ((now - last_valid_temp_ms) > FAILSAFE_NO_TEMP_MS) {
            *out_reason = "no recent temperature reading";
            return false;
        }
    }
    relays[ch].requested = state;
    return true;
}

static void relay_tick(unsigned long now) {
    for (uint8_t i = 0; i < RELAY_COUNT; i++) {
        RelayState& r = relays[i];
        if (r.requested == r.actual) continue;
        if (!can_change_now(i, r.requested, now)) continue;
        apply_relay_change(i, r.requested, now);
    }
}

static void heater_safety_tick(unsigned long now) {
    RelayState& h = relays[RELAY_CH_HEATER];
    if (!h.actual) return;

    if ((now - h.last_on) >= HEATER_MAX_ON_MS) {
        emit_error("HEATER_MAX_ON", "heater forced off - 10 min limit reached");
        heater_cooldown_until_ms = now + HEATER_COOLDOWN_MS;
        relay_force(RELAY_CH_HEATER, false, now);
        return;
    }
    if (bme_reading.valid && bme_reading.temperature_c >= HEATER_TEMP_CEILING_C) {
        emit_error("HEATER_OVERTEMP", "heater forced off - temperature ceiling");
        relay_force(RELAY_CH_HEATER, false, now);
    }
}

// --- PID compute + time-proportional output ---

static void configure_loop(uint8_t idx, QuickPID* pid,
                           float* sp, float* pv, float* out,
                           float* kp, float* ki, float* kd,
                           uint8_t relay_ch, float sp_min, float sp_max,
                           const char* name, const char* json_key, bool is_fan) {
    PIDLoopInfo& l = loops[idx];
    l.pid = pid;
    l.setpoint = sp;
    l.input = pv;
    l.output = out;
    l.kp = kp; l.ki = ki; l.kd = kd;
    l.relay_channel = relay_ch;
    l.sp_min = sp_min;
    l.sp_max = sp_max;
    l.name = name;
    l.json_key = json_key;
    l.is_fan = is_fan;
    l.cycle_start_ms = 0;
    l.locked_output = 0.0f;
    l.cycle_active = false;
}

static PIDLoopInfo* find_loop_by_name(const char* name) {
    for (uint8_t i = 0; i < PID_LOOP_COUNT; i++) {
        if (strcmp(name, loops[i].name) == 0) return &loops[i];
    }
    return NULL;
}

static void pid_compute_all() {
    if (bme_reading.valid) {
        pv_temp = bme_reading.temperature_c;
        pv_hum  = bme_reading.humidity_rh;
    }
    if (scd_reading.valid) {
        pv_co2  = (float)scd_reading.co2_ppm;
    }
    // QuickPID's Compute() honours SetSampleTimeUs internally — safe to call every loop.
    // Anti-windup: default iAwCondition prevents integral accumulation at saturation.
    pid_temp.Compute();
    pid_hum.Compute();
    pid_co2.Compute();
}

static void apply_loop_outputs(unsigned long now) {
    for (uint8_t i = 0; i < PID_LOOP_COUNT; i++) {
        PIDLoopInfo& l = loops[i];

        // Lock the PID output at the start of each time-proportional window.
        // Mid-cycle PID changes take effect on the next window boundary.
        if (!l.cycle_active || (now - l.cycle_start_ms) >= PID_CONTROL_CYCLE_MS) {
            l.cycle_active = true;
            l.cycle_start_ms = now;
            l.locked_output = *l.output;
            if (l.is_fan && l.locked_output < FAN_BASELINE_DUTY) {
                l.locked_output = FAN_BASELINE_DUTY;
            }
        }

        bool should_be_on;
        if (l.locked_output <= PID_OUTPUT_DEADBAND_LOW) {
            should_be_on = false;
        } else if (l.locked_output >= PID_OUTPUT_DEADBAND_HIGH) {
            should_be_on = true;
        } else {
            const unsigned long elapsed = now - l.cycle_start_ms;
            const unsigned long on_duration =
                (unsigned long)(l.locked_output * (float)PID_CONTROL_CYCLE_MS);
            should_be_on = elapsed < on_duration;
        }

        // PID-driven requests share the relay_request safety layer.
        // Rejections (e.g. heater cooldown) are silent — last requested state stays.
        const char* reason = NULL;
        relay_request(l.relay_channel, should_be_on, &reason);
    }
}

// --- Serial command parsing ---

static const char* find_field(const char* json, const char* key) {
    char pattern[24];
    snprintf(pattern, sizeof(pattern), "\"%s\":", key);
    const char* p = strstr(json, pattern);
    if (!p) return NULL;
    p += strlen(pattern);
    while (*p == ' ' || *p == '\t') p++;
    return p;
}

static bool read_string(const char* p, char* buf, size_t bufsize) {
    if (buf && bufsize) buf[0] = '\0';
    if (!p || *p != '"') return false;
    p++;
    size_t i = 0;
    while (*p && *p != '"' && i < bufsize - 1) {
        buf[i++] = *p++;
    }
    buf[i] = '\0';
    return *p == '"';
}

static bool read_bool(const char* p, bool* out) {
    if (!p) return false;
    if (strncmp(p, "true", 4) == 0)  { *out = true;  return true; }
    if (strncmp(p, "false", 5) == 0) { *out = false; return true; }
    return false;
}

static bool parse_float(const char* p, float* out) {
    if (!p) return false;
    while (*p == ' ' || *p == '\t') p++;
    if (*p != '-' && *p != '+' && *p != '.' && !isdigit((unsigned char)*p)) {
        return false;
    }
    *out = (float)atof(p);
    return true;
}

static void handle_relay_cmd(const char* json) {
    char target[16];
    bool state = false;
    if (!read_string(find_field(json, "target"), target, sizeof(target))) {
        emit_ack("relay", false, "missing target");
        return;
    }
    if (!read_bool(find_field(json, "state"), &state)) {
        emit_ack("relay", false, "missing or invalid state");
        return;
    }
    const int8_t ch = channel_for_name(target);
    if (ch < 0) {
        emit_ack("relay", false, "unknown target");
        return;
    }
    const char* reason = NULL;
    if (!relay_request((uint8_t)ch, state, &reason)) {
        emit_ack("relay", false, reason ? reason : "rejected");
        return;
    }
    emit_ack("relay", true, NULL);
}

static void handle_setpoint_cmd(const char* json) {
    char param[16];
    if (!read_string(find_field(json, "param"), param, sizeof(param))) {
        emit_ack("setpoint", false, "missing param");
        return;
    }
    float value = 0.0f;
    if (!parse_float(find_field(json, "value"), &value)) {
        emit_ack("setpoint", false, "missing or invalid value");
        return;
    }
    PIDLoopInfo* target = find_loop_by_name(param);
    if (!target) {
        emit_ack("setpoint", false, "unknown param");
        return;
    }
    if (value < target->sp_min || value > target->sp_max) {
        emit_ack("setpoint", false, "value out of range");
        return;
    }
    *target->setpoint = value;
    // Spec §5.4: reset integral on setpoint change to prevent overshoot.
    target->pid->Initialize();
    target->cycle_active = false;
    save_persisted();
    emit_ack("setpoint", true, NULL);
}

static void handle_pid_mode_cmd(const char* json) {
    char mode[16];
    if (!read_string(find_field(json, "mode"), mode, sizeof(mode))) {
        emit_ack("pid_mode", false, "missing mode");
        return;
    }
    if (strcmp(mode, "auto") == 0) {
        pid_mode = PID_MODE_AUTO;
        // QuickPID's SetMode(automatic) calls Initialize() — bumpless restart.
        pid_temp.SetMode(QuickPID::Control::automatic);
        pid_hum.SetMode(QuickPID::Control::automatic);
        pid_co2.SetMode(QuickPID::Control::automatic);
        for (uint8_t i = 0; i < PID_LOOP_COUNT; i++) loops[i].cycle_active = false;
    } else if (strcmp(mode, "manual") == 0) {
        pid_mode = PID_MODE_MANUAL;
        pid_temp.SetMode(QuickPID::Control::manual);
        pid_hum.SetMode(QuickPID::Control::manual);
        pid_co2.SetMode(QuickPID::Control::manual);
    } else {
        emit_ack("pid_mode", false, "invalid mode");
        return;
    }
    emit_ack("pid_mode", true, NULL);
}

static void handle_pid_tune_cmd(const char* json) {
    char loop_name[16];
    if (!read_string(find_field(json, "loop"), loop_name, sizeof(loop_name))) {
        emit_ack("pid_tune", false, "missing loop");
        return;
    }
    float kp = 0.0f, ki = 0.0f, kd = 0.0f;
    if (!parse_float(find_field(json, "kp"), &kp) ||
        !parse_float(find_field(json, "ki"), &ki) ||
        !parse_float(find_field(json, "kd"), &kd)) {
        emit_ack("pid_tune", false, "missing or invalid gains");
        return;
    }
    if (kp < 0.0f || ki < 0.0f || kd < 0.0f) {
        emit_ack("pid_tune", false, "negative gain");
        return;
    }
    PIDLoopInfo* target = find_loop_by_name(loop_name);
    if (!target) {
        emit_ack("pid_tune", false, "unknown loop");
        return;
    }
    *target->kp = kp;
    *target->ki = ki;
    *target->kd = kd;
    target->pid->SetTunings(kp, ki, kd);
    save_persisted();
    emit_ack("pid_tune", true, NULL);
}

static void process_command(const char* json) {
    char cmd_str[16];
    if (!read_string(find_field(json, "cmd"), cmd_str, sizeof(cmd_str))) {
        emit_ack("?", false, "missing cmd field");
        return;
    }
    if (strcmp(cmd_str, "relay") == 0) {
        handle_relay_cmd(json);
    } else if (strcmp(cmd_str, "setpoint") == 0) {
        handle_setpoint_cmd(json);
    } else if (strcmp(cmd_str, "pid_mode") == 0) {
        handle_pid_mode_cmd(json);
    } else if (strcmp(cmd_str, "pid_tune") == 0) {
        handle_pid_tune_cmd(json);
    } else if (strcmp(cmd_str, "status") == 0) {
        emit_status();
        emit_relay_state(millis());
        emit_pid_status();
        emit_ack("status", true, NULL);
    } else {
        emit_ack(cmd_str, false, "unknown command");
    }
}

static void tick_serial_rx() {
    while (Serial.available() > 0) {
        char c = (char)Serial.read();
        if (c == '\n' || c == '\r') {
            if (rx_len > 0) {
                rx_buffer[rx_len] = '\0';
                process_command(rx_buffer);
                rx_len = 0;
            }
        } else if (rx_len < RX_LINE_BUFFER_SIZE - 1) {
            rx_buffer[rx_len++] = c;
        } else {
            emit_error("RX_OVERFLOW", "command line too long");
            rx_len = 0;
        }
    }
}

// --- State handlers ---

static void handle_sensor_init(unsigned long now) {
    if (!bme.begin(BME280_ADDR, &Wire)) {
        emit_error("BME280_INIT_FAIL", "BME280 begin() failed");
        transition_to(STATE_SENSOR_FAULT);
        return;
    }

    scd4x.begin(Wire);
    // Defensive: clear any leftover measurement mode from a previous run.
    // The Sensirion driver enforces the datasheet's 500 ms settle internally.
    scd4x.stopPeriodicMeasurement();

    uint16_t err = scd4x.startPeriodicMeasurement();
    if (err != 0) {
        emit_error("SCD40_INIT_FAIL", "startPeriodicMeasurement failed");
        transition_to(STATE_SENSOR_FAULT);
        return;
    }

    last_bme_read_ms     = now;
    last_scd_read_ms     = now;
    last_reading_emit_ms = now;
    last_valid_temp_ms   = now;  // grace period - failsafe clock starts here
    transition_to(STATE_WARMUP);
}

static void handle_warmup(unsigned long now) {
    tick_bme(now);
    tick_scd(now);
    relay_tick(now);

    if (check_sensor_fault()) return;

    if (scd_reading.valid) {
        transition_to(STATE_RUNNING);
    }
}

static void handle_running(unsigned long now) {
    tick_bme(now);
    tick_scd(now);
    tick_emit(now);

    // PID drives relays only in auto mode. Manual mode freezes PID influence;
    // relays then only respond to explicit relay commands from the Linux side.
    if (pid_mode == PID_MODE_AUTO) {
        pid_compute_all();
        apply_loop_outputs(now);
    }

    relay_tick(now);
    heater_safety_tick(now);
    tick_pid_emit(now);
    check_sensor_fault();
}

static void handle_sensor_fault(unsigned long now) {
    tick_bme(now);
    tick_scd(now);
    tick_emit(now);
    relay_tick(now);
    heater_safety_tick(now);

    if ((now - last_valid_temp_ms) > FAILSAFE_NO_TEMP_MS) {
        emit_error("FAILSAFE", "no valid temperature reading for 30s");
        transition_to(STATE_FAILSAFE);
        return;
    }

    if (bme_reading.valid && scd_reading.valid &&
        bme_consecutive_failures == 0 && scd_consecutive_failures == 0) {
        transition_to(STATE_RUNNING);
    }
}

static void handle_failsafe(unsigned long now) {
    tick_bme(now);
    tick_scd(now);
    tick_emit(now);

    // Hold the safe relay state regardless of incoming requests or min cycle.
    relay_force(RELAY_CH_HEATER, false, now);
    relay_force(RELAY_CH_FAN, true, now);
    relay_force(RELAY_CH_HUMIDIFIER, false, now);

    if (bme_reading.valid && (now - last_valid_temp_ms) <= FAILSAFE_NO_TEMP_MS) {
        if (scd_reading.valid && bme_consecutive_failures == 0 && scd_consecutive_failures == 0) {
            transition_to(STATE_RUNNING);
        } else {
            transition_to(STATE_SENSOR_FAULT);
        }
    }
}

// --- Arduino entry points ---

void setup() {
    Serial.begin(115200);
    Wire.begin();

    eeprom_init();
    load_persisted();  // may override sp_*/k* defaults

    // Initialise relay struct + hardware to safe state (active LOW: HIGH = off)
    for (uint8_t i = 0; i < RELAY_COUNT; i++) {
        relays[i] = RelayState{};
        pinMode(relay_pins[i], OUTPUT);
        digitalWrite(relay_pins[i], HIGH);
    }

    // Configure QuickPID instances using freshly-loaded tunings.
    pid_temp.SetTunings(kp_temp, ki_temp, kd_temp);
    pid_temp.SetOutputLimits(0.0f, 1.0f);
    pid_temp.SetSampleTimeUs(PID_SAMPLE_TEMP_US);
    pid_temp.SetMode(QuickPID::Control::automatic);

    pid_hum.SetTunings(kp_hum, ki_hum, kd_hum);
    pid_hum.SetOutputLimits(0.0f, 1.0f);
    pid_hum.SetSampleTimeUs(PID_SAMPLE_HUM_US);
    pid_hum.SetMode(QuickPID::Control::automatic);

    pid_co2.SetTunings(kp_co2, ki_co2, kd_co2);
    pid_co2.SetOutputLimits(0.0f, 1.0f);
    pid_co2.SetSampleTimeUs(PID_SAMPLE_CO2_US);
    // CO2 -> fan: higher PV (more CO2) should produce higher output (more fan).
    pid_co2.SetControllerDirection(QuickPID::Action::reverse);
    pid_co2.SetMode(QuickPID::Control::automatic);

    configure_loop(PID_LOOP_TEMP, &pid_temp, &sp_temp, &pv_temp, &out_temp,
                   &kp_temp, &ki_temp, &kd_temp, RELAY_CH_HEATER,
                   TEMP_SP_MIN, TEMP_SP_MAX, "temperature", "temp", false);
    configure_loop(PID_LOOP_HUM, &pid_hum, &sp_hum, &pv_hum, &out_hum,
                   &kp_hum, &ki_hum, &kd_hum, RELAY_CH_HUMIDIFIER,
                   HUM_SP_MIN, HUM_SP_MAX, "humidity", "hum", false);
    configure_loop(PID_LOOP_CO2, &pid_co2, &sp_co2, &pv_co2, &out_co2,
                   &kp_co2, &ki_co2, &kd_co2, RELAY_CH_FAN,
                   CO2_SP_MIN, CO2_SP_MAX, "co2", "co2", true);

    transition_to(STATE_SENSOR_INIT);
}

void loop() {
    const unsigned long now = millis();

    // Always drain incoming commands; handlers gate state-dependent effects.
    tick_serial_rx();

    switch (system_state) {
        case STATE_BOOT:
            transition_to(STATE_SENSOR_INIT);
            break;
        case STATE_SENSOR_INIT:
            handle_sensor_init(now);
            break;
        case STATE_WARMUP:
            handle_warmup(now);
            break;
        case STATE_RUNNING:
            handle_running(now);
            break;
        case STATE_SENSOR_FAULT:
            handle_sensor_fault(now);
            break;
        case STATE_FAILSAFE:
            handle_failsafe(now);
            break;
    }
}
