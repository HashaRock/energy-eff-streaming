# Codec / DRM Energy Testbench

This document describes the design, implementation, and usage of the second measurement suite added to this project. It extends the original HTTP protocol comparison with two new experimental axes: **video container/codec format** and **DRM-style encryption scheme**.

---

## Background and Motivation

The original test suite (`orchestrator/orchestrator.py`) revealed that:
- HTTP/2 costs ~11% more J/MB than HTTP/1.1 for HLS on loopback.
- TLS adds ≤2% overhead (within noise).
- AES-128 HLS encryption showed **zero measurable impact** — but only because the original driver (`driver_hls.py`) fetches the AES-128 key over the network but **never actually decrypts the segment bytes**. It measures network overhead only, not cipher overhead.
- Bulk container comparisons (MP4 vs MKV vs AV1) yielded J/MB = 0 for all formats because loopback transfers complete in ~0.2 s — below the 500 ms powermetrics sampling window.

This testbench addresses all three gaps:

| Gap | Fix |
|-----|-----|
| Bulk transfers too fast for powermetrics | nginx `limit_rate 10m` on `/bulk/` → ~9 s per file → ~18 samples |
| No codec comparison for HLS | New AV1/fMP4 HLS variant generated with ffmpeg |
| Encryption overhead not measured | New driver (`driver_hls_drm.py`) decrypts segments with three cipher modes |

---

## Files Added

| File | Purpose |
|------|---------|
| `setup/generate_codec_drm_assets.sh` | One-time asset generation (run before experiments) |
| `server/nginx/mac/nginx_codec_drm.conf` | nginx config: rate-limited bulk + new HLS dirs + key endpoint |
| `client/drivers/driver_hls_drm.py` | DRM-aware HLS driver with three decryption modes |
| `orchestrator/orchestrator_codec_drm.py` | Standalone orchestrator; writes `data/raw/client_energy_codec_drm.csv` |
| `analysis/analyze_codec_drm.py` | Plots and LaTeX table from the new CSV |
| `requirements.txt` | Added `pycryptodome>=3.20.0` |

The original files (`orchestrator.py`, `driver_hls.py`, `client_energy_all.csv`) are **not touched**.

---

## Quick Start

```bash
# 1. Install new dependency
pip install pycryptodome

# 2. Generate assets (one-time, ~2-5 min depending on AV1 encode speed)
bash setup/generate_codec_drm_assets.sh

# 3. Dry run to verify experiment matrix
python3 orchestrator/orchestrator_codec_drm.py --dry-run

# 4. Run all experiments (~4 hours total)
python3 orchestrator/orchestrator_codec_drm.py --group all

# Run only one group during development
python3 orchestrator/orchestrator_codec_drm.py --group codec --trials 2
python3 orchestrator/orchestrator_codec_drm.py --group drm   --trials 2

# 5. Analyse and plot
python3 analysis/analyze_codec_drm.py
```

---

## Experiment Design

### Group A — Codec / Container Comparison (5 configs × 5 trials)

All HTTP/1.1, no TLS.

| Config | Method | Asset | What varies |
|--------|--------|-------|-------------|
| `bulk/mp4` | curl download | `bulk/test.mp4` (H.264/AAC) | Container format |
| `bulk/mkv` | curl download | `bulk/test.mkv` (H.264/AAC in MKV) | Container format |
| `bulk/av1_mp4` | curl download | `bulk/test_av1.mp4` (AV1/AAC) | Codec |
| `hls/h264_ts` | HLS emulator | `hls/plain/` (H.264 TS segments) | Delivery + codec |
| `hls/av1_fmp4` | HLS emulator | `hls/av1_plain/` (AV1 fMP4 segments) | Delivery + codec |

**Bulk rate limiting:** nginx `limit_rate 10m` (10 MB/s) slows loopback downloads to ~9 s for a 90 MB file, which yields ~18 powermetrics samples at the 500 ms polling rate. Without this, all three bulk configs give J/MB = 0 (as seen in the original suite).

### Group B — DRM / Encryption Comparison (5 configs × 5 trials)

All HTTP/1.1, no TLS. Fixed H.264 content to isolate the encryption variable.

| Config | Segments used | Key endpoint | Cipher in driver | Models |
|--------|--------------|--------------|-----------------|--------|
| `none` | `hls/plain/` | — | No decryption | Unencrypted baseline |
| `aes128_net_only` | `hls/encrypted/` | `/keys/aes128.bin` | Key fetched, not used | Original AES-128 test (network cost only) |
| `aes128_decrypt` | `hls/encrypted/` | `/keys/aes128.bin` | AES-128-CBC | Standard HLS AES-128 with software decryption |
| `cenc` | `hls/encrypted/` | `/keys/cenc.bin` | AES-128-CTR | CENC Common Encryption (Widevine cipher mode) |
| `cbcs` | `hls/encrypted/` | `/keys/cbcs.bin` | AES-128-CBC | CBCS Common Encryption (PlayReady cipher mode) |

**Why CENC/CBCS use the same segments as AES-128:**

True CENC and CBCS packaging requires shaka-packager and a license server infrastructure (Google Widevine requires a Google-issued license; Microsoft PlayReady requires Windows DRM infrastructure). Neither is feasible in a local academic testbed.

