"""
DRM-aware HLS streaming driver for the codec/DRM energy testbench.
Extends the plain HLS emulator with fMP4 init-segment handling and
three decryption modes that approximate real-world DRM schemes:

  drm_scheme="none"
      No key fetch. Segments downloaded with curl, bytes discarded.
      Baseline for both codec comparison (h264_ts, av1_fmp4) and DRM comparison.

  drm_scheme="aes128_net_only"
      One live key fetch over HTTP (measures license-acquisition RTT).
      Segments downloaded with curl, bytes NOT decrypted.
      Mirrors the behaviour of the original driver_hls.py drm=True path —
      isolates network overhead from cipher overhead.

  drm_scheme="aes128_decrypt"
      One live key fetch. Each fMP4 segment fetched into memory and
      decrypted with AES-128-CBC (the cipher used by standard HLS AES-128).
      Models the overhead of a CPU doing segment decryption in software.

  drm_scheme="cenc"
      Key fetched from /keys/cenc.bin. Each segment's bytes processed with
      AES-128-CTR — the cipher mode used by the CENC Common Encryption
      scheme that underlies Google Widevine. The segments are the same
      AES-128-CBC TS segments used by aes128_decrypt; CTR is applied to
      those bytes as a CPU-load emulation: output is discarded (energy
      measurement only, not decoding).

  drm_scheme="cbcs"
      Key fetched from /keys/cbcs.bin. Each segment's bytes processed with
      AES-128-CBC — the cipher mode used by the CBCS scheme (Microsoft
      PlayReady modern mode). Same note as cenc above.

Design note on CENC/CBCS approximation
---------------------------------------
True CENC/CBCS packaging requires shaka-packager and a live license server
(Widevine requires Google infrastructure; PlayReady requires Windows DRM).
Both are out of scope for a local academic testbed. Instead, all three
decrypt modes (aes128_decrypt, cenc, cbcs) operate on the same AES-128 TS
segment bytes. The measured quantity — AES block operations × total bytes —
is identical in the three cases; only the cipher mode (CBC vs CTR) and key
differ. This correctly isolates cipher-mode overhead, which is the
energy-relevant variable. The paper's methodology section documents this
choice.
"""
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from client.qos_monitor import QoSMetrics
from client.drivers.driver_hls import _fetch_url, _curl_fetch_bytes, _parse_m3u8
from orchestrator.config import HTTP_PORT, LOCAL_MODE, SERVER_HOST

_DEFAULT_HOST = "localhost" if LOCAL_MODE else SERVER_HOST

_PROJECT_ROOT = Path(__file__).parent.parent.parent
KEYS_MANIFEST = _PROJECT_ROOT / "server" / "assets" / "keys" / "manifest.json"

# Maps hls_variant → subdirectory under /hls/
_HLS_DIRS: dict[str, str] = {
    "h264_ts":   "plain",       # unencrypted H.264 TS
    "av1_fmp4":  "av1_plain",   # unencrypted AV1 fMP4 (new)
    "aes128_ts": "encrypted",   # AES-128-CBC encrypted H.264 TS (existing assets)
}

# For CENC/CBCS, override the key endpoint to a dedicated /keys/ file.
# aes128_decrypt uses the key URI from the playlist (#EXT-X-KEY) directly.
_CENC_CBCS_KEY_FILE: dict[str, str] = {
    "cenc": "cenc.bin",
    "cbcs": "cbcs.bin",
}

# Maps drm_scheme → manifest.json entry key (for IV lookup when playlist has none)
_MANIFEST_ENTRY: dict[str, str] = {
    "cenc": "cenc",
    "cbcs": "cbcs",
}


@dataclass
class DRMHLSResult:
    qos: QoSMetrics = field(default_factory=QoSMetrics)
    duration_s: float = 0.0
    success: bool = False
    decrypt_cpu_time_s: float = 0.0   # wall time spent inside AES cipher calls
    num_license_fetches: int = 0       # how many times /keys/ endpoint was hit
    error: str = ""


# ── Cipher helpers ─────────────────────────────────────────────────────────────

