#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# trust_dev_cert.sh  –  Trust the local dev self-signed certs on macOS
#
# Run this ONCE after first running ./run_dev.sh (which generates the certs).
# After trusting, Chrome / Safari / curl will accept https://localhost:8090
# and https://localhost:8091 without warnings.
#
# Usage:
#   cd app/tests/tls
#   ./trust_dev_cert.sh
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$(uname)" != "Darwin" ]]; then
    echo "This script is macOS-only."
    echo "On Linux: sudo cp *.crt /usr/local/share/ca-certificates/ && sudo update-ca-certificates"
    exit 1
fi

trust_cert() {
    local label="$1"
    local cert="$2"

    if [[ ! -f "$cert" ]]; then
        echo "SKIP  $label: $cert not found (run ./run_dev.sh first to generate it)"
        return
    fi

    echo "Trusting $label ($cert) in login keychain..."
    # -d  = add to keychain database
    # -r trustRoot = mark as trusted root CA
    # -k  = target keychain (login keychain; no sudo required)
    security add-trusted-cert \
        -d \
        -r trustRoot \
        -k "${HOME}/Library/Keychains/login.keychain-db" \
        "$cert"
    echo "  OK: $label trusted."
}

trust_cert "guest dev cert  (port 8090)" "${SCRIPT_DIR}/guest.crt"
trust_cert "admin dev cert  (port 8091)" "${SCRIPT_DIR}/admin.crt"

echo ""
echo "Done. Restart Chrome/Safari for the change to take effect."
echo ""
echo "  Guest portal : https://localhost:8090"
echo "  Admin portal : https://localhost:8091/admin"
echo ""
echo "To REMOVE trust later:"
echo "  security remove-trusted-cert ${SCRIPT_DIR}/guest.crt"
echo "  security remove-trusted-cert ${SCRIPT_DIR}/admin.crt"
