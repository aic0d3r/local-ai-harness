#!/usr/bin/env python3
"""charts.py — render docs/charts/local-vs-cloud.png (dark, banner palette).
  ./scripts/charts.py
Numbers are hard-coded from logged runs; edit TTFT/DECODE and rerun.
"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

OUT = Path(__file__).resolve().parent.parent / "docs/charts/local-vs-cloud.png"

# docs/brand/banner.html palette
BG, TEXT, MUTED, ACCENT, CLOUD = "#14100e", "#f4ede6", "#b3a597", "#ff7a1a", "#6fb6c4"

# (label, value, is_local), all at 70 W. TTFT and GLM decode: runs 2026-10-07..10
# (docs/local-vs-cloud-model-comparison.md); Flash-Next decode: ./harness bench, bench-v1, 2026-11-23.
TTFT = [("Flash-Next (local)", 0.03, True), ("glm-5.3-flashx", 1.36, False)]
DECODE = [("Flash-Next (local)", 74.1, True), ("glm-5.3-flash", 45, False),
          ("glm-5.3", 74, False), ("glm-5.3-flashx", 151, False)]


def row(ax, rows, title, fmt, best_low):
    rows = sorted(rows, key=lambda r: r[1], reverse=not best_low)  # best on top
    labels, vals, local = zip(*rows)
    y = range(len(rows))[::-1]
    ax.barh(y, vals, height=0.62, color=[ACCENT if l else CLOUD for l in local])
    top = max(vals)
    for yi, v, l in zip(y, vals, local):
        ax.text(v + top * 0.012, yi, fmt(v), va="center", fontsize=22, fontweight="bold",
                color=ACCENT if l else TEXT)
    ax.set_yticks(list(y), labels, fontsize=20)
    for t, l in zip(ax.get_yticklabels(), local):
        t.set_color(ACCENT if l else TEXT)
        t.set_fontweight("bold" if l else "normal")
    ax.set_xlim(0, top * 1.12)
    ax.set_xticks([])
    ax.tick_params(length=0, pad=12)
    ax.set_facecolor(BG)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_title(title, loc="left", color=MUTED, fontsize=19, pad=10)


def main():
    plt.rcParams["font.family"] = ["Fira Sans", "DejaVu Sans"]
    fig, (a, b) = plt.subplots(2, 1, figsize=(16, 9), dpi=100, facecolor=BG,
                               gridspec_kw={"height_ratios": [len(TTFT), len(DECODE)]})
    ratio = round(TTFT[1][1] / TTFT[0][1])
    fig.text(0.03, 0.94, f"Local Flash-Next starts answering ~{ratio}x sooner than the cloud",
             color=TEXT, fontsize=32, fontweight="bold")
    fig.text(0.03, 0.885, "Qwen3.8 Flash-Next 125B on an ASUS ROG Flow Z13 (Strix Halo, 70 W) vs z.ai GLM 5.3 tiers",
             color=MUTED, fontsize=19)
    row(a, TTFT, "Time to first token, warm turn  ·  lower is better", lambda v: f"{v:.2f} s", True)
    row(b, DECODE, "Decode speed  ·  higher is better", lambda v: f"{v:g} t/s", False)
    fig.legend(handles=[Patch(color=ACCENT, label="local, this box"), Patch(color=CLOUD, label="cloud, z.ai API")],
               loc="lower right", bbox_to_anchor=(0.97, 0.005), ncol=2, frameon=False,
               fontsize=17, labelcolor=TEXT, handlelength=1, handleheight=1)
    fig.text(0.03, 0.025, "Oct 7-10, 2026 runs; Flash-Next decode from ./harness bench (bench-v1), Nov 23, 2026",
             color=MUTED, fontsize=15)
    fig.subplots_adjust(left=0.2, right=0.97, top=0.8, bottom=0.08, hspace=0.45)
    fig.savefig(OUT, facecolor=BG)
    print(OUT)


if __name__ == "__main__":
    main()
