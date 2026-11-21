#!/usr/bin/env python3
"""Reconstruct `mc_t1/probes.json` from frozen panels via D/E OR-aggregate constraints.

Recovers exact values where determined (D=0 -> all dead; E=0 -> none agentish;
D=1 with unique candidate -> live) and marks unconstrained entries `recovered: "inferred"`.
"""
from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path

import argparse

from r_spec import filter_probeable, is_probeable_endpoint

csv.field_size_limit(10**9)
ROOT = Path(__file__).resolve().parents[2]
MC = ROOT / "_enrich" / "multichain"
CACHE = MC / "uri_cache"
CENSUS = ROOT / "_chain" / "multichain"
AGENT_PROTOCOL = {"mcp", "a2a", "oasf", "web"}


def services_of(card: dict) -> list:
    s = card.get("services")
    return s if isinstance(s, list) else []


def probe_endpoints(svcs: list) -> list[str]:
    out, seen = [], set()
    for s in svcs:
        if not isinstance(s, dict):
            continue
        if str(s.get("name", "")).strip().lower() not in AGENT_PROTOCOL:
            continue
        ep = str(s.get("endpoint", "")).strip()
        if ep.startswith("http://") or ep.startswith("https://"):
            if ep not in seen:
                seen.add(ep)
                out.append(ep)
    return out


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


print("indexing census…")
uri_by_key: dict[tuple[str, str], str] = {}
for d in sorted(CENSUS.iterdir()):
    if not d.is_dir():
        continue
    cens = sorted(d.glob("census_*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not cens:
        continue
    with cens[0].open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            u = (r.get("token_uri") or "").strip()
            if u:
                uri_by_key[(d.name, r["agent_id"])] = u

ap = argparse.ArgumentParser()
ap.add_argument("--out", default=str(MC / "probes.json"))
args = ap.parse_args()

print("reading panels…")
# One constraint per (endpoint subset, D, E). Each agent yields two: the raw
# endpoint list behind D/E, and the R_spec subset behind D_prime/E_prime.
# Using only the raw pair is not enough — it lets the solver mark a
# non-probeable endpoint live where the original had a probeable one, which
# flips D_prime and corrupts S8mc/S9mc for that agent.
constraints: list[tuple[list[str], int | None, int | None]] = []
eps_all: set[str] = set()
for p in sorted(MC.glob("panel_*.csv")):
    if p.name == "panel_all.csv":
        continue
    chain = p.stem[len("panel_") :]
    with p.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("B") != "1":
                continue
            u = uri_by_key.get((chain, r["agent_id"]))
            card = card_of(u) if u else None
            if not card:
                continue
            eps = probe_endpoints(services_of(card))
            if not eps:
                continue
            D = None if r["D"] == "" else int(r["D"])
            E = None if r["E"] == "" else int(r["E"])
            constraints.append((eps, D, E))
            eps_all.update(eps)
            eps_p = filter_probeable(eps)
            if eps_p:
                Dp = None if r["D_prime"] == "" else int(r["D_prime"])
                Ep = None if r["E_prime"] == "" else int(r["E_prime"])
                constraints.append((eps_p, Dp, Ep))

print(f"agents with endpoints: {len(constraints)}  distinct endpoints: {len(eps_all)}")

live: dict[str, int | None] = {e: None for e in eps_all}
agent: dict[str, int | None] = {e: None for e in eps_all}

# Pass A — negative constraints are absolute
for eps, D, E in constraints:
    if D == 0:
        for e in eps:
            live[e] = 0
            agent[e] = 0
    if E == 0:
        for e in eps:
            agent[e] = 0

# Pass B — unique-candidate propagation to fixpoint
by_ep: dict[str, list[int]] = defaultdict(list)
for i, (eps, _D, _E) in enumerate(constraints):
    for e in eps:
        by_ep[e].append(i)

changed = True
rounds = 0
while changed:
    changed = False
    rounds += 1
    for eps, D, E in constraints:
        if D == 1:
            cands = [e for e in eps if live[e] != 0]
            if len(cands) == 1 and live[cands[0]] is None:
                live[cands[0]] = 1
                changed = True
        if E == 1:
            cands = [e for e in eps if agent[e] != 0]
            if len(cands) == 1 and agent[cands[0]] is None:
                agent[cands[0]] = 1
                live[cands[0]] = 1
                changed = True
print(f"propagation rounds: {rounds}")

exact_live = sum(1 for v in live.values() if v is not None)
exact_agent = sum(1 for v in agent.values() if v is not None)
print(f"determined exactly: live {exact_live}/{len(eps_all)}  agentish {exact_agent}/{len(eps_all)}")

# Pass C — satisfy the remaining OR-constraints with the fewest positives.
# Tightest sets first: satisfying a probeable subset also satisfies the raw
# superset, so the reverse order would light up two endpoints instead of one.
inferred: set[str] = set()
for eps, D, E in sorted(constraints, key=lambda c: len(c[0])):
    if E == 1 and not any(agent[e] == 1 for e in eps):
        pick = next((e for e in eps if agent[e] is None), None)
        if pick:
            agent[pick] = 1
            live[pick] = 1
            inferred.add(pick)
    if D == 1 and not any(live[e] == 1 for e in eps):
        pick = next((e for e in eps if live[e] is None), None)
        if pick:
            live[pick] = 1
            inferred.add(pick)

for e in eps_all:
    if live[e] is None:
        live[e] = 0
    if agent[e] is None:
        agent[e] = 0
    if live[e] == 0:
        agent[e] = 0

violations = 0
for eps, D, E in constraints:
    if D is not None and (1 if any(live[e] for e in eps) else 0) != D:
        violations += 1
    if E is not None and (1 if any(agent[e] for e in eps) else 0) != E:
        violations += 1
print(f"constraint violations after solve: {violations}")

out: dict[str, dict] = {}
for e in sorted(eps_all):
    out[e] = {
        "endpoint": e,
        "live": int(live[e]),
        "agentish": int(agent[e]),
        "http_status": None,
        "error": "",
        "probeable_rspec": int(is_probeable_endpoint(e)),
        "rounds": 1,
        "recovered": "inferred" if e in inferred else "exact",
        "recovered_note": "rebuilt 2026-08-10 from frozen panels; original probe file lost",
    }

path = Path(args.out)
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
print(f"wrote {path} entries={len(out)} inferred={len(inferred)}")
print(f"live={sum(v['live'] for v in out.values())} agentish={sum(v['agentish'] for v in out.values())}")
