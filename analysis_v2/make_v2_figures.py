#!/usr/bin/env python3
"""Rebuild the four measurement-study figures from an unpacked Zenodo v2.0.0 deposit.

    python analysis_v2/make_v2_figures.py [--out figures]

Produces `fig1_estimator`, `fig2_per_network`, `fig3_concentration`, and `fig4_hosting`
(.png and .pdf) using `matplotlib`. Prints no endpoint URL, hostname, or wallet address.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

ROOT = Path(__file__).resolve().parents[1]
Z = 1.959963984540054

CHAIN_NAMES = {
    "arbitrum": "Arbitrum One",
    "avalanche": "Avalanche C-Chain",
    "base": "Base",
    "bsc": "BNB Smart Chain",
    "celo": "Celo",
    "ethereum": "Ethereum",
    "gnosis": "Gnosis",
    "linea": "Linea",
    "mantle": "Mantle",
    "metis": "Metis",
    "optimism": "OP Mainnet",
    "polygon": "Polygon PoS",
    "scroll": "Scroll",
}

plt.rcParams.update(
    {
        "font.family": "DejaVu Sans",
        "font.size": 9,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "savefig.dpi": 600,
    }
)
GREY_DARK, GREY_MID, GREY_FILL, GREY_FILL2 = "#2b2b2b", "#7a7a7a", "#e8e8e8", "#c9c9c9"


def _wilson(p: float, n: float) -> tuple[float, float]:
    d = 1.0 + Z * Z / n
    c = p + Z * Z / (2.0 * n)
    h = Z * math.sqrt(p * (1.0 - p) / n + Z * Z / (4.0 * n * n))
    return (c - h) / d, (c + h) / d


def _rem(pairs: list[list[int]]) -> tuple[float, float, float]:
    act = [(float(w), float(y)) for w, y in pairs if w > 0]
    sw = sum(w for w, _ in act)
    sw2 = sum(w * w for w, _ in act)
    r = sum(w * y for w, y in act) / sw
    lo, hi = _wilson(r, (sw * sw) / sw2)
    return r, lo, hi


def _theta(total_w: float, cert: list[tuple[int, str]], pairs: list[list[int]]) -> tuple[float, float, float]:
    c_mass = float(sum(w for w, _ in cert))
    c_yes = float(sum(w * (0 if lab == "U" else int(lab)) for w, lab in cert))
    r, lo, hi = _rem(pairs)
    lift = lambda v: (c_yes + (total_w - c_mass) * v) / total_w
    return lift(r), lift(lo), lift(hi)


def _save(fig, out_dir: Path, stem: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for ext in ("png", "pdf"):
        fig.savefig(out_dir / f"{stem}.{ext}", bbox_inches="tight")
    plt.close(fig)
    print(f"saved {out_dir / stem}.png/.pdf")


def fig1_estimator(summary: dict, out_dir: Path) -> None:
    cert_dir = ROOT / "_enrich" / "coding" / "certainty"
    rows = list(csv.DictReader((cert_dir / "certainty_labels.csv").open(encoding="utf-8")))
    wts = json.loads((cert_dir / "weights_mc_t2.json").read_text(encoding="utf-8"))

    N = summary["n_agents"]
    S9 = summary["S9mc"]["n"]
    L0 = wts["checks"]["L0"]

    cert_a = [
        (int(r["weight"]), r["coder2_label"].strip().upper())
        for r in rows
        if r["role"] == "certainty" and r["stratum"] == "A"
    ]
    cert_b = [
        (int(r["weight"]), r["coder2_label"].strip().upper())
        for r in rows
        if r["role"] == "certainty" and r["stratum"] == "B"
    ]
    ta, la, ha = _theta(float(sum(wts["w_A"])), cert_a, wts["sample_A"])
    tb, lb, hb = _theta(float(sum(wts["w_B"])), cert_b, wts["sample_B"])
    g_hat = S9 * ta + L0 * tb
    g_lo = g_hat - math.hypot(S9 * (ta - la), L0 * (tb - lb))
    g_hi = g_hat + math.hypot(S9 * (ha - ta), L0 * (hb - tb))
    ppv = 100.0 * g_hat / N
    lo, hi = 100.0 * g_lo / N, 100.0 * g_hi / N
    ra, _, _ = _rem(wts["sample_A"])
    rb, _, _ = _rem(wts["sample_B"])
    frozen_ppv = 100.0 * (S9 * ra + L0 * rb) / N

    a_pos = sum(1 for _, lab in cert_a if lab == "1")
    b_pos = sum(1 for _, lab in cert_b if lab == "1")
    a_share = 100.0 * sum(w for w, _ in cert_a) / sum(wts["w_A"])
    b_share = 100.0 * sum(w for w, _ in cert_b) / sum(wts["w_B"])

    fig, ax = plt.subplots(figsize=(8.0, 3.7))
    ax.set_xlim(0, 112)
    ax.set_ylim(0, 58)
    ax.axis("off")

    def box(x, y, w, h, lines, fill=GREY_FILL):
        ax.add_patch(
            FancyBboxPatch(
                (x, y),
                w,
                h,
                boxstyle="round,pad=0.6,rounding_size=1.2",
                linewidth=0.8,
                edgecolor=GREY_DARK,
                facecolor=fill,
            )
        )
        for i, ln in enumerate(lines):
            ax.text(
                x + w / 2,
                y + h - (i + 0.5) * h / len(lines),
                ln,
                ha="center",
                va="center",
                fontsize=7.6,
                color=GREY_DARK,
                fontweight="bold" if i == 0 else "normal",
            )

    def arrow(x1, y1, x2, y2, label=None, ls="-"):
        ax.add_patch(
            FancyArrowPatch(
                (x1, y1),
                (x2, y2),
                arrowstyle="-|>",
                mutation_scale=11,
                linewidth=0.9,
                linestyle=ls,
                color=GREY_DARK,
                shrinkA=2,
                shrinkB=2,
            )
        )
        if label:
            ax.text((x1 + x2) / 2, (y1 + y2) / 2 + 2.0, label, ha="center", va="bottom", fontsize=8.0, color=GREY_DARK)

    box(1, 22, 19, 14, [f"N = {N:,}", "registered tokens", "13 networks, mc_t2"], fill=GREY_FILL2)
    box(27, 40, 22, 13, [f"S9mc = {S9:,}", "tokens whose endpoint", "answered agent-like"])
    box(27, 6, 22, 13, [f"L\u2080 = {L0:,}", "live tokens the flag", "left at 0"])
    arrow(20.6, 32, 26.4, 45)
    arrow(20.6, 26, 26.4, 13)
    arrow(10.5, 21.4, 10.5, 15.2, ls=(0, (4, 3)))
    ax.text(
        10.5,
        14.0,
        f"remaining {N - S9 - L0:,}\ntokens enter\nneither term (\u00a72.3)",
        ha="center",
        va="top",
        fontsize=7.3,
        color=GREY_DARK,
    )

    box(
        57,
        38,
        25,
        17,
        [
            rf"$\hat{{\theta}}_A$ = {ta:.3f}",
            f"heaviest 10: {a_pos} agent-like",
            f"({a_share:.1f}% of links)",
            rf"remainder $\hat{{\pi}}$ = {ra:.3f}",
        ],
    )
    box(
        57,
        3,
        25,
        17,
        [
            rf"$\hat{{\theta}}_B$ = {tb:.4f}",
            f"heaviest 10: {b_pos} misses",
            f"({b_share:.1f}% of links)",
            rf"remainder $\hat{{\rho}}$ = {rb:.4f}",
        ],
    )
    arrow(49.6, 46.5, 56.4, 46.5, label="\u00d7")
    arrow(49.6, 12.5, 56.4, 12.5, label="\u00d7")

    box(91, 31, 17, 13, [f"\u011c = {round(g_hat):,}", "confirmable", "identities"])
    arrow(82.6, 45, 90.4, 39.5)
    arrow(82.6, 13, 90.4, 32.5)
    ax.text(89.6, 26.5, f"PPV = \u011c / N = {ppv:.2f}%", ha="left", va="top", fontsize=8.8, color=GREY_DARK, fontweight="bold")
    ax.text(
        89.6,
        22.6,
        f"95% CI {lo:.2f}\u2013{hi:.2f}%\n(MOVER\u2013Wilson, \u00a72.4)\nfrozen protocol: {frozen_ppv:.2f}%",
        ha="left",
        va="top",
        fontsize=7.4,
        color=GREY_DARK,
    )
    ax.text(
        69.5,
        29.0,
        "blind human labels: certainty\nstratum + stratified sample (\u03ba = 0.94)",
        ha="center",
        va="center",
        fontsize=7.2,
        color=GREY_MID,
        style="italic",
    )
    _save(fig, out_dir, "fig1_estimator")


def fig2_per_network(summary: dict, out_dir: Path) -> None:
    chains = summary["chains"]
    rows = sorted(chains.items(), key=lambda kv: kv[1]["S9mc_pct"], reverse=True)
    names = [CHAIN_NAMES[k] for k, _ in rows]
    s0 = [v["S0mc_pct"] for _, v in rows]
    s9 = [v["S9mc_pct"] for _, v in rows]
    n = [v["n"] for _, v in rows]

    fig, ax = plt.subplots(figsize=(6.6, 4.2))
    y = list(range(len(rows)))[::-1]
    for yi, a, b in zip(y, s0, s9):
        ax.plot([b, a], [yi, yi], color=GREY_MID, linewidth=1.1, zorder=1)
    ax.scatter(
        s0,
        y,
        s=42,
        facecolors="white",
        edgecolors=GREY_DARK,
        linewidths=1.1,
        zorder=2,
        label="declares an agent protocol (S0mc)",
    )
    ax.scatter(
        s9,
        y,
        s=42,
        facecolors=GREY_DARK,
        edgecolors=GREY_DARK,
        zorder=3,
        label="live agent-like endpoint (S9mc)",
    )
    ax.set_xscale("log")
    ax.set_xlim(0.1, 110)
    ax.set_xticks([0.1, 1, 10, 100])
    ax.set_xticklabels(["0.1%", "1%", "10%", "100%"])
    ax.set_yticks(y)
    ax.set_yticklabels([f"{nm}  (n = {ni:,})" for nm, ni in zip(names, n)], fontsize=8.2)
    ax.set_xlabel("share of the network's registered tokens (log scale)")
    ax.legend(loc="lower right", frameon=False, fontsize=8.0)
    ax.grid(axis="x", color=GREY_FILL, linewidth=0.7, zorder=0)
    _save(fig, out_dir, "fig2_per_network")


def fig3_concentration(endpoint_unit: dict, out_dir: Path) -> None:
    top = [t["tokens"] for t in endpoint_unit["top_endpoints"]]
    total_ep = endpoint_unit["S11mc_distinct_live_endpoints"]
    singles = endpoint_unit["endpoints_single_token"]
    # Share of distinct S9mc tokens with >= 1 shared live agent-like endpoint (4,473 / 5,990 = 74.7%).
    shared_pct = 74.7

    fig, ax = plt.subplots(figsize=(6.2, 3.8))
    ranks = list(range(1, len(top) + 1))
    ax.stem(ranks, top, linefmt="-", markerfmt="o", basefmt=" ")
    for artist in ax.lines:
        artist.set_color(GREY_DARK)
        artist.set_markersize(3.6)
    for coll in ax.collections:
        coll.set_color(GREY_DARK)
    ax.set_yscale("log")
    ax.set_ylim(0.7, 4000)
    ax.set_xlim(0, len(top) + 1)
    ax.set_xlabel("live agent-like endpoints, ranked by the number of tokens served")
    ax.set_ylabel("tokens served (log scale)")
    ax.axhline(1, color=GREY_MID, linewidth=0.9, linestyle=(0, (4, 3)))
    ax.annotate(
        f"{top[0]:,} tokens on the largest endpoint",
        xy=(1, top[0]),
        xytext=(4.2, 2400),
        fontsize=8.2,
        color=GREY_DARK,
        arrowprops=dict(arrowstyle="-", color=GREY_MID, linewidth=0.8),
    )
    ax.text(
        len(top) + 0.4,
        420,
        f"{singles:,} of {total_ep:,} endpoints serve exactly one token;\n"
        f"{shared_pct:.1f}% of S9mc tokens share at least one endpoint",
        ha="right",
        va="top",
        fontsize=8.2,
        color=GREY_DARK,
    )
    ax.grid(axis="y", color=GREY_FILL, linewidth=0.7, zorder=0)
    _save(fig, out_dir, "fig3_concentration")


def fig4_hosting(out_dir: Path) -> None:
    p2 = json.loads((ROOT / "_enrich" / "multichain" / "mc_t3p" / "p2_results.json").read_text(encoding="utf-8"))
    h = p2["hosting"]["t3p_live_agentlike"]
    ep, hosts = h["providers_by_endpoints"], h["providers_by_hosts"]
    main = ["Vercel", "Cloudflare", "AWS", "Railway", "Render", "GitHub Pages"]
    rest_ep = sum(v for k, v in ep.items() if k not in main)
    rest_h = sum(v for k, v in hosts.items() if k not in main)
    labels = main + ["other providers"]
    e = [ep.get(k, 0) for k in main] + [rest_ep]
    s = [hosts.get(k, 0) for k in main] + [rest_h]
    ne, nh = h["n_endpoints"], h["n_hosts"]
    pe = [100 * v / ne for v in e]
    ph = [100 * v / nh for v in s]

    fig, ax = plt.subplots(figsize=(6.4, 3.6))
    y = list(range(len(labels)))[::-1]
    bh = 0.38
    ax.barh(
        [yi + bh / 2 for yi in y],
        pe,
        height=bh,
        color=GREY_DARK,
        label=f"share of live agent-like endpoints (n = {ne:,})",
    )
    ax.barh(
        [yi - bh / 2 for yi in y],
        ph,
        height=bh,
        color=GREY_FILL2,
        edgecolor=GREY_DARK,
        linewidth=0.6,
        label=f"share of hosts (n = {nh:,})",
    )
    for yi, a, b in zip(y, pe, ph):
        ax.text(a + 0.8, yi + bh / 2, f"{a:.1f}%", va="center", fontsize=7.6, color=GREY_DARK)
        ax.text(b + 0.8, yi - bh / 2, f"{b:.1f}%", va="center", fontsize=7.6, color=GREY_DARK)
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=8.4)
    ax.set_xlim(0, 92)
    ax.set_xlabel("share at the third probe (%)")
    ax.legend(loc="lower right", frameon=False, fontsize=7.8)
    ax.text(
        91,
        y[3],
        f"HHI over providers: {h['hhi_providers_endpoints']:,.0f} by endpoints,\n"
        f"{h['hhi_providers_hosts']:,.0f} by hosts; {h['distinct_as']} autonomous systems",
        ha="right",
        va="center",
        fontsize=7.6,
        color=GREY_DARK,
    )
    ax.grid(axis="x", color=GREY_FILL, linewidth=0.7, zorder=0)
    ax.set_axisbelow(True)
    _save(fig, out_dir, "fig4_hosting")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--t2", default=str(ROOT / "_enrich" / "multichain" / "mc_t2" / "MC_SUMMARY.json"))
    ap.add_argument(
        "--endpoint-unit",
        default=str(ROOT / "_enrich" / "multichain" / "mc_t2" / "MC_ENDPOINT_UNIT.json"),
    )
    ap.add_argument("--out", type=Path, default=ROOT / "figures")
    args = ap.parse_args()

    summary = json.loads(Path(args.t2).read_text(encoding="utf-8"))
    endpoint_unit = json.loads(Path(args.endpoint_unit).read_text(encoding="utf-8"))
    fig1_estimator(summary, args.out)
    fig2_per_network(summary, args.out)
    fig3_concentration(endpoint_unit, args.out)
    fig4_hosting(args.out)


if __name__ == "__main__":
    main()
