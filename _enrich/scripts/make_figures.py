#!/usr/bin/env python3
"""Rebuild the two dataset figures from the unpacked deposit.

    python make_figures.py [--t1 PATH] [--t2 PATH] [--out DIR]

Defaults expect the layout produced by unpack_deposit.py:
`_enrich/multichain/MC_SUMMARY.json` (mc_t1) and
`_enrich/multichain/mc_t2/MC_SUMMARY.json` (mc_t2).
Requires matplotlib (not part of the core requirements).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

ROOT = Path(__file__).resolve().parents[2]

CHAIN_NAMES = {
    "arbitrum": "Arbitrum One", "avalanche": "Avalanche C-Chain", "base": "Base",
    "bsc": "BNB Smart Chain", "celo": "Celo", "ethereum": "Ethereum",
    "gnosis": "Gnosis", "linea": "Linea", "mantle": "Mantle", "metis": "Metis",
    "optimism": "OP Mainnet", "polygon": "Polygon PoS", "scroll": "Scroll",
}

plt.rcParams.update({
    "font.size": 8.5, "font.family": "DejaVu Sans",
    "axes.spines.top": False, "axes.spines.right": False,
    "svg.fonttype": "none", "pdf.fonttype": 42,
})

BOX_PAD = 0.012  # visual padding of FancyBboxPatch beyond the nominal rect
BOX_KW = dict(boxstyle=f"round,pad={BOX_PAD},rounding_size={BOX_PAD}", linewidth=0.8)
C_MAIN, C_SIDE, C_QA = "#dbe9f6", "#e8f0e4", "#f6ecd9"
EDGE = "#4a4a4a"

_FIT: list[tuple[str, tuple, list]] = []  # (name, rect, texts) for the overflow check


def box(ax, name, x, y, w, h, title, lines, fc):
    ax.add_patch(FancyBboxPatch((x, y), w, h, fc=fc, ec=EDGE, **BOX_KW))
    t_title = ax.text(x + w / 2, y + h - 0.020, title, ha="center", va="top",
                      fontsize=8.4, fontweight="bold")
    t_body = ax.text(x + w / 2, y + h - 0.070, "\n".join(lines), ha="center",
                     va="top", fontsize=7.2, linespacing=1.4)
    _FIT.append((name, (x, y, w, h), [t_title, t_body]))
    return x, y, w, h


def arrow(ax, x0, y0, x1, y1):
    ax.add_patch(FancyArrowPatch((x0, y0), (x1, y1), arrowstyle="-|>",
                                 mutation_scale=8, lw=0.9, color=EDGE,
                                 shrinkA=0, shrinkB=0))


def elbow(ax, pts):
    """Orthogonal polyline whose last segment carries the arrowhead."""
    xs = [p[0] for p in pts[:-1]]
    ys = [p[1] for p in pts[:-1]]
    ax.plot(xs, ys, lw=0.9, color=EDGE, solid_capstyle="round")
    arrow(ax, *pts[-2], *pts[-1])


def check_fit(fig, ax) -> None:
    """Fail loudly if any label leaves its box: catches silent layout regressions."""
    fig.canvas.draw()
    ren = fig.canvas.get_renderer()
    inv = ax.transData.inverted()
    bad = []
    for name, (x, y, w, h), texts in _FIT:
        for t in texts:
            bb = t.get_window_extent(ren)
            (tx0, ty0), (tx1, ty1) = inv.transform([(bb.x0, bb.y0), (bb.x1, bb.y1)])
            if (tx0 < x - BOX_PAD or tx1 > x + w + BOX_PAD
                    or ty0 < y - BOX_PAD or ty1 > y + h + BOX_PAD):
                bad.append(f"{name}: text ({tx0:.3f},{ty0:.3f})-({tx1:.3f},{ty1:.3f}) "
                           f"outside box ({x:.3f},{y:.3f})+({w:.3f},{h:.3f})")
    if bad:
        raise SystemExit("fig1 label overflow:\n  " + "\n  ".join(bad))


def fig_pipeline(s2: dict, out: Path) -> None:
    n2 = s2["n_agents"]
    s0, s8, s9 = s2["S0mc"]["n"], s2["S8mc"]["n"], s2["S9mc"]["n"]
    s11 = s2["S11mc"]["distinct_live_endpoints"]
    s10 = s2["S10mc"]["clusters"]

    fig, ax = plt.subplots(figsize=(7.4, 4.8))
    fig.subplots_adjust(left=0.005, right=0.995, top=0.995, bottom=0.005)
    ax.set_xlim(0, 1); ax.set_ylim(0.10, 0.985); ax.axis("off")
    _FIT.clear()
    P = BOX_PAD

    # left column: sources feed everything
    src = box(ax, "sources", 0.012, 0.605, 0.180, 0.350, "On-chain sources",
              ["13 EVM networks", "Identity + Reputation", "registries (ERC-8004)",
               "recorded per-chain", "observation blocks", "two snapshots:",
               "mc_t1 = 2026-08-10", "(run 9–10 Aug UTC)", "mc_t2 = 2026-08-16"],
              C_MAIN)

    # top row: the main flow
    cen = box(ax, "census", 0.242, 0.770, 0.185, 0.185, "1 · Census",
              [f"{n2:,} tokens (mc_t2)", "ownerOf · tokenURI", "agent wallet"], C_MAIN)
    car = box(ax, "cards", 0.477, 0.770, 0.190, 0.185, "2 · Cards",
              ["remote + inline URIs", "structural descriptions", "IPFS recovery"],
              C_MAIN)
    prb = box(ax, "probes", 0.717, 0.770, 0.180, 0.185, "3 · Probes",
              ["33,065 endpoints", "GET, 3 rounds (t2)", "pre-registered spec"], C_MAIN)

    # side layers under the census column
    mnt = box(ax, "mint", 0.242, 0.420, 0.185, 0.210, "5a · Mint events",
              ["Transfer from 0x0", "batch · payer · gas", "timestamps:",
               "exact + interpolated"], C_SIDE)
    rep = box(ax, "reputation", 0.242, 0.135, 0.185, 0.182, "5b · Reputation",
              ["getClients at HEAD", "per-(agent, issuer)", "rows + summaries"], C_SIDE)

    # annotation under the cards column, panel and audit on the right
    ann = box(ax, "annotation", 0.477, 0.135, 0.205, 0.182, "Human annotation",
              ["300 endpoints, stratified", "2 blinded coders",
               "consensus + adjudication"], C_QA)
    pan = box(ax, "panel", 0.772, 0.400, 0.213, 0.240, "4 · Panel",
              ["flags + tiers", f"S0mc {s0:,}", f"S8mc {s8:,}", f"S9mc {s9:,}",
               f"S11mc {s11:,} · S10mc {s10}"], C_MAIN)
    aud = box(ax, "audit", 0.772, 0.135, 0.213, 0.155, "Independent audit",
              ["164/164 working copy", "163/163 on the deposit"], C_QA)

    top_mid = cen[1] + cen[3] / 2                      # y of the top-row centers
    # main flow, box edge to box edge
    arrow(ax, src[0] + src[2] + P, top_mid, cen[0] - P, top_mid)
    arrow(ax, cen[0] + cen[2] + P, top_mid, car[0] - P, top_mid)
    arrow(ax, car[0] + car[2] + P, top_mid, prb[0] - P, top_mid)
    # probes -> panel: straight drop where the two boxes overlap horizontally
    arrow(ax, 0.86, prb[1] - P, 0.86, pan[1] + pan[3] + P)
    # sources -> mint / reputation: one trunk, two branches
    bus_x = src[0] + src[2] / 2
    mint_mid = mnt[1] + mnt[3] / 2
    rep_mid = rep[1] + rep[3] / 2
    ax.plot([bus_x, bus_x], [src[1] - P, rep_mid], lw=0.9, color=EDGE,
            solid_capstyle="round")
    arrow(ax, bus_x, mint_mid, mnt[0] - P, mint_mid)
    arrow(ax, bus_x, rep_mid, rep[0] - P, rep_mid)
    # probes -> annotation: down the corridor left of the panel, then into the box
    ann_mid = ann[1] + ann[3] / 2
    elbow(ax, [(0.741, prb[1] - P), (0.741, ann_mid),
               (ann[0] + ann[2] + P, ann_mid)])
    # audit -> panel: the verification arrow
    arrow(ax, 0.86, aud[1] + aud[3] + P, 0.86, pan[1] - P)
    ax.text(0.874, (aud[1] + aud[3] + pan[1]) / 2, "checks", fontsize=6.7,
            ha="left", va="center", color=EDGE)

    check_fit(fig, ax)
    for ext in ("pdf", "png"):
        fig.savefig(out / f"fig1_pipeline.{ext}", dpi=300)
    plt.close(fig)


def fig_population_funnel(s1: dict, s2: dict, out: Path) -> None:
    fig, (axa, axb) = plt.subplots(1, 2, figsize=(7.5, 3.4),
                                   gridspec_kw={"width_ratios": [0.9, 1.15]})

    chains = sorted(s2["chains"], key=lambda c: s2["chains"][c]["n"])
    vals = [s2["chains"][c]["n"] for c in chains]
    names = [CHAIN_NAMES.get(c, c) for c in chains]
    bars = axa.barh(range(len(chains)), vals, color="#5b8db8", height=0.62)
    axa.set_yticks(range(len(chains)), names, fontsize=7.6)
    axa.set_xscale("log")
    axa.set_xlim(70, 2.2e6)
    axa.set_xlabel("tokens at mc_t2 (log scale)", fontsize=8)
    for r, v in zip(bars, vals):
        axa.text(v * 1.18, r.get_y() + r.get_height() / 2, f"{v:,}",
                 va="center", fontsize=6.9)
    axa.text(-0.245, 1.03, "a", transform=axa.transAxes, fontsize=11,
             fontweight="bold")

    cats = ["all\ntokens", "S0mc\ndeclared", "S8mc\nlive 2xx",
            "S9mc\nagent-like", "S11mc\nendpoints", "S10mc\nclusters"]
    t1 = [s1["n_agents"], s1["S0mc"]["n"], s1["S8mc"]["n"], s1["S9mc"]["n"],
          s1["S11mc"]["distinct_live_endpoints"], s1["S10mc"]["clusters"]]
    t2 = [s2["n_agents"], s2["S0mc"]["n"], s2["S8mc"]["n"], s2["S9mc"]["n"],
          s2["S11mc"]["distinct_live_endpoints"], s2["S10mc"]["clusters"]]
    x = range(len(cats))
    w = 0.38
    b1 = axb.bar([i - w / 2 for i in x], t1, w, color="#a8c4dc",
                 label="mc_t1 (Aug 10)")
    b2 = axb.bar([i + w / 2 for i in x], t2, w, color="#39678f",
                 label="mc_t2 (Aug 16)")
    axb.set_yscale("log")
    axb.set_ylim(80, 3.2e6)
    axb.set_xticks(list(x), cats, fontsize=6.5)
    axb.set_xlim(-0.65, 5.65)
    axb.set_ylabel("count (log scale)", fontsize=8)
    for bars_ in (b1, b2):
        for r in bars_:
            axb.text(r.get_x() + r.get_width() / 2, r.get_height() * 1.12,
                     f"{int(r.get_height()):,}", ha="center", fontsize=6.0,
                     rotation=90, va="bottom")
    axb.axvline(3.5, color="#9a9a9a", lw=0.8, ls=(0, (3, 3)))
    axb.text(1.5, 1.7e6, "tokens", ha="center", fontsize=7.2, color="#555555")
    axb.text(4.5, 1.7e6, "other units", ha="center", fontsize=7.2,
             color="#555555")
    axb.legend(fontsize=7.2, frameon=False, loc="upper right",
               bbox_to_anchor=(1.03, 0.72))
    axb.text(-0.14, 1.03, "b", transform=axb.transAxes, fontsize=11,
             fontweight="bold")

    fig.tight_layout(pad=0.6)
    for ext in ("pdf", "png"):
        fig.savefig(out / f"fig2_population_funnel.{ext}", dpi=300)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--t1", default=str(ROOT / "_enrich" / "multichain" / "MC_SUMMARY.json"))
    ap.add_argument("--t2", default=str(ROOT / "_enrich" / "multichain" / "mc_t2" / "MC_SUMMARY.json"))
    ap.add_argument("--out", default=str(ROOT / "figures"))
    args = ap.parse_args()

    s1 = json.loads(Path(args.t1).read_text(encoding="utf-8"))
    s2 = json.loads(Path(args.t2).read_text(encoding="utf-8"))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    fig_pipeline(s2, out)
    fig_population_funnel(s1, s2, out)
    print("wrote", ", ".join(p.name for p in sorted(out.glob("fig*.p*"))))


if __name__ == "__main__":
    main()
