#!/usr/bin/env bash
# Trust the self-signed admin cert in macOS Keychain (dev only).
# Run once; removes the need to bypass the browser cert warning.
CERT="/Users/davida/HA-workspaces/add_ons/ev_payments_service/ev_portal/rootfs/app/tests/tls/admin.crt"
sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain "$CERT"
echo "Cert trusted. Restart your browser."
