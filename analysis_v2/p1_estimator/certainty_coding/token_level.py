#!/usr/bin/env python3
"""Token-level reading of the certainty-stratum coding.

Counts tokens linked to a certainty endpoint labeled 1 directly; applies the
token-weighted remainder rate to remaining tokens in each stratum (U as 0 primary,
U as 1 sensitivity), with Kish-effective Wilson / MOVER intervals.
"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import numpy as np
from scipy import stats

csv.field_size_limit(10**9)
HERE = Path(__file__).resolve().parent
P1 = HERE.parent
ROOT = HERE.parents[2]
MC = ROOT / "_enrich" / "multichain" / "mc_t2"
CACHE = ROOT / "_enrich" / "multichain" / "uri_cache"
CENSUS = ROOT / "_chain" / "multichain"
Z = stats.norm.ppf(0.975)
N_TOK = 401149


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


def card_of(uri):
    h = hashlib.sha1(uri.encode("utf-8", "replace")).hexdigest()
    p = CACHE / h[:2] / f"{h}.json"
    if not p.exists():
        return None
    try:
        c = json.loads(p.read_text(encoding="utf-8")).get("card")
        return c if isinstance(c, dict) else None
    except Exception:
        return None


def main() -> None:
    key = list(csv.DictReader((HERE / "key.internal.csv").open(encoding="utf-8")))
    ans = {int(r["idx"]): r["label"].strip().upper()
           for r in csv.DictReader((HERE / "package_coder2" / "answers_coder2_set2.csv").open(encoding="utf-8-sig"))}
    cert = {k["endpoint"]: (k["stratum"], ans[int(k["idx"])]) for k in key if k["role"] == "certainty"}
    wts = json.loads((P1 / "weights_mc_t2.json").read_text(encoding="utf-8"))

    probes = json.loads((MC / "probes.json").read_text(encoding="utf-8"))
    frame = {e: r for e, r in probes.items() if r.get("probeable_rspec") == 1 and r.get("live") == 1}
    live_A = {e for e, r in frame.items() if r.get("agentish") == 1}
    live_B = {e for e, r in frame.items() if r.get("agentish") == 0}

    uri_by_key = {}
    for d in sorted(CENSUS.iterdir()):
        if d.is_dir() and (cens := sorted(d.glob("census_*.csv"))):
            with cens[-1].open(encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    if u := (r.get("token_uri") or "").strip():
                        uri_by_key[(d.name, r["agent_id"])] = u

    counts = {"S9": 0, "L0": 0}
    for u_as in ("0", "1"):
        counts[f"A_conf_U{u_as}"] = counts[f"B_conf_U{u_as}"] = 0
    counts["A_on_certainty"] = counts["B_only_certainty"] = 0
    pos = {u: {e for e, (_, lab) in cert.items() if lab == "1" or (u == "1" and lab == "U")} for u in ("0", "1")}
    for p in sorted(MC.glob("panel_*.csv")):
        if p.name == "panel_all.csv":
            continue
        chain = p.stem[len("panel_"):]
        with p.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                u = uri_by_key.get((chain, r["agent_id"]))
                c = card_of(u) if u else None
                if not c:
                    continue
                eps = {str(s.get("endpoint", "")).strip() for s in (c.get("services") or []) if isinstance(s, dict)}
                if r.get("S9mc") == "1":
                    counts["S9"] += 1
                    hit = eps & live_A
                    counts["A_on_certainty"] += bool(hit & cert.keys())
                    for ua in ("0", "1"):
                        counts[f"A_conf_U{ua}"] += bool(hit & pos[ua])
                else:
                    hit = eps & live_B
                    if hit:
                        counts["L0"] += 1
                        counts["B_only_certainty"] += hit <= cert.keys()
                        for ua in ("0", "1"):
                            counts[f"B_conf_U{ua}"] += bool(hit & pos[ua])
    assert counts["S9"] == 5990 and counts["L0"] == 17683, counts

    rA, lA, hA = remainder(wts["sample_A"])
    rB, lB, hB = remainder(wts["sample_B"])
    out = {"counts": counts}
    for ua in ("0", "1"):
        # tokens decided by the certainty coding: confirmed ones count 1; A tokens linked only to
        # certainty endpoints labeled 0 are still given r_hat (conservative only if r_hat < 1).
        nA = counts["S9"] - counts[f"A_conf_U{ua}"]
        nB = counts["L0"] - counts[f"B_conf_U{ua}"] - counts["B_only_certainty"]
        G = counts[f"A_conf_U{ua}"] + nA * rA + counts[f"B_conf_U{ua}"] + nB * rB
        L = G - np.hypot(nA * (rA - lA), nB * (rB - lB))
        U = G + np.hypot(nA * (hA - rA), nB * (hB - rB))
        out[f"token_level_U_as_{ua}"] = {"ppv_pct": round(100 * G / N_TOK, 4),
                                         "ci95_pct": [round(100 * L / N_TOK, 4), round(100 * U / N_TOK, 4)],
                                         "G_hat": round(G, 1)}
    (HERE / "token_level.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
