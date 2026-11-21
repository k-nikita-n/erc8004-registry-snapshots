#!/usr/bin/env python3
"""Score the blind certainty-stratum coding against the pre-coding key.

    python score.py [path/to/answers_coder2_set2.csv]

Reads answers CSV, `key.internal.csv`, `../weights_mc_t2.json`, and `_enrich/coding/coder_1.csv`.
Writes `score.json` (link-share estimand; see `certainty_score_deposit.py` for offline deposit scoring).
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import numpy as np
from scipy import stats

HERE = Path(__file__).resolve().parent
P1 = HERE.parent
ROOT = HERE.parents[2]
CODER1 = ROOT / "_enrich" / "coding" / "coder_1.csv"
Z = stats.norm.ppf(0.975)
A_TOK, B_TOK, N_TOK = 5990, 17683, 401149
AGREEMENT_FLOOR = 0.80


def wilson(p, n):
    d = 1 + Z * Z / n
    c = p + Z * Z / (2 * n)
    h = Z * np.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n))
    return (c - h) / d, (c + h) / d


def remainder(pairs):
    w = np.array([p[0] for p in pairs], dtype=float)
    y = np.array([p[1] for p in pairs], dtype=float)
    w, y = w[w > 0], y[w > 0]
    r = float((w * y).sum() / w.sum())
    lo, hi = wilson(r, float(w.sum() ** 2 / (w ** 2).sum()))
    return r, float(lo), float(hi)


def stratum_theta(total_w, cert, pairs, u_as):
    C = float(sum(w for w, _ in cert))
    Cy = float(sum(w * (u_as if lab == "U" else int(lab)) for w, lab in cert))
    Rm = total_w - C
    r, lo, hi = remainder(pairs)
    lift = lambda v: (Cy + Rm * v) / total_w
    return lift(r), lift(lo), lift(hi)


def estimate(wts, cert_A, cert_B, u_as):
    tA, lA, hA = stratum_theta(float(sum(wts["w_A"])), cert_A, wts["sample_A"], u_as)
    tB, lB, hB = stratum_theta(float(sum(wts["w_B"])), cert_B, wts["sample_B"], u_as)
    G = A_TOK * tA + B_TOK * tB
    L = G - np.hypot(A_TOK * (tA - lA), B_TOK * (tB - lB))
    U = G + np.hypot(A_TOK * (hA - tA), B_TOK * (hB - tB))
    return {"ppv_pct": round(100 * G / N_TOK, 4), "ci95_pct": [round(100 * L / N_TOK, 4), round(100 * U / N_TOK, 4)],
            "theta_A": round(tA, 4), "theta_B": round(tB, 5)}


def main() -> None:
    ans_path = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "package_coder2" / "answers_coder2_set2.csv"
    answers = {int(r["idx"]): (r["label"].strip().upper(), r.get("note", ""))
               for r in csv.DictReader(ans_path.open(encoding="utf-8-sig"))}
    key = list(csv.DictReader((HERE / "key.internal.csv").open(encoding="utf-8")))
    missing = [int(k["idx"]) for k in key if answers.get(int(k["idx"]), ("",))[0] not in ("0", "1", "U")]
    if missing:
        raise SystemExit(f"unlabeled or invalid items: {missing}")

    wts = json.loads((P1 / "weights_mc_t2.json").read_text(encoding="utf-8"))
    frame = json.loads((P1 / "certainty_frame_mc_t2.internal.json").read_text(encoding="utf-8"))
    assert not any(r["in_coding_sample"] for s in ("A", "B") for r in frame[s])
    for s, top in (("A", wts["w_A"]), ("B", wts["w_B"])):
        assert [r["weight"] for r in frame[s]] == top[:10], f"frame/weights mismatch in {s}"

    cert = {"A": [], "B": []}
    for k in key:
        if k["role"] == "certainty":
            cert[k["stratum"]].append((int(k["weight"]), answers[int(k["idx"])][0]))
    assert len(cert["A"]) == len(cert["B"]) == 10

    c1 = {r["endpoint"]: r["label"].strip().upper() for r in csv.DictReader(CODER1.open(encoding="utf-8"))}
    pairs = [(c1[k["endpoint"]], answers[int(k["idx"])][0]) for k in key if k["role"] == "control"]
    decisive = [(a, b) for a, b in pairs if a in "01" and b in "01"]
    agree = sum(a == b for a, b in decisive) / len(decisive) if decisive else float("nan")

    out = {
        "answers_file": str(ans_path.name),
        "certainty_labels": {s: [{"weight": w, "label": lab} for w, lab in sorted(cert[s], reverse=True)]
                             for s in ("A", "B")},
        "controls": {"n": len(pairs), "decisive_pairs": len(decisive), "raw_agreement": round(agree, 4),
                     "coder2_U": sum(b == "U" for _, b in pairs), "floor": AGREEMENT_FLOOR,
                     "passes_floor": bool(decisive) and agree >= AGREEMENT_FLOOR},
        "primary_U_as_0": estimate(wts, cert["A"], cert["B"], 0),
        "sensitivity_U_as_1": estimate(wts, cert["A"], cert["B"], 1),
        "prespecified_srs_pct": 1.096,
    }
    (HERE / "score.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
