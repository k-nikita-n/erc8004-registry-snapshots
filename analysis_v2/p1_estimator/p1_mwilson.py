#!/usr/bin/env python3
"""Modified Wilson (Brown, Cai & DasGupta 2001) components for MOVER.

Replaces the Wilson boundary with a Poisson/chi2 bound for 1 <= x <= x*(n)
(x* = 2 for n <= 50, else 3; mirrored near n) over the 108-cell exact grid,
the realized sample, and the certainty-stratum estimator. Writes p1_mwilson.json.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
from scipy import stats

from p1_coverage import ERC, GEOMETRIES, PIS, RHOS, SIZES, support, wilson

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = HERE / "p1_mwilson.json"
ALPHA = 0.05


def _load_weights() -> dict:
    for p in (HERE / "weights_mc_t2.json", ROOT / "_enrich" / "coding" / "certainty" / "weights_mc_t2.json"):
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    raise SystemExit("weights_mc_t2.json not found; unpack certainty_coding.zip via unpack_deposit.py first")


def _load_certainty_labels() -> dict[str, list[dict]]:
    sc_path = HERE / "certainty_coding" / "score.json"
    if sc_path.exists():
        return json.loads(sc_path.read_text(encoding="utf-8"))["certainty_labels"]
    csv_path = ROOT / "_enrich" / "coding" / "certainty" / "certainty_labels.csv"
    if not csv_path.exists():
        raise SystemExit("certainty_labels.csv not found; unpack certainty_coding.zip via unpack_deposit.py first")
    out: dict[str, list[dict]] = {"A": [], "B": []}
    with csv_path.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["role"] == "certainty":
                out[r["stratum"]].append({"weight": int(r["weight"]), "label": r["coder2_label"].strip().upper()})
    return out


def x_star(n):
    return 2 if n <= 50 else 3


def mwilson(k, n):
    """Modified Wilson; k may be non-integer (effective counts under weights)."""
    k = np.asarray(k, dtype=float)
    lo, hi = wilson(k, n)
    xs = x_star(n)
    small = (k > 0) & (k <= xs)
    lo = np.where(small, stats.chi2.ppf(ALPHA / 2, 2 * np.maximum(k, 1e-9)) / (2 * n), lo)
    big = (k < n) & (k >= n - xs)
    hi = np.where(big, 1 - stats.chi2.ppf(ALPHA / 2, 2 * np.maximum(n - k, 1e-9)) / (2 * n), hi)
    return lo, hi


def mover(a, b, pA, lA, hA, qB, lB, hB):
    est = a * pA + b * qB
    return (est - np.sqrt((a * (pA - lA)) ** 2 + (b * (qB - lB)) ** 2),
            est + np.sqrt((a * (hA - pA)) ** 2 + (b * (hB - qB)) ** 2))


def cell(pi, rho, nA, nB, a, b):
    G = a * pi + b * rho
    kA, wA = support(nA, pi)
    kB, wB = support(nB, rho)
    W = wA[:, None] * wB[None, :]
    W /= W.sum()
    pA, qB = (kA / nA)[:, None], (kB / nB)[None, :]
    res = {}
    for name, f in (("mover_wilson", wilson), ("mover_mwilson", mwilson)):
        lA, hA = f(kA, nA)
        lB, hB = f(kB, nB)
        L, U = mover(a, b, pA, lA[:, None], hA[:, None], qB, lB[None, :], hB[None, :])
        res[name] = {"coverage": float((W * ((L <= G) & (G <= U))).sum()),
                     "truth_below": float((W * (G < L)).sum()),
                     "truth_above": float((W * (G > U)).sum()),
                     "mean_width_pp": 100 * float((W * (U - L)).sum()) / ERC["N"]}
    return res


def remainder(pairs, f):
    w = np.array([p[0] for p in pairs], dtype=float)
    y = np.array([p[1] for p in pairs], dtype=float)
    w, y = w[w > 0], y[w > 0]
    r = float((w * y).sum() / w.sum())
    n_eff = float(w.sum() ** 2 / (w ** 2).sum())
    lo, hi = f(r * n_eff, n_eff)
    return r, float(lo), float(hi)


def headline(f):
    wts = _load_weights()
    cert_all = _load_certainty_labels()
    out = []
    for s, pairs, tot in (("A", wts["sample_A"], sum(wts["w_A"])), ("B", wts["sample_B"], sum(wts["w_B"]))):
        cert = cert_all[s]
        C = sum(c["weight"] for c in cert)
        Cy = sum(c["weight"] for c in cert if c["label"] == "1")          # treat U as 0
        r, lo, hi = remainder(pairs, f)
        lift = lambda v: (Cy + (tot - C) * v) / tot
        out.append((lift(r), lift(lo), lift(hi)))
    (tA, lA, hA), (tB, lB, hB) = out
    L, U = mover(ERC["a"], ERC["b"], tA, lA, hA, tB, lB, hB)
    G = ERC["a"] * tA + ERC["b"] * tB
    return [round(100 * v / ERC["N"], 4) for v in (G, float(L), float(U))]


def main() -> None:
    grid = []
    for gname, ratio in GEOMETRIES.items():
        a = ERC["a"]
        b = ERC["b"] if gname == "erc8004" else ratio * a
        for pi in PIS:
            for rho in RHOS:
                for nA, nB in SIZES:
                    grid.append({"geometry": gname, "pi": pi, "rho": rho, "nA": nA, **cell(pi, rho, nA, nB, a, b)})
    summary = {}
    for m in ("mover_wilson", "mover_mwilson"):
        cov = np.array([g[m]["coverage"] for g in grid])
        wid = np.array([g[m]["mean_width_pp"] for g in grid])
        worst = min(grid, key=lambda g: g[m]["coverage"])
        summary[m] = {"min_coverage": round(float(cov.min()), 4), "median_coverage": round(float(np.median(cov)), 4),
                      "share_below_0.94": round(float((cov < 0.94).mean()), 3),
                      "share_above_0.975": round(float((cov > 0.975).mean()), 3),
                      "median_width_pp": round(float(np.median(wid)), 4),
                      "worst_cell": {k: worst[k] for k in ("geometry", "pi", "rho", "nA")}}
    erc_cell = next(g for g in grid if g["geometry"] == "erc8004" and g["pi"] == 0.714
                    and g["rho"] == 0.00667 and g["nA"] == 150)

    a, b, N = ERC["a"], ERC["b"], ERC["N"]
    obs = {}
    for name, f in (("mover_wilson", wilson), ("mover_mwilson", mwilson)):
        lA, hA = f(ERC["k"], ERC["nA"])
        lB, hB = f(ERC["m"], ERC["nB"])
        L, U = mover(a, b, ERC["k"] / ERC["nA"], lA, hA, ERC["m"] / ERC["nB"], lB, hB)
        obs[name] = {"rho_ci": [round(float(lB), 5), round(float(hB), 5)],
                     "ppv_ci_pct": [round(100 * float(L) / N, 4), round(100 * float(U) / N, 4)]}

    out = {"generated": "2026-10-02, modified Wilson MOVER sensitivity",
           "rule": "lower = chi2.ppf(alpha/2, 2x)/(2n) for 1 <= x <= x*(n), x* = 2 (n<=50) or 3; mirrored near n",
           "summary": summary, "erc8004_design_point": {k: erc_cell[k] for k in ("mover_wilson", "mover_mwilson")},
           "observed_sample": obs,
           "headline_certainty": {"mover_wilson": headline(wilson), "mover_mwilson": headline(mwilson)},
           "grid": grid}
    OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("summary", "erc8004_design_point", "observed_sample", "headline_certainty")},
                     indent=1))


if __name__ == "__main__":
    main()
