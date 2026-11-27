#!/usr/bin/env python3
"""charts.py — render the docs/charts/*.png benchmark charts (dark, banner palette).
  ./scripts/charts.py
Numbers are hard-coded from logged runs (see the comment above each chart); edit and rerun.
"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

DIR = Path(__file__).resolve().parent.parent / "docs/charts"

# docs/brand/banner.html palette
BG, TEXT, MUTED, ACCENT, CLOUD = "#14100e", "#f4ede6", "#b3a597", "#ff7a1a", "#6fb6c4"
RIG = "ASUS ROG Flow Z13 (Strix Halo, 70 W)"


def mmss(s):
    return f"{s // 60}m{s % 60:02d}s"


def row(ax, rows, title, fmt, best_low=None, track=None):
    """rows: (label, value, color, note?). best_low=None keeps the given order.
    track: faint full-scale bar behind each; the value then sits inside the bar's tip, the note just outside."""
    if best_low is not None:
        rows = sorted(rows, key=lambda r: r[1], reverse=not best_low)  # best on top
    y = list(range(len(rows)))[::-1]
    top = max([r[1] for r in rows] + [track or 0])
    for yi, (label, v, color, *note) in zip(y, rows):
        if track:
            ax.barh(yi, track, height=0.62, color=TEXT, alpha=0.06, linewidth=0)
        hatch = color == "wrong"
        c = CLOUD if hatch else color
        ax.barh(yi, v, height=0.62, color=BG if hatch else c, edgecolor=c, hatch="//" if hatch else None,
                linewidth=1.5 if hatch else 0)
        hi = c == ACCENT
        if track:
            ax.text(v - top * 0.01, yi, fmt(v), va="center", ha="right", fontsize=22, fontweight="bold", color=BG)
            t = ax.text(v, yi, "", va="center")  # anchor for the note at the bar's end
        else:
            t = ax.text(v + top * 0.012, yi, fmt(v), va="center", fontsize=22, fontweight="bold",
                        color=ACCENT if hi else TEXT)
        if note:
            ax.annotate("  " + note[0], xycoords=t, xy=(1, 0.5), va="center", fontsize=17,
                        fontweight="bold" if hi else "normal", color=ACCENT if hi else MUTED)
    ax.set_yticks(y, [r[0] for r in rows], fontsize=20)
    for t, r in zip(ax.get_yticklabels(), rows):
        t.set_color(ACCENT if r[2] == ACCENT else TEXT)
        t.set_fontweight("bold" if r[2] == ACCENT else "normal")
    ax.set_xlim(0, top * 1.3 if any(len(r) > 3 for r in rows) else top * 1.2)
    ax.set_xticks([])
    ax.tick_params(length=0, pad=12)
    ax.set_facecolor(BG)
    for s in ax.spines.values():
        s.set_visible(False)
    if title:
        ax.set_title(title, loc="left", color=MUTED, fontsize=19, pad=10)


def chart(name, headline, sub, panels, legend, foot, height=9, left=0.2, hspace=0.45):
    """panels: [(rows, title, fmt, best_low)]; legend: [(color, label)]."""
    fig, axes = plt.subplots(len(panels), 1, figsize=(16, height), dpi=100, facecolor=BG, squeeze=False,
                             gridspec_kw={"height_ratios": [len(p[0]) for p in panels]})
    fig.text(0.03, 1 - 0.6 / height, headline, color=TEXT, fontsize=32, fontweight="bold")
    fig.text(0.03, 1 - 1.1 / height, sub, color=MUTED, fontsize=19)
    for ax, p in zip(axes[:, 0], panels):
        row(ax, *p)
    legend and fig.legend(handles=[Patch(facecolor=BG if c == "wrong" else c, edgecolor=CLOUD if c == "wrong" else c,
                              hatch="//" if c == "wrong" else None, label=l) for c, l in legend],
               loc="lower right", bbox_to_anchor=(0.97, 0.005), ncol=len(legend), frameon=False,
               fontsize=17, labelcolor=TEXT, handlelength=1, handleheight=1)
    fig.text(0.03, 0.22 / height, foot, color=MUTED, fontsize=15)
    fig.subplots_adjust(left=left, right=0.97, top=1 - 1.8 / height, bottom=0.75 / height, hspace=hspace)
    out = DIR / name
    fig.savefig(out, facecolor=BG)
    plt.close(fig)
    print(out)


