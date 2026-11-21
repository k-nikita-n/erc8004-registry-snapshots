#!/usr/bin/env python3
"""Compute S11mc (distinct live agent-like endpoints) and per-endpoint token sharing.

    python mc_qa_endpoint_unit.py                  # baseline mc_t1
    python mc_qa_endpoint_unit.py --label mc_t2    # writes into the labelled dir
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from r_spec import is_probeable_endpoint

csv.field_size_limit(10**9)
ROOT = Path(__file__).resolve().parents[2]
MC_BASE = ROOT / "_enrich" / "multichain"
CACHE = MC_BASE / "uri_cache"
CENSUS = ROOT / "_chain" / "multichain"


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
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--label",
        default="mc_t1",
        help="mc_t2 reads probes/panels from _enrich/multichain/mc_t2/ and writes there",
    )
    args = ap.parse_args()
    mc = MC_BASE if args.label == "mc_t1" else MC_BASE / args.label
    if not CACHE.is_dir():
        raise SystemExit(
            f"fetch cache not found at {CACHE}; rebuilding S11mc from raw cards requires the internal working copy. "
            f"On an unpacked deposit, precomputed {mc / 'MC_ENDPOINT_UNIT.json'} ships inside panels_{args.label}.zip."
        )
    probes_path = mc / "probes.json"
    if not probes_path.exists():
        raise SystemExit(f"no probes at {probes_path}")

    probes = json.loads(probes_path.read_text(encoding="utf-8"))
    agentish_eps = {e for e, r in probes.items() if r.get("agentish") == 1 and is_probeable_endpoint(e)}
    print(f"label={args.label} probes total={len(probes)}  agentish&probeable endpoints={len(agentish_eps)}")

    print("indexing census…")
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

    tokens_per_ep: Counter = Counter()
    eps_per_token: list[int] = []
    chain_eps: dict[str, set[str]] = defaultdict(set)
    s9_total = 0

    for p in sorted(mc.glob("panel_*.csv")):
        if p.name == "panel_all.csv":
            continue
        chain = p.stem[len("panel_") :]
        with p.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("S9mc") != "1":
                    continue
                s9_total += 1
                u = uri_by_key.get((chain, r["agent_id"]))
                c = card_of(u) if u else None
                if not c:
                    continue
                hit = set()
                for s in c.get("services", []) or []:
                    if not isinstance(s, dict):
                        continue
                    e = str(s.get("endpoint", "")).strip()
                    if e in agentish_eps:
                        hit.add(e)
                eps_per_token.append(len(hit))
                for e in hit:
                    tokens_per_ep[e] += 1
                    chain_eps[chain].add(e)

    print(f"\nS9mc tokens: {s9_total}")
    print(f"S11mc = distinct live agent-ish endpoints: {len(tokens_per_ep)}")
    print(f"median endpoints per token: {sorted(eps_per_token)[len(eps_per_token)//2] if eps_per_token else 0}")

    shared = sum(v for v in tokens_per_ep.values() if v > 1)
    solo = sum(1 for v in tokens_per_ep.values() if v == 1)
    print(f"\nendpoints used by exactly 1 token: {solo}")
    print(f"tokens sitting on a SHARED endpoint (>1 token): {shared}")
    if s9_total:
        print(f"  -> {100*shared/s9_total:.1f}% of S9mc tokens share an endpoint with another token")

    print("\ntop endpoints by token count:")
    for e, n in tokens_per_ep.most_common(12):
        print(f"  {n:>6}  {e[:88]}")

    print("\nper chain distinct live endpoints:")
    for ch, s in sorted(chain_eps.items(), key=lambda x: -len(x[1])):
        print(f"  {ch:<10} endpoints={len(s)}")

    out = {
        "label": args.label,
        "S9mc_tokens": s9_total,
        "S11mc_distinct_live_endpoints": len(tokens_per_ep),
        "endpoints_single_token": solo,
        "tokens_on_shared_endpoint": shared,
        "shared_pct_of_S9mc": round(100 * shared / s9_total, 2) if s9_total else 0,
        "top_endpoints": tokens_per_ep.most_common(30),
        "per_chain_endpoints": {k: len(v) for k, v in chain_eps.items()},
    }
    dest = mc / "MC_ENDPOINT_UNIT.json"
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nwrote {dest}")


if __name__ == "__main__":
    main()
