#!/usr/bin/env python3
"""Multichain ERC-8004 Reputation Registry census (`getClients(agentId)` across 13 chains).

Verifies contract deployment via `eth_getCode`, batches calls via Multicall3,
and records per-chain HEAD `block_read`. Read-only with respect to census/panels/probes.

    python mc_reputation.py --check-only          # verify deployment only
    python mc_reputation.py --chain-workers 4     # full pass
"""
from __future__ import annotations

import argparse
import csv
import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rpc_pool import CHAIN_RPCS, load_pool, pad_uint

csv.field_size_limit(10**9)
ROOT = Path(__file__).resolve().parents[2]
CENSUS = ROOT / "_chain" / "multichain"
OUT = ROOT / "_enrich" / "reputation"
REPORTS = ROOT / "_enrich" / "reports"

REPUTATION = "0x8004baa17c55a88189ae136b182e5fda19de9b63"
SEL_GET_CLIENTS = "42dd519c"  # getClients(uint256) -> address[]
IDS_PER_MULTICALL = 50
SCRIPT_VERSION = "mc_reputation_v1.0"
# Bulk feedback issuer excluded from independent-feedback counts.
DENYLIST = frozenset({"0xf653068677a9a26d5911da8abd1500d043ec807e"})


def decode_address_array(data: str) -> list[str]:
    if not data or data in ("0x", "0x0"):
        return []
    h = data[2:] if data.startswith("0x") else data
    if len(h) < 128:
        return []
    try:
        off = int(h[0:64], 16) * 2
        n = int(h[off : off + 64], 16)
        out, pos = [], off + 64
        for _ in range(n):
            out.append("0x" + h[pos + 24 : pos + 64])
            pos += 64
        return out
    except Exception:
        return []


def latest_census(chain: str) -> Path | None:
    files = sorted((CENSUS / chain).glob("census_*.csv")) if (CENSUS / chain).is_dir() else []
    return files[-1] if files else None


def agent_ids(chain: str, only_uri: bool) -> list[int]:
    p = latest_census(chain)
    if not p:
        return []
    ids = []
    with p.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if only_uri and r.get("has_token_uri") != "1":
                continue
            try:
                ids.append(int(r["agent_id"]))
            except (KeyError, ValueError):
                continue
    return ids


def check_deployment(chain: str) -> dict:
    """Is the reputation registry actually at REPUTATION on this chain?"""
    try:
        pool, _ = load_pool(chain, probe_id=0)
        code = pool.call("eth_getCode", [REPUTATION, "latest"]) or "0x"
        head = int(pool.call("eth_blockNumber", []), 16)
        deployed = len(code) > 4
        probe = ""
        if deployed:
            # one live call so we know the selector matches this bytecode
            res = pool.call(
                "eth_call",
                [{"to": REPUTATION, "data": "0x" + SEL_GET_CLIENTS + pad_uint(0)}, "latest"],
            )
            probe = "ok" if res is not None else "no result"
        return {
            "chain": chain,
            "deployed": deployed,
            "code_len": (len(code) - 2) // 2,
            "code_hash": __import__("hashlib").sha256(code.encode()).hexdigest()[:16],
            "head": head,
            "selector_probe": probe,
        }
    except Exception as e:
        return {"chain": chain, "deployed": None, "error": f"{type(e).__name__}: {e}"}


