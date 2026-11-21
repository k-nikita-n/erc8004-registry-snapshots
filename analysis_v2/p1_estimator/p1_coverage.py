#!/usr/bin/env python3
"""Exact binomial-grid coverage of 95% intervals for the two-phase PPV estimator.

Estimator: G_hat = a * p_hat + b * q_hat, PPV_hat = G_hat / N, where
a = flagged live tokens (S9mc), b = live unflagged tokens (L0),
p_hat = k / n_A (flagged stratum precision), q_hat = m / n_B (unflagged miss rate).

Interval methods:
  sub_boot      substituted per-stratum percentile-bootstrap bounds
  joint_boot    joint percentile bootstrap of a*p* + b*q*
  sub_wilson    substituted per-stratum Wilson bounds
  mover_wilson  MOVER (Zou & Donner 2008) with Wilson components
  jeffreys      equal-tailed quantiles of a*Beta(k+.5, n_A-k+.5) + b*Beta(m+.5, n_B-m+.5)

Writes p1_coverage.json next to this script.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import numpy as np
from scipy import stats

HERE = Path(__file__).resolve().parent
OUT = HERE / "p1_coverage.json"

SEED = 20260930
B = 4000
Z = stats.norm.ppf(0.975)
PMF_EPS = 1e-10
METHODS = ("sub_boot", "joint_boot", "sub_wilson", "mover_wilson", "jeffreys")

# ERC-8004 mc_t2 counts
ERC = {"a": 5990, "b": 17683, "N": 401149, "nA": 150, "nB": 150, "k": 107, "m": 1}

PIS = (0.50, 0.714, 0.90)
RHOS = (0.001, 0.00667, 0.02, 0.05)
SIZES = ((50, 50), (150, 150), (300, 300))
GEOMETRIES = {            # b / a: live-unflagged tokens per flagged token
    "few_unflagged": 0.3,
    "erc8004": ERC["b"] / ERC["a"],
    "many_unflagged": 30.0,
}


def wilson(k, n):
    k = np.asarray(k, dtype=float)
    p = k / n
    d = 1 + Z * Z / n
    c = p + Z * Z / (2 * n)
    h = Z * np.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n))
    return (c - h) / d, (c + h) / d


def support(n, p):
    k = np.arange(n + 1)
    pmf = stats.binom.pmf(k, n, p)
    keep = pmf > PMF_EPS
    return k[keep], pmf[keep]


def intervals(a, b, nA, nB, kA, kB, rng, methods=METHODS):
    """Interval methods for every (k, m) on the grid kA x kB. Returns dict of (L, U)."""
    pA, qB = kA / nA, kB / nB
    est = a * pA[:, None] + b * qB[None, :]
    wl_p, wu_p = wilson(kA, nA)
    wl_q, wu_q = wilson(kB, nB)
    res = {
        "sub_wilson": (a * wl_p[:, None] + b * wl_q[None, :],
                       a * wu_p[:, None] + b * wu_q[None, :]),
        "mover_wilson": (est - np.sqrt((a * (pA - wl_p))[:, None] ** 2 + (b * (qB - wl_q))[None, :] ** 2),
                         est + np.sqrt((a * (wu_p - pA))[:, None] ** 2 + (b * (wu_q - qB))[None, :] ** 2)),
    }
    if not {"sub_boot", "joint_boot", "jeffreys"} & set(methods):
        return res

    PA = rng.binomial(nA, pA[:, None], size=(len(kA), B)) / nA
    QB = rng.binomial(nB, qB[:, None], size=(len(kB), B)) / nB
    pA_lo, pA_hi = np.quantile(PA, [0.025, 0.975], axis=1)
    qB_lo, qB_hi = np.quantile(QB, [0.025, 0.975], axis=1)

    JA = rng.beta(kA[:, None] + 0.5, nA - kA[:, None] + 0.5, size=(len(kA), B))
    JB = rng.beta(kB[:, None] + 0.5, nB - kB[:, None] + 0.5, size=(len(kB), B))

    shape = (len(kA), len(kB))
    Lj, Uj, Lb, Ub = (np.empty(shape) for _ in range(4))
    for i in range(len(kA)):
        Lj[i], Uj[i] = np.quantile(a * PA[i][None, :] + b * QB, [0.025, 0.975], axis=1)
        Lb[i], Ub[i] = np.quantile(a * JA[i][None, :] + b * JB, [0.025, 0.975], axis=1)

    res["sub_boot"] = (a * pA_lo[:, None] + b * qB_lo[None, :],
                       a * pA_hi[:, None] + b * qB_hi[None, :])
    res["joint_boot"] = (Lj, Uj)
    res["jeffreys"] = (Lb, Ub)
    return res


def cell(pi, rho, nA, nB, a, b, N, rng, methods=METHODS):
    G = a * pi + b * rho
    kA, wA = support(nA, pi)
    kB, wB = support(nB, rho)
    W = wA[:, None] * wB[None, :]
    W = W / W.sum()
    iv = intervals(a, b, nA, nB, kA, kB, rng, methods)
    out = {"true_ppv": G / N, "p_m_zero": (1 - rho) ** nB}
    for meth in methods:
        L, U = iv[meth]
        width = float((W * (U - L)).sum())
        out[meth] = {
            "coverage": float((W * ((L <= G) & (G <= U))).sum()),
            "truth_below_interval": float((W * (G < L)).sum()),
            "truth_above_interval": float((W * (G > U)).sum()),
            "mean_width_pp": 100 * width / N,
            "mean_rel_width": width / G,
        }
    return out


def observed(rng):
    """Every method on the realized ERC-8004 sample (k=107/150, m=1/150)."""
    a, b, N = ERC["a"], ERC["b"], ERC["N"]
    kA, kB = np.array([ERC["k"]]), np.array([ERC["m"]])
    iv = intervals(a, b, ERC["nA"], ERC["nB"], kA, kB, rng)
    est = a * ERC["k"] / ERC["nA"] + b * ERC["m"] / ERC["nB"]
    res = {"ppv_pct": 100 * est / N}
    for meth in METHODS:
        L, U = iv[meth]
        res[meth] = [round(100 * float(L[0, 0]) / N, 4), round(100 * float(U[0, 0]) / N, 4)]
    return res


def neyman_share(a, b, pi, rho):
    sa, sb = a * math.sqrt(pi * (1 - pi)), b * math.sqrt(rho * (1 - rho))
    return sa / (sa + sb)


def allocation(rng):
    """MOVER-Wilson expected width and coverage by allocation share, ERC geometry."""
    a, b, N = ERC["a"], ERC["b"], ERC["N"]
    rows = []
    for pi, rho in ((0.714, 0.00667), (0.714, 0.02)):
        for n in (150, 300, 600):
            for s in (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
                nA = int(round(s * n))
                nB = n - nA
                r = cell(pi, rho, nA, nB, a, b, N, rng, methods=("mover_wilson",))
                rows.append({"pi": pi, "rho": rho, "n": n, "share_A": s, "nA": nA, "nB": nB,
                             "coverage": round(r["mover_wilson"]["coverage"], 4),
                             "mean_width_pp": round(r["mover_wilson"]["mean_width_pp"], 4)})
    return rows


def sample_size(rng, targets_pp=(0.30, 0.20, 0.10)):
    """Smallest total n reaching an expected MOVER-Wilson width, balanced vs Neyman share."""
    a, b, N = ERC["a"], ERC["b"], ERC["N"]
    grid = (100, 150, 200, 300, 400, 600, 800, 1200, 1600, 2400)
    rows = []
    for pi, rho in ((0.714, 0.00667), (0.714, 0.02)):
        s_ney = neyman_share(a, b, pi, rho)
        for label, s in (("balanced", 0.5), ("neyman", s_ney)):
            widths = {}
            for n in grid:
                nA = int(round(s * n))
                r = cell(pi, rho, nA, n - nA, a, b, N, rng, methods=("mover_wilson",))
                widths[n] = r["mover_wilson"]["mean_width_pp"]
            need = {f"{t:.2f}pp": next((n for n in grid if widths[n] <= t), None) for t in targets_pp}
            rows.append({"pi": pi, "rho": rho, "allocation": label, "share_A": round(s, 3),
                         "width_pp_by_n": {str(n): round(w, 4) for n, w in widths.items()},
                         "min_n_for_width": need})
    return rows


def rule_of_three():
    """Upper contribution of an unobserved miss rate: m = 0 in n_B gives q_upper ~ 3 / n_B."""
    a, b, N = ERC["a"], ERC["b"], ERC["N"]
    rows = []
    for nB in (50, 150, 300, 600):
        cp_hi = 1 - 0.05 ** (1 / nB)                  # one-sided 95% Clopper-Pearson, m = 0
        rows.append({"nB": nB, "q_upper_cp": round(cp_hi, 5), "three_over_n": round(3 / nB, 5),
                     "ppv_upper_contribution_pp": round(100 * b * cp_hi / N, 4)})
    tol = {f"{d:.2f}pp": math.ceil(3 * b / (d / 100 * N)) for d in (0.20, 0.10, 0.05)}
    return {"rows": rows, "nB_for_tolerance": tol}


def main() -> None:
    t0 = time.time()
    rng = np.random.default_rng(SEED)
    grid = []
    for gname, ratio in GEOMETRIES.items():
        a = ERC["a"]
        b = ERC["b"] if gname == "erc8004" else ratio * a
        for pi in PIS:
            for rho in RHOS:
                for nA, nB in SIZES:
                    r = cell(pi, rho, nA, nB, a, b, ERC["N"], rng)
                    grid.append({"geometry": gname, "b_over_a": round(ratio, 3),
                                 "pi": pi, "rho": rho, "nA": nA, "nB": nB, **r})
    print(f"grid: {len(grid)} cells, {time.time() - t0:.0f} s")

    summary = {}
    for meth in METHODS:
        cov = np.array([g[meth]["coverage"] for g in grid])
        summary[meth] = {"min_coverage": round(float(cov.min()), 4),
                         "median_coverage": round(float(np.median(cov)), 4),
                         "share_cells_below_0.93": round(float((cov < 0.93).mean()), 3),
                         "share_cells_above_0.975": round(float((cov > 0.975).mean()), 3)}

    out = {
        "generated": "2026-09-30, two-phase PPV estimator coverage",
        "seed": SEED, "draws_per_outcome": B, "pmf_eps": PMF_EPS,
        "model": "unweighted binomial strata; exact enumeration over (k, m)",
        "erc8004": ERC,
        "observed_intervals_pct": observed(rng),
        "coverage_summary": summary,
        "neyman_share_A": {"planning_rho_0.00667": round(neyman_share(ERC["a"], ERC["b"], 0.714, 0.00667), 3),
                           "planning_rho_0.02": round(neyman_share(ERC["a"], ERC["b"], 0.714, 0.02), 3)},
        "allocation": allocation(rng),
        "sample_size": sample_size(rng),
        "rule_of_three": rule_of_three(),
        "grid": grid,
    }
    OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("observed_intervals_pct", "coverage_summary", "neyman_share_A")}, indent=1))
    print(f"wrote {OUT}  ({time.time() - t0:.0f} s)")


if __name__ == "__main__":
    main()
