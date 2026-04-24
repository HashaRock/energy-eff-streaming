#!/usr/bin/env python3
"""
Analysis and visualisation for the codec/DRM energy testbench.
Reads data/raw/client_energy_codec_drm.csv and produces:

  analysis/plots/codec_drm_01_codec_bulk.png       Bulk download J/MB by container
  analysis/plots/codec_drm_02_codec_hls.png         HLS codec J/MB: H.264/TS vs AV1/fMP4
  analysis/plots/codec_drm_03_drm_overhead.png      DRM scheme J/MB comparison
  analysis/plots/codec_drm_04_decrypt_cpu.png        Decrypt CPU time by scheme
  analysis/tables/codec_drm_table.tex                IEEE-format LaTeX table

Usage:
    python3 analysis/analyze_codec_drm.py [--input PATH]
"""
import argparse
import os
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import pandas as pd
from scipy.stats import median_abs_deviation

PROJECT_ROOT = Path(__file__).parent.parent
PLOTS_DIR = PROJECT_ROOT / "analysis" / "plots"
TABLES_DIR = PROJECT_ROOT / "analysis" / "tables"
DEFAULT_CSV = PROJECT_ROOT / "data" / "raw" / "client_energy_codec_drm.csv"

PLOTS_DIR.mkdir(parents=True, exist_ok=True)
TABLES_DIR.mkdir(parents=True, exist_ok=True)

# Colour palette (matches existing analyze.py style)
BLUE   = "#2196F3"
ORANGE = "#FF9800"
GREEN  = "#4CAF50"
RED    = "#F44336"
PURPLE = "#9C27B0"
GREY   = "#9E9E9E"

CODEC_COLORS = {
    "mp4":         BLUE,
    "mkv":         ORANGE,
    "av1_mp4":     GREEN,
    "h264_ts":     BLUE,
    "av1_fmp4":    GREEN,
}

DRM_COLORS = {
    "none":             GREY,
    "aes128_net_only":  BLUE,
    "aes128_decrypt":   ORANGE,
    "cenc":             RED,
    "cbcs":             PURPLE,
}

DRM_LABELS = {
    "none":             "No Encryption\n(baseline)",
    "aes128_net_only":  "AES-128\n(net only)",
    "aes128_decrypt":   "AES-128\n+ decrypt",
    "cenc":             "CENC-CTR\n(Widevine-like)",
    "cbcs":             "CBCS-CBC\n(PlayReady-like)",
}

CODEC_LABELS = {
    "mp4":      "MP4\n(H.264)",
    "mkv":      "MKV\n(H.264)",
    "av1_mp4":  "MP4\n(AV1)",
    "h264_ts":  "H.264/TS\n(HLS)",
    "av1_fmp4": "AV1/fMP4\n(HLS)",
}

FIGSIZE = (7, 4)
TITLE_SIZE = 11
LABEL_SIZE = 9


# ── Data loading and derived metrics ──────────────────────────────────────────