def main():
    plt.rcParams["font.family"] = ["Fira Sans", "DejaVu Sans"]
    local, cloud = (ACCENT, "local, this box"), (CLOUD, "cloud, z.ai API")

    # TTFT and GLM decode: runs 2026-10-07..10 (docs/local-vs-cloud-model-comparison.md);
    # Flash-Next decode: ./harness bench, bench-v1, 2026-11-23, 70 W.
    ttft = [("Flash-Next (local)", 0.03, ACCENT), ("glm-5.3-flashx", 1.36, CLOUD)]
    decode = [("Flash-Next (local)", 74.1, ACCENT), ("glm-5.3-flash", 45, CLOUD),
              ("glm-5.3", 74, CLOUD), ("glm-5.3-flashx", 151, CLOUD)]
    chart("local-vs-cloud.png",
          f"Local Flash-Next starts answering ~{round(ttft[1][1] / ttft[0][1])}x sooner than the cloud",
          f"Qwen3.8 Flash-Next 125B on an {RIG} vs z.ai GLM 5.3 tiers",
          [(ttft, "Time to first token, warm turn  ·  lower is better", lambda v: f"{v:.2f} s", True),
           (decode, "Decode speed  ·  higher is better", lambda v: f"{v:g} t/s", False)],
          [local, cloud], "Oct 7-10, 2026 runs; Flash-Next decode from ./harness bench (bench-v1), Nov 23, 2026")

    # docs/local-vs-cloud-model-comparison.md, Results table (runs 1-8).
    runs = [("glm-5.3-flash (opencode)", 68, "wrong", "wrong mechanism"),
            ("glm-5.3-flashx (opencode)", 90, CLOUD),
            ("glm-5.3-flashx (pi)", 161, CLOUD),
            ("Flash-Next (pi, medium)", 175, ACCENT, "upstream PR found"),
            ("GLM 5.3 753B (opencode)", 375, CLOUD),
            ("GLM 5.3 753B (pi)", 510, CLOUD),
            ("Flash-Next (pi, max)", 511, ACCENT, "upstream PR found"),
            ("glm-5.3-flash (pi)", 549, CLOUD)]
    chart("wallclock-by-run.png", "One real bug, eight runs: only the local model found the upstream PR",
          "Wall-clock time per run, fastest first; all eight called the bug correctly",
          [(runs, None, mmss, None)],
          [local, cloud, ("wrong", "cloud, wrong mechanism")],
          "Oct 7-10, 2026; local on the Flow Z13 at 70 W", height=10, left=0.24)

    # docs/BENCHMARKS.md, "Engines on Strix Halo" (halogen measured on 0.18.5; Strata 0.1.44.1 tuned; gufo 0.12.0).
    alt = MUTED
    dec = [("halogen", 65, ACCENT), ("Strata", 43, alt), ("gufo", 40, alt)]
    pf = [("halogen", 1520, ACCENT), ("Strata", 990, alt), ("gufo", 1270, alt)]
    chart("engines-strix-halo.png", "halogen is the fastest engine on Strix Halo",
          f"Qwen3.8 Flash-Next UD-IQ4_XS, 256k context, on an {RIG}; same prompts, greedy, thinking off",
          [(dec, "Decode, 10-prompt battery median  ·  higher is better", lambda v: f"{v:g} t/s", False),
           (pf, "Cold prefill, ~119k-131k token prompt  ·  higher is better", lambda v: f"~{v:,} t/s", False)],
          [(ACCENT, "default"), (alt, "open alternatives")],
          "halogen 0.18.5 (pin now 0.19.1), Strata 0.1.44.1 tuned, gufo 0.12.0; Oct 2026", left=0.14)

    # docs/BENCHMARKS.md, "Prompt prefix and tool routing (2026-11-22)", osmos-evo, thinking off.
    before, after = MUTED, ACCENT
    chart("prompt-prefix.png", "8 tools instead of 25: a cold turn drops from 31 s to 17.5 s",
          "osmos (DeepSeek-V4.1-Flash) on Strix Halo, new session, a one-word message",
          [([("before (25 tools)", 13968, before), ("after (8 tools)", 7952, after)],
            "Cold prompt  ·  lower is better", lambda v: f"{v:,} tokens", None),
           ([("before (25 tools)", 31.4, before), ("after (8 tools)", 17.5, after)],
            "Cold read time  ·  lower is better", lambda v: f"{v:g} s", None)],
          [(before, "before"), (after, "after")],
          "Measured Nov 22, 2026; the rest of the tools are called from codemode", height=7.5, left=0.18)

    # docs/BENCHMARKS.md, "Efficiency levers" table: share saved vs the same task without the lever.
    pct = lambda b, a: round((b - a) / b * 100)
    best = pct(13980, 822)

    def shade(v):  # MUTED -> ACCENT by share of the best saving; only the best is exact ACCENT (orange label)
        if v == best:
            return ACCENT
        m, a = (bytes.fromhex(c[1:]) for c in (MUTED, ACCENT))
        return "#" + "".join(f"{round(x + (y - x) * v / best):02x}" for x, y in zip(m, a))

    lv = lambda label, v, note: (label, v, shade(v), note)
    tokens = [lv("serena: read a function", best, "~3,500 → ~205 tokens"),  # logged in chars, ~4/token
              lv("screenshot cap", pct(2552, 436), "2,552 → 436 tokens"),  # halogen 0.21.0, 2026-11-25
              lv("sidecar: commit diff", pct(18500, 7100), "18.5k → 7.1k tokens"),
              lv("8 tools, not 25: cold prompt", pct(13968, 7952), "13,968 → 7,952 tokens"),
              lv("semble-first search", pct(4368, 2706), "4,368 → 2,706 output tokens"),
              lv("serena-first routing", pct(37450, 23260), "~9,400 → ~5,800 tokens read")]
    time = [lv("sidecar: compaction wait", pct(52, 24), "~52 s → ~24 s"),
            lv("8 tools, not 25: cold turn", pct(31.4, 17.5), "31.4 s → 17.5 s"),
            lv("semble-first search", 22, "wall time"),
            lv("serena-first routing", 16, "wall time")]
    chart("efficiency-levers.png", f"Every layer keeps tokens off the main model: up to {best}% less",
          "Same task with and without each lever, on Strix Halo; the faint track is the cost without it",
          [(tokens, "Tokens per task  ·  % less", lambda v: f"{v}%", None, 100),
           (time, "Time spent waiting  ·  % less", lambda v: f"{v}%", None, 100)],
          [], "Oct-Nov 2026 runs; every number with its run in docs/BENCHMARKS.md#efficiency-levers",
          height=9.5, left=0.24, hspace=0.25)


if __name__ == "__main__":
    main()
