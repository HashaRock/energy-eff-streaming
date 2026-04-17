"""
HLS streaming driver — emulates a player's segment fetch loop.
Does NOT use a real player to avoid background telemetry contaminating energy readings.
"""
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional
import urllib.request
import ssl

from client.qos_monitor import QoSMetrics
from orchestrator.config import HTTP_PORT, HTTPS_PORT, SERVER_HOST, TLS_INSECURE, LOCAL_MODE

_DEFAULT_HOST = "localhost" if LOCAL_MODE else SERVER_HOST


@dataclass
class HLSResult:
    qos: QoSMetrics
    duration_s: float
    success: bool
    error: str = ""


# ── m3u8 minimal parser ────────────────────────────────────────────────────────

def _parse_m3u8(text: str, base_url: str) -> tuple[list[str], float]:
    """Return (segment_urls, segment_duration_s) from m3u8 playlist text."""
    segments = []
    duration = 6.0   # default
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"#EXTINF:([\d.]+)", line)
        if m:
            duration = float(m.group(1))
        if line and not line.startswith("#"):
            if line.startswith("http"):
                segments.append(line.strip())
            else:
                segments.append(base_url.rsplit("/", 1)[0] + "/" + line.strip())
    return segments, duration


def _fetch_url(url: str, insecure: bool = False) -> tuple[bytes, float]:
    ctx = None
    if insecure and url.startswith("https"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    t0 = time.monotonic()
    with urllib.request.urlopen(url, context=ctx, timeout=30) as resp:
        data = resp.read()
    return data, time.monotonic() - t0


def _curl_fetch_bytes(url: str, protocol: str, insecure: bool) -> tuple[int, float]:
    proto_flags = {"HTTP1": ["--http1.1"], "HTTP2": ["--http2"], "HTTP3": ["--http3-only"]}
    cmd = [
        "curl",
        *proto_flags.get(protocol, ["--http1.1"]),
        "--output", "/dev/null",
        "--silent",
        "--write-out", "%{size_download}\t%{time_total}",
    ]
    if insecure:
        cmd.append("--insecure")
    cmd.append(url)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        return 0, 0.0
    parts = result.stdout.strip().split("\t")
    return int(float(parts[0])), float(parts[1])


# ── Main driver ────────────────────────────────────────────────────────────────

def run_hls(
    protocol: str,
    tls: bool,
    drm: bool,
    server_host: str = _DEFAULT_HOST,
) -> HLSResult:
    insecure = tls and TLS_INSECURE
    variant = "encrypted" if drm else "plain"

    if tls:
        base = f"https://{server_host}:{HTTPS_PORT}/hls/{variant}"
    else:
        base = f"http://{server_host}:{HTTP_PORT}/hls/{variant}"
    playlist_url = f"{base}/master.m3u8"

    qos = QoSMetrics()
    t_start = time.monotonic()

    # Fetch master playlist
    try:
        playlist_data, _ = _fetch_url(playlist_url, insecure=insecure)
    except Exception as e:
        return HLSResult(qos, 0.0, False, f"playlist fetch failed: {e}")

    playlist_text = playlist_data.decode("utf-8", errors="replace")

    # If master playlist links to a sub-playlist, follow it
    if "#EXT-X-STREAM-INF" in playlist_text:
        lines = [l.strip() for l in playlist_text.splitlines() if l.strip() and not l.startswith("#")]
        if lines:
            sub_url = lines[0] if lines[0].startswith("http") else base + "/" + lines[0]
            try:
                sub_data, _ = _fetch_url(sub_url, insecure=insecure)
                playlist_text = sub_data.decode("utf-8", errors="replace")
                playlist_url = sub_url
            except Exception as e:
                return HLSResult(qos, 0.0, False, f"sub-playlist fetch failed: {e}")

    # Fetch AES key if encrypted (one key fetch for the whole stream)
    if drm and "#EXT-X-KEY" in playlist_text:
        key_match = re.search(r'URI="([^"]+)"', playlist_text)
        if key_match:
            key_url = key_match.group(1)
            try:
                _fetch_url(key_url, insecure=insecure)
                qos.num_key_requests += 1
            except Exception:
                pass

    segments, seg_duration = _parse_m3u8(playlist_text, playlist_url)
    if not segments:
        return HLSResult(qos, 0.0, False, "no segments found in playlist")

    buffer_level_s = 0.0
    playback_clock = time.monotonic()

    for seg_url in segments:
        seg_t0 = time.monotonic()
        seg_bytes, seg_dl_s = _curl_fetch_bytes(seg_url, protocol, insecure)
        qos.total_bytes_b += seg_bytes
        qos.num_segment_requests += 1

        # Simulate buffer consumption
        elapsed_real = time.monotonic() - playback_clock
        buffer_level_s += seg_duration - elapsed_real
        playback_clock = time.monotonic()

        if buffer_level_s < 0:
            # Stall: client would have had to pause playback
            qos.stall_count += 1
            qos.total_stall_duration_s += abs(buffer_level_s)
            buffer_level_s = 0.0
        else:
            # Sleep to emulate real-time playback pace
            sleep_s = max(0.0, seg_duration - seg_dl_s)
            if sleep_s > 0:
                time.sleep(sleep_s)

    qos.playback_duration_s = time.monotonic() - t_start
    return HLSResult(qos=qos, duration_s=qos.playback_duration_s, success=True)
