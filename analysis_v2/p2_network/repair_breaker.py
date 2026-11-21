#!/usr/bin/env python3
"""Sequential per-host retry for mc_t2-live endpoints cut by the mc_t3p circuit breaker.

Uses `mc_fetch_score.probe_one` (1.2 s per-host gap, up to 8 hosts in parallel).
Writes `_enrich/multichain/mc_t3p/probes_breaker_repair.json`.
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "_enrich" / "scripts"))
import mc_fetch_score as mfs  # noqa: E402

MC = ROOT / "_enrich" / "multichain"
T2, T3 = MC / "mc_t2" / "probes.json", MC / "mc_t3p" / "probes.json"
REPAIR = MC / "mc_t3p" / "probes_breaker_repair.json"
GAP_S = 1.2


def main() -> None:
    mfs.LOG_PATH = MC / "mc_t3p" / "run.log"
    t2 = json.loads(T2.read_text(encoding="utf-8"))
    t3 = json.loads(T3.read_text(encoding="utf-8"))
    targets = [e for e, r in t2.items() if r.get("probeable_rspec") == 1 and r.get("live")
               and str((t3.get(e) or {}).get("error", "")).startswith("host_dead")]
    rec = json.loads(REPAIR.read_text(encoding="utf-8")) if REPAIR.exists() else {}
    todo = [e for e in targets if e not in rec]
    by_host: dict[str, list[str]] = defaultdict(list)
    for e in sorted(todo):
        by_host[urlparse(e).netloc].append(e)
    stamp = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime())
    mfs.log(f"mc_t3p breaker repair: targets={len(targets)} todo={len(todo)} hosts={len(by_host)} gap={GAP_S}s")
    print(f"targets={len(targets)} todo={len(todo)} hosts={len(by_host)}", flush=True)
    with mfs._host_lock:
        mfs._host_state.clear()
    done = [0]

    def do_host(host: str) -> list[tuple[str, dict]]:
        out = []
        for i, ep in enumerate(by_host[host]):
            if i:
                time.sleep(GAP_S)
            r = mfs.probe_one(ep, rounds=1, save_body=False)
            r["recovered"] = f"t3p_breaker_repair_{stamp}"
            r["probed_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            out.append((ep, r))
            done[0] += 1
            if done[0] % 100 == 0:
                print(f"  {done[0]}/{len(todo)}", flush=True)
        return out

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(by_host)))) as pool:
        for part in pool.map(do_host, list(by_host)):
            for ep, r in part:
                rec[ep] = r
            REPAIR.write_text(json.dumps(rec), encoding="utf-8")
    REPAIR.write_text(json.dumps(rec), encoding="utf-8")
    live = sum(1 for e in targets if (rec.get(e) or {}).get("live"))
    ag = sum(1 for e in targets if (rec.get(e) or {}).get("agentish"))
    dead = sum(1 for e in targets if str((rec.get(e) or {}).get("error", "")).startswith("host_dead"))
    msg = f"mc_t3p breaker repair DONE: targets={len(targets)} now_live={live} now_agentish={ag} breaker_again={dead}"
    mfs.log(msg)
    print(msg, flush=True)


if __name__ == "__main__":
    main()
