#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# run_dev.sh  –  Launch the EV Charger Portal locally via uvicorn
#
# Usage:
#   ./run_dev.sh                       # uses tests/dev_options.json
#   EV_OPTIONS_PATH=/my/opts.json ./run_dev.sh
#
# Prerequisites:
#   pip install -r requirements.txt
#
# You need a local MQTT broker running on the host/port in dev_options.json.
# Quick option with Docker:
#   docker run -d --name mosquitto -p 1883:1883 eclipse-mosquitto \
#       mosquitto -c /mosquitto-no-auth.conf
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export EV_OPTIONS_PATH="${EV_OPTIONS_PATH:-${SCRIPT_DIR}/tests/dev_options.json}"
export EV_DB_PATH="${EV_DB_PATH:-${SCRIPT_DIR}/tests/ev_portal.db}"

if [[ ! -f "$EV_OPTIONS_PATH" ]]; then
    echo "ERROR: options file not found: $EV_OPTIONS_PATH" >&2
    exit 1
fi

echo "-------------------------------------------------------"
echo "  EV Charger Portal – dev server"
echo "  Options : $EV_OPTIONS_PATH"
echo "  DB      : $EV_DB_PATH"
echo "  URL     : http://localhost:8080"
echo "  Health  : http://localhost:8080/health"
echo "  Start   : http://localhost:8080/start?charger_id=test"
echo "-------------------------------------------------------"

exec uvicorn main:app \
    --host 0.0.0.0 \
    --port 8080 \
    --reload \
    --log-level info
