# IFTTT Webhook Notifications

Get phone notifications (or SMS, Slack, etc.) when the cabinet drifts out of
range — humidity low, temperature out of range, CO2 high, sensor offline, or
heater safety lockout.

This guide creates a single IFTTT applet that fires for every alert type.
The server posts a JSON payload with `value1` / `value2` / `value3` so a
single applet template can render every alert.

## 1. Create an IFTTT account

Sign up at https://ifttt.com (free tier is enough for a handful of alerts
per day).

## 2. Enable the Webhooks service

1. Visit https://ifttt.com/maker_webhooks
2. Click **Connect**
3. Click **Documentation** (top right)
4. Copy your webhook key — it looks like `bX_2K9eF...` (~22 chars)

## 3. Add the key to the server `.env`

On the Uno Q:

```bash
cd /home/arduino/plant-cabinet-controller/server
echo 'IFTTT_WEBHOOK_KEY=bX_2K9eF...your-key...' >> .env
sudo systemctl restart plant-cabinet-server
```

Verify it's loaded:

```bash
curl http://localhost:5000/api/alerts/config | jq .ifttt_configured
# -> true
```

## 4. Create the applet

1. Go to https://ifttt.com/create
2. **If This** -> search **Webhooks** -> choose
   *"Receive a web request with a JSON payload"*
3. **Event Name:** `plant_cabinet_alert` (must match exactly — case-sensitive)
4. **Then That** -> pick a delivery action (suggestions below)

## 5. Payload fields

The server POSTs a JSON body to
`https://maker.ifttt.com/trigger/plant_cabinet_alert/json/with/key/{key}`:

| Field | Content | Example |
|---|---|---|
| `Value1` | alert type | `humidity_low` |
| `Value2` | current reading | `52.3% RH` |
| `Value3` | threshold | `below 55% RH` |

Reference these in the applet's action template as `{{Value1}}` etc.

## 6. Recommended actions

**Phone push notification (iOS / Android)** — install the IFTTT app, then:

- *Then That* -> **Notifications** -> *"Send a rich notification from the IFTTT app"*
- Title: `Plant Cabinet: {{Value1}}`
- Message: `{{Value2}} ({{Value3}})`

**SMS** (US/CA, requires IFTTT Pro):

- *Then That* -> **SMS** -> *"Send me an SMS"*
- Message: `Cabinet {{Value1}}: {{Value2}}`

**Slack:**

- *Then That* -> **Slack** -> *"Post to channel"*
- Message: `:warning: Plant cabinet *{{Value1}}* — {{Value2}} ({{Value3}})`

## 7. Test the wire

From the Uno Q:

```bash
curl -X POST -H "Content-Type: application/json" \
    -d '{"value1":"test","value2":"hello","value3":"world"}' \
    "https://maker.ifttt.com/trigger/plant_cabinet_alert/json/with/key/$(grep IFTTT_WEBHOOK_KEY server/.env | cut -d= -f2)"
```

Within a minute the notification should arrive on your phone. If it doesn't:

- Confirm the event name in the applet is exactly `plant_cabinet_alert`.
- Confirm the webhook key in `.env` matches the one at
  https://ifttt.com/maker_webhooks (click *Settings* on that page).
- Check the applet is enabled (toggle on the applet's page).

## Alert types

| Type | Trigger | Sustained | Severity |
|---|---|---|---|
| `humidity_low` | RH below `humidity_low_warning` (default 55%) | 5 min | warning |
| `humidity_critical` | RH below `humidity_low_critical` (default 45%) | immediate | critical |
| `temp_low` | temperature below `temp_low_warning` (default 16°C) | 5 min | warning |
| `temp_high` | temperature above `temp_high_warning` (default 28°C) | 5 min | warning |
| `co2_high` | CO2 above `co2_high_warning` (default 1200 ppm) | 5 min | warning |
| `sensor_offline` | no MCU data | 2 min | critical |
| `heater_lockout` | MCU emits HEATER_MAX_ON or HEATER_OVERTEMP | immediate | critical |

Each alert type has its own 30-minute cooldown clock — the same condition
won't re-fire within that window even if it keeps tripping. Cooldown only
starts on a *successful* IFTTT POST, so a transient network blip can't
silently suppress the next genuine alert.

## Adjusting thresholds and cooldown

- `GET /api/alerts/config` — inspect current thresholds, cooldown, configured
  rules, and whether IFTTT is wired up.
- `POST /api/config` — partially update `alert_thresholds` (see
  `server-api-spec.md` §4.7). New thresholds take effect on the next reading.
- Cooldown lives on the `NotificationManager` and is currently fixed at the
  `DEFAULT_COOLDOWN_S` constant in `server/notifications.py`. Change there
  and restart the server to adjust.

## Disabling notifications

Remove or comment out `IFTTT_WEBHOOK_KEY` in `.env` and restart the server.
The alert monitor keeps writing rows to the `alerts` table and surfacing
them on the dashboard — only the webhook calls are skipped.
