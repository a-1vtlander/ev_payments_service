# EV Charger Portal

A Home Assistant add-on that provides a local HTTP portal for EV charger
session requests.  It bridges the charger UI to HA automations via MQTT and
handles payment pre-authorisation / capture through the Square API.

---

## Architecture

```
Browser / Charger UI
        │  HTTP  (port 8080)
        ▼
  FastAPI app (uvicorn)
        │  MQTT  (paho)
        ▼
  Home Assistant (MQTT broker)
        │
        ▼
  HA Automations → Square payment flow
```

Key files under `ev_portal/rootfs/app/`:

| File | Purpose |
|------|---------|
| `main.py` | FastAPI routes |
| `lifespan.py` | App startup/shutdown, wires MQTT + Square |
| `config.py` | Loads and validates `/data/options.json` |
| `state.py` | Shared runtime globals |
| `db.py` | SQLite session persistence |
| `square.py` | Square API client (pre-auth, capture, cancel) |
| `mqtt.py` | paho MQTT client factory |
| `finalize.py` | Background task: capture or void on session end |

---

## Configuration

### Home Assistant (production)

HA Supervisor writes all add-on options to `/data/options.json` automatically
when you save the add-on configuration in the UI.  The schema is defined in
`ev_portal/config.yaml`.

Required fields (must be set in the HA UI before the add-on will start):

| Key | Description |
|-----|-------------|
| `mqtt_host` | Hostname / IP of your MQTT broker |
| `mqtt_port` | Broker port (default `1883`) |
| `mqtt_username` | Broker username (blank for anonymous) |
| `mqtt_password` | Broker password (blank for anonymous) |
| `home_id` | Logical home identifier used in MQTT topic paths |
| `charger_id` | Charger identifier used in MQTT topic paths |
| `square_app_id` | Square application ID |
| `square_access_token` | Square access token |
| `square_location_id` | Square location ID (leave blank to auto-fetch) |
| `square_sandbox` | `true` for sandbox, `false` for production (default `true`) |
| `square_charge_cents` | Pre-auth amount in cents (default `100`) |

> **Security note** – credentials are never logged.  The startup log shows the
> broker host/port, Square environment (sandbox vs production), and file paths
> only.

---

## Local Development

### Prerequisites

- Python 3.9+
- [mosquitto](https://mosquitto.org/download/) (`brew install mosquitto` on macOS)
- A Square sandbox account (free at <https://developer.squareup.com/>)

### Quick start

```bash
# 1. Install Python dependencies
pip install -r ev_portal/rootfs/app/requirements.txt

# 2. Create a local options file (never commit this file)
cp ev_portal/rootfs/app/tests/dev_options.json.template \
   ev_portal/rootfs/app/tests/dev_options.json
# Then fill in your Square sandbox credentials in dev_options.json

# 3. Start a local MQTT broker (anonymous, dev only)
mosquitto -p 11883 -d

# 4. Start the app
cd ev_portal/rootfs/app
bash run_dev.sh
```

`run_dev.sh` sets `EV_OPTIONS_PATH` to `tests/dev_options.json` and
`EV_DB_PATH` to `tests/ev_portal.db`, then launches uvicorn with `--reload`.

### Manual launch (without dev_setup.sh)

```bash
export EV_OPTIONS_PATH=/path/to/your/dev_options.json
export EV_DB_PATH=/tmp/ev_portal_dev.db

cd ev_portal/rootfs/app
uvicorn main:app --host 0.0.0.0 --port 8080 --reload --log-level info
```

### dev_options.json format

```json
{
  "mqtt_host": "127.0.0.1",
  "mqtt_port": 11883,
  "mqtt_username": "",
  "mqtt_password": "",
  "home_id": "my-home",
  "charger_id": "charger-01",
  "square_sandbox": true,
  "square_app_id": "sandbox-sq0idb-XXXXXXXXXXXXXXXXXXXX",
  "square_access_token": "EAAAlw...",
  "square_location_id": "",
  "square_charge_cents": 100
}
```

> `dev_options.json` is listed in `.gitignore` and must **never** be committed.

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `EV_OPTIONS_PATH` | `/data/options.json` | Path to the options JSON file |
| `EV_DB_PATH` | `/data/ev_portal.db` | Path to the SQLite database |

---

## Running tests

```bash
cd ev_portal/rootfs/app

# Unit tests (no network, no broker required)
pytest tests/test_db.py tests/test_unit.py tests/test_finalize.py tests/test_main.py -v

# Square sandbox tests (requires network + Square sandbox credentials in dev_options.json)
pytest -m sandbox -v

# End-to-end tests (requires mosquitto on PATH + Square sandbox credentials)
pytest -m e2e -v

# All non-network tests
pytest -m "not sandbox and not e2e" -v
```

---

## MQTT topic scheme

All topics are derived from `home_id` and `charger_id`:

```
ev/charger/{home_id}/{charger_id}/booking/request_session      ← app publishes
ev/charger/{home_id}/{charger_id}/booking/response             ← app subscribes
ev/charger/{home_id}/{charger_id}/booking/authorize_session    ← app publishes
ev/charger/{home_id}/{charger_id}/booking/authorize_session/response ← app subscribes
ev/charger/{home_id}/{charger_id}/booking/finalize_session     ← app subscribes
```
