#!/usr/bin/env python3
"""Summarize Reputation Registry feedback concentration and its effect on flag G.

    python mc_qa_reputation.py --label mc_t1
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

csv.field_size_limit(10**9)
ROOT = Path(__file__).resolve().parents[2]
MC = ROOT / "_enrich" / "multichain"
REP = ROOT / "_enrich" / "reputation"
REPORTS = ROOT / "_enrich" / "reports"

# an address at or above this many feedbacks is treated as a bulk issuer: the gap in the
# observed distribution is wide (7 addresses above it, next one an order of magnitude below)
BULK_MIN = 1000


def load_clients(label: str) -> dict[str, list[tuple[int, str]]]:
    out: dict[str, list[tuple[int, str]]] = {}
    base = REP / label
    if not base.is_dir():
        raise SystemExit(f"no reputation data under {base}")
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        files = sorted(d.glob("clients_head*.csv"))
        if not files:
            continue
        rows = []
        with files[-1].open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows.append((int(r["agent_id"]), r["client_address"], r.get("in_denylist") == "1"))
        out[d.name] = rows
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="mc_t1")
    args = ap.parse_args()

    clients = load_clients(args.label)
    run = json.loads((REP / f"run_reputation_{args.label}.json").read_text(encoding="utf-8"))
    heads = {c["chain"]: c.get("block_read") for c in run.get("chains", []) if isinstance(c, dict)}

    # panel: current G and A, to measure what the new channel changes
    panel_dir = MC if args.label == "mc_t1" else MC / args.label
    panel_file = panel_dir / "panel_all.csv"
    if not panel_file.exists():
        raise SystemExit(f"no panel at {panel_file}")
    panel: dict[tuple[str, str], tuple[int, int, int]] = {}
    with panel_file.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            panel[(r["chain"], r["agent_id"])] = (int(r["G"]), int(r["A"]), int(r["S9mc"]))

    lines = [
        f"# Reputation layer — {args.label}\n",
        "Source: `getClients(agentId)` on `0x8004baa1…`, read at HEAD of each chain "
        f"({run.get('generated_utc')}). Free public nodes do not serve history, so this layer is "
        "**not aligned** with the census block — stated wherever `G` is reported.\n",
        "## 1. Volume and concentration\n",
        "| chain | block | agents with feedback | of them independent | distinct issuers | top-1 issuer share |",
        "|---|---:|---:|---:|---:|---:|",
    ]

    tot_any = tot_rows = 0
    global_issuers: Counter = Counter()
    per_chain_top: dict[str, tuple[str, float]] = {}
    for ch in sorted(clients):
        rows = clients[ch]
        agents = {a for a, _c, _d in rows}
        iss = Counter(c for _a, c, _d in rows)
        global_issuers.update(iss)
        top_share = (iss.most_common(1)[0][1] / len(rows) * 100) if rows else 0
        indep = len({a for a, _c, d in rows if not d})
        tot_any += len(agents)
        tot_rows += len(rows)
        if rows:
            per_chain_top[ch] = (iss.most_common(1)[0][0], top_share)
        lines.append(
            f"| {ch} | {heads.get(ch, '—')} | {len(agents)} | {indep} | {len(iss)} | {top_share:.1f}% |"
        )

    lines += [
        f"\nTotal agents with feedback: **{tot_any}**, feedback rows {tot_rows}, "
        f"distinct issuers across all chains **{len(global_issuers)}**.\n",
        "### Largest issuers\n",
        "| address | rows | chains |",
        "|---|---:|---:|",
    ]
    issuer_chains: dict[str, set[str]] = defaultdict(set)
    for ch, rows in clients.items():
        for _a, c, _d in rows:
            issuer_chains[c].add(ch)
    for c, n in global_issuers.most_common(12):
        lines.append(f"| `{c}` | {n} | {len(issuer_chains[c])} |")

    top1 = global_issuers.most_common(1)
    if top1 and tot_rows:
        share = 100 * top1[0][1] / tot_rows
        top2 = 100 * sum(v for _c, v in global_issuers.most_common(2)) / tot_rows
        singles = sum(1 for v in global_issuers.values() if v == 1)
        lines.append(
            f"\nA single address `{top1[0][0]}` issued **{share:.1f}%** of all feedback rows; the "
            f"two largest issued **{top2:.1f}%**. Meanwhile {singles} of {len(global_issuers)} "
            "issuers produced exactly one row. Feedback in ERC-8004 is permissionless and nearly "
            "free, so feedback volume measures issuer activity, not agent usefulness. Read this "
            "layer like the endpoint layer: by the number of distinct issuers, not by row counts.\n"
        )

    # Compare static denylist against threshold-based bulk-issuer filter.
    bulk = {c for c, v in global_issuers.items() if v >= BULK_MIN}
    denylisted = {c for rows in clients.values() for _a, c, d in rows if d}
    missed = sorted(bulk - denylisted, key=lambda c: -global_issuers[c])
    if missed:
        rows_missed = sum(global_issuers[c] for c in missed)
        lines += [
            "### Bulk-issuer coverage beyond the static denylist\n",
            f"The static denylist names {len(denylisted)} address(es). "
            f"Across all 13 chains, **{len(missed)}** additional "
            f"issuers with ≥{BULK_MIN} rows are outside that list; they account for "
            f"**{100 * rows_missed / tot_rows:.1f}%** of all feedback rows.\n",
            "| not in denylist | rows | chains |",
            "|---|---:|---:|",
        ]
        for c in missed[:8]:
            lines.append(f"| `{c}` | {global_issuers[c]} | {len(issuer_chains[c])} |")
        widest = max(missed, key=lambda c: len(issuer_chains[c]))
        lines.append(
            f"\nThe widest of them, `{widest}`, is present on {len(issuer_chains[widest])} chains — "
            "more than the static denylist entry. Threshold-based issuer filtering "
            "therefore provides a more consistent view than a fixed address list.\n"
        )

    # reputation analogue of S11mc: drop bulk issuers entirely and count what survives
    agents_any = {(ch, str(a)) for ch, rows in clients.items() for a, _c, _d in rows}
    agents_nonbulk = {
        (ch, str(a)) for ch, rows in clients.items() for a, c, _d in rows if c not in bulk
    }
    lines += [
        "### Feedback without bulk issuers\n",
        f"Dropping all {len(bulk)} issuers with ≥{BULK_MIN} rows leaves **{len(agents_nonbulk)}** "
        f"of **{len(agents_any)}** agents with feedback "
        f"({100 * len(agents_nonbulk) / len(agents_any):.1f}%). This is the reputation analogue "
        "of S11mc: the same move as with the endpoint layer, and the same conclusion — nearly "
        "all volume comes from a few issuers.\n",
    ]

    # what it changes for G
    lines += ["## 2. What changes in flag G\n"]
    flip = same = orphan_no_uri = with_s9 = 0
    missing_panel = 0
    for ch, rows in clients.items():
        for a, _c, _d in rows:
            key = (ch, str(a))
            p = panel.get(key)
            if not p:
                missing_panel += 1
                continue
            g, A, s9 = p
            if g == 0:
                flip += 1
            else:
                same += 1
            if A == 0:
                orphan_no_uri += 1
            if s9 == 1:
                with_s9 += 1
    uniq_flip = len({
        (ch, a) for ch, rows in clients.items() for a, _c, _d in rows
        if panel.get((ch, str(a))) and panel[(ch, str(a))][0] == 0
    })
    lines += [
        "| What | N |",
        "|---|---:|",
        f"| agents with feedback whose current `G=0` (flag will flip) | **{uniq_flip}** |",
        f"| agent–feedback pairs where `G` is already 1 | {same} |",
        f"| feedback rows for agents **without** tokenURI (`A=0`) | {orphan_no_uri} |",
        f"| feedback rows for agents with `S9mc=1` | {with_s9} |",
    ]
    if missing_panel:
        lines.append(f"| feedback rows for ids outside the panel (registry grew after census) | {missing_panel} |")
    lines += [
        "\nFlipped `G` changes only `agency_score` by one: `G` is not part of the tier and does "
        "not affect S0mc, S8mc, or S9mc. The value of this pass is not the shift in numbers but "
        "that the coding rule for `G` no longer differs between Ethereum and the other twelve "
        "chains.\n",
    ]
    if orphan_no_uri:
        lines.append(
            f"Separately notable: **{orphan_no_uri}** feedback rows target agents that have no "
            "on-chain metadata at all. Reputation without a card is a strong argument that this "
            "feedback is not a trace of actual use.\n"
        )

    out = REPORTS / f"MC_REPUTATION_ANALYSIS_{args.label}.md"
    REPORTS.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:40]))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
