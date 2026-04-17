#!/usr/bin/env python3
"""
Reads the merged results CSV, computes medians, generates plots and LaTeX tables.

Usage:
    python3 analysis/analyze.py --input data/combined/results_<ts>.csv
    python3 analysis/analyze.py          # auto-picks the latest results_*.csv
"""
import argparse
import glob
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

DATA_DIR  = os.path.join(os.path.dirname(__file__), "..", "data")
PLOTS_DIR = os.path.join(os.path.dirname(__file__), "plots")
TABLES_DIR = os.path.join(os.path.dirname(__file__), "tables")
os.makedirs(PLOTS_DIR, exist_ok=True)
os.makedirs(TABLES_DIR, exist_ok=True)

METRIC = "joules_per_mb_client"   # j_raw / MB (no idle correction — see merge.py)
_IEEE_STYLE = {
    "figure.figsize": (3.5, 2.8),   # single IEEE column width
    "font.size": 8,
    "axes.titlesize": 8,
    "axes.labelsize": 8,
    "xtick.labelsize": 7,
    "ytick.labelsize": 7,
    "legend.fontsize": 7,
}


def _mad(series: pd.Series) -> float:
    return float((series - series.median()).abs().median())


def compute_medians(df: pd.DataFrame) -> pd.DataFrame:
    group_cols = ["delivery_mode", "http_protocol", "tls_enabled", "container_format", "drm_enabled"]
    numeric_cols = [c for c in df.select_dtypes(include="number").columns
                    if c not in ("trial_num",)]
    med = df.groupby(group_cols)[numeric_cols].median().reset_index()
    mad_cols = {}
    for col in ["joules_per_mb_client", "joules_per_mb_server", "joules_per_mb_total"]:
        if col in df.columns:
            mad_cols[f"mad_{col}"] = df.groupby(group_cols)[col].apply(_mad).values
    for k, v in mad_cols.items():
        med[k] = v
    return med


def _save(fig: plt.Figure, name: str) -> None:
    path = os.path.join(PLOTS_DIR, name)
    fig.savefig(path, bbox_inches="tight", dpi=150)
    plt.close(fig)
    print(f"  [plot] {path}")


# ── Plot 1: J/MB for HLS by protocol (HTTP1 vs HTTP2) ─────────────────────────
def plot_delivery_mode(med: pd.DataFrame) -> None:
    """HLS energy by HTTP protocol — only measurable streaming configs."""
    with plt.rc_context(_IEEE_STYLE):
        fig, ax = plt.subplots()
        hls = med[med.delivery_mode == "hls"]
        protos = ["HTTP1", "HTTP2"]
        labels = ["HTTP/1.1", "HTTP/2"]
        x = np.arange(len(protos))
        w = 0.5
        vals = [hls[hls.http_protocol == p]["joules_per_mb_client"].median() for p in protos]
        errs = [hls[hls.http_protocol == p]["mad_joules_per_mb_client"].median() for p in protos]
        ax.bar(x, vals, w, yerr=errs, capsize=4, color=["#4c72b0", "#dd8452"])
        ax.set_xticks(x); ax.set_xticklabels(labels)
        ax.set_ylabel("J / MB"); ax.set_title("HLS Energy by HTTP Protocol")
        _save(fig, "01_hls_protocol.png")


# ── Plot 2: J/MB HLS with TLS on vs off (HTTP/1.1 only) ──────────────────────
def plot_protocol(med: pd.DataFrame) -> None:
    """TLS impact on HLS energy (HTTP/1.1, since H2 forces TLS)."""
    with plt.rc_context(_IEEE_STYLE):
        hls_h1 = med[(med.delivery_mode == "hls") & (med.http_protocol == "HTTP1")]
        labels = {True: "TLS on", False: "TLS off"}
        x = np.arange(len(labels))
        w = 0.5
        vals = [hls_h1[hls_h1.tls_enabled == k]["joules_per_mb_client"].median() for k in labels]
        errs = [hls_h1[hls_h1.tls_enabled == k]["mad_joules_per_mb_client"].median() for k in labels]
        fig, ax = plt.subplots()
        ax.bar(x, vals, w, yerr=errs, capsize=4, color=["#4c72b0", "#55a868"])
        ax.set_xticks(x); ax.set_xticklabels(list(labels.values()))
        ax.set_ylabel("J / MB"); ax.set_title("TLS Impact on HLS (HTTP/1.1)")
        _save(fig, "02_hls_tls.png")


