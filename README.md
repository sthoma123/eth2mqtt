# ETH2MQTT

A standalone MQTT gateway for **Devantech ETH00x / ETH80xx / dScript ("2824"-style)** relay & I/O boards, with full **Home Assistant MQTT discovery**. Runs as a Home Assistant local app (formerly "add-on") — no other Home Assistant integration or third-party framework required.

It replaces the `readETH008.py` driver from the old pvOpt home-automation project: same wire protocol, but reimplemented as a self-contained polling/event gateway instead of a call-driven library.

## Features

- **Auto-discovers itself into Home Assistant** via MQTT discovery — no manual entity/YAML configuration needed.
- **Digital outputs** (relays) as `switch` entities — controllable from Home Assistant, bidirectional.
- **Digital inputs** (new-style boards) as `binary_sensor` entities.
- **Analog channels** as `sensor` entities (only channels you've explicitly named — see [Configuration](#configuration)).
- **Supply voltage** as a `sensor` entity per board.
- **Push/event listener** on port `17494` — board-initiated input-change notifications are published immediately, not just on the next poll.
- **Per-box and per-bridge availability** — entities go "unavailable" in Home Assistant when a board stops responding.
- **Named via `devantech.ini`** — give any channel a name (e.g. to match an existing Homematic entity name 1:1); anything you don't name still shows up, with a generic name like `Keller2 Relay 3`.
- **Live-reloads `devantech.ini`** — edit names/units/invert-flags and they take effect within ~15s, no restart needed. (Adding/removing a whole box still needs a restart.)
- **Auto-detects the MQTT broker** via the Home Assistant Supervisor if the Mosquitto add-on is installed; otherwise falls back to configured broker options.
- Board type (old ETH008/ETH8020 binary protocol vs. new dScript-style protocol) and relay/input bit counts are **auto-detected at runtime**, not hardcoded.

## Installation (as a Home Assistant local app)

1. Copy this folder to `/addons/eth2mqtt` on your Home Assistant host (via Samba, SSH, or the Studio Code Server app).
2. In Home Assistant: **Settings → Apps → App Store**, scroll to **Local Apps**, and install **ETH2MQTT**.
3. Set the options (see below) and start it.
4. Edit `devantech.ini` on the add-on's config share (`/addon_configs/local_eth2mqtt/devantech.ini`, reachable the same way as step 1) to match your boards.

> Home Assistant renamed "Add-ons" to "Apps" in release 2026.2 — the mechanism (drop a folder under `/addons`) is unchanged, only the UI labels are new.

## Configuration

### App options

| Option             | Default          | Description                                                                 |
|---------------------|------------------|-------------------------------------------------------------------------------|
| `log_level`          | `info`           | `debug`, `info`, `warning`, or `error`                                        |
| `poll_interval`      | `10`             | Seconds between polls of each box                                             |
| `discovery_prefix`   | `homeassistant`  | Home Assistant's MQTT discovery topic prefix                                  |
| `topic_prefix`       | `eth2mqtt`       | Prefix for all state/command topics                                           |
| `mqtt_host`          | *(empty)*        | Only needed if the Supervisor can't auto-discover a broker (e.g. no Mosquitto add-on) |
| `mqtt_port`          | `1883`           | -                                                                             |
| `mqtt_username`      | *(empty)*        | -                                                                             |
| `mqtt_password`      | *(empty)*        | -                                                                             |

### `devantech.ini`

```ini
[boxes]
# box_id = hostname_or_ip[:port]        (port defaults to 17494)
kellerschalter = 192.168.0.182
Kueche = 192.168.0.186

[METADATA]
# ETH/<box_id>                = Box display name, ,
# ETH/<box_id>/<n>            = Relay <n> name, [n], [type]      (n = 1-based relay number)
# ETH/<box_id>/V              = Voltage sensor name, ,
# ETH/<box_id>/A<n>           = Analog channel <n> name, [unit], [type]
# ETH/<box_id>/I<n>           = Digital input <n> name, [n], [type]   (new boards only)
ETH/kellerschalter=KellerSchalter, ,
ETH/kellerschalter/1=SchwimmSolar, , int
ETH/kellerschalter/2=SchwimmFilter, , int
ETH/Kueche/A4=Schalter Kueche unten, ADC, int
```

- `box_id` in `[METADATA]` keys is matched case-insensitively against `[boxes]`.
- The middle field is `n` (case-insensitive) to mark a relay/input as **physically inverted** — the gateway then reports/accepts the *logical* (non-inverted) state on MQTT, while still writing/reading the true physical bit to the board. Any other text in that field becomes the `unit_of_measurement` for analog channels.
- Only analog channels (`A<n>`) that are explicitly named get published — there's no protocol-level way to detect which analog inputs are physically wired, so nothing is guessed there. Relays and digital inputs are auto-detected from the board's own reply length and are *all* published (aliased ones get their name, the rest get a generic one).
- Editing this file (on the live config share) is picked up automatically within ~15s — see [Features](#features).

### Board push/event configuration

Point each board's own "target IP" / event-notification setting (in the board's web configuration) at your Home Assistant host's IP, port `17494`. This is what makes input-change events show up on MQTT immediately instead of waiting for the next poll.

## MQTT topics

All topics are prefixed with `topic_prefix` (default `eth2mqtt`):

| Topic                                   | Direction | Payload                          |
|------------------------------------------|-----------|-----------------------------------|
| `bridge/status`                          | out       | `online` / `offline` (gateway LWT) |
| `<box>/availability`                     | out       | `online` / `offline` (per box)     |
| `<box>/relay/<n>/state`                  | out       | `ON` / `OFF`                       |
| `<box>/relay/<n>/set`                    | in        | `ON` / `OFF`                       |
| `<box>/input/<n>/state`                  | out       | `ON` / `OFF`                       |
| `<box>/analog/<n>/state`                 | out       | number                             |
| `<box>/voltage/state`                    | out       | number (Volts)                     |
| `<box>/event/<n>`                        | out       | `{"event_type": "on"/"off"}` (push notifications) |

Home Assistant discovery configs are published (retained) under `<discovery_prefix>/<component>/eth2mqtt_<box>/<object_id>/config`.

## Running outside Home Assistant (development)

```bash
pip install -r requirements.txt
ETH2MQTT_MQTT_HOST=mqtt.local python eth2mqtt.py
```

Without `/data/options.json` present (i.e. not running under the Supervisor), options are read from `ETH2MQTT_<OPTION_NAME>` environment variables, and `devantech.ini` is looked for next to the script if `/config/devantech.ini` doesn't exist.

## Known limitations / things to verify against real hardware

- **Digital input reading (`0x34`)** for new-style boards is new functionality — the original driver never read digital inputs at all. The reply byte layout is implemented by analogy with the documented commands and hasn't been verified against real hardware; check the `debug`-level logs on first run if this applies to your board.
- Adding or removing a box in `[boxes]` requires an app restart (poll threads are only created at startup); the gateway logs a warning if it detects this rather than silently ignoring it.
- Renaming an alias leaves the old (retained) MQTT discovery topic on the broker under its old `unique_id` — Home Assistant will just show a duplicate/orphaned entity until you manually clear that retained topic (there's no automatic discovery cleanup).

## Project layout

| File              | Purpose                                                        |
|-------------------|------------------------------------------------------------------|
| `devantech.py`    | Pure Devantech binary protocol implementation (no MQTT/HA code)  |
| `eth2mqtt.py`      | The gateway: ini parsing, MQTT client, HA discovery, polling, push listener |
| `devantech.ini`    | Box list and channel aliases (seed copy; edit the live copy once installed) |
| `config.yaml`      | Home Assistant app manifest                                      |
| `Dockerfile`       | Container image build                                            |
| `requirements.txt` | Python dependencies (`paho-mqtt`)                                 |
