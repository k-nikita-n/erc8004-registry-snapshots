#!/usr/bin/env python3
"""Sensitivity of the certainty-stratum PPV estimate to human labeling error.

Bounds error rates from double-coded pairs (stratum A: 0/60 disagreements;
stratum B: 2/27 resolved by adjudication) via one-sided 95% Clopper-Pearson
bounds and Rogan-Gladen adjustment. Writes p1_label_error.json.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = HERE / "p1_label_error.json"
A_TOK, B_TOK, N_TOK = 5990, 17683, 401149


def cp_upper(x, n, alpha=0.05):
    """One-sided Clopper-Pearson upper bound; x = 0 only (closed form)."""
    assert x == 0
    return 1 - alpha ** (1 / n)


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


def main() -> None:
    wts = _load_weights()
    cert_all = _load_certainty_labels()

    def parts(s, pairs, tot):
        cert = cert_all[s]
        C = sum(c["weight"] for c in cert)
        Cy = sum(c["weight"] for c in cert if c["label"] == "1")
        w = [p[0] for p in pairs if p[0] > 0]
        y = [p[1] for p in pairs if p[0] > 0]
        r = sum(wi * yi for wi, yi in zip(w, y)) / sum(w)
        return C, Cy, tot - C, r

    CA, CyA, RA, rA = parts("A", wts["sample_A"], sum(wts["w_A"]))
    CB, CyB, RB, rB = parts("B", wts["sample_B"], sum(wts["w_B"]))
    totA, totB = CA + RA, CB + RB

    def ppv(pA, qB):
        tA = (CyA + RA * pA) / totA
        tB = (CyB + RB * qB) / totB
        return round(100 * (A_TOK * tA + B_TOK * tB) / N_TOK, 4)

    eA = cp_upper(0, 60)
    eB = cp_upper(0, 27)
    rg = lambda p, se, sp: min(1.0, max(0.0, (p - (1 - sp)) / (se + sp - 1)))
    scen = {
        "primary": ppv(rA, rB),
        "A_coder_sens_low (true agents missed)": ppv(rg(rA, 1 - eA, 1.0), rB),
        "A_coder_spec_low (non-agents called agents)": ppv(rg(rA, 1.0, 1 - eA), rB),
        "A_both_at_bound": ppv(rg(rA, 1 - eA, 1 - eA), rB),
        "B_hidden_misses_at_0of27_bound": ppv(rA, rB + (1 - rB) * eB),
        "B_coder2_upward_calls_correct (rubric-excluded)": ppv(rA, rB + (1 - rB) * 2 / 27),
    }
    wsA = sum(p[0] for p in wts["sample_A"] if p[0] > 0)
    wsB = sum(p[0] for p in wts["sample_B"] if p[0] > 0)
    per_item = {"A_one_weight1_item_flipped_pp": round(100 * A_TOK * (RA / totA) / wsA / N_TOK, 4),
                "B_one_weight1_item_flipped_pp": round(100 * B_TOK * (RB / totB) / wsB / N_TOK, 4)}
    out = {"generated": "2026-10-02, label-error sensitivity",
           "per_item_effect": per_item,
           "bounds": {"A_error_rate_cp95_from_0_of_60": round(eA, 4), "B_hidden_miss_cp95_from_0_of_27": round(eB, 4),
                      "B_upward_disagreements": "2 of 27 decisive pairs"},
           "remainder_rates": {"r_A": round(rA, 4), "r_B": round(rB, 5),
                               "remainder_share_A": round(RA / totA, 4), "remainder_share_B": round(RB / totB, 4)},
           "ppv_pct": scen}
    OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
