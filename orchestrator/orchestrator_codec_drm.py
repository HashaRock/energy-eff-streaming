#!/usr/bin/env python3
"""
Codec/Container and DRM Encryption energy measurement orchestrator.
Standalone — writes to data/raw/client_energy_codec_drm.csv and does NOT
touch the existing client_energy_all.csv from the original experiment suite.

Usage:
    python3 orchestrator/orchestrator_codec_drm.py [--dry-run] [--trials N]
                                                    [--group codec|drm|all]

Experiment groups
-----------------
codec  (5 configs × N trials)
    Rate-limited bulk downloads: MP4, MKV, AV1-MP4
    HLS codec comparison:        H.264/TS, AV1/fMP4
    nginx: nginx_codec_drm.conf (rate-capped at 10 MB/s on /bulk/)

drm    (5 configs × N trials)
    HLS encryption comparison:
        none           — plain H.264/TS baseline
        aes128_net_only— key fetch, no decrypt  (matches original drm=True)
        aes128_decrypt — key fetch + AES-128-CBC per segment
        cenc           — key fetch + AES-128-CTR (Widevine-like CENC)
        cbcs           — key fetch + AES-128-CBC (PlayReady-like CBCS)
    nginx: nginx_codec_drm.conf

Both groups run HTTP/1.1 plain (no TLS) to isolate the codec/DRM variable.
All 10 configs run in a single session sharing one idle baseline measurement.
"""
import argparse
import csv
import datetime
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.config import (
    DATA_DIR, TRIALS_PER_CONFIG, COOLDOWN_S, IDLE_BASELINE_S, LOCAL_MODE
)
from orchestrator.local_coordinator import LocalCoordinator
from client.measure_client import EnergyMeasurer
from client.drivers.driver_bulk import run_bulk
from client.drivers.driver_hls_drm import run_hls_drm

# ── Paths ──────────────────────────────────────────────────────────────────────
RAW_DIR = os.path.join(DATA_DIR, "raw")
os.makedirs(RAW_DIR, exist_ok=True)

OUTPUT_CSV   = os.path.join(RAW_DIR, "client_energy_codec_drm.csv")
NGINX_CONF   = "nginx_codec_drm.conf"   # serves both bulk (rate-limited) + HLS

# ── Experiment matrix ──────────────────────────────────────────────────────────
# Each entry: group, delivery, hls_variant (None for bulk), drm_scheme, container
CODEC_CONFIGS: list[dict] = [
    # Rate-limited bulk: isolates container format at controlled bandwidth
    {"group": "codec", "delivery": "bulk_ratelimited", "hls_variant": None,
     "drm_scheme": "none", "container": "mp4"},
    {"group": "codec", "delivery": "bulk_ratelimited", "hls_variant": None,
     "drm_scheme": "none", "container": "mkv"},
    {"group": "codec", "delivery": "bulk_ratelimited", "hls_variant": None,
     "drm_scheme": "none", "container": "av1_mp4"},
    # HLS codec: H.264/TS (existing assets) vs AV1/fMP4 (new assets)
    {"group": "codec", "delivery": "hls_codec", "hls_variant": "h264_ts",
     "drm_scheme": "none", "container": "h264_ts"},
    {"group": "codec", "delivery": "hls_codec", "hls_variant": "av1_fmp4",
     "drm_scheme": "none", "container": "av1_fmp4"},
]

DRM_CONFIGS: list[dict] = [
    # Baseline: plain H.264/TS, no encryption
    {"group": "drm", "delivery": "hls_drm", "hls_variant": "h264_ts",
     "drm_scheme": "none", "container": "h264_ts"},
    # AES-128 TS: key fetched over network, segments NOT decrypted
    # (matches original driver_hls.py drm=True; measures license RTT only)
    {"group": "drm", "delivery": "hls_drm", "hls_variant": "aes128_ts",
     "drm_scheme": "aes128_net_only", "container": "aes128_ts"},
    # AES-128 TS: key fetched + AES-128-CBC decryption per segment
    {"group": "drm", "delivery": "hls_drm", "hls_variant": "aes128_ts",
     "drm_scheme": "aes128_decrypt", "container": "aes128_ts"},
    # CENC approximation: same AES-128 TS segments, CTR-mode cipher (Widevine-like)
    {"group": "drm", "delivery": "hls_drm", "hls_variant": "aes128_ts",
     "drm_scheme": "cenc", "container": "aes128_ts"},
    # CBCS approximation: same AES-128 TS segments, CBC-mode cipher (PlayReady-like)
    {"group": "drm", "delivery": "hls_drm", "hls_variant": "aes128_ts",
     "drm_scheme": "cbcs", "container": "aes128_ts"},
]

# ── CSV schema ─────────────────────────────────────────────────────────────────
OUTPUT_FIELDS = [
    "run_id", "trial_num",
    "experiment_group",        # "codec" | "drm"
    "delivery_mode",           # "bulk_ratelimited" | "hls_codec" | "hls_drm"
    "container_format",        # "mp4" | "mkv" | "av1_mp4" | "h264_ts" | "av1_fmp4" | "aes128_ts"
    "drm_scheme",              # "none" | "aes128_net_only" | "aes128_decrypt" | "cenc" | "cbcs"
    "http_protocol",           # always "HTTP1" (protocol isolated; other axes vary separately)
    "tls_enabled",             # always False
    "client_energy_j_raw",
    "client_idle_watts",
    "client_energy_j_corrected",
    "client_duration_s",
    "bytes_transferred_b",
    "num_requests",
    "decrypt_cpu_time_s",      # seconds in AES cipher calls only (0 for non-decrypt schemes)
    "num_license_fetches",     # how many times /keys/ endpoint was hit (0 or 1)
    "stall_count",
    "stall_duration_s",
    "client_platform",
    "client_measure_method",
    "n_samples",
    "timestamp_iso",
]


