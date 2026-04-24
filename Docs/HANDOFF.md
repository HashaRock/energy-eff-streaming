# Codec/DRM Testbench — Handoff Notes

**Session date:** 2026-04-24  
**Status:** Implementation ~90% complete. Assets generated. One script bug blocking a clean run.

---

## What Was Built

A second, standalone measurement suite layered on top of the existing experiment infrastructure. It adds two new experimental axes that the original suite could not measure:

### 1. Codec / Container Energy Comparison

The original bulk download tests (MP4 vs MKV vs AV1) all returned J/MB = 0 because loopback transfers finish in ~0.2 s — below the 500 ms powermetrics sampling window. The fix: add `limit_rate 10m` (10 MB/s) to nginx so a 90 MB file takes ~9 s and captures ~18 samples.

A new AV1/fMP4 HLS variant was also generated so codec overhead can be compared within HLS delivery (not just bulk).

### 2. Encryption / DRM Energy Comparison

The original `driver_hls.py` fetches the AES-128 key over the network but **never decrypts segments** — so "zero overhead" only meant zero *network* overhead. The new driver actually decrypts, and does so with three cipher modes to approximate real DRM schemes:

| Scheme | What it does | Models |
|--------|-------------|--------|
| `none` | No key fetch, no decrypt | Plaintext baseline |
| `aes128_net_only` | Key fetched, segments not decrypted | Original behaviour (network overhead only) |
| `aes128_decrypt` | Key fetched + AES-128-CBC per segment | Standard HLS AES-128 with software CDM |
| `cenc` | Key from `/keys/cenc.bin` + AES-128-CTR | CENC cipher mode (Google Widevine) |
| `cbcs` | Key from `/keys/cbcs.bin` + AES-128-CBC | CBCS cipher mode (Microsoft PlayReady) |

**Note on CENC/CBCS:** Real Widevine/PlayReady packaging requires vendor infrastructure (Google/Microsoft license servers, hardware TEE). Instead, all three decrypt modes run against the same existing `hls/encrypted/` AES-128 TS segments; only the cipher mode and key endpoint differ. This correctly measures the relative CPU energy cost of CTR vs CBC vs no-decrypt — which is the energy-relevant variable — without needing real DRM packaging.

---

## Files Created

| File | Purpose | Status |
|------|---------|--------|
| `server/nginx/mac/nginx_codec_drm.conf` | nginx: rate-limited `/bulk/` + `/keys/` endpoint | ✅ Done, validated |
| `client/drivers/driver_hls_drm.py` | HLS driver with decryption (CBC, CTR, CBCS) | ✅ Done |
| `orchestrator/orchestrator_codec_drm.py` | Standalone orchestrator; outputs to `data/raw/client_energy_codec_drm.csv` | ✅ Done |
| `setup/generate_codec_drm_assets.sh` | Generates AV1 fMP4 HLS + CENC/CBCS key files | ⚠️ Script exits early due to bash glob + `pipefail` bug (see below) |
| `analysis/analyze_codec_drm.py` | 4 plots + LaTeX table from new CSV | ✅ Done |
| `requirements.txt` | Added `pycryptodome>=3.20.0` | ✅ Done |
| `Docs/codec_drm_testbench.md` | Full design documentation | ✅ Done |

**Nothing in the original suite was touched.** `client_energy_all.csv`, `orchestrator.py`, `driver_hls.py`, and all existing analysis outputs are unchanged.

### Assets generated so far

`bash setup/generate_codec_drm_assets.sh` was partially run. These exist:

```
server/assets/hls/av1_plain/      ✅  23 AV1 fMP4 segments + master.m3u8
server/assets/keys/aes128.bin     ✅  (copy of server/tls/hls.key)
server/assets/keys/cenc.bin       ✅  random 16-byte key
server/assets/keys/cbcs.bin       ✅  random 16-byte key
server/assets/keys/manifest.json  ✅  key hex + IV map
```

---

## The One Remaining Bug

`setup/generate_codec_drm_assets.sh` has `set -euo pipefail` at the top. The sanity-check loop at the bottom does:

```bash
n_segs=$(ls "$dir"/*.m4s "$dir"/*.ts 2>/dev/null | wc -l | tr -d ' ')
```

