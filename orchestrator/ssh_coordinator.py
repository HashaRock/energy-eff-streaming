"""
Thin wrapper around paramiko that maintains a persistent SSH tunnel to server_agent.py.
Falls back to subprocess SSH if paramiko is not installed.
"""
import io
import os
import socket
import subprocess
import time
from typing import Optional

from orchestrator.config import (
    SERVER_HOST, SERVER_SSH_PORT, SERVER_SSH_USER, SERVER_SSH_KEY, SERVER_AGENT_PORT
)

try:
    import paramiko
    _PARAMIKO = True
except ImportError:
    _PARAMIKO = False


class ServerCoordinator:
    def __init__(
        self,
        host: str = SERVER_HOST,
        port: int = SERVER_AGENT_PORT,
        ssh_port: int = SERVER_SSH_PORT,
        ssh_user: str = SERVER_SSH_USER,
        ssh_key: str = SERVER_SSH_KEY,
    ) -> None:
        self._host = host
        self._port = port
        self._ssh_port = ssh_port
        self._ssh_user = ssh_user
        self._ssh_key = os.path.expanduser(ssh_key)
        self._sock: Optional[socket.socket] = None
        self._ssh_client = None

    def connect(self) -> None:
        if _PARAMIKO:
            self._connect_paramiko()
        else:
            self._connect_direct()

    def _connect_paramiko(self) -> None:
        import paramiko
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            self._host, port=self._ssh_port,
            username=self._ssh_user, key_filename=self._ssh_key,
        )
        # Open a TCP tunnel to the server_agent port
        transport = client.get_transport()
        channel = transport.open_channel(
            "direct-tcpip",
            (self._host, self._port),
            ("127.0.0.1", 0),
        )
        self._ssh_client = client
        self._sock = socket.fromfd(channel.fileno(), socket.AF_INET, socket.SOCK_STREAM)
        self._sock = channel   # use paramiko channel directly as socket-like object
        self._ping()

    def _connect_direct(self) -> None:
        # Direct TCP — assumes server_agent port is already forwarded or accessible
        self._sock = socket.create_connection((self._host, self._port), timeout=10)
        self._ping()

    def _ping(self) -> None:
        resp = self._cmd("PING")
        if resp.strip() != "PONG":
            raise RuntimeError(f"server_agent PING failed: {resp!r}")
        print(f"[coordinator] connected to server_agent at {self._host}:{self._port}")

    def _cmd(self, line: str) -> str:
        if self._sock is None:
            raise RuntimeError("Not connected — call connect() first")
        self._sock.sendall((line + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = self._sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        return buf.decode("utf-8", errors="replace")

    def start_server_measurement(self, run_id: str) -> None:
        resp = self._cmd(f"START_MEASURE {run_id}")
        if not resp.startswith("OK"):
            raise RuntimeError(f"START_MEASURE failed: {resp!r}")

    def stop_server_measurement(self, run_id: str) -> None:
        resp = self._cmd(f"STOP_MEASURE {run_id}")
        if not resp.startswith("OK"):
            raise RuntimeError(f"STOP_MEASURE failed: {resp!r}")

    def fetch_server_csv(self, run_id: str, local_path: str) -> None:
        """Pull the RAPL time-series CSV for run_id to local_path."""
        if self._sock is None:
            raise RuntimeError("Not connected")
        self._sock.sendall(f"GET_CSV {run_id}\n".encode())
        with open(local_path, "wb") as f:
            buf = b""
            while True:
                chunk = self._sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if b"EOF\n" in buf:
                    # Write everything before EOF marker
                    content, _ = buf.split(b"EOF\n", 1)
                    f.write(content)
                    break
                # Write safe prefix (keep last few bytes in case EOF spans chunks)
                safe = buf[:-4]
                f.write(safe)
                buf = buf[-4:]

    def start_idle_baseline(self, duration_s: int = 30) -> None:
        resp = self._cmd(f"IDLE_START {duration_s}")
        if not resp.startswith("OK"):
            raise RuntimeError(f"IDLE_START failed: {resp!r}")

    def fetch_idle_csv(self, local_path: str) -> None:
        self.fetch_server_csv("idle", local_path)
        # Override: send IDLE_GET instead
        if self._sock is None:
            raise RuntimeError("Not connected")
        self._sock.sendall(b"IDLE_GET\n")
        with open(local_path, "wb") as f:
            buf = b""
            while True:
                chunk = self._sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if b"EOF\n" in buf:
                    content, _ = buf.split(b"EOF\n", 1)
                    f.write(content)
                    break
                safe = buf[:-4]
                f.write(safe)
                buf = buf[-4:]

    def reload_nginx(self, conf_name: str) -> None:
        resp = self._cmd(f"RELOAD_NGINX {conf_name}")
        if not resp.startswith("OK"):
            raise RuntimeError(f"RELOAD_NGINX failed: {resp!r}")
        time.sleep(1.0)   # allow nginx to fully start

    def disconnect(self) -> None:
        if self._sock:
            try:
                self._cmd("QUIT")
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
        if self._ssh_client:
            try:
                self._ssh_client.close()
            except Exception:
                pass
