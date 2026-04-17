"""
Local nginx manager for single-machine (Mac) experiments.
Replaces ssh_coordinator.py when running server and client on the same host.
"""
import os
import subprocess
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
NGINX_CONF_DIR = PROJECT_ROOT / "server" / "nginx" / "mac"
NGINX_BIN = "/opt/homebrew/bin/nginx"
PID_FILE = "/tmp/eec_nginx.pid"
ERROR_LOG = "/tmp/eec_nginx_error.log"


class LocalCoordinator:
    """Drop-in replacement for ServerCoordinator when running locally."""

    def connect(self) -> None:
        # Nothing to do — no SSH needed
        print("[local] running in single-machine mode (server + client on same host)")

    def start_server_measurement(self, run_id: str) -> None:
        pass   # energy captured by EnergyMeasurer covering the whole machine

    def stop_server_measurement(self, run_id: str) -> None:
        pass

    def fetch_server_csv(self, run_id: str, local_path: str) -> None:
        pass   # no separate server measurement file

    def start_idle_baseline(self, duration_s: int = 30) -> None:
        pass   # idle baseline done by EnergyMeasurer

    def fetch_idle_csv(self, local_path: str) -> None:
        pass

    def reload_nginx(self, conf_name: str) -> None:
        if conf_name is None:
            return
        conf_path = NGINX_CONF_DIR / conf_name
        if not conf_path.exists():
            raise FileNotFoundError(f"nginx config not found: {conf_path}")

        # Stop existing nginx — try graceful stop, then kill by PID, then pkill
        try:
            subprocess.run([NGINX_BIN, "-s", "stop"], capture_output=True, timeout=5)
            time.sleep(0.8)
        except Exception:
            pass
        # Belt-and-suspenders: kill by PID file if still alive
        if os.path.exists(PID_FILE):
            try:
                with open(PID_FILE) as f:
                    pid = int(f.read().strip())
                os.kill(pid, 15)   # SIGTERM
                time.sleep(0.5)
            except (OSError, ValueError):
                pass
            try:
                os.remove(PID_FILE)
            except OSError:
                pass

        # Start nginx with new config
        result = subprocess.run(
            [NGINX_BIN, "-c", str(conf_path)],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            raise RuntimeError(f"nginx failed to start:\n{result.stderr}")
        time.sleep(0.8)   # let nginx fully bind
        print(f"[local] nginx started with {conf_name}")

    def disconnect(self) -> None:
        # Stop nginx cleanly at end of experiment session
        if os.path.exists(PID_FILE):
            subprocess.run([NGINX_BIN, "-s", "stop"], capture_output=True, timeout=5)
            print("[local] nginx stopped")
