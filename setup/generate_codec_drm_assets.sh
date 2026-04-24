#!/usr/bin/env bash
# generate_codec_drm_assets.sh
# Generates the new HLS codec and DRM variant assets required by
# orchestrator_codec_drm.py. Safe to re-run: existing outputs are skipped.
#
# Outputs created
# ───────────────
# server/assets/hls/av1_plain/     AV1 fMP4 HLS segments (unencrypted)
# server/assets/hls/aes128_fmp4/   H.264 fMP4 HLS segments (AES-128-CBC encrypted)
# server/assets/keys/aes128.bin    16-byte raw AES-128 key for the above
# server/assets/keys/cenc.bin      16-byte raw key for CENC/CTR emulation
# server/assets/keys/cbcs.bin      16-byte raw key for CBCS/CBC emulation
# server/assets/keys/manifest.json key metadata consumed by driver_hls_drm.py
#
# Requirements: ffmpeg (with libx264 + libsvtav1 or libaom-av1), openssl, xxd
# Optional:     packager (shaka-packager) — only needed if you extend to true CENC
#
# Usage:
#   bash setup/generate_codec_drm_assets.sh [--force]
#   --force  regenerate all outputs even if they already exist

set -euo pipefail

FORCE=0
for arg in "$@"; do
  [[ "$arg" == "--force" ]] && FORCE=1
done

# ── Resolve project root ───────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ASSETS="$PROJECT_ROOT/server/assets"
TLS_DIR="$PROJECT_ROOT/server/tls"
HLS_DIR="$ASSETS/hls"
KEYS_DIR="$ASSETS/keys"
RAW_DIR="$ASSETS/raw"
BULK_DIR="$ASSETS/bulk"

# ── Colour helpers ─────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[generate]${NC} $*"; }
warn()  { echo -e "${YELLOW}[generate]${NC} $*"; }
error() { echo -e "${RED}[generate]${NC} $*" >&2; }

# ── Dependency checks ──────────────────────────────────────────────────────────
info "Checking dependencies..."
for cmd in ffmpeg openssl xxd; do
  if ! command -v "$cmd" &>/dev/null; then
    error "Required tool not found: $cmd"
    exit 1
  fi
done

# Check for AV1 encoder (prefer SVT-AV1 for speed; libaom as fallback)
AV1_ENCODER=""
if ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libsvtav1; then
  AV1_ENCODER="libsvtav1"
elif ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libaom-av1; then
  AV1_ENCODER="libaom-av1"
else
  warn "No AV1 encoder found in ffmpeg. AV1 fMP4 HLS will be skipped."
  warn "Install ffmpeg with --with-svt-av1 or --with-libaom."
fi

# ── Source file selection ──────────────────────────────────────────────────────
# Prefer the original BBB source; fall back to the pre-encoded bulk MP4.
if [[ -f "$RAW_DIR/bbb_source.mp4" ]]; then
  H264_SRC="$RAW_DIR/bbb_source.mp4"
  info "Using source: $H264_SRC"
else
  H264_SRC="$BULK_DIR/test.mp4"
  warn "bbb_source.mp4 not found; using bulk/test.mp4 as H.264 source."
fi

# AV1 source: use the pre-encoded bulk AV1 file if available (avoids re-encode).
AV1_SRC=""
if [[ -f "$BULK_DIR/test_av1.mp4" ]]; then
  AV1_SRC="$BULK_DIR/test_av1.mp4"
  info "Using AV1 source: $AV1_SRC (stream-copy; no re-encode)"
fi

# ── Helpers ────────────────────────────────────────────────────────────────────
mkdir -p "$KEYS_DIR"

needs_rebuild() {
  local dir="$1"
  local marker="$dir/master.m3u8"
  [[ "$FORCE" == "1" ]] && return 0  # always rebuild with --force
  [[ ! -f "$marker" ]]               # rebuild if master.m3u8 missing
}

# ── 1. AV1 fMP4 HLS ───────────────────────────────────────────────────────────
AV1_DIR="$HLS_DIR/av1_plain"
if [[ -n "$AV1_ENCODER" ]]; then
  if needs_rebuild "$AV1_DIR"; then
    info "Generating AV1 fMP4 HLS → $AV1_DIR"
    mkdir -p "$AV1_DIR"

    if [[ -n "$AV1_SRC" ]]; then
      # Stream-copy AV1 from pre-encoded bulk file into fMP4 HLS
      ffmpeg -y -loglevel warning \
        -i "$AV1_SRC" \
        -c copy \
        -f hls \
        -hls_time 8 \
        -hls_segment_type fmp4 \
        -hls_fmp4_init_filename init.mp4 \
        -hls_segment_filename "$AV1_DIR/seg_%04d.m4s" \
        -hls_list_size 0 \
        -hls_flags independent_segments \
        "$AV1_DIR/master.m3u8"
    else
      # Re-encode from H.264 source to AV1 fMP4 HLS (slower)
      warn "No pre-encoded AV1 source found; re-encoding (this may take several minutes)."
      if [[ "$AV1_ENCODER" == "libsvtav1" ]]; then
        AV1_OPTS="-c:v libsvtav1 -crf 35 -preset 8"
      else
        AV1_OPTS="-c:v libaom-av1 -crf 35 -cpu-used 8"
      fi
      ffmpeg -y -loglevel warning \
        -i "$H264_SRC" \
        $AV1_OPTS -c:a aac -b:a 128k \
        -f hls \
        -hls_time 8 \
        -hls_segment_type fmp4 \
        -hls_fmp4_init_filename init.mp4 \
        -hls_segment_filename "$AV1_DIR/seg_%04d.m4s" \
        -hls_list_size 0 \
        -hls_flags independent_segments \
        "$AV1_DIR/master.m3u8"
    fi
    info "AV1 fMP4 HLS: $(ls "$AV1_DIR"/*.m4s 2>/dev/null | wc -l | tr -d ' ') segments"
  else
    info "AV1 fMP4 HLS already exists — skipping (use --force to rebuild)"
  fi
