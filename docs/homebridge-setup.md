# Homebridge Setup for Plant Cabinet

Step-by-step setup to expose the cabinet's sensors and relays in Apple Home
via Homebridge running on the Uno Q's Linux side.

This wires the Flask REST API (`/api/readings`, `/api/relays`) into HomeKit
through community HTTP plugins — no custom Homebridge plugin required for the
baseline accessory set.

## Prerequisites

- Plant cabinet server running and reachable at `http://localhost:5000` on the
  Uno Q. Verify with `curl http://localhost:5000/api/readings`.
- A HomeKit hub on the same Wi-Fi network: Apple TV 4K, HomePod, or an iPad
  that stays at home.
- iPhone or iPad with the Home app for pairing.

## 1. Install Node.js

Homebridge requires Node.js 18 or later. Use the NodeSource repo:

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs
node --version    # expect v20.x
npm --version
```

## 2. Install Homebridge and the management UI

```bash
sudo npm install -g --unsafe-perm homebridge homebridge-config-ui-x
```

The `--unsafe-perm` flag is needed because some Homebridge plugins compile
native bindings during install.

## 3. Install the HTTP plugins

```bash
sudo npm install -g \
    homebridge-http-temperature-sensor \
    homebridge-http-humidity-sensor \
    homebridge-http-switch
```

These three plugins back the five accessories defined in
`server/homebridge/config.json`:

| Accessory | Plugin |
|---|---|
| Cabinet Temperature | `homebridge-http-temperature-sensor` |
| Cabinet Humidity | `homebridge-http-humidity-sensor` |
| Cabinet Humidifier / Fan / Heater | `homebridge-http-switch` |

## 4. Install the config

```bash
mkdir -p ~/.homebridge
cp /home/arduino/plant-cabinet-controller/server/homebridge/config.json \
   ~/.homebridge/config.json
```

Open `~/.homebridge/config.json` and change the bridge `username` field to a
unique MAC-format string (each Homebridge instance on the network needs its
own). Anything in the form `XX:XX:XX:XX:XX:XX` works — generating with
`openssl rand -hex 6 | sed 's/\(..\)/\1:/g; s/:$//' | tr 'a-f' 'A-F'` produces
a fresh one.

The `pin` (`031-45-154`) is what you type in the Home app to pair. You can
change it but must keep the `XXX-XX-XXX` format.

## 5. Test-run Homebridge

```bash
homebridge -U ~/.homebridge
```

Watch the log for:
- Lines confirming each accessory is registered (e.g. `Cabinet Temperature ...`).
- The pairing QR code and pin.
- Any plugin errors when the HTTP poll runs (most often the regex in
  `statusPattern` doesn't match — see Troubleshooting).

Leave it running while you pair.

## 6. Pair with Apple Home

1. Open the Home app on iOS.
2. Tap **+** -> **Add Accessory** -> **More options...**.
3. Select **Plant Cabinet**.
4. Enter pin **031-45-154** (or whatever you set).
5. Tap through the per-accessory naming — accept the defaults or rename.

Within 30 s of pairing, the four sensor cards and three switches should
appear with live values.

## 7. Run as a systemd service

Once pairing works, install Homebridge as a service so it starts on boot:

`/etc/systemd/system/homebridge.service`:
```ini
[Unit]
Description=Homebridge (Plant Cabinet HomeKit)
After=network-online.target plant-cabinet-server.service
Wants=network-online.target

[Service]
Type=simple
User=arduino
ExecStart=/usr/bin/homebridge -U /home/arduino/.homebridge
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now homebridge.service
sudo journalctl -u homebridge -f      # follow the log
```

## CO2 -> AirQuality (optional)

HomeKit's `AirQualitySensor` characteristic is a 1-5 scale, not a ppm value.
`server/homebridge/co2_quality.py` provides the canonical mapping:

| CO2 (ppm) | HomeKit | Label |
|---|---|---|
| < 600 | 1 | Excellent |
| 600 - 800 | 2 | Good |
| 800 - 1000 | 3 | Fair |
| 1000 - 1200 | 4 | Inferior |
| > 1200 | 5 | Poor |

Two ways to expose it:

**A. Add a Flask endpoint.** The simplest path. Wire a thin `/api/airquality`
view that returns `{"value": co2_to_homekit_quality(latest_co2)}`, then add an
`HttpAirQuality`-style accessory pointing at it. Lets you use a stock HTTP
sensor plugin.

**B. Custom Homebridge plugin.** Write a small plugin that polls
`/api/readings`, runs `co2_to_homekit_quality`, and publishes an
`AirQualitySensor`. Fewer moving parts, but requires writing JS.

Either way `co2_quality.py` is the source of truth for the boundaries — keep
the mapping table here in sync with the spec.

## Troubleshooting

**Temperature/humidity stuck at the initial value.**
Run the same regex against a live response and check `patternGroupToExtract`:
```bash
curl -s http://localhost:5000/api/readings | \
    python3 -c "import re,sys; m=re.search(r'\"temperature_c\":\s*(-?[0-9.]+)', sys.stdin.read()); print(m.group(1) if m else 'no match')"
```

**Switch state doesn't update after a manual toggle.**
The `statusPattern` is sensitive to whitespace inside the JSON. Test:
```bash
curl -s http://localhost:5000/api/relays
```
The pattern in `config.json` assumes the compact JSON Flask produces by
default. If you've enabled `JSON_AS_ASCII` or pretty-printing, loosen the
regex (e.g. add `\\s*` between every token).

**Pairing fails / "Accessory already added".**
Wipe HomeKit state and re-pair:
```bash
sudo systemctl stop homebridge
rm -rf ~/.homebridge/persist ~/.homebridge/accessories
sudo systemctl start homebridge
```
Then forget the accessory in the Home app and re-add.

**Port 51826 blocked.**
HomeKit pairing needs the port reachable from the iOS device. Open it:
```bash
sudo ufw allow 51826/tcp
```
(or whichever firewall is active).

**Multiple Homebridge instances on the same LAN.**
Each needs a unique `username` MAC and `port`. Conflicts show up as silent
pairing failures or accessories vanishing in Home.