def load_data(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df["mb"] = df["bytes_transferred_b"] / 1e6
    # Use raw energy (idle-corrected values are unreliable on macOS; see paper §IV)
    df["joules_per_mb"] = np.where(
        df["mb"] > 0,
        df["client_energy_j_raw"] / df["mb"],
        np.nan
    )
    df["decrypt_ms"] = df["decrypt_cpu_time_s"] * 1000.0
    return df


def compute_medians(df: pd.DataFrame, group_cols: list) -> pd.DataFrame:
    agg = (
        df.groupby(group_cols, sort=False)
          .agg(
              jpm_med=("joules_per_mb",      "median"),
              jpm_mad=("joules_per_mb",       lambda x: float(median_abs_deviation(x.dropna()))),
              dur_med=("client_duration_s",  "median"),
              dec_med=("decrypt_ms",          "median"),
              n_samples_med=("n_samples",     "median"),
              n_trials=("run_id",             "count"),
          )
          .reset_index()
    )
    return agg


# ── Plot helpers ──────────────────────────────────────────────────────────────

def _bar_with_error(ax, x_pos, height, err, color, width=0.55):
    ax.bar(x_pos, height, width, color=color, zorder=2, edgecolor="white", linewidth=0.5)
    ax.errorbar(x_pos, height, yerr=err, fmt="none", color="black",
                capsize=4, capthick=1, linewidth=1.2, zorder=3)


def _finish_ax(ax, title: str, xlabel: str, ylabel: str):
    ax.set_title(title, fontsize=TITLE_SIZE, fontweight="bold", pad=6)
    ax.set_xlabel(xlabel, fontsize=LABEL_SIZE)
    ax.set_ylabel(ylabel, fontsize=LABEL_SIZE)
    ax.yaxis.grid(True, linestyle="--", alpha=0.5, zorder=0)
    ax.set_axisbelow(True)
    ax.spines[["top", "right"]].set_visible(False)


# ── Plot 1: Bulk codec comparison ─────────────────────────────────────────────

def plot_codec_bulk(df: pd.DataFrame) -> None:
    subset = df[(df["experiment_group"] == "codec") &
                (df["delivery_mode"] == "bulk_ratelimited")].copy()
    if subset.empty:
        print("[analyze] no bulk_ratelimited data — skipping plot 1")
        return

    order = ["mp4", "mkv", "av1_mp4"]
    med = compute_medians(subset, ["container_format"])
    med = med.set_index("container_format").reindex(order).reset_index()

    fig, ax = plt.subplots(figsize=FIGSIZE)
    for i, row in med.iterrows():
        _bar_with_error(ax, i, row["jpm_med"], row["jpm_mad"],
                        CODEC_COLORS.get(row["container_format"], GREY))

    ax.set_xticks(range(len(order)))
    ax.set_xticklabels([CODEC_LABELS[c] for c in order], fontsize=LABEL_SIZE)
    _finish_ax(ax, "Bulk Download: Energy by Container Format (rate-limited, HTTP/1.1)",
               "Container", "J / MB (raw)")

    # Annotate with median ± MAD
    for i, row in med.iterrows():
        if pd.notna(row["jpm_med"]):
            ax.text(i, row["jpm_med"] + row["jpm_mad"] + ax.get_ylim()[1]*0.01,
                    f"{row['jpm_med']:.4f}", ha="center", va="bottom",
                    fontsize=7, color="black")

    plt.tight_layout()
    out = PLOTS_DIR / "codec_drm_01_codec_bulk.png"
    plt.savefig(out, dpi=150)
    plt.close()
    print(f"[analyze] saved {out}")


# ── Plot 2: HLS codec comparison ──────────────────────────────────────────────

def plot_codec_hls(df: pd.DataFrame) -> None:
    subset = df[(df["experiment_group"] == "codec") &
                (df["delivery_mode"] == "hls_codec")].copy()
    if subset.empty:
        print("[analyze] no hls_codec data — skipping plot 2")
        return

    order = ["h264_ts", "av1_fmp4"]
    med = compute_medians(subset, ["container_format"])
    med = med.set_index("container_format").reindex(order).reset_index()

    fig, ax = plt.subplots(figsize=(5.5, 4))
    for i, row in med.iterrows():
        _bar_with_error(ax, i, row["jpm_med"], row["jpm_mad"],
                        CODEC_COLORS.get(row["container_format"], GREY), width=0.4)

    ax.set_xticks(range(len(order)))
    ax.set_xticklabels([CODEC_LABELS[c] for c in order], fontsize=LABEL_SIZE)
    _finish_ax(ax, "HLS Streaming: Energy by Codec/Container (HTTP/1.1 plain)",
               "Codec / Container", "J / MB (raw)")

    for i, row in med.iterrows():
        if pd.notna(row["jpm_med"]):
            ax.text(i, row["jpm_med"] + row["jpm_mad"] + ax.get_ylim()[1]*0.01,
                    f"{row['jpm_med']:.4f}", ha="center", va="bottom",
                    fontsize=7, color="black")

    plt.tight_layout()
    out = PLOTS_DIR / "codec_drm_02_codec_hls.png"
    plt.savefig(out, dpi=150)
    plt.close()
    print(f"[analyze] saved {out}")


# ── Plot 3: DRM overhead ──────────────────────────────────────────────────────

def plot_drm_overhead(df: pd.DataFrame) -> None:
    subset = df[df["experiment_group"] == "drm"].copy()
    if subset.empty:
        print("[analyze] no drm group data — skipping plot 3")
        return

    order = ["none", "aes128_net_only", "aes128_decrypt", "cenc", "cbcs"]
    med = compute_medians(subset, ["drm_scheme"])
    med = med.set_index("drm_scheme").reindex(order).reset_index()

    fig, ax = plt.subplots(figsize=(8, 4))
    for i, row in med.iterrows():
        _bar_with_error(ax, i, row["jpm_med"], row["jpm_mad"],
                        DRM_COLORS.get(row["drm_scheme"], GREY))

    ax.set_xticks(range(len(order)))
    ax.set_xticklabels([DRM_LABELS[s] for s in order], fontsize=LABEL_SIZE)
    _finish_ax(ax, "HLS Streaming: Energy by Encryption Scheme (HTTP/1.1, H.264/TS)",
               "Encryption Scheme", "J / MB (raw)")

    for i, row in med.iterrows():
        if pd.notna(row["jpm_med"]):
            ax.text(i, row["jpm_med"] + row["jpm_mad"] + ax.get_ylim()[1]*0.01,
                    f"{row['jpm_med']:.4f}", ha="center", va="bottom",
                    fontsize=7, color="black")

    plt.tight_layout()
    out = PLOTS_DIR / "codec_drm_03_drm_overhead.png"
    plt.savefig(out, dpi=150)
    plt.close()
    print(f"[analyze] saved {out}")


# ── Plot 4: Decrypt CPU time ──────────────────────────────────────────────────

def plot_decrypt_cpu(df: pd.DataFrame) -> None:
    subset = df[
        (df["experiment_group"] == "drm") &
        (df["drm_scheme"].isin(["aes128_decrypt", "cenc", "cbcs"]))
    ].copy()
    if subset.empty:
        print("[analyze] no decrypt schemes in drm data — skipping plot 4")
        return

    order = ["aes128_decrypt", "cenc", "cbcs"]
    med = compute_medians(subset, ["drm_scheme"])
    med = med.set_index("drm_scheme").reindex(order).reset_index()

    fig, ax = plt.subplots(figsize=(5.5, 4))
    for i, row in med.iterrows():
        ax.bar(i, row["dec_med"], 0.45,
               color=DRM_COLORS.get(row["drm_scheme"], GREY),
               zorder=2, edgecolor="white", linewidth=0.5)

    ax.set_xticks(range(len(order)))
    ax.set_xticklabels([DRM_LABELS[s] for s in order], fontsize=LABEL_SIZE)
    _finish_ax(ax, "Decryption CPU Time per Stream (Python AES, median over 5 trials)",
               "Cipher Mode", "Decrypt wall time (ms, total per stream)")

    for i, row in med.iterrows():
        if pd.notna(row["dec_med"]):
            ax.text(i, row["dec_med"] + ax.get_ylim()[1]*0.01,
                    f"{row['dec_med']:.1f} ms", ha="center", va="bottom",
                    fontsize=7, color="black")

    plt.tight_layout()
    out = PLOTS_DIR / "codec_drm_04_decrypt_cpu.png"
    plt.savefig(out, dpi=150)
    plt.close()
    print(f"[analyze] saved {out}")


# ── LaTeX table ───────────────────────────────────────────────────────────────

def write_latex_table(df: pd.DataFrame) -> None:
    codec_med = compute_medians(
        df[df["experiment_group"] == "codec"],
        ["delivery_mode", "container_format", "drm_scheme"]
    )
    drm_med = compute_medians(
        df[df["experiment_group"] == "drm"],
        ["delivery_mode", "container_format", "drm_scheme"]
    )

    lines = [
        r"\begin{table}[t]",
        r"\caption{Codec and Encryption Energy Comparison (macOS, HTTP/1.1 plain)}",
        r"\label{tab:codec_drm}",
        r"\begin{center}",
        r"\begin{tabular}{llllrrr}",
        r"\hline",
        r"\textbf{Group} & \textbf{Delivery} & \textbf{Format} & \textbf{Scheme}"
        r" & \textbf{J/MB} & \textbf{MAD} & \textbf{Dur (s)} \\",
        r"\hline",
    ]

    def fmt_row(group: str, delivery: str, fmt: str, scheme: str,
                jpm: float, mad: float, dur: float) -> str:
        j = f"{jpm:.4f}" if pd.notna(jpm) else "—"
        m = f"{mad:.4f}" if pd.notna(mad) else "—"
        d = f"{dur:.0f}"  if pd.notna(dur) else "—"
        return (rf"{group} & {delivery} & \texttt{{{fmt}}} & {scheme}"
                rf" & {j} & {m} & {d} \\")

    for _, row in codec_med.iterrows():
        lines.append(fmt_row(
            "Codec", row["delivery_mode"].replace("_", "\\_"),
            row["container_format"], row["drm_scheme"],
            row["jpm_med"], row["jpm_mad"], row["dur_med"]
        ))

    lines.append(r"\hline")

    for _, row in drm_med.iterrows():
        lines.append(fmt_row(
            "DRM", row["delivery_mode"].replace("_", "\\_"),
            row["container_format"], row["drm_scheme"],
            row["jpm_med"], row["jpm_mad"], row["dur_med"]
        ))

    lines += [
        r"\hline",
        r"\end{tabular}",
        r"\end{center}",
        r"\end{table}",
    ]

    out = TABLES_DIR / "codec_drm_table.tex"
    with open(out, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[analyze] saved {out}")


# ── Print terminal summary ────────────────────────────────────────────────────

def print_summary(df: pd.DataFrame) -> None:
    print("\n── Codec group ──────────────────────────────────────────")
    codec = compute_medians(df[df["experiment_group"] == "codec"],
                            ["delivery_mode", "container_format"])
    for _, r in codec.iterrows():
        jpm = f"{r['jpm_med']:.4f}" if pd.notna(r['jpm_med']) else "N/A"
        print(f"  {r['delivery_mode']:20s}  {r['container_format']:12s}  "
              f"J/MB={jpm}  dur={r['dur_med']:.0f}s  n={int(r['n_trials'])}")

    print("\n── DRM group ────────────────────────────────────────────")
    drm = compute_medians(df[df["experiment_group"] == "drm"], ["drm_scheme"])
    for _, r in drm.iterrows():
        jpm = f"{r['jpm_med']:.4f}" if pd.notna(r['jpm_med']) else "N/A"
        dec = f"{r['dec_med']:.1f}ms" if r['dec_med'] > 0 else "—"
        print(f"  {r['drm_scheme']:20s}  J/MB={jpm}  decrypt={dec}  n={int(r['n_trials'])}")
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Codec/DRM energy analysis")
    parser.add_argument("--input", default=str(DEFAULT_CSV),
                        help="Path to client_energy_codec_drm.csv")
    args = parser.parse_args()

    if not os.path.isfile(args.input):
        print(f"[analyze] CSV not found: {args.input}")
        print("Run the orchestrator first:  python3 orchestrator/orchestrator_codec_drm.py")
        sys.exit(1)

    df = load_data(args.input)
    print(f"[analyze] loaded {len(df)} rows from {args.input}")

    zero_sample_rows = df[df["n_samples"] < 2]
    if not zero_sample_rows.empty:
        print(f"[analyze] WARNING: {len(zero_sample_rows)} rows have <2 powermetrics samples "
              f"— energy values unreliable for those runs.")
        print("  Affected configs:", zero_sample_rows["delivery_mode"].unique().tolist())

    print_summary(df)
    plot_codec_bulk(df)
    plot_codec_hls(df)
    plot_drm_overhead(df)
    plot_decrypt_cpu(df)
    write_latex_table(df)
    print("[analyze] all outputs written.")


if __name__ == "__main__":
    main()
