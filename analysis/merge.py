#!/usr/bin/env python3
"""
Merges client_energy_all.csv + server_energy_summary.csv on run_id,
computes derived metrics, and writes data/combined/results_<ts>.csv.

Usage:
    python3 analysis/merge.py [--client CSV] [--server CSV] [--out DIR]
"""
import argparse
import datetime
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
RAW_DIR  = os.path.join(DATA_DIR, "raw")
OUT_DIR  = os.path.join(DATA_DIR, "combined")


def merge(client_csv: str, server_csv: str | None, out_dir: str) -> str:
    client_df = pd.read_csv(client_csv)

    if server_csv and os.path.isfile(server_csv):
        server_df = pd.read_csv(server_csv)
        df = client_df.merge(server_df, on="run_id", how="left", suffixes=("", "_srv"))
    else:
        df = client_df.copy()
        for col in ["server_energy_pkg_j", "server_energy_dram_j", "server_energy_total_j",
                    "server_idle_watts", "server_energy_corrected_j", "server_duration_s",
                    "server_rapl_wrapped"]:
            df[col] = float("nan")

    # ── Derived metrics ────────────────────────────────────────────────────────
    mb = df["bytes_transferred_b"] / 1e6

    # Avoid divide-by-zero
    safe_mb = mb.replace(0, float("nan"))
    safe_req = df["num_requests"].replace(0, float("nan"))
    safe_dur = df["client_duration_s"].replace(0, float("nan"))

    # Use raw energy: idle correction overcorrects on Mac (startup activity inflates idle baseline)
    client_j = pd.to_numeric(df["client_energy_j_raw"], errors="coerce")
    df["joules_per_mb_client"] = client_j / safe_mb
    df["joules_per_mb_server"] = (
        pd.to_numeric(df.get("server_energy_corrected_j", float("nan")), errors="coerce") / safe_mb
    )
    df["joules_per_mb_total"] = df["joules_per_mb_client"].fillna(0) + df["joules_per_mb_server"].fillna(0)
    df["joules_per_request_client"] = client_j / safe_req
    df["joules_per_request_server"] = (
        pd.to_numeric(df.get("server_energy_corrected_j", float("nan")), errors="coerce") / safe_req
    )
    df["rebuffer_ratio"] = (
        df["stall_duration_s"].fillna(0) / safe_dur
    )
    df["throughput_mbps"] = (mb * 8) / safe_dur

    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(out_dir, f"results_{ts}.csv")
    df.to_csv(out_path, index=False)
    print(f"[merge] wrote {len(df)} rows → {out_path}")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", default=os.path.join(RAW_DIR, "client_energy_all.csv"))
    parser.add_argument("--server", default=os.path.join(RAW_DIR, "server_energy_summary.csv"))
    parser.add_argument("--out", default=OUT_DIR)
    args = parser.parse_args()
    merge(args.client, args.server, args.out)


if __name__ == "__main__":
    main()