def _open_csv(path: str):
    exists = os.path.isfile(path)
    f = open(path, "a", newline="")
    writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
    if not exists:
        writer.writeheader()
    return f, writer


def _label(cfg: dict) -> str:
    if cfg["delivery"] == "bulk_ratelimited":
        return f"bulk/{cfg['container']}"
    return f"{cfg['delivery']}/{cfg['container']}  drm={cfg['drm_scheme']}"


def run_experiment(args) -> None:
    group_filter = args.group  # "codec", "drm", or "all"
    n_trials = args.trials or TRIALS_PER_CONFIG

    # Build config list based on filter
    all_configs: list[dict] = []
    if group_filter in ("codec", "all"):
        all_configs.extend(CODEC_CONFIGS)
    if group_filter in ("drm", "all"):
        all_configs.extend(DRM_CONFIGS)

    if args.dry_run:
        print(f"Dry run — {len(all_configs)} configs × {n_trials} trials = "
              f"{len(all_configs) * n_trials} runs")
        print(f"nginx config: {NGINX_CONF}\n")
        for c in all_configs:
            print(f"  [{c['group']:5s}]  {_label(c)}")
        return

    coordinator = LocalCoordinator()
    coordinator.connect()
    coordinator.reload_nginx(NGINX_CONF)

    measurer = EnergyMeasurer()
    idle_watts = measurer.measure_idle(IDLE_BASELINE_S)

    csv_f, writer = _open_csv(OUTPUT_CSV)

    try:
        for cfg_idx, cfg in enumerate(all_configs):
            group    = cfg["group"]
            delivery = cfg["delivery"]
            variant  = cfg["hls_variant"]
            scheme   = cfg["drm_scheme"]
            container = cfg["container"]

            print(f"\n[orchestrator] config {cfg_idx+1}/{len(all_configs)}: "
                  f"[{group}] {_label(cfg)}")

            for trial in range(n_trials):
                ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                run_id = (f"{group}_{delivery}_{container}_{scheme}"
                          f"_t{trial}_{ts}")
                print(f"  trial {trial+1}/{n_trials}  run_id={run_id}")

                measurer.start(idle_watts)

                # ── Run transfer ───────────────────────────────────────────────
                bytes_b = 0
                duration_s = 0.0
                num_requests = 1
                stall_count = 0
                stall_duration_s = 0.0
                decrypt_cpu_time_s = 0.0
                num_license_fetches = 0
                success = True
                error = ""

                try:
                    if delivery == "bulk_ratelimited":
                        r = run_bulk("HTTP1", False, container)
                        bytes_b = r.bytes_b
                        duration_s = r.duration_s
                        success = r.success
                        error = r.error

                    else:  # hls_codec or hls_drm
                        r = run_hls_drm(variant, scheme)
                        bytes_b = r.qos.total_bytes_b
                        duration_s = r.duration_s
                        num_requests = r.qos.num_requests
                        stall_count = r.qos.stall_count
                        stall_duration_s = r.qos.total_stall_duration_s
                        decrypt_cpu_time_s = r.decrypt_cpu_time_s
                        num_license_fetches = r.num_license_fetches
                        success = r.success
                        error = r.error

                except Exception as exc:
                    success = False
                    error = str(exc)

                energy = measurer.stop()

                if not success:
                    print(f"    [WARN] {error}")

                row = {
                    "run_id":                  run_id,
                    "trial_num":               trial,
                    "experiment_group":        group,
                    "delivery_mode":           delivery,
                    "container_format":        container,
                    "drm_scheme":              scheme,
                    "http_protocol":           "HTTP1",
                    "tls_enabled":             False,
                    "client_energy_j_raw":     round(energy.energy_j_raw, 6),
                    "client_idle_watts":       round(energy.idle_watts, 4),
                    "client_energy_j_corrected": round(energy.energy_j_corrected, 6),
                    "client_duration_s":       round(duration_s, 3),
                    "bytes_transferred_b":     bytes_b,
                    "num_requests":            num_requests,
                    "decrypt_cpu_time_s":      round(decrypt_cpu_time_s, 6),
                    "num_license_fetches":     num_license_fetches,
                    "stall_count":             stall_count,
                    "stall_duration_s":        round(stall_duration_s, 3),
                    "client_platform":         energy.platform,
                    "client_measure_method":   energy.method,
                    "n_samples":               energy.n_samples,
                    "timestamp_iso":           ts,
                }
                writer.writerow(row)
                csv_f.flush()

                mb = bytes_b / 1e6
                j_per_mb = (energy.energy_j_raw / mb) if mb > 0 else 0
                print(f"    bytes={bytes_b:,}  dur={duration_s:.1f}s  "
                      f"J/MB={j_per_mb:.4f}  samples={energy.n_samples}"
                      + (f"  decrypt={decrypt_cpu_time_s*1000:.1f}ms" if decrypt_cpu_time_s else ""))

                time.sleep(COOLDOWN_S)

    finally:
        csv_f.close()
        coordinator.disconnect()
        measurer.shutdown()

    print(f"\n[orchestrator] done. Results → {OUTPUT_CSV}")
    print("Run:  python3 analysis/analyze_codec_drm.py")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Codec/DRM energy comparison orchestrator"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print experiment matrix without running")
    parser.add_argument("--trials", type=int, default=None,
                        help=f"Trials per config (default: {TRIALS_PER_CONFIG})")
    parser.add_argument("--group", choices=["codec", "drm", "all"], default="all",
                        help="Which experiment group to run (default: all)")
    args = parser.parse_args()
    run_experiment(args)


if __name__ == "__main__":
    main()
