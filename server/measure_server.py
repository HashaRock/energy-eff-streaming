#!/usr/bin/env python3
"""
RAPL energy time-series daemon for the Ubuntu server.
Writes one CSV row per poll interval until SIGTERM/SIGINT.

Usage:
    python3 measure_server.py --output /tmp/rapl_<run_id>.csv [--interval_ms 100]
"""
import argparse
import csv
import os
import signal
import sys
import time


def _find_rapl_domains() -> dict[str, str]:
    """Return {domain_name: energy_uj_path}."""
    base = "/sys/class/powercap"
    domains = {}
    if not os.path.isdir(base):
        return domains
    for entry in sorted(os.listdir(base)):
        energy_path = os.path.join(base, entry, "energy_uj")
        name_path   = os.path.join(base, entry, "name")
        if os.path.isfile(energy_path) and os.path.isfile(name_path):
            with open(name_path) as f:
                name = f.read().strip()
            domains[name] = energy_path
    return domains


def _read_uj(path: str) -> int:
    try:
        with open(path) as f:
            return int(f.read().strip())
    except OSError:
        return 0


def _max_range(energy_path: str) -> int:
    p = os.path.join(os.path.dirname(energy_path), "max_energy_range_uj")
    try:
        with open(p) as f:
            return int(f.read().strip())
    except OSError:
        return 2**32


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--interval_ms", type=int, default=100)
    args = parser.parse_args()

    domains = _find_rapl_domains()
    if not domains:
        sys.exit("ERROR: No RAPL domains found. Is intel_rapl_common loaded?")

    max_ranges = {path: _max_range(path) for path in domains.values()}

    running = True

    def _stop(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    # Build CSV header: timestamp_ns + one column per domain name
    fieldnames = ["timestamp_ns"] + list(domains.keys())

    with open(args.output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        interval_s = args.interval_ms / 1000.0

        while running:
            row = {"timestamp_ns": time.monotonic_ns()}
            for name, path in domains.items():
                row[name] = _read_uj(path)
            writer.writerow(row)
            f.flush()
            time.sleep(interval_s)

    print("DONE", flush=True)


if __name__ == "__main__":
    main()
