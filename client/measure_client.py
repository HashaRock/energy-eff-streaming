"""
Cross-platform energy measurement abstraction.

Linux  → Intel RAPL via /sys/class/powercap/intel-rapl/
macOS  → power_monitor.py background daemon (calls powermetrics -n 1 in a loop)

macOS note: powermetrics buffers its stdout when writing to a pipe, so streaming
(-n 0) produces no data. We run it as a once-per-second loop in a daemon process
(power_monitor.py) that writes timestamped readings to a CSV. Per-experiment
energy is computed by integrating the samples that fall within start/stop timestamps.

Usage:
    m = EnergyMeasurer()
    idle_watts = m.measure_idle(duration_s=30)
    m.start(idle_watts)
    # ... do work ...
    result = m.stop()
"""
import csv
import os
import platform
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


MONITOR_CSV  = "/tmp/eec_power_monitor.csv"
MONITOR_PID_FILE = "/tmp/eec_power_monitor.pid"
PROJECT_ROOT = Path(__file__).parent.parent


@dataclass
class EnergyResult:
    energy_j_raw: float
    idle_watts: float
    energy_j_corrected: float
    duration_s: float
    platform: str
    method: str   # "rapl" or "powermetrics"
    n_samples: int = 0


# ── RAPL helpers (Linux) ───────────────────────────────────────────────────────

def _find_rapl_domains() -> dict[str, str]:
    base = "/sys/class/powercap"
    domains = {}
    if not os.path.isdir(base):
        return domains
    for entry in os.listdir(base):
        energy_path = os.path.join(base, entry, "energy_uj")
        name_path   = os.path.join(base, entry, "name")
        if os.path.isfile(energy_path) and os.path.isfile(name_path):
            with open(name_path) as f:
                name = f.read().strip()
            domains[name] = energy_path
    return domains


def _read_rapl_uj(paths: list[str]) -> dict[str, int]:
    vals = {}
    for p in paths:
        try:
            with open(p) as f:
                vals[p] = int(f.read().strip())
        except OSError:
            vals[p] = 0
    return vals


def _rapl_max_range(energy_path: str) -> int:
    p = os.path.join(os.path.dirname(energy_path), "max_energy_range_uj")
    try:
        with open(p) as f:
            return int(f.read().strip())
    except OSError:
        return 2**32


def _delta_uj(before: int, after: int, max_range: int) -> int:
    if after >= before:
        return after - before
    return max_range - before + after


# ── macOS power monitor helpers ────────────────────────────────────────────────

