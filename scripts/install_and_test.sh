#!/usr/bin/env bash
set -euo pipefail

# install_and_test.sh
# Create a fresh venv, install deps, run tests, and perform a small server smoke-test.

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
APP_DIR="$ROOT_DIR/addons/ev_portal/rootfs/app"
VENV_DIR="$ROOT_DIR/.venv_install_test"

echo "Using app dir: $APP_DIR"

if [ -d "$VENV_DIR" ]; then
  echo "Removing existing venv at $VENV_DIR"
  rm -rf "$VENV_DIR"
fi

python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
pip install --upgrade pip setuptools wheel

echo "Installing production requirements..."
pip install -r "$APP_DIR/requirements.txt"

if [ -f "$APP_DIR/requirements-test.txt" ]; then
  echo "Installing test requirements..."
  pip install -r "$APP_DIR/requirements-test.txt"
fi

echo "Running pytest (unit + integration tests; skipping sandbox/e2e)..."
cd "$APP_DIR"
"$VENV_DIR/bin/pytest" -q -m "not sandbox and not e2e" || {
  echo "pytest failed" >&2
  deactivate
  exit 2
}

echo "Starting dev server (background)..."
./run_dev.sh > /tmp/ev_portal_dev.log 2>&1 &
SERVER_PID=$!
echo $SERVER_PID > /tmp/ev_portal_dev.pid
sleep 3

echo "Checking guest /start endpoint..."
curl -sS -D /tmp/ev_portal_curl_headers.txt -o /tmp/ev_portal_start.html http://127.0.0.1:8090/start || true
echo "Guest /start response saved to /tmp/ev_portal_start.html"

echo "Checking admin /admin endpoint (TLS, skipping cert validation)..."
curl -k -sS -D /tmp/ev_portal_admin_headers.txt -o /tmp/ev_portal_admin.html https://127.0.0.1:8091/admin || true
echo "Admin /admin response saved to /tmp/ev_portal_admin.html"

echo "Stopping dev server (PID $SERVER_PID)"
kill $SERVER_PID || true
sleep 1
if ps -p $SERVER_PID >/dev/null 2>&1; then
  kill -9 $SERVER_PID || true
fi

deactivate || true
echo "Finished. Logs: /tmp/ev_portal_dev.log; start page: /tmp/ev_portal_start.html; admin page: /tmp/ev_portal_admin.html"
