"""
P2P download driver using webtorrent-cli.
Reads the magnet link written by generate_assets.sh on the server.
"""
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass

from orchestrator.config import SERVER_HOST, TORRENT_MAGNET_FILE


@dataclass
class P2PResult:
    bytes_b: int
    duration_s: float
    success: bool
    error: str = ""


def _read_magnet(magnet_file: str) -> str:
    with open(magnet_file) as f:
        return f.read().strip()


def run_p2p(magnet_or_file: str = TORRENT_MAGNET_FILE) -> P2PResult:
    if os.path.isfile(magnet_or_file):
        with open(magnet_or_file) as f:
            magnet = f.read().strip()
    else:
        magnet = magnet_or_file

    outdir = tempfile.mkdtemp(prefix="eec_p2p_")
    cmd = [
        "webtorrent", "download", magnet,
        "--out", outdir,
        "--no-tracker",   # rely on the known peer (server)
    ]

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(outdir, ignore_errors=True)
        return P2PResult(0, time.monotonic() - t0, False, "webtorrent timed out")
    except FileNotFoundError:
        shutil.rmtree(outdir, ignore_errors=True)
        return P2PResult(0, 0.0, False, "webtorrent not found — run: npm install -g webtorrent-cli")

    duration_s = time.monotonic() - t0

    if proc.returncode != 0:
        shutil.rmtree(outdir, ignore_errors=True)
        return P2PResult(0, duration_s, False, proc.stderr.strip()[:500])

    # Sum bytes of downloaded files
    total_bytes = 0
    for root, _, files in os.walk(outdir):
        for fname in files:
            try:
                total_bytes += os.path.getsize(os.path.join(root, fname))
            except OSError:
                pass

    shutil.rmtree(outdir, ignore_errors=True)
    return P2PResult(bytes_b=total_bytes, duration_s=duration_s, success=True)
