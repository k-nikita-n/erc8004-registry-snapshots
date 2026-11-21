#!/usr/bin/env python3
"""Recover failed `ipfs://` cards across public gateways with content-hash verification.

Writes a recovered card to `uri_cache` only when its `card_hash` matches the
recorded `mc_t1` panel hash; mismatches go to `ipfs_recovered_mismatch/`.

    python mc_ipfs_recover.py --label mc_t2
"""
from __future__ import annotations

import argparse
import json
import random
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

import mc_fetch_score as mc

# Public IPFS gateways queried in rotation.
GATEWAYS = (
    "https://ipfs.io/ipfs/",
    "https://dweb.link/ipfs/",
    "https://cloudflare-ipfs.com/ipfs/",
    "https://gateway.pinata.cloud/ipfs/",
    "https://w3s.link/ipfs/",
    "https://nftstorage.link/ipfs/",
    "https://ipfs.eth.aragon.network/ipfs/",
    "https://permaweb.eu.org/ipfs/",
)

TIMEOUT_S = 30
WORKERS = 6
ROUNDS = 4               # a gateway keeps resolving the DHT after our timeout,
ROUND_PAUSE_S = 90       # so a later pass often succeeds where the first timed out
GW_MIN_INTERVAL_S = 0.8  # per-gateway politeness
GW_429_COOLDOWN_S = 30.0

ROOT = Path(__file__).resolve().parents[2]
MC_DIR = ROOT / "_enrich" / "multichain"

_gw_lock = threading.Lock()
_gw_state: dict[str, dict] = {}  # gw -> {"next_ok": t, "last": t}


def _gw_wait(gw: str) -> None:
    """Block until this gateway may be hit again (politeness + 429 cooldown)."""
    while True:
        with _gw_lock:
            st = _gw_state.setdefault(gw, {"next_ok": 0.0, "last": 0.0})
            now = time.time()
            ready = max(st["next_ok"], st["last"] + GW_MIN_INTERVAL_S)
            if now >= ready:
                st["last"] = now
                return
            wait = ready - now
        time.sleep(min(wait, 2.0))


def _gw_throttle(gw: str) -> None:
    with _gw_lock:
        st = _gw_state.setdefault(gw, {"next_ok": 0.0, "last": 0.0})
        st["next_ok"] = time.time() + GW_429_COOLDOWN_S