When a directory has no `.m4s` files (e.g. `hls/plain/` which only has `.ts`), `ls *.m4s` exits non-zero and `pipefail` kills the script before it finishes printing.

**One-line fix** — replace that `ls` line (around line 232) with:

```bash
n_segs=$(find "$dir" -maxdepth 1 \( -name "*.m4s" -o -name "*.ts" \) 2>/dev/null | wc -l | tr -d ' ')
```

`find` always exits 0 when the directory exists, so `pipefail` doesn't trigger.

After that fix, the script completes cleanly. The assets it was trying to summarise already exist (they were generated before the crash), so this is a cosmetic print bug — not a missing-asset bug.

---

## Steps to Resume and Complete

```bash
# 0. Make sure pycryptodome is installed
pip install pycryptodome

# 1. Fix the sanity-check loop in generate_codec_drm_assets.sh
#    Replace line ~232:
#      n_segs=$(ls "$dir"/*.m4s "$dir"/*.ts 2>/dev/null | wc -l | tr -d ' ')
#    With:
#      n_segs=$(find "$dir" -maxdepth 1 \( -name "*.m4s" -o -name "*.ts" \) 2>/dev/null | wc -l | tr -d ' ')

# 2. Re-run the asset script (skips already-generated assets)
bash setup/generate_codec_drm_assets.sh

# 3. Verify dry run looks right
python3 orchestrator/orchestrator_codec_drm.py --dry-run
# Expected: 10 configs × 5 trials = 50 runs

# 4. Quick smoke test (1 trial per config, ~15 min)
python3 orchestrator/orchestrator_codec_drm.py --group codec --trials 1
# Check: tail -n 6 data/raw/client_energy_codec_drm.csv
# All rows should have n_samples >= 2 (if bulk rows show 0, lower limit_rate in nginx config)

# 5. Run the DRM group smoke test
python3 orchestrator/orchestrator_codec_drm.py --group drm --trials 1

# 6. Full 5-trial run (~2 hours)
python3 orchestrator/orchestrator_codec_drm.py --group all

# 7. Analyse
python3 analysis/analyze_codec_drm.py
# Outputs: analysis/plots/codec_drm_0{1,2,3,4}.png
#          analysis/tables/codec_drm_table.tex
```

---

## Potential Issues to Watch For

**Bulk n_samples = 0:** If bulk trials still show 0 powermetrics samples, the rate limit may be too high for the file sizes. Reduce `limit_rate 10m` to `limit_rate 5m` in `server/nginx/mac/nginx_codec_drm.conf` and re-run.

**AV1 fMP4 driver stalls:** The `av1_fmp4` HLS driver uses `_fetch_url` (urllib) instead of curl for fMP4 segments since curl discards bytes. If urllib is slow on large segments, increase `timeout=30` in `_fetch_url` calls inside `driver_hls.py` (which `driver_hls_drm.py` imports from).

**CENC/CBCS key mismatch warning:** The driver applies AES-CTR/CBC with the cenc.bin/cbcs.bin keys to bytes that are actually encrypted with the aes128.bin key. The output is discarded — this is intentional. The energy measurement is valid; just don't interpret the decrypted bytes as video.

**Paper integration:** Once analysis runs, include `codec_drm_table.tex` in the paper via `\input{codec_drm_table}` and add the 4 PNG plots as new figures. The methodology section should mention the CENC/CBCS approximation approach (described in detail in `Docs/codec_drm_testbench.md`).

---

## Key Design Decisions (for context)

- **Why existing `hls/encrypted/` TS segments for DRM tests, not new fMP4?** ffmpeg 7.1.1 does not support AES-128 encrypted fMP4 HLS output (`"Encrypted fmp4 not yet supported"`). shaka-packager could do it, but isn't installed on the Mac client. Using existing TS assets avoids this dependency while keeping the cipher-overhead comparison valid.

- **Why raw energy, not idle-corrected?** macOS background processes keep idle CPU power at ~347 mW; HLS transfers add only ~68 mW on top. Idle correction produces zero or negative values. All configs run under identical background conditions so relative comparisons are valid. This matches the original suite's decision (`analysis/merge.py` line 46).

- **Why a separate orchestrator file?** Keeps new experiment data in a separate CSV (`client_energy_codec_drm.csv`) and avoids any risk of corrupting the existing `client_energy_all.csv` that the paper is already built from.
