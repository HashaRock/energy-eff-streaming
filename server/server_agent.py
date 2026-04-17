#!/usr/bin/env python3
"""
Persistent TCP command socket that the orchestrator SSHes into.
Listens on SERVER_AGENT_PORT (default 9999) and handles measurement lifecycle.

Commands (newline-terminated):
  START_MEASURE <run_id>       → forks measure_server.py, responds "OK <pid>"
  STOP_MEASURE <run_id>        → SIGTERMs the subprocess, responds "OK"
  GET_CSV <run_id>             → streams the CSV file over the socket, ends with "EOF\n"
  IDLE_START <duration_s>      → runs measure_server.py for duration_s, saves as idle baseline
  IDLE_GET                     → returns idle baseline CSV content
  RELOAD_NGINX <conf_name>     → switches nginx to the named config, responds "OK"
  PING                         → responds "PONG"
  QUIT                         → closes the connection

Usage (on the server):
    python3 server_agent.py [--port 9999]
"""
import argparse
import os
import signal
import socketserver
import subprocess
import sys
import tempfile
import threading
import time

NGINX_CONF_DIR = os.environ.get("EEC_NGINX_CONF_DIR", "/opt/eec/server/nginx")
RAPL_SCRIPT    = os.path.join(os.path.dirname(__file__), "measure_server.py")
TMPDIR         = "/tmp/eec_rapl"

os.makedirs(TMPDIR, exist_ok=True)

_procs: dict[str, subprocess.Popen] = {}   # run_id → process
_idle_csv: str = ""


class AgentHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        global _idle_csv
        for raw_line in self.rfile:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            parts = line.split(maxsplit=1)
            cmd = parts[0].upper()
            arg = parts[1] if len(parts) > 1 else ""

            if cmd == "PING":
                self._send("PONG\n")

            elif cmd == "START_MEASURE":
                run_id = arg or "default"
                csv_path = os.path.join(TMPDIR, f"rapl_{run_id}.csv")
                proc = subprocess.Popen(
                    [sys.executable, RAPL_SCRIPT, "--output", csv_path, "--interval_ms", "100"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                _procs[run_id] = proc
                self._send(f"OK {proc.pid}\n")

            elif cmd == "STOP_MEASURE":
                run_id = arg or "default"
                proc = _procs.pop(run_id, None)
                if proc and proc.poll() is None:
                    proc.send_signal(signal.SIGTERM)
                    proc.wait(timeout=5)
                self._send("OK\n")

            elif cmd == "GET_CSV":
                run_id = arg or "default"
                csv_path = os.path.join(TMPDIR, f"rapl_{run_id}.csv")
                if os.path.isfile(csv_path):
                    with open(csv_path, "rb") as f:
                        self.wfile.write(f.read())
                self._send("EOF\n")

            elif cmd == "IDLE_START":
                duration_s = int(arg) if arg.isdigit() else 30
                csv_path = os.path.join(TMPDIR, "rapl_idle.csv")
                proc = subprocess.Popen(
                    [sys.executable, RAPL_SCRIPT, "--output", csv_path, "--interval_ms", "500"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                time.sleep(duration_s)
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=5)
                _idle_csv = csv_path
                self._send("OK\n")

            elif cmd == "IDLE_GET":
                if _idle_csv and os.path.isfile(_idle_csv):
                    with open(_idle_csv, "rb") as f:
                        self.wfile.write(f.read())
                self._send("EOF\n")

            elif cmd == "RELOAD_NGINX":
                conf_name = arg.strip()
                conf_path = os.path.join(NGINX_CONF_DIR, conf_name)
                result = subprocess.run(
                    ["sudo", "nginx", "-s", "stop"], capture_output=True
                )
                time.sleep(0.5)
                result = subprocess.run(
                    ["sudo", "nginx", "-c", conf_path], capture_output=True
                )
                if result.returncode == 0:
                    self._send("OK\n")
                else:
                    self._send(f"ERR {result.stderr.decode()[:200]}\n")

            elif cmd == "QUIT":
                self._send("BYE\n")
                return

            else:
                self._send(f"ERR unknown command: {cmd}\n")

    def _send(self, text: str) -> None:
        self.wfile.write(text.encode())
        self.wfile.flush()


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9999)
    args = parser.parse_args()
    with ThreadedTCPServer(("0.0.0.0", args.port), AgentHandler) as server:
        print(f"[server_agent] listening on port {args.port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
