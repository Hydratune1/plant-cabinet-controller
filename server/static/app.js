// Plant Cabinet Controller — Dashboard SPA (vanilla JS, polling-based)

const POLL_READINGS_MS = 5000;
const POLL_STATUS_MS   = 10000;
const POLL_RELAYS_MS   = 5000;
const POLL_ALERTS_MS   = 30000;
const POLL_CHART_MS    = 30000;
const SETPOINT_DEBOUNCE_MS = 500;
const STALE_READING_THRESHOLD_S = 10;

// Per-sensor colour bands. value < critLow OR > critHigh → crit (red);
// value < warnLow OR > warnHigh → warn (amber); otherwise ok (green).
// Set warnLow/critLow to -Infinity to disable the lower bound (e.g. CO2).
const SENSOR_THRESHOLDS = {
    temperature_c: { warnLow: 18,         warnHigh: 25,  critLow: 16,         critHigh: 28 },
    humidity_rh:   { warnLow: 60,         warnHigh: 80,  critLow: 45,         critHigh: 90 },
    co2_ppm:       { warnLow: -Infinity,  warnHigh: 800, critLow: -Infinity,  critHigh: 1200 },
    pressure_hpa:  { warnLow: 990,        warnHigh: 1030,critLow: 970,        critHigh: 1050 },
};

const charts = {};
let activePeriod = "24h";
const setpointTimers = {};

// --- Helpers ---

async function fetchJson(url, options = {}) {
    const r = await fetch(url, options);
    if (!r.ok) throw new Error(`${url} -> HTTP ${r.status}`);
    return r.json();
}

function postJson(url, body) {
    return fetchJson(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
    });
}

function formatNumber(value, decimals = 1) {
    if (value === null || value === undefined || Number.isNaN(value)) return "–";
    return Number(value).toFixed(decimals);
}

function formatUptime(seconds) {
    if (seconds === null || seconds === undefined) return "–";
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    if (d > 0) return `${d}d ${h}h`;
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
}

function applyStatusColor(el, value, thresholds) {
    el.classList.remove("warn", "crit");
    if (value === null || value === undefined) return;
    if (value < thresholds.critLow || value > thresholds.critHigh) {
        el.classList.add("crit");
    } else if (value < thresholds.warnLow || value > thresholds.warnHigh) {
        el.classList.add("warn");
    }
}

// --- Sensor cards ---

async function refreshReadings() {
    let data;
    try { data = await fetchJson("/api/readings"); }
    catch { return; }
    updateSensorCard("temperature", data.temperature_c, 1, "temperature_c");
    updateSensorCard("humidity",    data.humidity_rh,   1, "humidity_rh");
    updateSensorCard("co2",         data.co2_ppm,       0, "co2_ppm");
    updateSensorCard("pressure",    data.pressure_hpa,  1, "pressure_hpa");
}

function updateSensorCard(name, value, decimals, thresholdKey) {
    const card = document.querySelector(`.card.sensor[data-sensor="${name}"]`);
    if (!card) return;
    const valueEl = card.querySelector(".value");
    valueEl.textContent = formatNumber(value, decimals);
    applyStatusColor(valueEl, value, SENSOR_THRESHOLDS[thresholdKey]);
}

// --- Connection status + system footer ---

async function refreshStatus() {
    let data;
    try { data = await fetchJson("/api/status"); }
    catch {
        setConnectionStatus(false);
        return;
    }
    const lastAge = data.last_reading_age_s;
    const fresh = lastAge !== null && lastAge !== undefined && lastAge < STALE_READING_THRESHOLD_S;
    setConnectionStatus(data.mcu_connected && fresh);

    setSystemField("sys-mcu-state",      data.mcu_state ?? "–");
    setSystemField("sys-mcu-uptime",     formatUptime(data.mcu_uptime_s));
    setSystemField("sys-server-uptime",  formatUptime(data.server_uptime_s));
    setSystemField("sys-firmware",       data.firmware_version ?? "–");
    setSystemField("sys-db-size",        data.db_size_mb != null ? `${data.db_size_mb} MB` : "–");
}

function setSystemField(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
}

function setConnectionStatus(connected) {
    const dot  = document.getElementById("connection-dot");
    const text = document.getElementById("connection-text");
    if (connected) {
        dot.className  = "dot ok";
        text.textContent = "Connected";
    } else {
        dot.className  = "dot crit";
        text.textContent = "Offline";
    }
}

