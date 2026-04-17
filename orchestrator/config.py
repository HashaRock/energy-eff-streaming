"""
Single source of truth for the experiment matrix and all runtime configuration.
Edit SERVER_HOST and SSH credentials before running experiments.
For single-machine Mac mode, set LOCAL_MODE = True (or pass --local to orchestrator).
"""
import itertools
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent

# ── Mode ───────────────────────────────────────────────────────────────────────
# LOCAL_MODE: run nginx + client on the same Mac (no SSH, combined energy measurement)
LOCAL_MODE = True   # set False to use SSH + remote server

# ── Server connection (remote mode only) ──────────────────────────────────────
SERVER_HOST     = os.environ.get("EEC_SERVER_HOST", "192.168.1.100")
SERVER_SSH_PORT = int(os.environ.get("EEC_SERVER_SSH_PORT", "22"))
SERVER_SSH_USER = os.environ.get("EEC_SERVER_SSH_USER", "ubuntu")
SERVER_SSH_KEY  = os.path.expanduser(os.environ.get("EEC_SERVER_SSH_KEY", "~/.ssh/id_rsa"))

# ── Server ports ───────────────────────────────────────────────────────────────
HTTP_PORT  = 8080   # plain HTTP (H1 only)
HTTPS_PORT = 8443   # HTTPS (H1+TLS, H2, H3/QUIC)

# ── Paths (local Mac mode) ─────────────────────────────────────────────────────
LOCAL_ASSETS_ROOT    = PROJECT_ROOT / "server" / "assets"
LOCAL_NGINX_CONF_DIR = PROJECT_ROOT / "server" / "nginx" / "mac"
TORRENT_MAGNET_FILE  = str(LOCAL_ASSETS_ROOT / "torrent" / "magnet.txt")

# ── Paths (remote server mode) ────────────────────────────────────────────────
SERVER_ASSETS_ROOT    = "/opt/eec/server/assets"
SERVER_NGINX_CONF_DIR = "/opt/eec/server/nginx"
SERVER_AGENT_PORT     = 9999

# ── Experiment axes ────────────────────────────────────────────────────────────
DELIVERY_MODES    = ["bulk", "hls"]
# P2P excluded from local Mac run — requires a separate seed peer.
# Add "p2p" back and run generate_assets.sh + webtorrent seed on a remote machine.
# HTTP/3 excluded on Mac — system curl uses LibreSSL which lacks QUIC support.
# Re-add "HTTP3" here when running on Linux with a QUIC-capable curl build.
HTTP_PROTOCOLS    = ["HTTP1", "HTTP2"]
TLS_OPTIONS       = [True, False]
CONTAINER_FORMATS = ["mp4", "mkv", "av1_mp4"]   # only meaningful for bulk
DRM_OPTIONS       = [True, False]                 # only meaningful for hls (AES-128)

# ── Run control ────────────────────────────────────────────────────────────────
TRIALS_PER_CONFIG = 5
COOLDOWN_S        = 10    # seconds between runs
IDLE_BASELINE_S   = 10    # seconds for idle power measurement

# ── TLS ────────────────────────────────────────────────────────────────────────
TLS_INSECURE = True   # skip cert verification (self-signed)

# ── Output paths ──────────────────────────────────────────────────────────────
DATA_DIR = str(PROJECT_ROOT / "data")


def _nginx_config_name(protocol: str, tls: bool) -> str:
    mapping = {
        ("HTTP1", False): "nginx_h1.conf",
        ("HTTP1", True):  "nginx_h1_tls.conf",
        ("HTTP2", True):  "nginx_h2.conf",
        ("HTTP3", True):  "nginx_h3.conf",
    }
    key = (protocol, tls)
    if key not in mapping:
        raise ValueError(f"No nginx config for protocol={protocol}, tls={tls}")
    return mapping[key]


def build_valid_matrix() -> list[dict]:
    """Return all valid experiment configurations as a list of dicts."""
    configs = []

    # ── bulk: sweeps protocol × tls × container; no drm ──────────────────────
    for proto, tls, fmt in itertools.product(HTTP_PROTOCOLS, TLS_OPTIONS, CONTAINER_FORMATS):
        if proto == "HTTP3" and not tls:
            continue   # QUIC requires TLS
        if proto == "HTTP2" and not tls:
            continue   # H2 always TLS
        configs.append({
            "delivery": "bulk",
            "protocol": proto,
            "tls": tls,
            "container": fmt,
            "drm": False,
            "nginx_conf": _nginx_config_name(proto, tls),
        })

    # ── hls: sweeps protocol × tls × drm; container is always .ts ────────────
    for proto, tls, drm in itertools.product(HTTP_PROTOCOLS, TLS_OPTIONS, DRM_OPTIONS):
        if proto == "HTTP3" and not tls:
            continue
        if proto == "HTTP2" and not tls:
            continue
        configs.append({
            "delivery": "hls",
            "protocol": proto,
            "tls": tls,
            "container": "ts",
            "drm": drm,
            "nginx_conf": _nginx_config_name(proto, tls),
        })

    # P2P disabled for local Mac run (no seed peer available)

    return configs


# Nginx config file → (protocol, tls) mapping (reverse of _nginx_config_name)
NGINX_CONF_MAP = {
    "nginx_h1.conf":     ("HTTP1", False),
    "nginx_h1_tls.conf": ("HTTP1", True),
    "nginx_h2.conf":     ("HTTP2", True),
    "nginx_h3.conf":     ("HTTP3", True),
}
