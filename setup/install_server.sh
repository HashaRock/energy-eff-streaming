#!/usr/bin/env bash
# Install all server-side dependencies on Ubuntu 22.04/24.04.
# Run as root or with sudo from the server machine.
set -euo pipefail

echo "=== EEC Server Setup ==="

# ── System deps ────────────────────────────────────────────────────────────────
apt-get update -qq
apt-get install -y --no-install-recommends \
    build-essential libssl-dev libpcre2-dev zlib1g-dev \
    ffmpeg openssl wget curl git \
    python3 python3-pip python3-venv \
    nodejs npm \
    ca-certificates gnupg lsb-release

# ── nginx with HTTP/3 (QUIC) ───────────────────────────────────────────────────
# The mainline nginx 1.25+ supports HTTP/3 via --with-http_v3_module
# but requires a QUIC-capable TLS library (BoringSSL or quiche).
# We use the nginx mainline packages which include QUIC on Ubuntu 22.04+.
echo "--- installing nginx with QUIC support ---"
if ! nginx -v 2>&1 | grep -q "nginx/1.2[5-9]"; then
    # Add nginx mainline PPA
    curl -s https://nginx.org/keys/nginx_signing.key | apt-key add -
    echo "deb http://nginx.org/packages/mainline/ubuntu $(lsb_release -cs) nginx" \
        > /etc/apt/sources.list.d/nginx.list
    apt-get update -qq
    apt-get install -y nginx
fi
echo "nginx version: $(nginx -v 2>&1)"

# ── shaka-packager ─────────────────────────────────────────────────────────────
echo "--- installing shaka-packager ---"
PACKAGER_URL="https://github.com/shaka-project/shaka-packager/releases/latest/download/packager-linux-x64"
if ! command -v packager &>/dev/null; then
    wget -q "${PACKAGER_URL}" -O /usr/local/bin/packager
    chmod +x /usr/local/bin/packager
fi
echo "packager version: $(packager --version 2>&1 | head -1)"

# ── webtorrent-cli ─────────────────────────────────────────────────────────────
echo "--- installing webtorrent-cli ---"
npm install -g webtorrent-cli 2>/dev/null || true
echo "webtorrent version: $(webtorrent --version 2>/dev/null || echo 'not found')"

# ── Python packages ────────────────────────────────────────────────────────────
echo "--- installing Python packages ---"
pip3 install --quiet paramiko

# ── RAPL permissions (non-root access) ────────────────────────────────────────
echo "--- configuring RAPL non-root access ---"
# Load the RAPL kernel module if not already loaded
modprobe intel_rapl_common 2>/dev/null || modprobe intel-rapl 2>/dev/null || true

# Create a udev rule so energy_uj files are world-readable after boot
cat > /etc/udev/rules.d/99-rapl.rules <<'EOF'
SUBSYSTEM=="powercap", ACTION=="add", RUN+="/bin/chmod -R o+r /sys/class/powercap"
EOF
# Apply immediately for this session
chmod -R o+r /sys/class/powercap/ 2>/dev/null || true
echo "  RAPL domains readable:"
ls /sys/class/powercap/ 2>/dev/null | head -5

# ── Project directory structure ────────────────────────────────────────────────
echo "--- creating /opt/eec directory structure ---"
mkdir -p /opt/eec/server/{assets/{raw,bulk,hls/{plain,encrypted},torrent},nginx,tls}
# Copy server scripts if this repo is present
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "${SCRIPT_DIR}")"

if [ -d "${REPO_ROOT}/server" ]; then
    cp "${REPO_ROOT}/server/measure_server.py" /opt/eec/server/
    cp "${REPO_ROOT}/server/server_agent.py"   /opt/eec/server/
    cp "${REPO_ROOT}/server/nginx"/*.conf      /opt/eec/server/nginx/
    echo "  copied server scripts to /opt/eec/server/"
fi

# ── Systemd service for server_agent ──────────────────────────────────────────
cat > /etc/systemd/system/eec-agent.service <<EOF
[Unit]
Description=EEC Server Agent
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/eec/server/server_agent.py --port 9999
Restart=always
Environment=EEC_NGINX_CONF_DIR=/opt/eec/server/nginx

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable eec-agent
echo "  eec-agent service installed (start with: systemctl start eec-agent)"

echo ""
echo "=== Server setup complete ==="
echo "Next steps:"
echo "  1. Copy server TLS certs: ./server/tls/gen_certs.sh <SERVER_IP>"
echo "  2. Generate test assets:  ./setup/generate_assets.sh <SERVER_IP>"
echo "  3. Start agent:           systemctl start eec-agent"
echo "  4. Start nginx:           sudo nginx -c /opt/eec/server/nginx/nginx_h1.conf"
