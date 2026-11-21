#!/usr/bin/env python3
"""Recompute the certainty-stratum PPV estimate directly from the deposited files.

    python analysis_v2/certainty_score_deposit.py

Reads `_enrich/coding/certainty/certainty_labels.csv` and
`_enrich/coding/certainty/weights_mc_t2.json` unpacked from `certainty_coding.zip`
(deposit v2.0.0), without requiring any internal un-deposited files.
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CERT = ROOT / "_enrich" / "coding" / "certainty"
Z = 1.959963984540054  # norm.ppf(0.975)
A_TOK, B_TOK, N_TOK = 5990, 17683, 401149


def wilson(p: float, n: float) -> tuple[float, float]:
    d = 1.0 + Z * Z / n
    c = p + Z * Z / (2.0 * n)
    h = Z * math.sqrt(p * (1.0 - p) / n + Z * Z / (4.0 * n * n))
    return (c - h) / d, (c + h) / d


def remainder(pairs: list[list[int]]) -> tuple[float, float, float]:
    active = [(float(w), float(y)) for w, y in pairs if w > 0]
    sw = sum(w for w, _ in active)
    sw2 = sum(w * w for w, _ in active)
    r = sum(w * y for w, y in active) / sw
    neff = (sw * sw) / sw2
    lo, hi = wilson(r, neff)
    return r, lo, hi


def stratum_theta(
    total_w: float, cert: list[tuple[int, str]], pairs: list[list[int]], u_as: int
) -> tuple[float, float, float]:
    c_mass = float(sum(w for w, _ in cert))
    c_yes = float(sum(w * (u_as if lab == "U" else int(lab)) for w, lab in cert))
    rem = total_w - c_mass
    r, lo, hi = remainder(pairs)
    lift = lambda v: (c_yes + rem * v) / total_w
    return lift(r), lift(lo), lift(hi)


def estimate(
    wts: dict, cert_a: list[tuple[int, str]], cert_b: list[tuple[int, str]], u_as: int
) -> dict:
    ta, la, ha = stratum_theta(float(sum(wts["w_A"])), cert_a, wts["sample_A"], u_as)
    tb, lb, hb = stratum_theta(float(sum(wts["w_B"])), cert_b, wts["sample_B"], u_as)
    g = A_TOK * ta + B_TOK * tb
    lo = g - math.hypot(A_TOK * (ta - la), B_TOK * (tb - lb))
    hi = g + math.hypot(A_TOK * (ha - ta), B_TOK * (hb - tb))
    return {
        "ppv_pct": round(100.0 * g / N_TOK, 4),
        "ci95_pct": [round(100.0 * lo / N_TOK, 4), round(100.0 * hi / N_TOK, 4)],
        "theta_A": round(ta, 4),
        "theta_B": round(tb, 5),
    }


def main() -> None:
    rows = list(csv.DictReader((CERT / "certainty_labels.csv").open(encoding="utf-8")))
    wts = json.loads((CERT / "weights_mc_t2.json").read_text(encoding="utf-8"))
    cert: dict[str, list[tuple[int, str]]] = {"A": [], "B": []}
    ctrls = []
    for r in rows:
        if r["role"] == "certainty":
            cert[r["stratum"]].append((int(r["weight"]), r["coder2_label"].strip().upper()))
        elif r["role"] == "control":
            ctrls.append((r["coder1_control_label"].strip().upper(), r["coder2_label"].strip().upper()))
    agree = sum(a == b for a, b in ctrls)
    out = {
        "controls_agreement": f"{agree}/{len(ctrls)}",
        "primary_U_as_0": estimate(wts, cert["A"], cert["B"], 0),
        "sensitivity_U_as_1": estimate(wts, cert["A"], cert["B"], 1),
    }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
