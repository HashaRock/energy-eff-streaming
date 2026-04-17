#!/usr/bin/env bash
# Generate all test media assets for the EEC experiment.
# Run this once on the Ubuntu server before starting experiments.
#
# Requirements: ffmpeg, shaka-packager (packager binary in PATH), openssl, webtorrent-cli
# Output: /opt/eec/server/assets/
#
# Usage:
#   ./setup/generate_assets.sh [SERVER_IP]
#   SERVER_IP is used in the HLS key URI for encrypted playlists.

set -euo pipefail

SERVER_IP="${1:-192.168.1.100}"
ASSETS_DIR="/opt/eec/server/assets"
TLS_DIR="/opt/eec/server/tls"

mkdir -p "${ASSETS_DIR}"/{raw,bulk,hls/{plain,encrypted},torrent}
mkdir -p "${TLS_DIR}"

echo "=== Step 1: Download source video (Big Buck Bunny 5-min 1080p) ==="
BBB_URL="https://download.blender.org/demo/movies/BBB/bbb_sunflower_1080p_30fps_normal.mp4.zip"
# Use the freely available 5-min clip from Blender
BBB_MP4="${ASSETS_DIR}/raw/bbb_source.mp4"
if [ ! -f "${BBB_MP4}" ]; then
    # Try direct MP4 link first (Blender Foundation CDN)
    DIRECT_URL="https://download.blender.org/peach/bigbuckbunny_movies/big_buck_bunny_1080p_h264.mov"
    wget -q --show-progress -O "${BBB_MP4}" "${DIRECT_URL}" || {
        echo "[WARN] Primary download failed. Trying mirror..."
        wget -q --show-progress -O "${BBB_MP4}" \
          "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4"
    }
else
    echo "  source already exists, skipping download"
fi

# Trim to 5 minutes to keep file sizes manageable
BBB_5MIN="${ASSETS_DIR}/raw/bbb_5min.mp4"
if [ ! -f "${BBB_5MIN}" ]; then
    echo "  trimming to 5 minutes..."
    ffmpeg -i "${BBB_MP4}" -t 300 -c copy "${BBB_5MIN}" -y -loglevel warning
fi

echo "=== Step 2: Transcode to test formats ==="

# H.264/AAC MP4 at 4 Mbps (the baseline for bulk + HLS source)
if [ ! -f "${ASSETS_DIR}/bulk/test.mp4" ]; then
    echo "  transcoding → test.mp4 (H.264, 4 Mbps)"
    ffmpeg -i "${BBB_5MIN}" \
      -c:v libx264 -b:v 4M -maxrate 4.5M -bufsize 8M -preset medium \
      -c:a aac -b:a 128k \
      "${ASSETS_DIR}/bulk/test.mp4" -y -loglevel warning
fi

# MKV container (same streams, just repackaged — tests container overhead)
if [ ! -f "${ASSETS_DIR}/bulk/test.mkv" ]; then
    echo "  repackaging → test.mkv"
    ffmpeg -i "${ASSETS_DIR}/bulk/test.mp4" \
      -c copy "${ASSETS_DIR}/bulk/test.mkv" -y -loglevel warning
fi

# AV1 in MP4 (SVT-AV1 at matching perceptual quality)
if [ ! -f "${ASSETS_DIR}/bulk/test_av1.mp4" ]; then
    echo "  transcoding → test_av1.mp4 (SVT-AV1)"
    # CRF 35 ≈ similar quality to H.264 at 4 Mbps for 1080p
    ffmpeg -i "${BBB_5MIN}" \
      -c:v libsvtav1 -crf 35 -preset 8 \
      -c:a aac -b:a 128k \
      "${ASSETS_DIR}/bulk/test_av1.mp4" -y -loglevel warning
fi

echo "=== Step 3: Package HLS (plain, unencrypted) ==="
if [ ! -f "${ASSETS_DIR}/hls/plain/master.m3u8" ]; then
    packager \
      "in=${ASSETS_DIR}/bulk/test.mp4,stream=video,output=${ASSETS_DIR}/hls/plain/seg_%04d.ts,playlist_name=${ASSETS_DIR}/hls/plain/stream.m3u8" \
      --hls_master_playlist_output "${ASSETS_DIR}/hls/plain/master.m3u8" \
      --segment_duration 6 \
      --generate_static_live_mpd \
      --mpd_output "${ASSETS_DIR}/hls/plain/stream.mpd"
else
    echo "  plain HLS already exists, skipping"
fi

echo "=== Step 4: Package HLS (AES-128 encrypted) ==="
if [ ! -f "${ASSETS_DIR}/hls/encrypted/master.m3u8" ]; then
    # Generate 16-byte AES key
    openssl rand 16 > "${TLS_DIR}/hls.key"
    echo "  HLS AES key: ${TLS_DIR}/hls.key"

    KEY_URI="http://${SERVER_IP}:8080/hls.key"

    packager \
      "in=${ASSETS_DIR}/bulk/test.mp4,stream=video,output=${ASSETS_DIR}/hls/encrypted/seg_%04d.ts,playlist_name=${ASSETS_DIR}/hls/encrypted/stream.m3u8" \
      --hls_master_playlist_output "${ASSETS_DIR}/hls/encrypted/master.m3u8" \
      --segment_duration 6 \
      --hls_key_path "${TLS_DIR}/hls.key" \
      --hls_key_uri "${KEY_URI}"
else
    echo "  encrypted HLS already exists, skipping"
fi

echo "=== Step 5: Create torrent and start seeding ==="
MAGNET_FILE="${ASSETS_DIR}/torrent/magnet.txt"
# Copy test file to torrent directory
cp "${ASSETS_DIR}/bulk/test.mp4" "${ASSETS_DIR}/torrent/test.mp4"

if [ ! -f "${MAGNET_FILE}" ]; then
    echo "  creating torrent and capturing magnet link..."
    # webtorrent outputs magnet link to stdout; capture it
    MAGNET=$(webtorrent seed "${ASSETS_DIR}/torrent/test.mp4" 2>/dev/null | grep -o 'magnet:?[^ ]*' | head -1 || true)
    if [ -n "${MAGNET}" ]; then
        echo "${MAGNET}" > "${MAGNET_FILE}"
        echo "  magnet: ${MAGNET}"
    else
        echo "  [WARN] could not capture magnet link automatically."
        echo "  Run manually: webtorrent seed ${ASSETS_DIR}/torrent/test.mp4"
        echo "  Then paste the magnet link into ${MAGNET_FILE}"
    fi
else
    echo "  torrent magnet already exists: $(cat ${MAGNET_FILE})"
fi

echo ""
echo "=== Asset generation complete ==="
du -sh "${ASSETS_DIR}"/bulk/* "${ASSETS_DIR}"/hls/plain/ "${ASSETS_DIR}"/hls/encrypted/ 2>/dev/null || true
echo ""
echo "Start the seeder before running experiments:"
echo "  webtorrent seed ${ASSETS_DIR}/torrent/test.mp4 --keep-seeding &"
