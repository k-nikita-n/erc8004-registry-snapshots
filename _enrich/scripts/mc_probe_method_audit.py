#!/usr/bin/env python3
"""Pre-registered method audit: does GET-only undercount live agent endpoints (D)?

Pool: probeable endpoints that answered with 400/402/405/406/415 (server spoke,
our GET was the wrong method/type). Sample 50, then:

  1. POST JSON-RPC MCP `initialize` to the same URL
  2. GET `/.well-known/agent-card.json` on the same origin

`agentish` is the same rule as `mc_fetch_score.py` (keys only, ct ignored).

    python mc_probe_method_audit.py --label mc_t2
"""
from __future__ import annotations

import argparse
import json
import math
import random
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MC = ROOT / "_enrich" / "multichain"
REPORTS = ROOT / "_enrich" / "reports"

SEED = 20260816  # same family as the coding draw; the pre-registered audit did not pin a seed
N_SAMPLE = 50
STATUSES = {400, 402, 405, 406, 415}
TIMEOUT = 12
UA = "ERC8004-probe-audit/1.0"
CTX = ssl.create_default_context()
AGENTISH_KEYS = frozenset({"tools", "skills", "capabilities", "agentcard", "mcpservers"})

MCP_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "erc8004-probe-audit", "version": "1.0"},
    },
}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - h) / d, (c + h) / d)


def agentish(body: bytes, ct: str = "") -> bool:
    try:
        text = body.decode("utf-8", "replace")
    except Exception:
        return False
    low = text.lower()
    if (ct or "").lower().startswith("application/json") or text.strip()[:1] in "{[":
        try:
            obj = json.loads(text)
        except Exception:
            obj = None
        if isinstance(obj, dict) and any(k.lower() in AGENTISH_KEYS for k in obj):
            return True
        if '"mcp"' in low or '"a2a"' in low:
            return True
        if re.search(r'"(tools|skills|capabilities|agentCard|mcpServers)"\s*:', text):
            return True
    return False


def _read(resp, limit: int = 200_000) -> tuple[int, bytes, str]:
    ct = str(resp.headers.get("Content-Type") or "")
    return int(getattr(resp, "status", None) or resp.getcode()), resp.read(limit), ct


def http_get(url: str) -> tuple[int | None, bytes | None, str, str | None]:
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": UA, "Accept": "application/json, */*"},
        )
    except (ValueError, TypeError) as e:
        return None, None, "", f"bad_url:{e}"[:180]
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=CTX) as resp:
            st, body, ct = _read(resp)
            return st, body, ct, None
    except urllib.error.HTTPError as e:
        try:
            body = e.read(200_000)
        except Exception:
            body = None
        ct = ""
        try:
            ct = str(e.headers.get("Content-Type") or "")
        except Exception:
            pass
        return e.code, body, ct, f"HTTP {e.code}"
    except Exception as e:
        return None, None, "", str(e)[:180]


def http_post_json(url: str, payload: dict) -> tuple[int | None, bytes | None, str, str | None]:
    data = json.dumps(payload).encode("utf-8")
    try:
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "User-Agent": UA,
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream, */*",
            },
        )
    except (ValueError, TypeError) as e:
        return None, None, "", f"bad_url:{e}"[:180]
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=CTX) as resp:
            st, body, ct = _read(resp)
            return st, body, ct, None
    except urllib.error.HTTPError as e:
        try:
            body = e.read(200_000)
        except Exception:
            body = None
        ct = ""
        try:
            ct = str(e.headers.get("Content-Type") or "")
        except Exception:
            pass
        return e.code, body, ct, f"HTTP {e.code}"
    except Exception as e:
        return None, None, "", str(e)[:180]