// --- Relay cards ---

async function refreshRelays() {
    let data;
    try { data = await fetchJson("/api/relays"); }
    catch { return; }
    for (const name of ["humidifier", "fan", "heater"]) {
        if (data[name]) updateRelayCard(name, data[name]);
    }
}

function updateRelayCard(name, info) {
    const card = document.querySelector(`.card.relay[data-relay="${name}"]`);
    if (!card) return;
    const dot = card.querySelector(".dot");
    const stateText = card.querySelector(".state-text");
    if (info.state) {
        dot.className = "dot on";
        stateText.textContent = "On";
    } else {
        dot.className = "dot off";
        stateText.textContent = "Off";
    }
    card.querySelectorAll(".mode-toggle button").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.mode === info.mode);
    });
}

async function postRelayMode(target, mode) {
    try {
        await postJson("/api/relays", { target, mode });
    } catch (e) {
        console.error("Failed to set relay mode:", e);
        return;
    }
    // Pick up the resulting state on the next poll cycle.
    setTimeout(refreshRelays, 250);
}

// --- Setpoints ---

async function loadSetpoints() {
    let data;
    try { data = await fetchJson("/api/setpoints"); }
    catch { return; }
    setSlider("sp-temp", data.temperature_c, 1, "°C");
    setSlider("sp-hum",  data.humidity_rh,   1, "%");
    setSlider("sp-co2",  data.co2_ppm,       0, " ppm");
}

function setSlider(id, value, decimals, suffix) {
    const slider = document.getElementById(id);
    const label  = document.getElementById(`${id}-value`);
    if (!slider || !label) return;
    if (value === null || value === undefined) return;
    slider.value = value;
    label.textContent = `${formatNumber(value, decimals)}${suffix}`;
}

function handleSliderInput(slider, decimals, suffix, field) {
    const label = document.getElementById(`${slider.id}-value`);
    if (label) label.textContent = `${formatNumber(slider.value, decimals)}${suffix}`;

    clearTimeout(setpointTimers[field]);
    setpointTimers[field] = setTimeout(async () => {
        try {
            await postJson("/api/setpoints", { [field]: Number(slider.value) });
        } catch (e) {
            console.error("Failed to update setpoint:", e);
        }
    }, SETPOINT_DEBOUNCE_MS);
}

// --- Charts ---

function chartOptions(kind) {
    const tickColor = "#9ca3af";
    const gridColor = "rgba(255,255,255,0.05)";
    const opts = {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
            legend: { labels: { color: "#eeeeee" } },
        },
        scales: {
            x: {
                type: "time",
                time: { tooltipFormat: "MMM d HH:mm", displayFormats: { hour: "HH:mm", day: "MMM d" } },
                grid:  { color: gridColor },
                ticks: { color: tickColor, maxRotation: 0, autoSkip: true, maxTicksLimit: 8 },
            },
        },
    };
    if (kind === "tempHum") {
        opts.scales.yTemp = {
            type: "linear", position: "left",
            title: { display: true, text: "°C", color: "#10b981" },
            ticks: { color: tickColor }, grid: { color: gridColor },
        };
        opts.scales.yHum = {
            type: "linear", position: "right",
            title: { display: true, text: "% RH", color: "#3b82f6" },
            ticks: { color: tickColor }, grid: { drawOnChartArea: false },
        };
    } else {
        opts.scales.y = {
            type: "linear",
            title: { display: true, text: "ppm", color: "#f59e0b" },
            ticks: { color: tickColor }, grid: { color: gridColor },
        };
    }
    return opts;
}

function initCharts() {
    const tempHumCtx = document.getElementById("chart-temp-hum");
    charts.tempHum = new Chart(tempHumCtx, {
        type: "line",
        data: {
            datasets: [
                { label: "Temperature (°C)", borderColor: "#10b981",
                  backgroundColor: "rgba(16,185,129,0.08)", yAxisID: "yTemp",
                  pointRadius: 0, borderWidth: 2, tension: 0.2, data: [] },
                { label: "Humidity (% RH)",  borderColor: "#3b82f6",
                  backgroundColor: "rgba(59,130,246,0.08)", yAxisID: "yHum",
                  pointRadius: 0, borderWidth: 2, tension: 0.2, data: [] },
            ],
        },
        options: chartOptions("tempHum"),
    });

    const co2Ctx = document.getElementById("chart-co2");
    charts.co2 = new Chart(co2Ctx, {
        type: "line",
        data: {
            datasets: [{
                label: "CO2 (ppm)", borderColor: "#f59e0b",
                backgroundColor: "rgba(245,158,11,0.08)",
                pointRadius: 0, borderWidth: 2, tension: 0.2, data: [],
            }],
        },
        options: chartOptions("co2"),
    });
}