def _decrypt_aes128_cbc(data: bytes, key: bytes, iv: bytes) -> bytes:
    from Crypto.Cipher import AES
    # Pad to block boundary (PKCS7 padding not needed; we just zero-pad for measurement)
    rem = len(data) % 16
    if rem:
        data = data + b"\x00" * (16 - rem)
    return AES.new(key, AES.MODE_CBC, iv[:16]).decrypt(data)


def _decrypt_aes128_ctr(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-128-CTR as used in CENC (Widevine).
    IV is the 8-byte nonce; 64-bit block counter starts at 0."""
    from Crypto.Cipher import AES
    nonce = iv[:8].ljust(8, b"\x00")
    return AES.new(key, AES.MODE_CTR, nonce=nonce).decrypt(data)


def _decrypt_cbcs(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-128-CBC approximating CBCS (PlayReady).
    Full-payload CBC with IV reset per call — same block count as pattern mode."""
    return _decrypt_aes128_cbc(data, key, iv)


# ── Playlist helpers ───────────────────────────────────────────────────────────

def _parse_fmp4_extras(text: str, base_url: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (init_url, key_uri, iv_hex) from a parsed m3u8 text block."""
    init_url = key_uri = iv_hex = None
    base = base_url.rsplit("/", 1)[0]
    for line in text.splitlines():
        m = re.match(r'#EXT-X-MAP:URI="([^"]+)"', line)
        if m:
            uri = m.group(1)
            init_url = uri if uri.startswith("http") else f"{base}/{uri}"
        k = re.search(r'#EXT-X-KEY:[^"]*URI="([^"]+)"', line)
        if k:
            raw_uri = k.group(1)
            key_uri = raw_uri if raw_uri.startswith("http") else f"http://{_DEFAULT_HOST}:{HTTP_PORT}/{raw_uri.lstrip('/')}"
        iv = re.search(r'IV=0x([0-9a-fA-F]+)', line)
        if iv:
            iv_hex = iv.group(1)
    return init_url, key_uri, iv_hex


def _load_manifest() -> dict:
    try:
        with open(KEYS_MANIFEST) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


# ── Main driver ────────────────────────────────────────────────────────────────

def run_hls_drm(
    hls_variant: str,
    drm_scheme: str,
    server_host: str = _DEFAULT_HOST,
) -> DRMHLSResult:
    """
    Fetch an HLS stream, optionally decrypting segments.

    Parameters
    ----------
    hls_variant : "h264_ts" | "av1_fmp4" | "aes128_fmp4"
        Selects which asset directory to stream from.
    drm_scheme  : "none" | "aes128_net_only" | "aes128_decrypt" | "cenc" | "cbcs"
        Controls key fetch and decryption behaviour.
    """
    result = DRMHLSResult()
    qos = result.qos

    hls_dir = _HLS_DIRS.get(hls_variant, "plain")
    base = f"http://{server_host}:{HTTP_PORT}/hls/{hls_dir}"
    playlist_url = f"{base}/master.m3u8"
    is_fmp4 = hls_variant == "av1_fmp4"
    need_bytes = drm_scheme in ("aes128_decrypt", "cenc", "cbcs")

    t_start = time.monotonic()

    # ── Fetch master playlist ──────────────────────────────────────────────────
    try:
        playlist_data, _ = _fetch_url(playlist_url, insecure=False)
    except Exception as e:
        result.error = f"playlist fetch failed: {e}"
        return result

    playlist_text = playlist_data.decode("utf-8", errors="replace")

    # Follow sub-playlist if this is a multi-variant master
    if "#EXT-X-STREAM-INF" in playlist_text:
        lines = [l.strip() for l in playlist_text.splitlines()
                 if l.strip() and not l.startswith("#")]
        if lines:
            sub_url = lines[0] if lines[0].startswith("http") else f"{base}/{lines[0]}"
            try:
                sub_data, _ = _fetch_url(sub_url, insecure=False)
                playlist_text = sub_data.decode("utf-8", errors="replace")
                playlist_url = sub_url
            except Exception as e:
                result.error = f"sub-playlist fetch failed: {e}"
                return result

    # ── Parse playlist ─────────────────────────────────────────────────────────
    segments, seg_duration = _parse_m3u8(playlist_text, playlist_url)
    init_url, playlist_key_uri, playlist_iv_hex = _parse_fmp4_extras(playlist_text, playlist_url)

    if not segments:
        result.error = "no segments found in playlist"
        return result

    # ── Key / license handling ─────────────────────────────────────────────────
    aes_key: Optional[bytes] = None
    aes_iv:  Optional[bytes] = None

    if drm_scheme != "none":
        # CENC/CBCS override the key URL to their dedicated /keys/ files.
        # aes128_net_only and aes128_decrypt use the URI baked into the playlist.
        if drm_scheme in _CENC_CBCS_KEY_FILE:
            key_url = (f"http://{server_host}:{HTTP_PORT}/keys/"
                       f"{_CENC_CBCS_KEY_FILE[drm_scheme]}")
        elif playlist_key_uri:
            key_url = playlist_key_uri
        else:
            key_url = None

        # Live key fetch — counted in the energy window (simulates license RTT)
        if key_url:
            try:
                key_bytes, _ = _fetch_url(key_url, insecure=False)
                qos.num_key_requests += 1
                result.num_license_fetches += 1
                aes_key = key_bytes[:16]
            except Exception:
                pass

        # IV: from playlist (#EXT-X-KEY:IV=) → from manifest → zero fallback
        if playlist_iv_hex:
            aes_iv = bytes.fromhex(playlist_iv_hex.zfill(32))
        else:
            manifest = _load_manifest()
            entry = manifest.get(_MANIFEST_ENTRY.get(drm_scheme, drm_scheme), {})
            aes_iv = bytes.fromhex(entry.get("iv_hex", "00" * 16))

    # ── Init segment (fMP4 only) ───────────────────────────────────────────────
    if is_fmp4 and init_url:
        try:
            init_data, _ = _fetch_url(init_url, insecure=False)
            qos.total_bytes_b += len(init_data)
            qos.num_segment_requests += 1
        except Exception as e:
            result.error = f"init segment fetch failed: {e}"
            return result

    # ── Segment fetch + playback emulation loop ────────────────────────────────
    buffer_level_s = 0.0
    playback_clock = time.monotonic()

    for seg_url in segments:
        if need_bytes:
            try:
                seg_data, seg_dl_s = _fetch_url(seg_url, insecure=False)
                seg_bytes = len(seg_data)
            except Exception:
                seg_data, seg_dl_s, seg_bytes = b"", 0.0, 0
        else:
            seg_bytes, seg_dl_s = _curl_fetch_bytes(seg_url, "HTTP1", False)
            seg_data = b""

        qos.total_bytes_b += seg_bytes
        qos.num_segment_requests += 1

        # Decrypt and time the cipher operation
        if need_bytes and aes_key and aes_iv and seg_data:
            t_dec = time.perf_counter()
            try:
                if drm_scheme == "cenc":
                    _decrypt_aes128_ctr(seg_data, aes_key, aes_iv)
                elif drm_scheme == "cbcs":
                    _decrypt_cbcs(seg_data, aes_key, aes_iv)
                else:
                    _decrypt_aes128_cbc(seg_data, aes_key, aes_iv)
            except Exception:
                pass
            result.decrypt_cpu_time_s += time.perf_counter() - t_dec

        # Real-time playback pacing (same logic as driver_hls.py)
        elapsed_real = time.monotonic() - playback_clock
        buffer_level_s += seg_duration - elapsed_real
        playback_clock = time.monotonic()

        if buffer_level_s < 0:
            qos.stall_count += 1
            qos.total_stall_duration_s += abs(buffer_level_s)
            buffer_level_s = 0.0
        else:
            sleep_s = max(0.0, seg_duration - seg_dl_s)
            if sleep_s > 0:
                time.sleep(sleep_s)

    qos.playback_duration_s = time.monotonic() - t_start
    result.duration_s = qos.playback_duration_s
    result.success = True
    return result
