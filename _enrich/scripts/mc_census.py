#!/usr/bin/env python3
"""Multichain ERC-8004 census on free RPC pool. Resumable, block-pinned per chain.

Layout:
  _chain/multichain/<chain>/<block>/shard_XXXXXX.csv   (resumable shards)
  _chain/multichain/<chain>/census_<block>.csv         (merged)
  _chain/multichain/<chain>/meta_<block>.json
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rpc_pool import (
    CHAIN_RPCS,
    IDENTITY,
    RpcPool,
    decode_string,
    load_pool,
    pad_uint,
)

ROOT = Path(__file__).resolve().parents[2]
OUT_ROOT = ROOT / "_chain" / "multichain"
SEL_OWNER = "6352211e"
SEL_TOKEN_URI = "c87b56dd"
SEL_GET_WALLET = "00339509"
SCRIPT_VERSION = "mc_census_v1.3"  # chains in parallel; per-shard block_read

SHARD = 5000
IDS_PER_MULTICALL = 50
CONFIRMATIONS = 12
# Free public nodes often prune historical state within seconds → eth_call at
# head-N returns "missing trie node". These chains MUST use block tag "latest".
LATEST_ONLY_CHAINS = frozenset({"bsc"})
FIELDS = [
    "agent_id",
    "owner",
    "token_uri",
    "agent_wallet",
    "uri_scheme",
    "has_token_uri",
    "block_read",
]
# Longest chains first: parallel pool should start them immediately so total
# wall time ≈ slowest chain and the snapshot spread stays small.
CHAIN_COST_ORDER = ("bsc", "base", "ethereum", "celo")


def uri_scheme(u: str) -> str:
    if not u:
        return ""
    low = u.lower()
    for s in ("https://", "http://", "ipfs://", "data:", "ar://"):
        if low.startswith(s):
            return s.rstrip(":/")
    return "other"


def decode_address(data: str) -> str:
    if not data or len(data) < 42:
        return ""
    return "0x" + data[-40:]


def find_max_id(pool: RpcPool, block: str, hint: int = 1024, chain: str = "") -> int:
    def exists(aid: int) -> bool:
        try:
            res = pool.call("eth_call", [{"to": IDENTITY, "data": "0x" + SEL_OWNER + pad_uint(aid)}, block])
            return int(res or "0x0", 16) != 0
        except Exception as e:
            print(f"[{chain}] find_max_id exists({aid}) err: {e}", flush=True)
            return False

    print(f"[{chain}] find_max_id start hint={hint}", flush=True)
    if not exists(0):
        return -1
    lo, hi = 0, max(1, hint)
    while exists(hi):
        lo = hi
        hi *= 2
        print(f"[{chain}] find_max_id expand lo={lo} hi={hi}", flush=True)
        if hi > 8_000_000:
            break
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if exists(mid):
            lo = mid
        else:
            hi = mid
        if (lo & 0x3FF) == 0:  # occasional progress
            print(f"[{chain}] find_max_id bisect lo={lo} hi={hi}", flush=True)
    print(f"[{chain}] find_max_id done max_id={lo}", flush=True)
    return lo


def fetch_range(
    pool: RpcPool, ids: list[int], block: str, ids_per_mc: int | None = None, log_prefix: str = ""
) -> dict[int, dict]:
    """ownerOf + tokenURI for ids, adaptive chunking."""
    chunk_n = ids_per_mc or IDS_PER_MULTICALL
    out: dict[int, dict] = {}
    stack = [ids[i : i + chunk_n] for i in range(0, len(ids), chunk_n)]
    total_chunks = len(stack)
    done_chunks = 0
    while stack:
        chunk = stack.pop()
        calls = []
        for aid in chunk:
            calls.append((IDENTITY, SEL_OWNER + pad_uint(aid)))
            calls.append((IDENTITY, SEL_TOKEN_URI + pad_uint(aid)))
        try:
            dec = pool.multicall(calls, block)
        except Exception as e:
            if log_prefix:
                print(f"{log_prefix} multicall fail n={len(chunk)}: {e}", flush=True)
            if len(chunk) > 1:
                mid = len(chunk) // 2
                stack.append(chunk[:mid])
                stack.append(chunk[mid:])
                continue
            aid = chunk[0]
            out[aid] = {
                "agent_id": aid,
                "owner": "",
                "token_uri": "",
                "agent_wallet": "",
                "uri_scheme": "",
                "has_token_uri": 0,
            }
            done_chunks += 1
            continue
        for i, aid in enumerate(chunk):
            ok_o, d_o = dec[2 * i]
            ok_u, d_u = dec[2 * i + 1]
            owner = decode_address(d_o) if ok_o else ""
            uri = decode_string(d_u) if ok_u else ""
            out[aid] = {
                "agent_id": aid,
                "owner": owner,
                "token_uri": uri,
                "agent_wallet": "",
                "uri_scheme": uri_scheme(uri),
                "has_token_uri": 1 if uri else 0,
            }
        done_chunks += 1
        if log_prefix and (done_chunks % 20 == 0 or not stack):
            print(f"{log_prefix} chunks ~{done_chunks}/{total_chunks} ids={len(out)}", flush=True)
    return out


def fetch_wallets(pool: RpcPool, ids: list[int], block: str) -> dict[int, str]:
    out: dict[int, str] = {}
    stack = [ids[i : i + IDS_PER_MULTICALL * 2] for i in range(0, len(ids), IDS_PER_MULTICALL * 2)]
    while stack:
        chunk = stack.pop()
        calls = [(IDENTITY, SEL_GET_WALLET + pad_uint(aid)) for aid in chunk]
        try:
            dec = pool.multicall(calls, block)
        except Exception:
            if len(chunk) > 1:
                mid = len(chunk) // 2
                stack.append(chunk[:mid])
                stack.append(chunk[mid:])
            continue
        for aid, (ok, data) in zip(chunk, dec):
            if ok:
                addr = decode_address(data)
                if addr and addr != "0x" + "0" * 40:
                    out[aid] = addr
    return out


def census_chain(
    chain: str,
    workers: int,
    label: str,
    sequential: bool = False,
    ids_per_mc: int | None = None,
    shard_size_override: int | None = None,
) -> dict:
    t0 = time.time()
    pool, probes = load_pool(chain)
    usable = [e for e in probes if e.healthy()]
    print(f"[{chain}] usable endpoints: {len(usable)}/{len(probes)}", flush=True)
    if not usable:
        return {"chain": chain, "status": "no_usable_rpc"}

    head = int(pool.call("eth_blockNumber", []), 16)
    use_latest = chain in LATEST_ONLY_CHAINS
    block_num = head if use_latest else head - CONFIRMATIONS
    # eth_call block tag: pinned hex, or "latest" when free RPCs prune state
    block = "latest" if use_latest else hex(block_num)
    # create run dir early so hang diagnostics leave a footprint
    run_dir = OUT_ROOT / chain / str(block_num)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "STARTED.json").write_text(
        json.dumps(
            {
                "chain": chain,
                "block": block_num,
                "head": head,
                "eth_call_block_tag": block,
                "ts": time.time(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    if use_latest:
        print(f"[{chain}] using eth_call block=latest (free RPC prune; pin unavailable)", flush=True)
    hint = {"bsc": 262144, "base": 65536, "ethereum": 49152, "celo": 8192}.get(chain, 1024)
    max_id = find_max_id(pool, block, hint=hint, chain=chain)
    if max_id < 0:
        return {"chain": chain, "status": "empty_registry", "block": block_num}
    n = max_id + 1
    print(f"[{chain}] block={block_num} tag={block} max_id={max_id} population={n}", flush=True)

    shard_size = shard_size_override or (2000 if sequential else SHARD)
    mc_n = ids_per_mc or (25 if sequential else IDS_PER_MULTICALL)
    shards = [(s, min(s + shard_size, n)) for s in range(0, n, shard_size)]

    def do_shard(bounds: tuple[int, int]) -> tuple[int, int]:
        s, e = bounds
        path = run_dir / f"shard_{s:08d}.csv"
        if path.exists():
            try:
                with path.open(encoding="utf-8") as f:
                    if sum(1 for _ in f) - 1 == e - s:
                        return s, -1
            except Exception:
                pass
        print(f"[{chain}] shard start {s}-{e}", flush=True)
        t_shard = time.time()
        # Record per-shard block height on latest-tag chains.
        if use_latest:
            try:
                shard_block = int(pool.call("eth_blockNumber", []), 16)
            except Exception:
                shard_block = -1
        else:
            shard_block = block_num
        rows = fetch_range(
            pool,
            list(range(s, e)),
            block,
            ids_per_mc=mc_n,
            log_prefix=f"[{chain}] {s}-{e}",
        )
        ordered = [rows.get(i, {"agent_id": i, "owner": "", "token_uri": "", "agent_wallet": "",
                                "uri_scheme": "", "has_token_uri": 0}) for i in range(s, e)]
        for r in ordered:
            r["block_read"] = shard_block
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(ordered)
        tmp.replace(path)
        n_uri = sum(r["has_token_uri"] for r in ordered)
        print(f"[{chain}] shard done {s}-{e} uri={n_uri} {time.time()-t_shard:.1f}s", flush=True)
        return s, n_uri

    done = 0
    mode = "sequential" if sequential else f"workers={workers}"
    print(f"[{chain}] launching {len(shards)} shards ({mode}, ids_per_mc={mc_n})", flush=True)
    if sequential:
        for b in shards:
            try:
                do_shard(b)
            except Exception as e:
                print(f"[{chain}] shard {b} FAILED: {e}", flush=True)
            done += 1
            if done % 5 == 0 or done == len(shards):
                print(f"[{chain}] shards {done}/{len(shards)} ({time.time()-t0:.0f}s)", flush=True)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(do_shard, b): b for b in shards}
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as e:
                    print(f"[{chain}] shard {futs[fut]} FAILED: {e}", flush=True)
                done += 1
                if done % 2 == 0 or done == len(shards):
                    print(f"[{chain}] shards {done}/{len(shards)} ({time.time()-t0:.0f}s)", flush=True)

    # merge
    all_rows: list[dict] = []
    for s, e in shards:
        path = run_dir / f"shard_{s:08d}.csv"
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as f:
            all_rows.extend(csv.DictReader(f))
    all_rows.sort(key=lambda r: int(r["agent_id"]))

    uri_ids = [int(r["agent_id"]) for r in all_rows if int(r["has_token_uri"])]
    print(f"[{chain}] pass2 wallets for {len(uri_ids)} uri agents", flush=True)
    wallets: dict[int, str] = {}
    if uri_ids:
        wchunks = [uri_ids[i : i + 2000] for i in range(0, len(uri_ids), 2000)]
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for part in ex.map(lambda c: fetch_wallets(pool, c, block), wchunks):
                wallets.update(part)
    idx = {int(r["agent_id"]): r for r in all_rows}
    for aid, w in wallets.items():
        if aid in idx:
            idx[aid]["agent_wallet"] = w

    out_csv = OUT_ROOT / chain / f"census_{block_num}.csv"
    with out_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(all_rows)

    blocks_read = [int(r["block_read"]) for r in all_rows if str(r.get("block_read", "")).lstrip("-").isdigit()]
    blocks_ok = [b for b in blocks_read if b > 0]
    n_uri = sum(int(r["has_token_uri"]) for r in all_rows)
    n_owner = sum(1 for r in all_rows if r["owner"])
    n_wallet_custom = sum(
        1 for r in all_rows if r["agent_wallet"] and r["agent_wallet"].lower() != (r["owner"] or "").lower()
    )
    head_end = int(pool.call("eth_blockNumber", []), 16)
    meta = {
        "script_version": SCRIPT_VERSION,
        "label": label,
        "chain": chain,
        "identity_registry": IDENTITY,
        "observation_block": block_num,
        "eth_call_block_tag": block,
        "head_at_start": head,
        "head_at_end": head_end,
        "confirmations_behind_head": 0 if use_latest else CONFIRMATIONS,
        "reproducibility_note": (
            "latest-tag scan; free RPCs lack historical state (missing trie node on pinned blocks)"
            if use_latest
            else "pinned block eth_call"
        ),
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0)),
        "block_read_min": min(blocks_ok) if blocks_ok else None,
        "block_read_max": max(blocks_ok) if blocks_ok else None,
        "block_read_spread": (max(blocks_ok) - min(blocks_ok)) if blocks_ok else 0,
        "block_read_missing": len(blocks_read) - len(blocks_ok),
        "population": n,
        "max_agent_id": max_id,
        "rows_written": len(all_rows),
        "n_has_token_uri": n_uri,
        "n_owner_resolved": n_owner,
        "n_wallet_set": len(wallets),
        "n_wallet_custom_neq_owner": n_wallet_custom,
        "rpc_usable": [e.url for e in usable],
        "elapsed_sec": round(time.time() - t0, 1),
    }
    (OUT_ROOT / chain / f"meta_{block_num}.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"[{chain}] DONE pop={n} uri={n_uri} custom_wallet={n_wallet_custom} in {meta['elapsed_sec']}s", flush=True)
    return meta


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--chains", default="all", help="comma list or 'all'")
    p.add_argument("--workers", type=int, default=12, help="shard workers inside one chain")
    p.add_argument("--chain-workers", type=int, default=6, help="chains scanned concurrently (1 = old behaviour)")
    p.add_argument("--label", default="mc_t1")
    p.add_argument("--sequential", action="store_true", help="one shard at a time (BSC-safe)")
    p.add_argument("--ids-per-mc", type=int, default=0, help="ids per multicall (0=default)")
    p.add_argument(
        "--bsc-shard-workers",
        type=int,
        default=1,
        help="1 = sequential (mc_t1 behaviour). >1 shortens BSC and its block_read spread, but free RPCs may throttle",
    )
    args = p.parse_args()

    chains = list(CHAIN_RPCS) if args.chains == "all" else [c.strip() for c in args.chains.split(",")]
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    ids_per = args.ids_per_mc or None
    run_path = OUT_ROOT / f"run_{args.label}.json"
    t_run = time.time()

    # Expensive chains first so the pool never leaves BSC waiting for a slot.
    order = {c: i for i, c in enumerate(CHAIN_COST_ORDER)}
    queue = sorted(chains, key=lambda c: (order.get(c, len(order)), c))

    def run_one(chain: str) -> dict:
        heavy = chain == "bsc" and args.bsc_shard_workers > 1
        try:
            return census_chain(
                chain,
                args.bsc_shard_workers if heavy else args.workers,
                args.label,
                sequential=(args.sequential or chain == "bsc") and not heavy,
                ids_per_mc=(ids_per or 25) if heavy else ids_per,
                shard_size_override=2000 if heavy else None,
            )
        except Exception as e:
            print(f"[{chain}] FATAL {type(e).__name__}: {e}", flush=True)
            return {"chain": chain, "status": f"fatal: {e}"}

    results: list[dict] = []
    cw = max(1, min(args.chain_workers, len(queue)))
    print(f"census: {len(queue)} chains, chain_workers={cw}, shard workers={args.workers}", flush=True)
    if cw == 1:
        for chain in queue:
            results.append(run_one(chain))
            run_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    else:
        with ThreadPoolExecutor(max_workers=cw) as ex:
            futs = {ex.submit(run_one, c): c for c in queue}
            for fut in as_completed(futs):
                results.append(fut.result())
                run_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    total_pop = sum(r.get("population", 0) for r in results)
    total_uri = sum(r.get("n_has_token_uri", 0) for r in results)
    starts = [r["started_utc"] for r in results if r.get("started_utc")]
    ends = [r["timestamp_utc"] for r in results if r.get("timestamp_utc")]
    run_meta = {
        "label": args.label,
        "script_version": SCRIPT_VERSION,
        "chain_workers": cw,
        "wall_sec": round(time.time() - t_run, 1),
        "first_chain_started_utc": min(starts) if starts else None,
        "last_chain_finished_utc": max(ends) if ends else None,
        "total_population": total_pop,
        "total_with_uri": total_uri,
        "chains": results,
    }
    run_path.write_text(json.dumps(run_meta, indent=2), encoding="utf-8")
    print(
        f"\nTOTAL population={total_pop} with_uri={total_uri} "
        f"wall={run_meta['wall_sec']}s span={run_meta['first_chain_started_utc']}..{run_meta['last_chain_finished_utc']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