async function loadChartData(period) {
    let payload;
    try { payload = await fetchJson(`/api/history?period=${encodeURIComponent(period)}`); }
    catch { return; }
    const points = payload.data || [];
    charts.tempHum.data.datasets[0].data = points.map(p => ({ x: p.timestamp, y: p.temperature_c }));
    charts.tempHum.data.datasets[1].data = points.map(p => ({ x: p.timestamp, y: p.humidity_rh }));
    charts.co2.data.datasets[0].data     = points.map(p => ({ x: p.timestamp, y: p.co2_ppm }));
    charts.tempHum.update("none");
    charts.co2.update("none");
}

function handleTabClick(button) {
    document.querySelectorAll(".chart-tabs button").forEach(b => b.classList.remove("active"));
    button.classList.add("active");
    activePeriod = button.dataset.period;
    loadChartData(activePeriod);
}

// --- Alerts banner ---

async function refreshAlerts() {
    let data;
    try { data = await fetchJson("/api/alerts?active=true"); }
    catch { return; }
    renderAlerts(data.alerts || []);
}

function renderAlerts(alerts) {
    // Header badge reflects total server-side active count, ignoring dismissals.
    const badge = document.getElementById("alerts-badge");
    if (badge) {
        if (alerts.length > 0) {
            badge.textContent = String(alerts.length);
            badge.classList.remove("hidden");
        } else {
            badge.classList.add("hidden");
        }
    }

    const banner = document.getElementById("alerts-banner");
    if (!banner) return;
    const dismissed = JSON.parse(sessionStorage.getItem("dismissedAlertIds") || "[]");
    const visible = alerts.filter(a => !dismissed.includes(a.id));
    if (visible.length === 0) {
        banner.classList.add("hidden");
        return;
    }
    const isCritical = visible.some(a => a.severity === "critical");
    banner.className = isCritical ? "alerts-banner crit" : "alerts-banner";

    // Build DOM safely — alert messages may contain arbitrary text.
    banner.replaceChildren();
    const msgSpan = document.createElement("span");
    msgSpan.textContent = visible.map(a => `• ${a.message}`).join("  ");
    const dismissBtn = document.createElement("button");
    dismissBtn.textContent = "Dismiss";
    dismissBtn.addEventListener("click", () => {
        sessionStorage.setItem("dismissedAlertIds", JSON.stringify(visible.map(a => a.id)));
        banner.classList.add("hidden");
    });
    banner.append(msgSpan, dismissBtn);
}

// --- Wiring ---

function setupEventHandlers() {
    document.querySelectorAll(".card.relay .mode-toggle").forEach(toggle => {
        const target = toggle.closest(".card.relay").dataset.relay;
        toggle.querySelectorAll("button").forEach(btn => {
            btn.addEventListener("click", () => postRelayMode(target, btn.dataset.mode));
        });
    });

    const wireSlider = (id, decimals, suffix, field) => {
        const slider = document.getElementById(id);
        if (slider) slider.addEventListener("input",
            e => handleSliderInput(e.target, decimals, suffix, field));
    };
    wireSlider("sp-temp", 1, "°C",   "temperature_c");
    wireSlider("sp-hum",  1, "%",    "humidity_rh");
    wireSlider("sp-co2",  0, " ppm", "co2_ppm");

    document.querySelectorAll(".chart-tabs button").forEach(btn => {
        btn.addEventListener("click", () => handleTabClick(btn));
    });
}

function refreshAll() {
    refreshStatus();
    refreshReadings();
    refreshRelays();
    refreshAlerts();
}

document.addEventListener("DOMContentLoaded", () => {
    setupEventHandlers();
    initCharts();
    loadSetpoints();
    refreshAll();
    loadChartData(activePeriod);

    setInterval(refreshReadings, POLL_READINGS_MS);
    setInterval(refreshStatus,   POLL_STATUS_MS);
    setInterval(refreshRelays,   POLL_RELAYS_MS);
    setInterval(refreshAlerts,   POLL_ALERTS_MS);
    setInterval(() => loadChartData(activePeriod), POLL_CHART_MS);
});
