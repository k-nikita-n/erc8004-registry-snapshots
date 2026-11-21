#!/usr/bin/env python3
"""Draw the stratified 300-endpoint sample and blind coding sheet for `agentish` validation.

    python mc_coding_sample.py --label mc_t2
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MC = ROOT / "_enrich" / "multichain"
CODING = ROOT / "_enrich" / "coding"

SEED = 20260816
N_A = 150  # heuristic says yes -> measures precision
N_B = 150  # heuristic says no but endpoint is live -> bounds misses
N_OVERLAP = 100  # second coder, 50 per stratum
ESCALATION_TRIGGER = 8  # misses in stratum B that force another 150


def load(label: str) -> tuple[dict, Path]:
    d = MC if label == "mc_t1" else MC / label
    p = d / "probes.json"
    if not p.exists():
        raise SystemExit(f"no probes.json under {d} — run the T2 pipeline first")
    return json.loads(p.read_text(encoding="utf-8")), d / "probe_bodies"


def body_of(bodies: Path, url: str) -> dict:
    h = hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()
    p = bodies / h[:2] / f"{h}.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="mc_t2")
    ap.add_argument("--n-a", type=int, default=N_A)
    ap.add_argument("--n-b", type=int, default=N_B)
    ap.add_argument("--escalate", action="store_true",
                    help="draw the pre-registered second wave of stratum B (only after >=8 misses)")
    ap.add_argument("--dry-run", action="store_true",
                    help="exercise the draw without saved bodies; writes to coding/_dryrun/")
    args = ap.parse_args()

    global CODING
    probes, bodies = load(args.label)
    if args.dry_run:
        CODING = CODING / "_dryrun"
    elif not bodies.exists():
        raise SystemExit(
            f"no probe_bodies under {bodies} — the run must use --save-probe-bodies, "
            "otherwise there is nothing to code"
        )

    A = sorted(e for e, r in probes.items()
               if r.get("probeable_rspec") == 1 and r.get("live") == 1 and r.get("agentish") == 1)
    B = sorted(e for e, r in probes.items()
               if r.get("probeable_rspec") == 1 and r.get("live") == 1 and r.get("agentish") == 0)
    print(f"label={args.label}  stratum A={len(A)}  stratum B={len(B)}")

    rng = random.Random(SEED)
    pick_a = rng.sample(A, min(args.n_a, len(A)))
    pick_b = rng.sample(B, min(args.n_b, len(B)))
    if args.escalate:
        rest = sorted(set(B) - set(pick_b))
        pick_b += random.Random(SEED + 1).sample(rest, min(args.n_b, len(rest)))
        print(f"escalation wave: stratum B extended to {len(pick_b)}")

    CODING.mkdir(parents=True, exist_ok=True)
    rows = [(e, "A") for e in pick_a] + [(e, "B") for e in pick_b]

    # the key file: keeps the stratum, never shown to the coder
    with (CODING / "sample.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["endpoint", "stratum", "seed", "label_run", "machine_agentish"])
        for e, s in rows:
            w.writerow([e, s, SEED, args.label, probes[e].get("agentish")])

    # blind sheet: shuffled, stratum and machine verdict stripped
    shuffled = rows[:]
    random.Random(SEED + 2).shuffle(shuffled)
    missing = 0
    sheet = []
    for i, (e, _s) in enumerate(shuffled, 1):
        b = body_of(bodies, e)
        if not b:
            missing += 1
        sheet.append({
            "idx": i,
            "endpoint": e,
            "http_status": b.get("http_status", ""),
            "content_type": b.get("content_type", ""),
            "body_len": b.get("body_len", ""),
            "truncated": b.get("truncated", ""),
            "body_snippet": (b.get("body_snippet") or "").replace("\r", " ").replace("\n", " ")[:2048],
            "label": "",
            "note": "",
        })

    with (CODING / "coding_sheet.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(sheet[0].keys()))
        w.writeheader()
        w.writerows(sheet)

    by_stratum = {"A": [e for e, s in rows if s == "A"], "B": [e for e, s in rows if s == "B"]}
    rng_ov = random.Random(SEED + 3)
    overlap = set(rng_ov.sample(by_stratum["A"], min(N_OVERLAP // 2, len(by_stratum["A"]))))
    overlap |= set(rng_ov.sample(by_stratum["B"], min(N_OVERLAP // 2, len(by_stratum["B"]))))
    ov = sorted(r["idx"] for r in sheet if r["endpoint"] in overlap)
    (CODING / "overlap_idx.json").write_text(
        json.dumps({"n": len(ov), "idx": ov, "note": "rows the second coder labels independently"}, indent=2),
        encoding="utf-8",
    )

    # readable view — 2 KB of raw JSON per row is unpleasant in a spreadsheet
    parts = [
        "<meta charset='utf-8'><style>body{font:14px/1.45 -apple-system,Segoe UI,sans-serif;max-width:1100px;"
        "margin:2rem auto;padding:0 1rem}pre{background:#f6f7f9;padding:.7rem;border-radius:6px;"
        "white-space:pre-wrap;word-break:break-all;max-height:22rem;overflow:auto}"
        ".m{color:#666;font-size:12px}h3{margin:2rem 0 .3rem}hr{border:0;border-top:1px solid #eee}</style>",
        f"<h1>agentish coding — {args.label}</h1>",
        f"<p class='m'>{len(sheet)} items, seed {SEED}. Rubric: "
        "<code>_enrich/coding/CODING_RUBRIC.md</code>. "
        "Enter labels in <code>coding_sheet.csv</code> by item number.</p>",
    ]
    for r in sheet:
        parts.append(f"<h3>#{r['idx']}</h3>")
        parts.append(f"<div class='m'>{html.escape(r['endpoint'])}</div>")
        parts.append(
            f"<div class='m'>HTTP {r['http_status']} · {html.escape(str(r['content_type']))} · "
            f"{r['body_len']} bytes{' · truncated' if r['truncated'] else ''}</div>"
        )
        parts.append(f"<pre>{html.escape(r['body_snippet'] or '(empty)')}</pre><hr>")
    (CODING / "coding_sheet.html").write_text("\n".join(parts), encoding="utf-8")

    print(f"drawn: A={len(pick_a)}  B={len(pick_b)}  total={len(rows)}")
    if missing:
        print(f"WARNING: {missing} sampled endpoints have no saved body — check --save-probe-bodies")
    print(f"overlap for the second coder: {len(ov)} rows -> overlap_idx.json")
    print(f"escalation rule: >= {ESCALATION_TRIGGER} misses in stratum B -> rerun with --escalate")
    for p in ("sample.csv", "coding_sheet.csv", "coding_sheet.html", "overlap_idx.json"):
        print(f"  wrote {CODING / p}")
    print("\nsample.csv holds the machine verdict — do not open it before coding is finished.")


if __name__ == "__main__":
    main()