def census_chain(chain: str, only_uri: bool, workers: int, label: str) -> dict:
    t0 = time.time()
    ids = agent_ids(chain, only_uri)
    if not ids:
        return {"chain": chain, "status": "no census"}
    pool, _ = load_pool(chain, probe_id=0)
    head = int(pool.call("eth_blockNumber", []), 16)
    chunks = [ids[i : i + IDS_PER_MULTICALL] for i in range(0, len(ids), IDS_PER_MULTICALL)]
    print(f"[{chain}] {len(ids)} agents -> {len(chunks)} multicalls @ head {head}", flush=True)

    result: dict[int, list[str]] = {}

    def do(chunk: list[int]) -> list[tuple[int, list[str]]]:
        calls = [(REPUTATION, SEL_GET_CLIENTS + pad_uint(a)) for a in chunk]
        try:
            res = pool.multicall(calls, "latest")
        except Exception:
            return [(a, []) for a in chunk]
        return [(a, decode_address_array(d) if ok else []) for a, (ok, d) in zip(chunk, res)]

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for part in ex.map(do, chunks):
            for aid, cls in part:
                result[aid] = cls
            done += 1
            if done % 200 == 0:
                hit = sum(1 for v in result.values() if v)
                print(f"[{chain}] {done}/{len(chunks)} with_clients={hit}", flush=True)

    d = OUT / label / chain
    d.mkdir(parents=True, exist_ok=True)
    rows = 0
    issuers: Counter = Counter()
    with (d / f"clients_head{head}.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["agent_id", "client_address", "in_denylist"])
        for aid in sorted(result):
            for c in result[aid]:
                c = c.lower()
                issuers[c] += 1
                w.writerow([aid, c, 1 if c in DENYLIST else 0])
                rows += 1

    with (d / f"summary_head{head}.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["agent_id", "clients_raw", "clients_independent", "has_independent_feedback"])
        for aid in sorted(result):
            cls = [c.lower() for c in result[aid]]
            indep = [c for c in cls if c not in DENYLIST]
            w.writerow([aid, len(cls), len(indep), 1 if indep else 0])

    any_c = sum(1 for v in result.values() if v)
    indep_c = sum(1 for v in result.values() if any(c.lower() not in DENYLIST for c in v))
    meta = {
        "chain": chain,
        "status": "done",
        "script_version": SCRIPT_VERSION,
        "label": label,
        "block_read": head,
        "block_note": "HEAD — historical eth_call unavailable on free public RPCs; not aligned with census block",
        "n_agents_queried": len(ids),
        "only_uri": only_uri,
        "n_with_any_client": any_c,
        "n_with_independent_client": indep_c,
        "n_client_rows": rows,
        "top_issuers": issuers.most_common(10),
        "elapsed_sec": round(time.time() - t0, 1),
    }
    (d / f"meta_head{head}.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"[{chain}] DONE any={any_c} indep={indep_c} rows={rows} in {meta['elapsed_sec']}s", flush=True)
    return meta


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chains", default="", help="comma-separated subset; default = all in CHAIN_RPCS")
    ap.add_argument(
        "--label",
        default="mc_t1",
        help="which census point this pass is meant to accompany; keeps the two HEAD reads apart",
    )
    ap.add_argument("--check-only", action="store_true", help="only verify the registry is deployed")
    ap.add_argument("--only-uri", action="store_true", help="query just agents that have a tokenURI")
    ap.add_argument("--chain-workers", type=int, default=4)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    chains = [c.strip() for c in args.chains.split(",") if c.strip()] or sorted(CHAIN_RPCS)
    OUT.mkdir(parents=True, exist_ok=True)

    print(f"checking deployment of {REPUTATION} on {len(chains)} chains…", flush=True)
    checks = []
    with ThreadPoolExecutor(max_workers=min(6, len(chains))) as ex:
        for r in ex.map(check_deployment, chains):
            checks.append(r)
            flag = "yes" if r.get("deployed") else ("ERR" if r.get("deployed") is None else "NO")
            print(f"  {r['chain']:<10} deployed={flag:<3} code={r.get('code_len', '-')}B "
                  f"probe={r.get('selector_probe', '-')} {r.get('error', '')}", flush=True)
    (OUT / "deployment_check.json").write_text(json.dumps(checks, indent=2), encoding="utf-8")

    hashes = Counter(r.get("code_hash") for r in checks if r.get("deployed"))
    if len(hashes) > 1:
        print(f"\nWARNING: bytecode differs across chains ({len(hashes)} variants) — "
              "getClients may not mean the same thing everywhere; inspect before trusting G")
    live = [r["chain"] for r in checks if r.get("deployed")]
    print(f"\nregistry present on {len(live)}/{len(chains)}: {', '.join(live) or 'none'}")
    if args.check_only:
        print(f"wrote {OUT / 'deployment_check.json'}")
        return
    if not live:
        raise SystemExit("registry found nowhere — nothing to census")

    metas = []
    with ThreadPoolExecutor(max_workers=args.chain_workers) as ex:
        futs = {ex.submit(census_chain, c, args.only_uri, args.workers, args.label): c for c in live}
        for fut in as_completed(futs):
            try:
                metas.append(fut.result())
            except Exception as e:
                metas.append({"chain": futs[fut], "status": f"fatal: {type(e).__name__}: {e}"})

    total_any = sum(m.get("n_with_any_client", 0) for m in metas)
    total_indep = sum(m.get("n_with_independent_client", 0) for m in metas)
    run = {
        "script_version": SCRIPT_VERSION,
        "label": args.label,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "chains": metas,
        "deployment_check": checks,
        "total_with_any_client": total_any,
        "total_with_independent_client": total_indep,
    }
    (OUT / f"run_reputation_{args.label}.json").write_text(json.dumps(run, indent=2), encoding="utf-8")

    lines = [
        f"# Reputation census across chains — {args.label}\n",
        f"Script `{SCRIPT_VERSION}`, {run['generated_utc']}. Registry `{REPUTATION}`.\n",
        "Read at **HEAD**, not at the census block: free public nodes do not serve historical "
        "`eth_call`. The reputation layer is therefore not time-aligned with the census, and this "
        "is stated wherever `G` is reported.\n",
        "| chain | block | agents | with clients | with independent | rows |",
        "|---|---|---|---|---|---|",
    ]
    for m in sorted(metas, key=lambda x: -(x.get("n_with_independent_client") or 0)):
        lines.append(
            f"| {m['chain']} | {m.get('block_read', '—')} | {m.get('n_agents_queried', '—')} | "
            f"{m.get('n_with_any_client', '—')} | {m.get('n_with_independent_client', '—')} | "
            f"{m.get('n_client_rows', '—')} |"
        )
    lines.append(f"\nTotal agents with independent feedback: **{total_indep}**.\n")
    lines.append(
        "What this fixes: before this pass the third evidence channel for `G` existed only on "
        "Ethereum, so the coding rule differed between subsamples. Now it is uniform across chains.\n"
    )
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"MC_REPUTATION_{args.label}.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\nwrote {OUT / f'run_reputation_{args.label}.json'} and {REPORTS / f'MC_REPUTATION_{args.label}.md'}")


if __name__ == "__main__":
    main()