Instead, we measure what is measurable: **the CPU and energy cost of applying each cipher mode to the same volume of ciphertext**. The key endpoint (`/keys/cenc.bin`, `/keys/cbcs.bin`) returns a random 16-byte key; the driver applies AES-128-CTR or AES-128-CBC to each segment's bytes. The output is discarded (we are measuring energy, not decoding video). The number of AES block operations per segment is identical regardless of whether the underlying bits were encrypted with that cipher, so the measurement correctly captures the relative cipher overhead.

The paper's methodology section must note this approximation.

### The Mock License Endpoint

nginx serves raw key files at `http://localhost:8080/keys/<name>.bin`. This endpoint:
- Simulates the **round-trip latency** of a DRM license request (even if the content is a static file; on loopback this is ~0.5 ms).
- Lets us count `num_license_fetches` in the CSV — one per stream (not per segment, matching real CDM behaviour).

---

## Energy Measurement

Same infrastructure as the original suite:
- macOS `powermetrics` daemon polls every 500 ms, writing `(timestamp_ns, cpu_power_mw)` to `/tmp/eec_power_monitor.csv`.
- `EnergyMeasurer.start()` / `.stop()` bracket each transfer; energy = avg_watts × duration_s.
- Raw energy is used (not idle-corrected) because macOS background processes keep the idle baseline above ~347 mW while HLS playback adds only ~68 mW — idle correction produces zero or negative values. Relative comparisons remain valid because all trials run under identical background conditions.

**Minimum sample count check:** The analysis script warns on rows with `n_samples < 2`. Bulk configs with rate limiting should always achieve ≥18 samples; HLS configs typically capture ≥360 samples (streaming at segment pace over ~180 s).

---

## New CSV Schema (`data/raw/client_energy_codec_drm.csv`)

| Column | Notes |
|--------|-------|
| `experiment_group` | `"codec"` or `"drm"` |
| `delivery_mode` | `"bulk_ratelimited"`, `"hls_codec"`, or `"hls_drm"` |
| `container_format` | `"mp4"`, `"mkv"`, `"av1_mp4"`, `"h264_ts"`, `"av1_fmp4"`, `"aes128_ts"` |
| `drm_scheme` | `"none"`, `"aes128_net_only"`, `"aes128_decrypt"`, `"cenc"`, `"cbcs"` |
| `decrypt_cpu_time_s` | Wall time inside AES cipher calls only (0 for non-decrypt schemes) |
| `num_license_fetches` | 0 or 1 (the `/keys/` GET) |
| All other columns | Same as `client_energy_all.csv` |

---

## Asset Generation Details

`setup/generate_codec_drm_assets.sh` produces:

```
server/assets/hls/av1_plain/      AV1 fMP4 HLS — stream-copied from bulk/test_av1.mp4
server/assets/hls/encrypted/      H.264 TS HLS (AES-128-CBC encrypted; existing)
server/assets/keys/aes128.bin     16-byte raw AES key for encrypted TS stream
server/assets/keys/cenc.bin       16-byte random key (CENC emulation)
server/assets/keys/cbcs.bin       16-byte random key (CBCS emulation)
server/assets/keys/manifest.json  {scheme → {key_hex, iv_hex}} for driver use
server/tls/hls.key                raw key file used by encrypted TS HLS playlist
```

Re-running the script is safe (existing outputs are skipped). Use `--force` to regenerate everything.

---

## Outputs from `analyze_codec_drm.py`

| File | Content |
|------|---------|
| `analysis/plots/codec_drm_01_codec_bulk.png` | Bar chart: J/MB by bulk container (MP4, MKV, AV1) |
| `analysis/plots/codec_drm_02_codec_hls.png` | Bar chart: J/MB by HLS codec (H.264/TS, AV1/fMP4) |
| `analysis/plots/codec_drm_03_drm_overhead.png` | Bar chart: J/MB by DRM scheme |
| `analysis/plots/codec_drm_04_decrypt_cpu.png` | Bar chart: decrypt wall time (ms) by cipher mode |
| `analysis/tables/codec_drm_table.tex` | IEEE two-column LaTeX table for paper |

---

## Expected Runtime

| Group | Configs | Trials | Approx. wall time |
|-------|---------|--------|-------------------|
| codec — bulk | 3 | 5 | ~7 min (9 s/trial + 10 s cooldown) |
| codec — HLS | 2 | 5 | ~30 min (180 s/trial + 10 s cooldown) |
| drm | 5 | 5 | ~75 min (same HLS pacing) |
| **Total** | 10 | 5 | **~2 hours** |

The single shared idle baseline (10 s) is measured once at startup.

---

## Limitations

- **macOS only.** The power monitor uses `powermetrics`; RAPL is Linux-only.
- **CENC/CBCS are approximations.** Full compliance requires shaka-packager + license server infrastructure. See design rationale above.
- **Python AES, not hardware CDM.** Real Widevine and PlayReady offload crypto to a Trusted Execution Environment; our Python AES cipher is the worst-case CPU cost and will over-estimate the overhead a real device would see.
- **Loopback network.** No real-world contention, variable bandwidth, or Wi-Fi overhead.
- **No server-side energy.** Single-machine mode; server energy is included in client measurements and cannot be separated.