def try_uri(item: dict, session: requests.Session) -> dict:
    uri = item["uri"]
    want_hash = item["card_hash_t1"]
    cid = mc._cid_from_uri(uri)
    if not cid:
        return {**item, "result": "no_cid"}

    gws = list(GATEWAYS)
    random.Random(cid).shuffle(gws)  # deterministic per-CID order, spreads load
    saw_junk = False
    for gw in gws:
        url = gw + cid
        _gw_wait(gw)
        try:
            r = session.get(
                url,
                timeout=TIMEOUT_S,
                headers={"Accept": "application/json, */*", "User-Agent": "erc8004-recover/1.0"},
                allow_redirects=True,
            )
        except requests.RequestException:
            continue
        body = r.content or b""
        if r.status_code == 429 or r.status_code in mc._GATEWAY_JUNK_STATUS:
            _gw_throttle(gw)
            saw_junk = True
            continue
        if r.status_code != 200:
            continue
        text = body.decode("utf-8", "replace")
        if mc._looks_like_gateway_junk(text, r.status_code):
            saw_junk = True
            continue
        try:
            card = json.loads(text)
        except json.JSONDecodeError:
            saw_junk = True
            continue
        if not isinstance(card, dict):
            return {**item, "result": "not_dict", "gateway": gw}
        got = mc.card_hash(card)
        if got == want_hash:
            return {**item, "result": "recovered", "gateway": gw, "card": card}
        return {**item, "result": "hash_mismatch", "gateway": gw,
                "card_hash_got": got, "card": card}
    return {**item, "result": "junk_only" if saw_junk else "not_found"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="mc_t2")
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--limit", type=int, default=0, help="debug: only first N URIs")
    args = ap.parse_args()

    out_dir = MC_DIR / args.label if args.label != "mc_t1" else MC_DIR
    worklist_p = out_dir / "ipfs_lost_worklist.json"
    if not worklist_p.exists():
        raise SystemExit(f"no worklist at {worklist_p}")
    worklist = json.loads(worklist_p.read_text(encoding="utf-8"))
    if args.limit:
        worklist = worklist[: args.limit]

    cache_dir = MC_DIR / "uri_cache"
    backups = sorted(MC_DIR.glob("uri_cache_backup_*.tar.gz"))
    if not backups:
        raise SystemExit("REFUSING to run: no uri_cache_backup_*.tar.gz next to uri_cache")
    print(f"backup found: {backups[-1].name}")

    # skip URIs whose cache entry is already resolved (rerun safety)
    todo = []
    for item in worklist:
        p = cache_dir / item["sha1"][:2] / f"{item['sha1']}.json"
        if p.exists():
            try:
                if json.loads(p.read_text(encoding="utf-8")).get("uri_status") == "resolved":
                    continue
            except Exception:
                pass
        todo.append(item)
    print(f"worklist {len(worklist)} URIs, still broken in cache: {len(todo)}")

    mismatch_dir = out_dir / "ipfs_recovered_mismatch"
    stats = {"recovered": 0, "junk_only": 0, "not_found": 0,
             "hash_mismatch": 0, "no_cid": 0, "not_dict": 0}
    by_gw: dict[str, int] = {}
    unrecovered = []
    t0 = time.time()
    session = requests.Session()

    attempted = len(todo)
    for rnd in range(1, ROUNDS + 1):
        if not todo:
            break
        if rnd > 1:
            print(f"round {rnd}: {len(todo)} URIs left, pausing {ROUND_PAUSE_S}s "
                  f"to let gateways finish DHT walks", flush=True)
            time.sleep(ROUND_PAUSE_S)
        next_round = []
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(try_uri, item, session): item for item in todo}
            done_n = 0
            for fut in as_completed(futs):
                res = fut.result()
                done_n += 1
                if res["result"] == "recovered":
                    stats["recovered"] += 1
                    gw_host = urllib.parse.urlparse(res["gateway"]).netloc
                    by_gw[gw_host] = by_gw.get(gw_host, 0) + 1
                    entry = {
                        "uri_status": "resolved",
                        "http_status": 200,
                        "card": res["card"],
                        "resolved_via": f"recovered:{gw_host}",
                        "uri": res["uri"],
                        "recovered_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "card_hash_verified_t1": True,
                    }
                    p = cache_dir / res["sha1"][:2] / f"{res['sha1']}.json"
                    p.parent.mkdir(parents=True, exist_ok=True)
                    tmp = p.with_suffix(".tmp")
                    tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
                    tmp.replace(p)
                elif res["result"] == "hash_mismatch":
                    stats["hash_mismatch"] += 1
                    mismatch_dir.mkdir(parents=True, exist_ok=True)
                    (mismatch_dir / f"{res['sha1']}.json").write_text(
                        json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
                    unrecovered.append({"uri": res["uri"], "why": "hash_mismatch"})
                elif res["result"] in ("no_cid", "not_dict"):
                    stats[res["result"]] += 1
                    unrecovered.append({"uri": res["uri"], "why": res["result"]})
                else:
                    next_round.append({k: res[k] for k in ("uri", "sha1", "card_hash_t1", "tokens")})
                if done_n % 50 == 0 or done_n == len(todo):
                    el = time.time() - t0
                    print(f"round {rnd}: {done_n}/{len(todo)}  recovered={stats['recovered']}  "
                          f"retry_later={len(next_round)}  ({el:.0f}s)", flush=True)
        todo = next_round

    for item in todo:  # still unresolved after the last round
        stats["not_found"] += 1
        unrecovered.append({"uri": item["uri"], "why": "not_found_after_rounds"})

    report = {
        "run_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "worklist": len(worklist),
        "attempted": attempted,
        "rounds": ROUNDS,
        "gateways": list(GATEWAYS),
        "stats": stats,
        "recovered_by_gateway": by_gw,
        "unrecovered": unrecovered,
    }
    rp = out_dir / "ipfs_recovery_report.json"
    rp.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nDONE recovered={stats['recovered']}/{attempted}  by_gateway={by_gw}")
    print(f"wrote {rp}")


if __name__ == "__main__":
    main()