# ── Plot 3: J/MB for all 6 HLS configs ────────────────────────────────────────
def plot_tls(med: pd.DataFrame) -> None:
    """All HLS configurations compared side-by-side."""
    with plt.rc_context({**_IEEE_STYLE, "figure.figsize": (5, 2.8)}):
        hls = med[med.delivery_mode == "hls"].copy()
        hls["label"] = (hls["http_protocol"].str.replace("HTTP", "H")
                        + "/" + hls["tls_enabled"].map({True: "TLS", False: "plain"})
                        + "/" + hls["drm_enabled"].map({True: "DRM", False: "open"}))
        hls = hls.sort_values(["http_protocol", "tls_enabled", "drm_enabled"])
        fig, ax = plt.subplots()
        x = np.arange(len(hls))
        ax.bar(x, hls["joules_per_mb_client"].values,
               yerr=hls["mad_joules_per_mb_client"].values, capsize=3,
               color="#4c72b0")
        ax.set_xticks(x)
        ax.set_xticklabels(hls["label"].values, rotation=30, ha="right")
        ax.set_ylabel("J / MB")
        ax.set_title("HLS: All Configurations")
        _save(fig, "03_hls_all_configs.png")


# ── Plot 4: J/MB encrypted vs plain HLS ───────────────────────────────────────
def plot_drm(med: pd.DataFrame) -> None:
    with plt.rc_context(_IEEE_STYLE):
        hls_h1 = med[(med.delivery_mode == "hls") & (med.http_protocol == "HTTP1")]
        labels = {False: "Plain HLS", True: "AES-128 HLS"}
        x = np.arange(len(labels))
        vals = [hls_h1[hls_h1.drm_enabled == k]["joules_per_mb_client"].median() for k in labels]
        errs = [hls_h1[hls_h1.drm_enabled == k]["mad_joules_per_mb_client"].median() for k in labels]
        w = 0.5
        fig, ax = plt.subplots()
        ax.bar(x, vals, w, yerr=errs, capsize=4, color=["#4c72b0", "#dd8452"])
        ax.set_xticks(x); ax.set_xticklabels(list(labels.values()))
        ax.set_ylabel("J / MB"); ax.set_title("AES-128 DRM Impact (HLS, HTTP/1.1)")
        _save(fig, "04_drm.png")


# ── Plot 5: J/MB by container format ─────────────────────────────────────────
def plot_container(med: pd.DataFrame) -> None:
    with plt.rc_context(_IEEE_STYLE):
        bulk = med[(med.delivery_mode == "bulk") & (med.http_protocol == "HTTP1")
                   & (med.tls_enabled == False)]
        containers = ["mp4", "mkv", "av1_mp4"]
        labels = ["MP4", "MKV", "AV1-MP4"]
        x = np.arange(len(containers))
        client_j = [bulk[bulk.container_format == c]["joules_per_mb_client"].median() for c in containers]
        server_j = [bulk[bulk.container_format == c]["joules_per_mb_server"].median() for c in containers]
        w = 0.35
        fig, ax = plt.subplots()
        ax.bar(x - w/2, client_j, w, label="Client", color="#4c72b0")
        ax.bar(x + w/2, server_j, w, label="Server", color="#dd8452")
        ax.set_xticks(x); ax.set_xticklabels(labels)
        ax.set_ylabel("J / MB"); ax.set_title("Container Format Impact (Bulk, H1, no TLS)")
        ax.legend()
        _save(fig, "05_container.png")


# ── Plot 6: throughput vs J/MB scatter ────────────────────────────────────────
def plot_efficiency_frontier(df: pd.DataFrame) -> None:
    with plt.rc_context(_IEEE_STYLE):
        fig, ax = plt.subplots()
        colors = {"bulk": "#4c72b0", "hls": "#dd8452", "p2p": "#55a868"}
        for mode, grp in df.groupby("delivery_mode"):
            ax.scatter(grp["throughput_mbps"], grp["joules_per_mb_client"],
                       label=mode, alpha=0.6, s=15, color=colors.get(mode, "gray"))
        ax.set_xlabel("Throughput (Mbps)")
        ax.set_ylabel("Client J / MB")
        ax.set_title("Efficiency Frontier")
        ax.legend()
        _save(fig, "06_efficiency_frontier.png")


