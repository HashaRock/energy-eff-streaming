#!/usr/bin/env python3
"""
Background power monitor for macOS.
Runs as a daemon, calls powermetrics once per second (-n 1), and appends
timestamp + CPU power readings to a CSV file.

The orchestrator starts this process at session start and stops it at the end.
Per-experiment energy is computed by correlating monotonic timestamps.

Usage (not called directly — managed by EnergyMeasurer):
    python3 client/power_monitor.py /tmp/eec_power_monitor.csv
"""
import os
import re
import signal
import subprocess
import sys
import time

PATTERN = re.compile(r"CPU Power:\s*([\d.]+)\s*mW")

running = True

def _stop(sig, frame):
    global running
    running = False

signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)


def main() -> None:
    output_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/eec_power_monitor.csv"
    interval_ms = int(sys.argv[2]) if len(sys.argv) > 2 else 1000

    prefix = [] if os.getuid() == 0 else ["sudo"]

    with open(output_path, "w", buffering=1) as f:   # line-buffered
        f.write("timestamp_ns,cpu_power_mw\n")

        while running:
            loop_start = time.monotonic()
            try:
                result = subprocess.run(
                    [*prefix, "powermetrics", "--samplers", "cpu_power",
                     "-i", str(interval_ms), "-n", "1"],
                    capture_output=True, text=True,
                    timeout=interval_ms / 1000 + 5,
                )
                ts = time.monotonic_ns()
                for m in PATTERN.finditer(result.stdout):
                    f.write(f"{ts},{m.group(1)}\n")
                    break   # one CPU Power line per sample
            except (subprocess.TimeoutExpired, OSError):
                pass

            # Maintain ~interval cadence accounting for process overhead
            elapsed = time.monotonic() - loop_start
            sleep_s = max(0, interval_ms / 1000 - elapsed)
            if sleep_s > 0 and running:
                time.sleep(sleep_s)


if __name__ == "__main__":
    main()