def _start_power_monitor(interval_ms: int = 1000) -> subprocess.Popen:
    """Launch power_monitor.py as a background daemon."""
    monitor_script = str(PROJECT_ROOT / "client" / "power_monitor.py")
    prefix = [] if os.getuid() == 0 else ["sudo"]
    proc = subprocess.Popen(
        [sys.executable, monitor_script, MONITOR_CSV, str(interval_ms)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # Write PID so we can kill it if the parent crashes
    with open(MONITOR_PID_FILE, "w") as f:
        f.write(str(proc.pid))
    # Wait for first sample to appear (up to interval + 2s)
    deadline = time.monotonic() + interval_ms / 1000 + 3
    while time.monotonic() < deadline:
        if os.path.isfile(MONITOR_CSV) and os.path.getsize(MONITOR_CSV) > 30:
            break
        time.sleep(0.2)
    return proc


def _stop_power_monitor(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        os.remove(MONITOR_PID_FILE)
    except OSError:
        pass


def _read_monitor_samples(t0_ns: int, t1_ns: int) -> tuple[list[float], float]:
    """
    Read power_monitor.csv and return (watts_list, avg_watts) for samples
    whose timestamps fall within [t0_ns, t1_ns].
    """
    if not os.path.isfile(MONITOR_CSV):
        return [], 0.0
    watts = []
    try:
        with open(MONITOR_CSV) as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    ts = int(row["timestamp_ns"])
                    if t0_ns <= ts <= t1_ns:
                        watts.append(float(row["cpu_power_mw"]) / 1000.0)
                except (ValueError, KeyError):
                    continue
    except OSError:
        pass
    avg = sum(watts) / len(watts) if watts else 0.0
    return watts, avg


def _idle_watts_from_monitor(duration_s: float) -> float:
    """Compute average watts from the LAST duration_s of monitor data."""
    if not os.path.isfile(MONITOR_CSV):
        return 0.0
    now_ns = time.monotonic_ns()
    t0_ns = now_ns - int(duration_s * 1e9)
    _, avg = _read_monitor_samples(t0_ns, now_ns)
    return avg


# ── Main class ─────────────────────────────────────────────────────────────────

class EnergyMeasurer:
    def __init__(self) -> None:
        self._sys = platform.system()
        self._method = "rapl" if self._sys == "Linux" else "powermetrics"

        # RAPL state (Linux)
        self._rapl_domains: dict[str, str] = {}
        self._rapl_max: dict[str, int] = {}
        self._rapl_before: dict[str, int] = {}
        self._rapl_t0: float = 0.0

        # powermetrics state (macOS) — monotonic nanosecond timestamps
        self._pm_t0_ns: int = 0
        self._monitor_proc: Optional[subprocess.Popen] = None

        self._idle_watts: float = 0.0

        if self._method == "rapl":
            self._rapl_domains = _find_rapl_domains()
            if not self._rapl_domains:
                raise RuntimeError(
                    "No RAPL domains found at /sys/class/powercap/. "
                    "Try: sudo modprobe intel_rapl_common"
                )
            for name, path in self._rapl_domains.items():
                self._rapl_max[path] = _rapl_max_range(path)
        else:
            # Start the background monitor immediately so it's warm by the time
            # experiments begin (first sample always discarded as warmup)
            self._monitor_proc = _start_power_monitor(interval_ms=500)
            print("[energy] power monitor started")

    def measure_idle(self, duration_s: int = 30) -> float:
        """Measure idle power. For macOS: sleep duration_s, read monitor data."""
        print(f"[energy] measuring idle baseline for {duration_s}s...")
        if self._method == "rapl":
            paths = list(self._rapl_domains.values())
            before = _read_rapl_uj(paths)
            t0 = time.monotonic()
            time.sleep(duration_s)
            after = _read_rapl_uj(paths)
            elapsed = time.monotonic() - t0
            total_uj = sum(
                _delta_uj(before[p], after[p], self._rapl_max[p]) for p in paths
            )
            idle_watts = (total_uj / 1e6) / elapsed
        else:
            time.sleep(duration_s)
            idle_watts = _idle_watts_from_monitor(duration_s)

        self._idle_watts = idle_watts
        print(f"[energy] idle baseline: {idle_watts:.3f} W")
        return idle_watts

    def start(self, idle_watts: float = 0.0) -> None:
        self._idle_watts = idle_watts
        if self._method == "rapl":
            paths = list(self._rapl_domains.values())
            self._rapl_before = _read_rapl_uj(paths)
            self._rapl_t0 = time.monotonic()
        else:
            self._pm_t0_ns = time.monotonic_ns()

    def stop(self) -> EnergyResult:
        if self._method == "rapl":
            t1 = time.monotonic()
            paths = list(self._rapl_domains.values())
            after = _read_rapl_uj(paths)
            duration_s = t1 - self._rapl_t0
            total_uj = sum(
                _delta_uj(self._rapl_before[p], after[p], self._rapl_max[p]) for p in paths
            )
            energy_j_raw = total_uj / 1e6
            n_samples = 0
        else:
            t1_ns = time.monotonic_ns()
            duration_s = (t1_ns - self._pm_t0_ns) / 1e9
            watts_list, avg_watts = _read_monitor_samples(self._pm_t0_ns, t1_ns)
            n_samples = len(watts_list)
            energy_j_raw = avg_watts * duration_s

        corrected = max(0.0, energy_j_raw - self._idle_watts * duration_s)
        return EnergyResult(
            energy_j_raw=round(energy_j_raw, 4),
            idle_watts=round(self._idle_watts, 4),
            energy_j_corrected=round(corrected, 4),
            duration_s=round(duration_s, 3),
            platform=self._sys.lower(),
            method=self._method,
            n_samples=n_samples,
        )

    def shutdown(self) -> None:
        """Stop the background monitor. Call at end of experiment session."""
        if self._monitor_proc:
            _stop_power_monitor(self._monitor_proc)
            self._monitor_proc = None
            print("[energy] power monitor stopped")
