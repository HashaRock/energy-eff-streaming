#!/usr/bin/env bash
# Install client-side dependencies.
# Detects Linux (Ubuntu) or macOS and installs accordingly.
set -euo pipefail

OS="$(uname -s)"
echo "=== EEC Client Setup (${OS}) ==="

if [ "${OS}" = "Linux" ]; then
    echo "--- Linux path ---"
    apt-get update -qq
    apt-get install -y --no-install-recommends \
        curl ffmpeg python3 python3-pip nodejs npm openssl

    # curl with HTTP/3 — Ubuntu 24.04's curl 8.x ships with ngtcp2 support.
    # On older Ubuntu (22.04), build curl from source with ngtcp2:
    if curl --version | grep -q "HTTP3"; then
        echo "curl already has HTTP/3 support"
    else
        echo "[WARN] System curl lacks HTTP/3. Install Ubuntu 24.04 or build curl from source."
        echo "       See: https://curl.se/docs/http3.html (ngtcp2 method)"
    fi

    # Verify RAPL
    modprobe intel_rapl_common 2>/dev/null || true
    if ls /sys/class/powercap/intel-rapl 2>/dev/null | grep -q rapl; then
        echo "RAPL available"
    else
        echo "[WARN] /sys/class/powercap/intel-rapl not found — check CPU support"
    fi

elif [ "${OS}" = "Darwin" ]; then
    echo "--- macOS path ---"
    if ! command -v brew &>/dev/null; then
        echo "[ERROR] Homebrew not found. Install from https://brew.sh"
        exit 1
    fi
    brew install ffmpeg 2>/dev/null || true

    # Check powermetrics
    if [ -x "/usr/bin/powermetrics" ]; then
        echo "powermetrics found (requires sudo to run)"
    else
        echo "[WARN] powermetrics not found — energy measurement will not work on macOS"
    fi

    # curl on macOS (LibreSSL) does NOT support HTTP/3
    if curl --version | grep -q "HTTP3"; then
        echo "curl has HTTP/3 support"
    else
        echo "[WARN] System curl lacks HTTP/3 (LibreSSL limitation)."
        echo "       For H3 tests, use a Linux client or:"
        echo "       brew install curl  # may include ngtcp2 on newer formulae"
    fi
fi

# ── Common: Python packages ────────────────────────────────────────────────────
echo "--- installing Python packages ---"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "${SCRIPT_DIR}")"
pip3 install --quiet -r "${REPO_ROOT}/requirements.txt"
echo "  Python packages installed"

# ── Common: webtorrent-cli ─────────────────────────────────────────────────────
echo "--- installing webtorrent-cli ---"
npm install -g webtorrent-cli 2>/dev/null || true
echo "webtorrent: $(webtorrent --version 2>/dev/null || echo 'not found')"

echo ""
echo "=== Client setup complete ==="
echo "Configure the server address in orchestrator/config.py, then:"
echo "  python3 orchestrator/orchestrator.py --dry-run   # verify matrix"
echo "  python3 orchestrator/orchestrator.py             # run experiments"
