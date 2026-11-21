#!/usr/bin/env python3
"""Finite-population Monte Carlo under heavy-tailed token weights (weights_mc_t2.json).

Estimand: theta_s = sum(w*y) / sum(w) per stratum, G = a*theta_A + b*theta_B, PPV = G / N.

Designs (150 coded endpoints per stratum):
  srs       equal-probability draw without replacement, token-weighted ratio
  take_all  TAKE_ALL heaviest endpoints coded with certainty + SRS of the remainder
  pps       150 draws with replacement proportional to token weight

Intervals:
  boot_box  substituted per-stratum percentile-bootstrap bounds
  mover     Wilson per stratum (Kish effective n for srs/take_all; n draws for pps), MOVER across strata

Writes p1_weights_sim.json.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
from scipy import stats

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = HERE / "p1_weights_sim.json"


def _load_weights() -> dict:
    for p in (HERE / "weights_mc_t2.json", ROOT / "_enrich" / "coding" / "certainty" / "weights_mc_t2.json"):
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    raise SystemExit("weights_mc_t2.json not found; unpack certainty_coding.zip via unpack_deposit.py first")

SEED_POP = 20260930
SEED_DRAW = 20261001
R = 4000
B = 1000
N_CODE = 150
TAKE_ALL = 10
CHUNK = 40
Z = stats.norm.ppf(0.975)
PI0, RHO0 = 0.714, 0.00667
A_TOK, B_TOK, N_TOK = 5990, 17683, 401149

SCENARIOS = {
    "empirical":  {"A_top": (1, 1, 1), "B_top": None, "cap": None},
    "adverse_A":  {"A_top": (0, 0, 0), "B_top": None, "cap": None},
    "adverse_B":  {"A_top": (1, 1, 1), "B_top": (1,), "cap": None},
    "no_tail":    {"A_top": (1, 1, 1), "B_top": None, "cap": 3},
}


def wilson(p, n):
    p = np.clip(np.asarray(p, dtype=float), 0.0, 1.0)
    n = np.asarray(n, dtype=float)
    d = 1 + Z * Z / n
    c = p + Z * Z / (2 * n)
    h = Z * np.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n))
    return (c - h) / d, (c + h) / d


def srs_idx(rng, N, n, reps):
    out = np.empty((reps, n), dtype=np.int64)
    step = max(1, 4_000_000 // N)
    for s in range(0, reps, step):
        e = min(reps, s + step)
        out[s:e] = np.argpartition(rng.random((e - s, N)), n - 1, axis=1)[:, :n]
    return out


def ratio(ws, ys):
    sw = ws.sum(axis=-1)
    return np.where(sw > 0, (ws * ys).sum(axis=-1) / np.where(sw > 0, sw, 1), 0.0)


def boot_ratio(rng, ws, ys):
    """Percentile bootstrap of the weighted ratio, per replication. ws, ys: (reps, n)."""
    reps, n = ws.shape
    lo, hi = np.empty(reps), np.empty(reps)
    for s in range(0, reps, CHUNK):
        e = min(reps, s + CHUNK)
        ib = rng.integers(0, n, size=(e - s, B, n))
        rows = np.arange(e - s)[:, None, None]
        rb = ratio(ws[s:e][rows, ib], ys[s:e][rows, ib])
        lo[s:e], hi[s:e] = np.quantile(rb, [0.025, 0.975], axis=1)
    return lo, hi


def stratum(rng, w, y, design):
    """Estimate and both intervals of theta = sum(w*y)/sum(w), for R replications."""
    N = len(w)
    theta = float((w * y).sum() / w.sum())
    if design == "pps":
        idx = rng.choice(N, size=(R, N_CODE), p=w / w.sum())
        ys = y[idx].astype(float)
        est = ys.mean(axis=1)
        k = ys.sum(axis=1)
        bl, bh = np.quantile(rng.binomial(N_CODE, est[:, None], size=(R, B)) / N_CODE, [0.025, 0.975], axis=1)
        wl, wh = wilson(est, N_CODE)
        srt = np.sort(idx, axis=1)
        coded = (np.diff(srt, axis=1) != 0).sum(axis=1) + 1
        return theta, est, (bl, bh), (wl, wh), coded
    k0 = TAKE_ALL if design == "take_all" else 0
    idx = srs_idx(rng, N - k0, N_CODE - k0, R) + k0
    ws, ys = w[idx].astype(float), y[idx].astype(float)
    r = ratio(ws, ys)
    n_eff = ws.sum(axis=1) ** 2 / np.maximum((ws ** 2).sum(axis=1), 1e-12)
    rl, rh = boot_ratio(rng, ws, ys)
    wl, wh = wilson(r, np.maximum(n_eff, 1.0))
    C = float(w[:k0].sum())
    Cy = float((w[:k0] * y[:k0]).sum())
    Rm = float(w[k0:].sum())

    def lift(v):
        return (Cy + Rm * v) / (C + Rm)
    return theta, lift(r), (lift(rl), lift(rh)), (lift(wl), lift(wh)), np.full(R, N_CODE)


def population(wts, scen, rng):
    wA = np.array(wts["w_A"], dtype=float)
    wB = np.array(wts["w_B"], dtype=float)
    if scen["cap"]:
        wA, wB = np.minimum(wA, scen["cap"]), np.minimum(wB, scen["cap"])
    yA = (rng.random(len(wA)) < PI0).astype(int)
    yB = (rng.random(len(wB)) < RHO0).astype(int)
    yA[:len(scen["A_top"])] = scen["A_top"]
    if scen["B_top"]:
        yB[:len(scen["B_top"])] = scen["B_top"]
    return wA, yA, wB, yB


def main() -> None:
    t0 = time.time()
    wts = _load_weights()
    res = {}
    for sname, scen in SCENARIOS.items():
        wA, yA, wB, yB = population(wts, scen, np.random.default_rng(SEED_POP))
        res[sname] = {}
        for design in ("srs", "take_all", "pps"):
            rng = np.random.default_rng(SEED_DRAW)
            tA, eA, bA, mA, cA = stratum(rng, wA, yA, design)
            tB, eB, bB, mB, cB = stratum(rng, wB, yB, design)
            G = A_TOK * tA + B_TOK * tB
            Gh = A_TOK * eA + B_TOK * eB
            ivs = {
                "boot_box": (A_TOK * bA[0] + B_TOK * bB[0], A_TOK * bA[1] + B_TOK * bB[1]),
                "mover": (Gh - np.sqrt((A_TOK * (eA - mA[0])) ** 2 + (B_TOK * (eB - mB[0])) ** 2),
                          Gh + np.sqrt((A_TOK * (mA[1] - eA)) ** 2 + (B_TOK * (mB[1] - eB)) ** 2)),
            }
            d = {
                "true_ppv_pct": round(100 * G / N_TOK, 4),
                "theta_A": round(tA, 4), "theta_B": round(tB, 5),
                "mean_est_pct": round(100 * float(Gh.mean()) / N_TOK, 4),
                "median_est_pct": round(100 * float(np.median(Gh)) / N_TOK, 4),
                "rel_bias": round(float(Gh.mean()) / G - 1, 4),
                "rmse_pp": round(100 * float(np.sqrt(((Gh - G) ** 2).mean())) / N_TOK, 4),
                "p_err_gt_0.1pp": round(float((np.abs(Gh - G) / N_TOK > 0.001).mean()), 4),
                "coded_endpoints_mean": round(float((cA + cB).mean()), 1),
            }
            if design == "srs":
                d["p_top3_A_unsampled"] = round(float(np.prod([(len(wA) - N_CODE - j) / (len(wA) - j)
                                                                for j in range(3)])), 4)
            for name, (L, U) in ivs.items():
                d[name] = {"coverage": round(float(((L <= G) & (G <= U)).mean()), 4),
                           "truth_above": round(float((G > U).mean()), 4),
                           "truth_below": round(float((G < L).mean()), 4),
                           "mean_width_pp": round(100 * float((U - L).mean()) / N_TOK, 4)}
            res[sname][design] = d
            print(f"{sname:<10} {design:<8} true={d['true_ppv_pct']:.3f}% mean={d['mean_est_pct']:.3f}% "
                  f"med={d['median_est_pct']:.3f}% rmse={d['rmse_pp']:.3f}pp coded={d['coded_endpoints_mean']:.0f} | "
                  + " ".join(f"{m}={d[m]['coverage']:.3f}(above {d[m]['truth_above']:.3f}, w {d[m]['mean_width_pp']:.3f})"
                             for m in ivs), flush=True)
    out = {"generated": "2026-09-30, heavy-tailed token-weight simulations",
           "R": R, "B": B, "n_code": N_CODE, "take_all": TAKE_ALL,
           "seeds": {"population": SEED_POP, "draws": SEED_DRAW},
           "planning": {"pi0": PI0, "rho0": RHO0},
           "scenarios": {k: {kk: vv for kk, vv in v.items()} for k, v in SCENARIOS.items()},
           "results": res}
    OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"wrote {OUT} ({time.time() - t0:.0f} s)")


if __name__ == "__main__":
    main()
