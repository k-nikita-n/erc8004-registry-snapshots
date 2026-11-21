#!/usr/bin/env python3
"""Re-probe under-determined mc_t1 endpoints to corroborate endpoint-level S11mc.

Re-probes probeable endpoints of S9mc tokens left at 0 in the panel-based
reconstruction (plus controls) via `mc_fetch_score`. Writes only
`probes_s11_repair.json` and `MC_ENDPOINT_UNIT_repaired.json` without modifying
frozen snapshot files.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from r_spec import is_probeable_endpoint

csv.field_size_limit(10**9)
ROOT = Path(__file__).resolve().parents[2]
MC = ROOT / "_enrich" / "multichain"
CACHE = MC / "uri_cache"
CENSUS = ROOT / "_chain" / "multichain"
TARGETS = MC / "s11_repair_targets.json"
REPAIR = MC / "probes_s11_repair.json"
OUTFILE = MC / "MC_ENDPOINT_UNIT_repaired.json"


def load_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


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


def build_index() -> dict[tuple[str, str], str]:
    idx: dict[tuple[str, str], str] = {}
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
                    idx[(d.name, r["agent_id"])] = u
    return idx


def collect(probes: dict) -> tuple[dict[str, int], dict[str, set[str]], int]:
    """Return (endpoint -> S9mc token count) for every probeable endpoint of an
    S9mc token, plus chain membership and the S9mc total."""
    print("indexing census…", flush=True)
    idx = build_index()
    tokens_per_ep: Counter = Counter()
    chain_of_ep: dict[str, set[str]] = defaultdict(set)
    s9_total = 0
    for p in sorted(MC.glob("panel_*.csv")):
        if p.name == "panel_all.csv":
            continue
        chain = p.stem[len("panel_") :]
        with p.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("S9mc") != "1":
                    continue
                s9_total += 1
                u = idx.get((chain, r["agent_id"]))
                c = card_of(u) if u else None
                if not c:
                    continue
                for s in c.get("services", []) or []:
                    if not isinstance(s, dict):
                        continue
                    e = str(s.get("endpoint", "")).strip()
                    if e and is_probeable_endpoint(e):
                        tokens_per_ep[e] += 1
                        chain_of_ep[e].add(chain)
    return dict(tokens_per_ep), chain_of_ep, s9_total


def is_throttled(rec: dict) -> bool:
    err = rec.get("error") or ""
    return rec.get("agentish") != 1 and (
        str(rec.get("http_status")) == "429"
        or "timed out" in err
        or "SSL" in err
        or "urlopen" in err
    )


def retry_throttled(args) -> None:
    """Second pass over endpoints the wide probe lost to its own concurrency."""
    import urllib.parse as up

    from concurrent.futures import ThreadPoolExecutor as TPE

    import mc_fetch_score as mfs

    rec = json.loads(REPAIR.read_text(encoding="utf-8"))
    todo = [e for e, v in rec.items() if is_throttled(v)]
    if not todo:
        print("nothing to retry")
        return
    by_host: dict[str, list[str]] = defaultdict(list)
    for e in todo:
        by_host[up.urlparse(e).netloc].append(e)
    print(f"retrying {len(todo)} endpoints across {len(by_host)} hosts, {args.retry_gap}s apart per host")

    stamp = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime())
    done = [0]
    t0 = time.time()

    def do_host(host: str) -> list[tuple[str, dict]]:
        out = []
        for i, ep in enumerate(by_host[host]):
            if i:
                time.sleep(args.retry_gap)
            r = mfs.probe_one(ep, rounds=args.rounds, save_body=False)
            r["recovered"] = f"s11_repair_retry_{stamp}"
            out.append((ep, r))
            done[0] += 1
            if done[0] % 100 == 0:
                print(f"  {done[0]}/{len(todo)}  {time.time() - t0:.0f}s", flush=True)
        return out

    with TPE(max_workers=min(8, len(by_host))) as pool:
        for part in pool.map(do_host, list(by_host)):
            for ep, r in part:
                rec[ep] = r
            REPAIR.write_text(json.dumps(rec, indent=1), encoding="utf-8")

    REPAIR.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    gained = sum(1 for e in todo if rec[e].get("agentish") == 1)
    still_bad = sum(1 for e in todo if is_throttled(rec[e]))
    total_ag = sum(1 for v in rec.values() if v.get("agentish") == 1)
    print(f"\nretry done: {gained}/{len(todo)} now agent-ish, {still_bad} still throttled")
    print(f"fully measured agent-ish endpoints among candidates: {total_ag}  (published S11mc 2371)")

    unit = load_json(OUTFILE) or {}
    unit["retry_pass_utc"] = stamp
    unit["retry_probed"] = len(todo)
    unit["retry_gained"] = gained
    unit["retry_still_throttled"] = still_bad
    unit["S11mc_fully_measured_after_retry"] = total_ag
    unit["S11mc_measured_vs_published_pct"] = round(100 * total_ag / 2371, 1)
    OUTFILE.write_text(json.dumps(unit, indent=2), encoding="utf-8")
    print(f"updated {OUTFILE}")


def finalize() -> None:
    """Recompute MC_ENDPOINT_UNIT_repaired.json from saved probe and repair files."""
    probes = json.loads((MC / "probes.json").read_text(encoding="utf-8"))
    rep = json.loads(REPAIR.read_text(encoding="utf-8"))
    targets = set(json.loads(TARGETS.read_text(encoding="utf-8"))["targets"])

    control = [e for e in rep if e not in targets]
    exact = [e for e in control if probes.get(e, {}).get("recovered") == "exact"]
    inferred = [e for e in control if probes.get(e, {}).get("recovered") == "inferred"]

    def ag(seq) -> int:
        return sum(1 for e in seq if rep[e].get("agentish") == 1)

    measured = ag(rep)
    unresolved = [e for e in rep if is_throttled(rep[e])]
    out = {
        "note": (
            "Endpoint layer of mc_t1 re-measured after the probes.json loss. Panels, MC_SUMMARY.json and "
            "MC_ENDPOINT_UNIT.json untouched. This file corroborates the published S11mc; it does not replace it."
        ),
        "published_S11mc": 2371,
        "recomputable_from_recovered_probes_only": 1298,
        "candidates_probed": len(rep),
        "measured_agentish_now": measured,
        "measured_vs_published_pct": round(100 * measured / 2371, 1),
        "still_unresolved_after_retries": len(unresolved),
        "control_survived_recovery": {
            "n": len(control),
            "agentish_now": ag(control),
            "exact_provenance_n": len(exact),
            "exact_provenance_agentish_now": ag(exact),
            "exact_drift_pct": round(100 * (1 - ag(exact) / len(exact)), 1) if exact else None,
            "inferred_provenance_n": len(inferred),
            "inferred_provenance_agentish_now": ag(inferred),
            "note": (
                "Only the 'exact' subset is a clean drift estimate: those endpoints were pinned by the "
                "panel constraints. The 'inferred' subset was a free choice among siblings, so its drop "
                "rate mixes real drift with the solver having flagged the wrong sibling."
            ),
        },
        "targets_under_determined": {"n": len(targets), "agentish_now": ag(targets & set(rep))},
    }
    OUTFILE.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\nwrote {OUTFILE}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="actually re-probe the targets (network)")
    ap.add_argument(
        "--control",
        action="store_true",
        help="also re-probe the endpoints that survived recovery; they are known to have been "
             "agent-ish at mc_t1, so their flip rate now measures how much drift the later "
             "timestamp injects into the repaired figure",
    )
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--limit", type=int, default=0, help="probe only the first N targets (smoke test)")
    ap.add_argument(
        "--retry-throttled",
        action="store_true",
        help="re-probe only endpoints the previous pass lost to 429/timeout/TLS, one at a time per "
             "host: hundreds of endpoints can share a single domain, and a wide pass rate-limits "
             "itself into false negatives",
    )
    ap.add_argument("--retry-gap", type=float, default=1.2, help="seconds between probes of the same host")
    ap.add_argument("--finalize", action="store_true", help="recompute the unit file from probes on disk")
    args = ap.parse_args()

    if args.finalize:
        finalize()
        return
    if args.retry_throttled:
        retry_throttled(args)
        finalize()
        return

    probes = json.loads((MC / "probes.json").read_text(encoding="utf-8"))
    tokens_per_ep, chain_of_ep, s9_total = collect(probes)

    known = {e for e in tokens_per_ep if probes.get(e, {}).get("agentish") == 1}
    targets = sorted((e for e in tokens_per_ep if e not in known), key=lambda e: -tokens_per_ep[e])

    print(f"\nS9mc tokens                              : {s9_total}")
    print(f"probeable endpoints of S9mc tokens       : {len(tokens_per_ep)}")
    print(f"  still flagged agent-ish (recomputable) : {len(known)}")
    print(f"  under-determined -> repair targets     : {len(targets)}")
    print(f"published S11mc                          : 2371")
    print(f"upper bound if every target comes back 1 : {len(tokens_per_ep)}")
    TARGETS.write_text(
        json.dumps({"generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "n_targets": len(targets), "targets": targets}, indent=2),
        encoding="utf-8",
    )
    print(f"\nwrote {TARGETS}")

    if not args.probe:
        print("\ndry run — nothing probed. add --probe to measure.")
        return

    todo = list(targets)
    if args.control:
        todo += sorted(known, key=lambda e: -tokens_per_ep[e])
    if args.limit:
        todo = todo[: args.limit]
    stamp = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime())
    print(f"\nprobing {len(todo)} endpoints, rounds={args.rounds}, workers={args.workers}…", flush=True)

    import mc_fetch_score as mfs

    done = 0
    out: dict[str, dict] = {}

    def one(ep: str) -> tuple[str, dict]:
        return ep, mfs.probe_one(ep, rounds=args.rounds, save_body=False)

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for ep, res in pool.map(one, todo):
            res["recovered"] = f"s11_repair_{stamp}"
            res["s9mc_tokens"] = tokens_per_ep.get(ep, 0)
            out[ep] = res
            done += 1
            if done % 200 == 0:
                print(f"  {done}/{len(todo)}  {time.time() - t0:.0f}s", flush=True)
                REPAIR.write_text(json.dumps(out, indent=1), encoding="utf-8")

    REPAIR.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"wrote {REPAIR}")

    probed_targets = {e: r for e, r in out.items() if e not in known}
    probed_control = {e: r for e, r in out.items() if e in known}
    regained = {e for e, r in probed_targets.items() if r.get("agentish") == 1}
    # every control endpoint was agent-ish at mc_t1; whatever fraction is no
    # longer agent-ish now is drift, not recovery error
    ctrl_still = {e for e, r in probed_control.items() if r.get("agentish") == 1}
    drift = 1 - (len(ctrl_still) / len(probed_control)) if probed_control else None

    s11_repaired = len(known | regained)
    print("\nrepair result")
    print(f"  targets probed          : {len(probed_targets)}")
    print(f"  came back agent-ish     : {len(regained)}")
    print(f"  S11mc recomputable      : {len(known)} -> {s11_repaired}  (published 2371)")
    if probed_control:
        print(f"\ncontrol (known agent-ish at mc_t1, re-probed now)")
        print(f"  probed                  : {len(probed_control)}")
        print(f"  still agent-ish         : {len(ctrl_still)}")
        print(f"  drift over the gap      : {100 * drift:.1f}%")
        if drift > 0:
            est = len(regained) / (1 - drift)
            print(f"  drift-adjusted regain   : {est:.0f} -> S11mc ~ {len(known) + est:.0f}")

    tokens_covered = sum(v for e, v in tokens_per_ep.items() if e in (known | regained))
    OUTFILE.write_text(
        json.dumps(
            {
                "note": "S11mc rebuilt after the probes.json loss; see the Data "
                        "Descriptor's Methods. Endpoint layer is a two-timestamp composite: "
                        "part survives the panel-based recovery, part re-probed later. "
                        "Panels untouched. Interim checkpoint; --finalize rewrites this file.",
                "probed_utc": stamp,
                "S11mc_published_prelost": 2371,
                "S11mc_from_recovered_probes_only": len(known),
                "S11mc_repaired": s11_repaired,
                "targets_probed": len(probed_targets),
                "targets_agentish": len(regained),
                "control_probed": len(probed_control),
                "control_still_agentish": len(ctrl_still),
                "control_drift_pct": round(100 * drift, 2) if drift is not None else None,
                "s9mc_tokens": s9_total,
                "endpoint_token_pairs_covered": tokens_covered,
                "top_repaired": sorted(
                    ((e, tokens_per_ep[e]) for e in regained), key=lambda x: -x[1]
                )[:30],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"wrote {OUTFILE}  (MC_ENDPOINT_UNIT.json untouched)")


if __name__ == "__main__":
    main()