def well_known(url: str) -> str:
    p = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse((p.scheme, p.netloc, "/.well-known/agent-card.json", "", "", ""))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="mc_t2")
    args = ap.parse_args()
    mc = MC if args.label == "mc_t1" else MC / args.label
    probes = json.loads((mc / "probes.json").read_text(encoding="utf-8"))

    status_counts = Counter(r.get("http_status") for r in probes.values())
    n429 = sum(1 for r in probes.values() if r.get("http_status") == 429)
    n_throttled = sum(1 for r in probes.values() if r.get("throttled_rounds"))
    pool = sorted(
        e
        for e, r in probes.items()
        if r.get("http_status") in STATUSES and r.get("probeable_rspec") == 1
    )
    rng = random.Random(SEED)
    sample = rng.sample(pool, min(N_SAMPLE, len(pool)))
    sample.sort()

    rows = []
    for i, url in enumerate(sample, 1):
        orig = probes[url]
        post_st, post_body, post_ct, post_err = http_post_json(url, MCP_INIT)
        post_ag = bool(post_body and agentish(post_body, post_ct or ""))
        wk = well_known(url)
        get_st, get_body, get_ct, get_err = http_get(wk)
        get_ag = bool(get_body and get_st and 200 <= get_st < 300 and agentish(get_body, get_ct or ""))
        rec = {
            "endpoint": url,
            "orig_status": orig.get("http_status"),
            "mcp_status": post_st,
            "mcp_agentish": int(post_ag),
            "mcp_err": post_err or "",
            "wk_url": wk,
            "wk_status": get_st,
            "wk_agentish": int(get_ag),
            "wk_err": get_err or "",
            "recovered": int(post_ag or get_ag),
        }
        rows.append(rec)
        flag = "RECOVERED" if rec["recovered"] else "—"
        print(f"[{i:02}/{len(sample)}] {orig.get('http_status')} -> mcp={post_st} a={int(post_ag)} "
              f"wk={get_st} a={int(get_ag)} {flag}  {url[:88]}", flush=True)
        time.sleep(0.4)

    k = sum(r["recovered"] for r in rows)
    n = len(rows)
    lo, hi = wilson(k, n)
    by_orig = Counter(r["orig_status"] for r in rows)
    rec_by_orig = Counter(r["orig_status"] for r in rows if r["recovered"])

    out_json = {
        "label": args.label,
        "seed": SEED,
        "n_sample": n,
        "pool_n": len(pool),
        "pool_statuses": sorted(STATUSES),
        "recovered": k,
        "rate": round(k / n, 4) if n else 0,
        "wilson95": [round(lo, 4), round(hi, 4)],
        "orig_status_in_sample": dict(by_orig),
        "recovered_by_orig_status": dict(rec_by_orig),
        "t2_status_counts": {str(a): b for a, b in status_counts.most_common()},
        "http_429_in_probes": n429,
        "throttled_rounds_gt0": n_throttled,
        "rows": rows,
    }
    dest = mc / "PROBE_METHOD_AUDIT.json"
    dest.write_text(json.dumps(out_json, indent=2), encoding="utf-8")

    rec_rows = [r for r in rows if r["recovered"]]
    rec_md = "\n".join(
        f"| `{r['endpoint'][:90]}` | {r['orig_status']} | "
        f"MCP {r['mcp_status']}/{r['mcp_agentish']} · WK {r['wk_status']}/{r['wk_agentish']} |"
        for r in rec_rows
    ) or "_no endpoint answered agent-like_"

    md = f"""# Probe method audit — {args.label} (pre-registered)

Question: does the GET probe understate liveness `D`? Not human-coded.

## Design

From endpoints where the server **answered** but not 2xx (`400/402/405/406/415`), a simple
random sample of **{n}** (pool {len(pool)}, seed {SEED}, list sorted before drawing). Each
address is re-queried with two protocol-correct requests:

1. POST JSON-RPC `initialize` to the same URL (MCP);
2. GET `/.well-known/agent-card.json` on the same origin (A2A).

`agentish` is the same rule as in scoring (keys `tools/skills/capabilities/…`; content type
does not feed the heuristic). 429 is **not** in this pool: that is throttling, not a wrong
method.

## Pool {args.label}

| HTTP | n in probes.json |
|---|---:|
| 406 | {status_counts.get(406, 0)} |
| 405 | {status_counts.get(405, 0)} |
| 402 | {status_counts.get(402, 0)} |
| 400 | {status_counts.get(400, 0)} |
| 415 | {status_counts.get(415, 0)} |
| **audit pool** | **{len(pool)}** |
| 429 | {n429} |
| `throttled_rounds>0` | {n_throttled} |

In the sample: {dict(by_orig)}.

## Result

**{k}/{n} = {100*k/n:.1f}%** answered agent-like under the correct method.
95% Wilson: [{100*lo:.1f}%; {100*hi:.1f}%].

Recovered:

{rec_md}

## 429 separately

On {args.label} after retries: HTTP 429 = **{n429}**, `endpoints_lost_to_throttling` = 0
(`MC_SUMMARY.json`). The mc_t1 throttling artifact (hundreds of endpoints on one bulk host
scoring dead) did not recur: the probe now pauses per host and retries 429. Still never mix
429 with 405/406.

## What this means for D

The GET probe confounds a method mismatch with deadness. The understatement of D on this
sample is {100*k/n:.1f}% [{100*lo:.1f}; {100*hi:.1f}]. This is a limitation, not a correction
to S9mc: canonical D/E stay round-1 GET, as on mc_t1. If the share is notable, report a
sensitivity variant "D under a protocol-correct MCP/A2A request" without recomputing the
headline.

Artifacts: `{args.label}/PROBE_METHOD_AUDIT.json`, this file.
"""
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "PROBE_METHOD_AUDIT.md").write_text(md, encoding="utf-8")
    print(f"\nrecovered {k}/{n} = {k/n:.3f}  Wilson [{lo:.3f}; {hi:.3f}]")
    print(f"wrote {dest}")
    print(f"wrote {REPORTS / 'PROBE_METHOD_AUDIT.md'}")


if __name__ == "__main__":
    main()
