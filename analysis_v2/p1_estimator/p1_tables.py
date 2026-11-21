#!/usr/bin/env python3
"""Markdown tables from p1_coverage.json (read-only)."""
from __future__ import annotations

import json
import statistics as st
from pathlib import Path

HERE = Path(__file__).resolve().parent
d = json.loads((HERE / "p1_coverage.json").read_text(encoding="utf-8"))
g = d["grid"]
M = ("sub_boot", "joint_boot", "sub_wilson", "mover_wilson", "jeffreys")


def row(cells):
    return "| " + " | ".join(str(c) for c in cells) + " |"


print("## ERC-8004 geometry, pi = 0.714: coverage / mean width (pp)\n")
print(row(["rho", "n_A/n_B", "P(m=0)", *M]))
print(row(["---"] * (3 + len(M))))
for c in g:
    if c["geometry"] == "erc8004" and c["pi"] == 0.714:
        print(row([c["rho"], f"{c['nA']}/{c['nB']}", f"{c['p_m_zero']:.3f}",
                   *(f"{c[m]['coverage']:.3f} / {c[m]['mean_width_pp']:.3f}" for m in M)]))

print("\n## Coverage by regime of the miss stratum\n")
print(row(["method", "all cells: min", "median", "P(m=0) >= 0.05: min", "median", "P(m=0) < 0.05: min", "median"]))
print(row(["---"] * 7))
for m in M:
    deg = [c[m]["coverage"] for c in g if c["p_m_zero"] >= 0.05]
    non = [c[m]["coverage"] for c in g if c["p_m_zero"] < 0.05]
    allc = [c[m]["coverage"] for c in g]
    print(row([m, f"{min(allc):.3f}", f"{st.median(allc):.3f}", f"{min(deg):.3f}", f"{st.median(deg):.3f}",
               f"{min(non):.3f}", f"{st.median(non):.3f}"]))

print("\n## Worst cells per method\n")
print(row(["method", "geometry", "pi", "rho", "n", "P(m=0)", "coverage", "truth below", "truth above"]))
print(row(["---"] * 9))
for m in M:
    for c in sorted(g, key=lambda c: c[m]["coverage"])[:3]:
        print(row([m, c["geometry"], c["pi"], c["rho"], c["nA"], f"{c['p_m_zero']:.3f}",
                   f"{c[m]['coverage']:.3f}", f"{c[m]['truth_below_interval']:.3f}",
                   f"{c[m]['truth_above_interval']:.3f}"]))

print("\n## Allocation (MOVER-Wilson, ERC geometry)\n")
print(row(["pi", "rho", "n", "share_A", "n_A/n_B", "coverage", "mean width pp"]))
print(row(["---"] * 7))
for r in d["allocation"]:
    print(row([r["pi"], r["rho"], r["n"], r["share_A"], f"{r['nA']}/{r['nB']}", r["coverage"], r["mean_width_pp"]]))

print("\n## Sample size for an expected MOVER-Wilson width\n")
for r in d["sample_size"]:
    print(f"- pi={r['pi']}, rho={r['rho']}, {r['allocation']} (share_A={r['share_A']}): "
          f"min n {r['min_n_for_width']}; widths {r['width_pp_by_n']}")

print("\n## Rule of three for the miss stratum\n")
print(json.dumps(d["rule_of_three"], indent=1))
print("\n## Observed ERC-8004 sample (pct)\n")
print(json.dumps(d["observed_intervals_pct"], indent=1))
