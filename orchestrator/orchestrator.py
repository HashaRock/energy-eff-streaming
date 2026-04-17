#!/usr/bin/env python3
"""
Main experiment orchestrator.
Sweeps the full configuration matrix, coordinates server + client energy measurement,
and writes results to data/raw/*.csv.

Usage:
    python3 orchestrator/orchestrator.py [options]

Options:
    --dry-run           Print the matrix without running anything
    --client-only       Skip SSH server coordination (manual server setup)
    --server-only       Only measure server energy (no client energy)
    --resume RUN_ID     Skip configs whose run_ids already appear in client CSV
    --trials N          Override TRIALS_PER_CONFIG from config
    --config KEY=VAL    Override config values (e.g. --config SERVER_HOST=10.0.0.5)
"""
import argparse
import csv
import datetime
import os
import sys
import time
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.config import (
    DATA_DIR, TRIALS_PER_CONFIG, COOLDOWN_S, IDLE_BASELINE_S,
    LOCAL_MODE, SERVER_HOST, build_valid_matrix
)
from client.measure_client import EnergyMeasurer
from client.drivers.driver_bulk import run_bulk
from client.drivers.driver_hls import run_hls
from client.drivers.driver_p2p import run_p2p

# ── Paths ──────────────────────────────────────────────────────────────────────
RAW_DIR      = os.path.join(DATA_DIR, "raw")
COMBINED_DIR = os.path.join(DATA_DIR, "combined")
os.makedirs(RAW_DIR, exist_ok=True)
os.makedirs(COMBINED_DIR, exist_ok=True)

CLIENT_CSV = os.path.join(RAW_DIR, "client_energy_all.csv")
SERVER_CSV = os.path.join(RAW_DIR, "server_energy_summary.csv")

CLIENT_FIELDS = [
    "run_id", "trial_num", "delivery_mode", "http_protocol", "tls_enabled",
    "container_format", "drm_enabled", "client_energy_j_raw", "client_idle_watts",
    "client_energy_j_corrected", "client_duration_s", "bytes_transferred_b",
    "num_requests", "stall_count", "stall_duration_s", "http_version_actual",
    "client_platform", "client_measure_method", "n_samples", "timestamp_iso",
]
SERVER_FIELDS = [
    "run_id", "server_energy_pkg_j", "server_energy_dram_j", "server_energy_total_j",
    "server_idle_watts", "server_energy_corrected_j", "server_duration_s",
    "server_rapl_wrapped", "timestamp_iso",
]


def _open_csv(path: str, fields: list[str]):
    exists = os.path.isfile(path)
    f = open(path, "a", newline="")
    writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
    if not exists:
        writer.writeheader()
    return f, writer


def _load_completed_run_ids() -> set[str]:
    if not os.path.isfile(CLIENT_CSV):
        return set()
    with open(CLIENT_CSV) as f:
        reader = csv.DictReader(f)
        return {row["run_id"] for row in reader}


def _compute_server_energy(csv_path: str, idle_watts: float) -> dict:
    """Read RAPL time-series CSV and return energy summary dict."""
    import csv as csv_mod
    rows = []
    if not os.path.isfile(csv_path):
        return {"server_energy_pkg_j": 0, "server_energy_dram_j": 0,
                "server_energy_total_j": 0, "server_idle_watts": idle_watts,
                "server_energy_corrected_j": 0, "server_duration_s": 0,
                "server_rapl_wrapped": False}

    with open(csv_path) as f:
        reader = csv_mod.DictReader(f)
        rows = list(reader)

    if len(rows) < 2:
        return {"server_energy_pkg_j": 0, "server_energy_dram_j": 0,
                "server_energy_total_j": 0, "server_idle_watts": idle_watts,
                "server_energy_corrected_j": 0, "server_duration_s": 0,
                "server_rapl_wrapped": False}

    first, last = rows[0], rows[-1]
    duration_s = (int(last["timestamp_ns"]) - int(first["timestamp_ns"])) / 1e9

    # Sum energy across all non-timestamp columns, using first→last delta
    pkg_uj = dram_uj = 0
    wrapped = False
    for col in first.keys():
        if col == "timestamp_ns":
            continue
        before = int(first[col])
        after  = int(last[col])
        delta  = after - before
        if delta < 0:
            wrapped = True
            # Approximate max range as 2^32 microjoules (~4.3 kJ) if unknown
            delta += 2**32
        if "dram" in col.lower() or "ram" in col.lower():
            dram_uj += delta
        else:
            pkg_uj += delta

    pkg_j   = pkg_uj  / 1e6
    dram_j  = dram_uj / 1e6
    total_j = pkg_j + dram_j
    corrected = max(0.0, total_j - idle_watts * duration_s)

    return {
        "server_energy_pkg_j": round(pkg_j, 6),
        "server_energy_dram_j": round(dram_j, 6),
        "server_energy_total_j": round(total_j, 6),
        "server_idle_watts": idle_watts,
        "server_energy_corrected_j": round(corrected, 6),
        "server_duration_s": round(duration_s, 3),
        "server_rapl_wrapped": wrapped,
    }