# ── Plot 7: box plots for top comparisons ─────────────────────────────────────
def plot_boxplots(df: pd.DataFrame) -> None:
    with plt.rc_context({**_IEEE_STYLE, "figure.figsize": (5, 3)}):
        fig, ax = plt.subplots()
        data_by_mode = [
            df[df.delivery_mode == m]["joules_per_mb_client"].dropna().values
            for m in ["bulk", "hls", "p2p"]
        ]
        ax.boxplot(data_by_mode, tick_labels=["bulk", "hls", "p2p"], patch_artist=True)
        ax.set_ylabel("Client J / MB")
        ax.set_title("Per-trial Distribution by Delivery Mode")
        _save(fig, "07_boxplots.png")


# ── LaTeX table: full summary ─────────────────────────────────────────────────
def write_latex_summary(med: pd.DataFrame) -> None:
    cols = ["delivery_mode", "http_protocol", "tls_enabled", "container_format",
            "drm_enabled", "joules_per_mb_client", "joules_per_mb_server",
            "joules_per_request_client", "stall_count"]
    sub = med[cols].copy()
    sub.columns = ["Mode", "Protocol", "TLS", "Container", "DRM",
                   "J/MB (C)", "J/MB (S)", "J/Req (C)", "Stalls"]

    lines = [
        r"\begin{table}[t]",
        r"\caption{Energy Summary by Configuration}",
        r"\label{tab:summary}",
        r"\centering",
        r"\begin{tabular}{llllllrrr}",
        r"\hline",
        r"Mode & Proto & TLS & Container & DRM & J/MB$_C$ & J/MB$_S$ & J/Req$_C$ & Stalls \\",
        r"\hline",
    ]
    for _, row in sub.iterrows():
        def fmt(v):
            if isinstance(v, float) and not np.isnan(v):
                return f"{v:.3f}"
            return str(v)
        lines.append(" & ".join(fmt(row[c]) for c in sub.columns) + r" \\")
    lines += [r"\hline", r"\end{tabular}", r"\end{table}"]

    path = os.path.join(TABLES_DIR, "summary_table.tex")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"  [table] {path}")


def write_latex_hls(med: pd.DataFrame) -> None:
    """Clean HLS-only table for the paper."""
    hls = med[med.delivery_mode == "hls"].copy()
    hls["proto_label"] = hls["http_protocol"].str.replace("HTTP", "HTTP/")
    hls["tls_label"] = hls["tls_enabled"].map({True: "Yes", False: "No"})
    hls["drm_label"] = hls["drm_enabled"].map({True: "AES-128", False: "None"})
    hls = hls.sort_values(["http_protocol", "tls_enabled", "drm_enabled"])

    lines = [
        r"\begin{table}[t]",
        r"\caption{HLS Streaming Energy by Protocol, TLS, and Encryption (5 trials, MAC client)}",
        r"\label{tab:hls}",
        r"\centering",
        r"\begin{tabular}{lllrr}",
        r"\hline",
        r"Protocol & TLS & Encryption & J/MB (median) & MAD \\",
        r"\hline",
    ]
    for _, r in hls.iterrows():
        lines.append(
            f"{r['proto_label']} & {r['tls_label']} & {r['drm_label']} & "
            f"{r['joules_per_mb_client']:.4f} & {r['mad_joules_per_mb_client']:.4f} \\\\"
        )
    lines += [r"\hline", r"\end{tabular}", r"\end{table}"]

    path = os.path.join(TABLES_DIR, "hls_table.tex")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"  [table] {path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=None,
                        help="Path to results_*.csv (defaults to latest in data/combined/)")
    args = parser.parse_args()

    if args.input:
        csv_path = args.input
    else:
        pattern = os.path.join(DATA_DIR, "combined", "results_*.csv")
        candidates = sorted(glob.glob(pattern))
        if not candidates:
            sys.exit(f"No results CSV found matching {pattern}. Run merge.py first.")
        csv_path = candidates[-1]

    print(f"[analyze] reading {csv_path}")
    df = pd.read_csv(csv_path)

    for col in ["joules_per_mb_client", "joules_per_mb_server", "throughput_mbps",
                "client_energy_j_corrected", "server_energy_corrected_j"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    med = compute_medians(df)
    out_path = os.path.join(DATA_DIR, "combined", "medians.csv")
    med.to_csv(out_path, index=False)
    print(f"[analyze] medians → {out_path}")

    print("[analyze] generating plots...")
    plot_delivery_mode(med)
    plot_protocol(med)
    plot_tls(med)
    plot_drm(med)
    plot_container(med)
    plot_efficiency_frontier(df)
    plot_boxplots(df)

    print("[analyze] generating LaTeX tables...")
    write_latex_summary(med)
    write_latex_hls(med)

    print("[analyze] done.")


if __name__ == "__main__":
    main()
