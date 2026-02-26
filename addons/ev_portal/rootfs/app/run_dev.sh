#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# run_dev.sh  –  Launch the EV Charger Portal locally (guest + admin servers)
#
# Usage:
#   ./run_dev.sh                       # uses tests/dev_options.json
#   EV_OPTIONS_PATH=/my/opts.json ./run_dev.sh
#
# Prerequisites:
#   pip install -r requirements.txt
#   cryptography package is required for self-signed TLS cert generation.
#
# You need a local MQTT broker running on the host/port in dev_options.json.
# Quick option with Docker:
#   docker run -d --name mosquitto -p 1883:1883 eclipse-mosquitto \
#       mosquitto -c /mosquitto-no-auth.conf
#
# Guest portal : http://localhost:8090
# Admin portal : https://localhost:8091/admin  (accept self-signed cert)
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export EV_OPTIONS_PATH="${EV_OPTIONS_PATH:-${SCRIPT_DIR}/tests/dev_options.json}"
export EV_DB_PATH="${EV_DB_PATH:-${SCRIPT_DIR}/tests/ev_portal.db}"
export EV_TLS_DIR="${EV_TLS_DIR:-${SCRIPT_DIR}/tests/tls}"

if [[ ! -f "$EV_OPTIONS_PATH" ]]; then
    echo "ERROR: options file not found: $EV_OPTIONS_PATH" >&2
    exit 1
fi

echo "-------------------------------------------------------"
echo "  EV Charger Portal – dev server"
echo "  Options  : $EV_OPTIONS_PATH"
echo "  DB       : $EV_DB_PATH"
echo "  TLS dir  : $EV_TLS_DIR"
echo "  Guest    : http://localhost:8090"
echo "  Admin    : https://localhost:8091/admin"
echo "  Health   : http://localhost:8090/health"
echo "-------------------------------------------------------"

exec python "${SCRIPT_DIR}/serve.py"
