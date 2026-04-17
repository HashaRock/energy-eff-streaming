#!/usr/bin/env bash
# Generate a self-signed TLS certificate + key for the EEC test server.
# Replace SERVER_IP with the actual server IP before running.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_IP="${1:-192.168.1.100}"

echo "[gen_certs] generating self-signed cert for IP: ${SERVER_IP}"

openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout "${SCRIPT_DIR}/server.key" \
  -out    "${SCRIPT_DIR}/server.crt" \
  -days 365 \
  -subj "/CN=eec-server" \
  -addext "subjectAltName=IP:${SERVER_IP},DNS:localhost"

echo "[gen_certs] done."
echo "  cert: ${SCRIPT_DIR}/server.crt"
echo "  key:  ${SCRIPT_DIR}/server.key"
echo ""
echo "To trust this cert on the client (optional, avoids --insecure):"
echo "  macOS: sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain ${SCRIPT_DIR}/server.crt"
echo "  Linux: sudo cp ${SCRIPT_DIR}/server.crt /usr/local/share/ca-certificates/eec-server.crt && sudo update-ca-certificates"
