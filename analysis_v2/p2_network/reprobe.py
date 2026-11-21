#!/usr/bin/env python3
"""Re-probe the 33,065 declared mc_t2 endpoints (mc_t3p, single round, no bodies saved).

Uses `mc_fetch_score.probe_one` (per-host pacing, circuit breaker, 429 retries, verifying TLS).
    python reprobe.py            # resumes from mc_t3p/probes.json if present
Writes _enrich/multichain/mc_t3p/{probes.json, PROBE_PROGRESS.json, RUN_META.json, run.log}.
"""
from __future__ import annotations

import atexit
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "_enrich" / "scripts"))
import mc_fetch_score as mfs  # noqa: E402

T2 = ROOT / "_enrich" / "multichain" / "mc_t2" / "probes.json"
OUT = ROOT / "_enrich" / "multichain" / "mc_t3p"


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    lock = OUT / "RUN.lock"
    if lock.exists():
        raise SystemExit(f"REFUSING TO START: {lock} exists — another run or an orphan; check and delete it")
    lock.write_text(json.dumps({"pid": os.getpid(), "started_utc": utc()}), encoding="utf-8")
    atexit.register(lambda: lock.unlink(missing_ok=True))
    mfs.LOG_PATH = OUT / "run.log"

    endpoints = sorted(json.loads(T2.read_text(encoding="utf-8")))
    probes_path = OUT / "probes.json"
    results: dict[str, dict] = {}
    if probes_path.exists():
        results = json.loads(probes_path.read_text(encoding="utf-8"))
    todo = [e for e in endpoints if e not in results]
    todo.sort(key=lambda u: (0 if mfs.is_probeable_endpoint(u) else 1, u))

    meta_path = OUT / "RUN_META.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {
        "label": "mc_t3p", "source_endpoints": "mc_t2/probes.json", "n_endpoints": len(endpoints),
        "probe_code": mfs.SCRIPT_VERSION, "user_agent": mfs.UA, "rounds": 1, "save_bodies": False,
        "workers": mfs.PROBE_WORKERS, "host_min_interval_s": mfs.PROBE_HOST_MIN_INTERVAL_S, "runs": []}
    meta["runs"].append({"started_utc": utc(), "todo": len(todo), "resumed_from": len(results)})
    mfs.write_json_atomic(meta_path, meta)
    mfs.log(f"mc_t3p: endpoints={len(endpoints)} todo={len(todo)} resumed={len(results)}")

    with mfs._host_lock:
        mfs._host_state.clear()

    def flush(done: int) -> None:
        mfs.write_json_atomic(probes_path, results)
        mfs.write_json_atomic(OUT / "PROBE_PROGRESS.json", {"done_new": done, "todo": len(todo),
                                                            "saved_total": len(results), "updated_utc": utc()})

    done = 0
    with ThreadPoolExecutor(max_workers=mfs.PROBE_WORKERS) as ex:
        futs = {ex.submit(mfs.probe_one, ep, 1, mfs.PROBE_ROUND_GAP_S, False): ep for ep in todo}
        for fut in as_completed(futs):
            r = fut.result()
            r["probed_utc"] = utc()
            results[r["endpoint"]] = r
            done += 1
            if done % 200 == 0 or done == len(todo):
                flush(done)
                mfs.log(f"mc_t3p progress {done}/{len(todo)}")
    flush(done)
    meta["runs"][-1]["finished_utc"] = utc()
    mfs.write_json_atomic(meta_path, meta)
    live = sum(1 for r in results.values() if r.get("live"))
    agentish = sum(1 for r in results.values() if r.get("agentish"))
    mfs.log(f"mc_t3p DONE: probed={len(results)} live={live} agentish={agentish}")
    print(f"DONE probed={len(results)} live={live} agentish={agentish}", flush=True)


if __name__ == "__main__":
    main()
