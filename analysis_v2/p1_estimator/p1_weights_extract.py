#!/usr/bin/env python3
"""Extract mc_t2 token-weight distributions for the two coding strata (counts only).

Outputs:
  weights_mc_t2.json                   - stratum weight arrays (w_A, w_B) and sample (weight, label) pairs
  certainty_frame_mc_t2.internal.json  - internal frame of the TOP_K heaviest endpoints per stratum
"""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

csv.field_size_limit(10**9)
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]                         # repository root
MC = ROOT / "_enrich" / "multichain" / "mc_t2"
CODING = ROOT / "_enrich" / "coding"
CACHE = ROOT / "_enrich" / "multichain" / "uri_cache"
CENSUS = ROOT / "_chain" / "multichain"
BODIES = MC / "probe_bodies"
OUT = HERE / "weights_mc_t2.json"
OUT_FRAME = HERE / "certainty_frame_mc_t2.internal.json"
TOP_K = 10


def body_info(url: str) -> dict:
    h = hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()
    p = BODIES / h[:2] / f"{h}.json"
    if not p.exists():
        return {"body_file": False}
    try:
        b = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"body_file": True, "readable": False}
    return {"body_file": True, "readable": True, "http_status": b.get("http_status"),
            "content_type": b.get("content_type"), "body_len": b.get("body_len"),
            "snippet_chars": len(b.get("body_snippet") or ""), "truncated": b.get("truncated")}


def card_of(uri: str) -> dict | None:
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
    if not CACHE.is_dir():
        raise SystemExit(
            f"fetch cache not found at {CACHE}; this script requires the internal working copy. "
            "On an unpacked v2.0.0 deposit, precomputed weights ship at _enrich/coding/certainty/weights_mc_t2.json."
        )
    probes = json.loads((MC / "probes.json").read_text(encoding="utf-8"))
    frame = {e: r for e, r in probes.items() if r.get("probeable_rspec") == 1 and r.get("live") == 1}
    live_A = {e for e, r in frame.items() if r.get("agentish") == 1}
    live_B = {e for e, r in frame.items() if r.get("agentish") == 0}

    uri_by_key: dict[tuple[str, str], str] = {}
    for d in sorted(CENSUS.iterdir()):
        if not d.is_dir():
            continue
        cens = sorted(d.glob("census_*.csv"))
        if not cens:
            continue
        with cens[-1].open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                u = (r.get("token_uri") or "").strip()
                if u:
                    uri_by_key[(d.name, r["agent_id"])] = u

    w_A: Counter = Counter()
    w_B: Counter = Counter()
    s9 = l0 = links_A = links_B = 0
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
                eps = {str(s.get("endpoint", "")).strip()
                       for s in (c.get("services") or []) if isinstance(s, dict)}
                if r.get("S9mc") == "1":
                    s9 += 1
                    hit = eps & live_A
                    links_A += len(hit)
                    for e in hit:
                        w_A[e] += 1
                else:
                    hit = eps & live_B
                    if hit:
                        l0 += 1
                        links_B += len(hit)
                        for e in hit:
                            w_B[e] += 1

    stratum = {r["endpoint"]: r["stratum"]
               for r in csv.DictReader(open(CODING / "sample.csv", encoding="utf-8"))}
    lab = {r["endpoint"]: r["label"]
           for r in csv.DictReader(open(CODING / "coder_1.csv", encoding="utf-8"))}
    sample_A = sorted([w_A.get(e, 0), int(lab[e] == "1")] for e in lab if stratum[e] == "A" and lab[e] in "01")
    sample_B = sorted([w_B.get(e, 0), int(lab[e] == "1")] for e in lab if stratum[e] == "B" and lab[e] in "01")
    in_frame_A = sum(1 for e in lab if stratum[e] == "A" and e in live_A)
    in_frame_B = sum(1 for e in lab if stratum[e] == "B" and e in live_B)

    wa = sorted((w_A.get(e, 0) for e in live_A), reverse=True)
    wb = sorted((w_B.get(e, 0) for e in live_B), reverse=True)
    out = {
        "generated": "2026-09-30, mc_t2 stratum token-weight distributions (counts only)",
        "checks": {"S9mc": s9, "L0": l0, "N_A": len(live_A), "N_B": len(live_B),
                   "links_A": links_A, "links_B": links_B,
                   "sum_w_A": sum(wa), "sum_w_B": sum(wb),
                   "top5_A": wa[:5], "top5_B": wb[:5],
                   "zero_weight_A": wa.count(0), "zero_weight_B": wb.count(0),
                   "sample_in_frame": [in_frame_A, in_frame_B],
                   "sample_sizes": [len(sample_A), len(sample_B)]},
        "w_A": wa,
        "w_B": wb,
        "sample_A": sample_A,
        "sample_B": sample_B,
    }
    OUT.write_text(json.dumps(out), encoding="utf-8")
    print(json.dumps(out["checks"], indent=1))

    coded = set(lab)
    frame_out = {}
    for name, wc in (("A", w_A), ("B", w_B)):
        top = sorted(wc.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_K]
        frame_out[name] = [{"rank": i + 1, "endpoint": e, "weight": w, "in_coding_sample": e in coded,
                            **body_info(e)} for i, (e, w) in enumerate(top)]
    OUT_FRAME.write_text(json.dumps(frame_out, indent=1), encoding="utf-8")
    for name, rows in frame_out.items():
        print(f"[certainty {name}] weights={[r['weight'] for r in rows]} "
              f"body_files={sum(r['body_file'] for r in rows)}/{len(rows)} "
              f"status={[r.get('http_status') for r in rows]} "
              f"snippet_chars={[r.get('snippet_chars') for r in rows]} "
              f"in_sample={sum(r['in_coding_sample'] for r in rows)}")
    print("[expect] S9mc=5990 L0=17683 N_A=3147 N_B=16861 top3_A=[2150, 831, 717] zero_A=14")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
