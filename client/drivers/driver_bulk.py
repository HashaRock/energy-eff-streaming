"""
Bulk HTTP file download driver.
Uses curl as a subprocess to ensure correct HTTP protocol negotiation (H1/H2/H3).
"""
import subprocess
import time
from dataclasses import dataclass

from orchestrator.config import HTTP_PORT, HTTPS_PORT, SERVER_HOST, TLS_INSECURE, LOCAL_MODE

_DEFAULT_HOST = "localhost" if LOCAL_MODE else SERVER_HOST


@dataclass
class BulkResult:
    bytes_b: int
    duration_s: float
    http_version_actual: str
    success: bool
    error: str = ""


_PROTOCOL_FLAGS = {
    "HTTP1": ["--http1.1"],
    "HTTP2": ["--http2"],
    "HTTP3": ["--http3-only"],
}

_CONTAINER_FILES = {
    "mp4":     "test.mp4",
    "mkv":     "test.mkv",
    "av1_mp4": "test_av1.mp4",
}


def run_bulk(
    protocol: str,
    tls: bool,
    container: str,
    server_host: str = _DEFAULT_HOST,
) -> BulkResult:
    filename = _CONTAINER_FILES.get(container, "test.mp4")
    if tls:
        url = f"https://{server_host}:{HTTPS_PORT}/bulk/{filename}"
    else:
        url = f"http://{server_host}:{HTTP_PORT}/bulk/{filename}"

    proto_flags = _PROTOCOL_FLAGS.get(protocol, ["--http1.1"])
    cmd = [
        "curl",
        *proto_flags,
        "--output", "/dev/null",
        "--silent",
        "--write-out", "%{size_download}\t%{time_total}\t%{http_version}",
    ]
    if tls and TLS_INSECURE:
        cmd.append("--insecure")
    cmd.append(url)

    t0 = time.monotonic()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return BulkResult(0, time.monotonic() - t0, "timeout", False, "curl timed out")
    except FileNotFoundError:
        return BulkResult(0, 0.0, "N/A", False, "curl not found")

    if result.returncode != 0:
        return BulkResult(0, time.monotonic() - t0, "N/A", False, result.stderr.strip())

    parts = result.stdout.strip().split("\t")
    if len(parts) < 3:
        return BulkResult(0, time.monotonic() - t0, "N/A", False, f"unexpected curl output: {result.stdout!r}")

    return BulkResult(
        bytes_b=int(float(parts[0])),
        duration_s=float(parts[1]),
        http_version_actual=parts[2].strip(),
        success=True,
    )
