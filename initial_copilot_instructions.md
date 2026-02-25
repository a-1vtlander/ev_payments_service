You are working in a NEW Git repository opened in this VS Code session.

This repository is a Home Assistant Supervisor add-on repository meant to provide a webserve interface
to enable an EV charger after payment authorization is handled via square. The service is only accessible
on the local lan and must be anonymously accessible. 

Repository Structure Rules

- The repository root is: ev_payment_service/
- Home Assistant Supervisor scans the repo root for add-ons by checking:
    <repo_root>/*/config.yaml
- Therefore the add-on must be created at:
    ev_payment_service/ev_portal/config.yaml

Do NOT create an additional "addons/" directory.
The add-on folder must be directly under the repository root.

Stage 1 Objective

Create a minimal Home Assistant Supervisor add-on named:

    EV Charger Portal

This add-on must:

- Run a LAN-local HTTP server on port 8080
- Be accessible anonymously on the local network
- Communicate with Home Assistant ONLY via MQTT
- Not use HA REST APIs
- Not use HA Ingress
- Not require authentication
- Not include Square logic
- Not include persistence yet

Functional Requirements (Stage 1 Only)

HTTP Endpoints:

1) GET /health
   - Return HTTP 200
   - Response body: "ok" (plain text)

2) GET /start?charger_id=<id>
   - If charger_id is missing → return HTTP 400
   - If provided:
       - Publish MQTT message:
           Topic: ev/<charger_id>/start
           Payload JSON:
             {
               "charger_id": "<id>",
               "timestamp": "<ISO8601>"
             }
       - Return simple HTML:
           "<h1>EV session requested for charger <id></h1>"

MQTT Requirements

- Use paho-mqtt
- Connect on container startup
- Auto-reconnect if connection drops
- Log connection events
- Log each publish event

Configuration Requirements

MQTT settings must be configurable in Home Assistant:

- mqtt_host (string)
- mqtt_port (int, default 1883)
- mqtt_username (string, optional)
- mqtt_password (string, optional)

These must be defined in ev_portal/config.yaml under schema.

Supervisor will provide configuration at runtime in:

    /data/options.json

The add-on must read MQTT configuration from /data/options.json.

Do NOT hardcode credentials.
Do NOT commit secrets.

Implementation Requirements

- Language: Python 3
- Framework: FastAPI + uvicorn
- MQTT: paho-mqtt
- Minimal dependencies only
- Bind to 0.0.0.0:8080
- Log to stdout only

Required Directory Structure

ev_payment_service/
  ev_portal/
    config.yaml
    Dockerfile
    rootfs/
      app/
        main.py
        requirements.txt

Add-on Behavior

- The container must start automatically and launch the web server.
- The add-on must appear in Home Assistant after adding the repo.
- The add-on must build and start successfully.

Deliverables

- Create all required files with full contents.
- List files created.
- Provide installation steps:
    Settings → Add-ons → Add-on Store → Repositories → add repo URL
    Install “EV Charger Portal”
- Provide minimal test steps:
    curl http://<ha-ip>:8080/health
    curl http://<ha-ip>:8080/start?charger_id=test

Stop after Stage 1.
Do not implement booking logic, Square integration, or persistence as those requirements are not clear. 