def run_experiment(args) -> None:
    matrix   = build_valid_matrix()
    n_trials = args.trials or TRIALS_PER_CONFIG
    completed = _load_completed_run_ids()

    if args.dry_run:
        print(f"Dry run — {len(matrix)} configs × {n_trials} trials = {len(matrix) * n_trials} runs")
        for c in matrix:
            print(f"  {c['delivery']:4s}  {c['protocol']:5s}  tls={str(c['tls']):5s}  "
                  f"container={c['container']:7s}  drm={c['drm']}")
        return

    # Choose coordinator based on mode
    use_local = args.local or LOCAL_MODE
    if use_local:
        from orchestrator.local_coordinator import LocalCoordinator
        coordinator = LocalCoordinator()
    elif not args.client_only:
        from orchestrator.ssh_coordinator import ServerCoordinator
        coordinator = ServerCoordinator()
    else:
        coordinator = None
    if coordinator:
        coordinator.connect()

    measurer = EnergyMeasurer() if not args.server_only else None
    idle_watts_client = 0.0
    idle_watts_server = 0.0

    # ── Idle baselines ─────────────────────────────────────────────────────────
    if measurer:
        idle_watts_client = measurer.measure_idle(IDLE_BASELINE_S)

    if coordinator:
        print(f"[orchestrator] collecting server idle baseline ({IDLE_BASELINE_S}s)...")
        coordinator.start_idle_baseline(IDLE_BASELINE_S)
        idle_csv_path = os.path.join(RAW_DIR, "rapl_idle.csv")
        coordinator.fetch_idle_csv(idle_csv_path)
        server_idle_summary = _compute_server_energy(idle_csv_path, idle_watts=0.0)
        idle_watts_server = (
            server_idle_summary["server_energy_total_j"] /
            max(server_idle_summary["server_duration_s"], 1)
        )
        print(f"[orchestrator] server idle: {idle_watts_server:.2f} W")

    client_f, client_writer = _open_csv(CLIENT_CSV, CLIENT_FIELDS)
    server_f, server_writer = _open_csv(SERVER_CSV, SERVER_FIELDS)

    try:
        for cfg_idx, config in enumerate(matrix):
            delivery  = config["delivery"]
            protocol  = config["protocol"]
            tls       = config["tls"]
            container = config["container"]
            drm       = config["drm"]
            nginx_conf = config.get("nginx_conf")

            print(f"\n[orchestrator] config {cfg_idx+1}/{len(matrix)}: "
                  f"{delivery} {protocol} tls={tls} container={container} drm={drm}")

            if coordinator and nginx_conf:
                coordinator.reload_nginx(nginx_conf)

            for trial in range(n_trials):
                ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                tls_str = "na" if tls == "N/A" else str(int(tls))
                drm_str = "na" if drm == "N/A" else str(int(bool(drm)))
                run_id = f"{delivery}_{protocol}_tls{tls_str}_{container}_drm{drm_str}_t{trial}_{ts}"

                if run_id in completed:
                    print(f"  [skip] {run_id}")
                    continue

                print(f"  trial {trial+1}/{n_trials}  run_id={run_id}")

                if coordinator:
                    coordinator.start_server_measurement(run_id)
                if measurer:
                    measurer.start(idle_watts_client)

                # ── Run transfer ───────────────────────────────────────────────
                bytes_b = 0; duration_s = 0.0; num_requests = 1
                stall_count = 0; stall_duration_s = 0.0; http_version = "N/A"
                success = True; error = ""

                try:
                    if delivery == "bulk":
                        r = run_bulk(protocol, tls, container)
                        bytes_b = r.bytes_b
                        duration_s = r.duration_s
                        http_version = r.http_version_actual
                        success = r.success
                        error = r.error

                    elif delivery == "hls":
                        r = run_hls(protocol, tls, drm)
                        bytes_b = r.qos.total_bytes_b
                        duration_s = r.duration_s
                        num_requests = r.qos.num_requests
                        stall_count = r.qos.stall_count
                        stall_duration_s = r.qos.total_stall_duration_s
                        http_version = protocol
                        success = r.success
                        error = r.error

                    elif delivery == "p2p":
                        r = run_p2p()
                        bytes_b = r.bytes_b
                        duration_s = r.duration_s
                        success = r.success
                        error = r.error

                except Exception as exc:
                    success = False
                    error = str(exc)

                # ── Stop measurement ───────────────────────────────────────────
                energy_result = None
                if measurer:
                    energy_result = measurer.stop()

                if coordinator:
                    coordinator.stop_server_measurement(run_id)

                # ── Pull server CSV ────────────────────────────────────────────
                server_energy = {}
                if coordinator:
                    rapl_csv_path = os.path.join(RAW_DIR, f"rapl_{run_id}.csv")
                    coordinator.fetch_server_csv(run_id, rapl_csv_path)
                    server_energy = _compute_server_energy(rapl_csv_path, idle_watts_server)

                # ── Write client row ───────────────────────────────────────────
                client_row = {
                    "run_id": run_id,
                    "trial_num": trial,
                    "delivery_mode": delivery,
                    "http_protocol": protocol,
                    "tls_enabled": tls,
                    "container_format": container,
                    "drm_enabled": drm,
                    "client_energy_j_raw": round(energy_result.energy_j_raw, 6) if energy_result else "N/A",
                    "client_idle_watts": round(energy_result.idle_watts, 4) if energy_result else "N/A",
                    "client_energy_j_corrected": round(energy_result.energy_j_corrected, 6) if energy_result else "N/A",
                    "client_duration_s": round(duration_s, 3),
                    "bytes_transferred_b": bytes_b,
                    "num_requests": num_requests,
                    "stall_count": stall_count,
                    "stall_duration_s": round(stall_duration_s, 3),
                    "http_version_actual": http_version,
                    "client_platform": energy_result.platform if energy_result else "N/A",
                    "client_measure_method": energy_result.method if energy_result else "N/A",
                    "n_samples": energy_result.n_samples if energy_result else 0,
                    "timestamp_iso": ts,
                }
                if not success:
                    print(f"    [WARN] transfer error: {error}")
                client_writer.writerow(client_row)
                client_f.flush()

                # ── Write server row ───────────────────────────────────────────
                if server_energy:
                    server_energy["run_id"] = run_id
                    server_energy["timestamp_iso"] = ts
                    server_writer.writerow(server_energy)
                    server_f.flush()

                print(f"    bytes={bytes_b:,}  duration={duration_s:.1f}s  "
                      f"client_j={client_row['client_energy_j_corrected']}")

                time.sleep(COOLDOWN_S)

    finally:
        client_f.close()
        server_f.close()
        if coordinator:
            coordinator.disconnect()
        if measurer and hasattr(measurer, "shutdown"):
            measurer.shutdown()

    print(f"\n[orchestrator] done. Results in {RAW_DIR}/")
    print("Run analysis/merge.py then analysis/analyze.py to compute metrics and plots.")


def main() -> None:
    parser = argparse.ArgumentParser(description="EEC streaming energy experiment orchestrator")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--local", action="store_true",
                        help="Single-machine mode: run nginx locally (Mac default)")
    parser.add_argument("--client-only", action="store_true",
                        help="Skip server coordination entirely")
    parser.add_argument("--server-only", action="store_true",
                        help="Skip client energy measurement")
    parser.add_argument("--resume", metavar="RUN_ID",
                        help="Skip configs already in client CSV")
    parser.add_argument("--trials", type=int, default=None)
    args = parser.parse_args()
    run_experiment(args)


if __name__ == "__main__":
    main()