else
  warn "Skipping AV1 fMP4 HLS (no AV1 encoder available)"
fi

# ── 2. Expose the existing AES-128 TS key at /keys/aes128.bin ─────────────────
# NOTE: ffmpeg does not support AES-128 encrypted fMP4 HLS output ("Encrypted
# fmp4 not yet supported"). The DRM comparison therefore uses the pre-existing
# hls/encrypted/ TS segments (generated by setup/generate_assets.sh) for all
# three decrypt tests (aes128_decrypt, cenc, cbcs). The key for those segments
# is already at server/tls/hls.key; we copy it to the /keys/ endpoint so the
# driver can fetch it over HTTP as a mock license acquisition.
AES_KEY_BIN="$TLS_DIR/hls.key"   # existing key (16 bytes raw)
if [[ ! -f "$AES_KEY_BIN" ]]; then
  error "hls.key not found at $AES_KEY_BIN"
  error "Run setup/generate_assets.sh first to generate the base HLS assets."
  exit 1
fi
AES_KEY_HEX="$(xxd -p -c 32 "$AES_KEY_BIN")"
# IV is embedded in hls/encrypted/master.m3u8 as IV=0x...; read it here
AES_IV_HEX="$(grep -oP '(?<=IV=0x)[0-9a-fA-F]+' "$HLS_DIR/encrypted/master.m3u8" 2>/dev/null | head -1 || echo '')"
info "Using existing AES-128 key from $AES_KEY_BIN for /keys/aes128.bin"
info "  (DRM tests use hls/encrypted/ TS segments — no new encrypted assets needed)"

# ── 3. Generate CENC and CBCS key files ───────────────────────────────────────
# Independent random 16-byte keys for the CENC/CTR and CBCS/CBC cipher-overhead
# emulations. The driver applies these cipher modes to the aes128_ts segments to
# measure CPU cost of CTR vs CBC vs no-decrypt, without needing real CENC packaging.

CENC_KEY_BIN="$TLS_DIR/cenc.key"
CBCS_KEY_BIN="$TLS_DIR/cbcs.key"

if [[ ! -f "$CENC_KEY_BIN" || "$FORCE" == "1" ]]; then
  openssl rand 16 > "$CENC_KEY_BIN"
  info "Generated CENC key → $CENC_KEY_BIN"
fi
if [[ ! -f "$CBCS_KEY_BIN" || "$FORCE" == "1" ]]; then
  openssl rand 16 > "$CBCS_KEY_BIN"
  info "Generated CBCS key → $CBCS_KEY_BIN"
fi

CENC_KEY_HEX="$(xxd -p -c 32 "$CENC_KEY_BIN")"
CBCS_KEY_HEX="$(xxd -p -c 32 "$CBCS_KEY_BIN")"
ZERO_IV="00000000000000000000000000000000"

# ── 4. Populate /keys/ directory (nginx mock license endpoint) ─────────────────
info "Populating keys/ directory..."
cp "$AES_KEY_BIN" "$KEYS_DIR/aes128.bin"
cp "$CENC_KEY_BIN" "$KEYS_DIR/cenc.bin"
cp "$CBCS_KEY_BIN" "$KEYS_DIR/cbcs.bin"

# ── 5. Write manifest.json ─────────────────────────────────────────────────────
# driver_hls_drm.py reads this for IV bytes for CENC/CBCS (no IV in playlist).
# aes128_ts IV comes from #EXT-X-KEY in the playlist itself, so it's listed
# here only for reference; the driver always reads it from the playlist live.
MANIFEST="$KEYS_DIR/manifest.json"
cat > "$MANIFEST" <<EOF
{
  "aes128_ts": {
    "key_hex": "${AES_KEY_HEX}",
    "iv_hex":  "${AES_IV_HEX:-$ZERO_IV}"
  },
  "cenc": {
    "key_hex": "${CENC_KEY_HEX}",
    "iv_hex":  "${ZERO_IV}"
  },
  "cbcs": {
    "key_hex": "${CBCS_KEY_HEX}",
    "iv_hex":  "${ZERO_IV}"
  }
}
EOF
info "Manifest written → $MANIFEST"

# ── 6. Quick sanity check ─────────────────────────────────────────────────────
echo ""
info "Asset inventory:"
for variant_dir in plain encrypted av1_plain; do
  dir="$HLS_DIR/$variant_dir"
  if [[ -d "$dir" ]]; then
    n_segs=$(find "$dir" -maxdepth 1 \( -name "*.m4s" -o -name "*.ts" \) 2>/dev/null | wc -l | tr -d ' ')
    echo "  hls/$variant_dir/   master.m3u8 + $n_segs segments"
  else
    warn "  hls/$variant_dir/   MISSING (run --force or check errors above)"
  fi
done
echo "  keys/   $(ls "$KEYS_DIR" | tr '\n' ' ')"

echo ""
info "Done. You can now run:"
info "  python3 orchestrator/orchestrator_codec_drm.py --dry-run"
info "  python3 orchestrator/orchestrator_codec_drm.py --group all"
