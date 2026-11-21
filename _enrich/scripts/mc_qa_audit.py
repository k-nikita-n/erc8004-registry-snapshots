#!/usr/bin/env python3
"""Independent offline QA audit of a snapshot's census, panels, and summaries (163 checks).

Checks: (1) census self-consistency, (2) panel-to-census joins, (3) recomputation
of summary counts and strata from rows, and (4) logical flag/tier/score invariants.

    python mc_qa_audit.py --label mc_t1       # offline checks (mc_t1 or mc_t2)
    python mc_qa_audit.py --onchain 24        # + live on-chain spot-check
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mc_deposit_shape import uri_shape  # noqa: E402

csv.field_size_limit(10**9)
ROOT = Path(__file__).resolve().parents[2]
MC_BASE = ROOT / "_enrich" / "multichain"
MC = MC_BASE
CACHE = MC_BASE / "uri_cache"
CENSUS = ROOT / "_chain" / "multichain"

ASSUMPTION_TIERS = frozenset(
    {"none", "uri_unresolved", "fetch_failed", "throttled", "non_json", "card_not_8004"}
)
PROBLEMS: list[str] = []
CHECKS = [0, 0]  # [passed, total]


def check(ok: bool, name: str, detail: str = "") -> bool:
    CHECKS[1] += 1
    if ok:
        CHECKS[0] += 1
        print(f"  ok    {name}")
    else:
        PROBLEMS.append(f"{name}: {detail}")
        print(f"  FAIL  {name}  {detail}")
    return ok


def z(v: str):
    return None if v == "" else int(v)


def tier_of(A, B, C, D, E, b0: str) -> str:
    if A != 1:
        return "none"
    if B != 1:
        return b0 if b0 in ("fetch_failed", "throttled", "non_json", "card_not_8004") else "uri_unresolved"
    if E == 1 or (C == 1 and D == 1):
        return "strong"
    if C == 1:
        return "moderate"
    return "weak"


def card_digest(card: dict) -> str:
    """card_hash is not a hash of the card: it is a normalised digest over name,
    description and endpoints, so cosmetic edits elsewhere do not read as churn."""
    svcs = [s for s in (card.get("services") or []) if isinstance(s, dict)]
    blob = json.dumps(
        {
            "n": str(card.get("name", "")).strip().lower(),
            "d": str(card.get("description", "")).strip().lower(),
            "e": sorted(
                str(s.get("endpoint", "")).strip().lower()
                for s in svcs
                if str(s.get("endpoint", "")).strip()
            ),
        },
        sort_keys=True,
    )
    return hashlib.sha1(blob.encode()).hexdigest()


def score_of(A, B, C, D, E, F, G) -> float:
    s = (1 if A else 0) + (2 if B else 0) + (2 if C else 0) + (1 if D else 0) + (2 if E else 0)
    s += 0.5 if F else 0
    s += 1 if G else 0
    return min(10.0, round(s, 1))


def load_summary() -> dict:
    return json.loads((MC / "MC_SUMMARY.json").read_text(encoding="utf-8"))


def census_file(chain: str, summary: dict) -> Path | None:
    used = (summary.get("census_used") or {}).get(chain) or {}
    f = used.get("file")
    if f and (CENSUS / chain / f).exists():
        return CENSUS / chain / f
    files = sorted((CENSUS / chain).glob("census_*.csv"))
    return files[-1] if files else None


def meta_for(chain: str, summary: dict) -> dict:
    used = (summary.get("census_used") or {}).get(chain) or {}
    blk = used.get("observation_block")
    p = CENSUS / chain / f"meta_{blk}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def audit_census(chains: list[str], summary: dict) -> dict[str, dict]:
    print("\n=== 1. census self-consistency ===")
    out: dict[str, dict] = {}
    for ch in chains:
        cf = census_file(ch, summary)
        meta = meta_for(ch, summary)
        if not cf:
            check(False, f"[{ch}] census file", "not found")
            continue
        ids: list[int] = []
        uri_n = 0
        wallet_custom = 0
        owners_empty = 0
        rows = 0
        with cf.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows += 1
                ids.append(int(r["agent_id"]))
                if r.get("has_token_uri") == "1":
                    uri_n += 1
                w = (r.get("agent_wallet") or "").lower()
                o = (r.get("owner") or "").lower()
                if w and w != "0x" + "0" * 40 and w != o:
                    wallet_custom += 1
                if not o:
                    owners_empty += 1
        s = set(ids)
        out[ch] = {"rows": rows, "uri": uri_n, "ids": s, "max": max(s) if s else -1,
                   "wallet_custom": wallet_custom, "owners_empty": owners_empty}
        check(len(s) == rows, f"[{ch}] no duplicate agent_id", f"{rows} rows, {len(s)} unique")
        check(s == set(range(len(s))), f"[{ch}] id range contiguous from 0",
              f"max={max(s) if s else -1} n={len(s)}")
        if meta:
            check(meta.get("population") == rows, f"[{ch}] rows == meta.population",
                  f"{rows} vs {meta.get('population')}")
            check(meta.get("n_has_token_uri") == uri_n, f"[{ch}] URI count == meta",
                  f"{uri_n} vs {meta.get('n_has_token_uri')}")
            check(meta.get("n_wallet_custom_neq_owner") == wallet_custom,
                  f"[{ch}] wallet!=owner == meta", f"{wallet_custom} vs {meta.get('n_wallet_custom_neq_owner')}")
        if owners_empty:
            check(False, f"[{ch}] every row has an owner", f"{owners_empty} empty")
    return out


def audit_panels(chains: list[str], summary: dict, cens: dict[str, dict]) -> dict:
    print("\n=== 2. panel vs census, 3. summary vs panels, 4. invariants ===")
    tot = Counter()
    tiers = Counter()
    per_chain: dict[str, Counter] = {}
    s10_clusters: set[tuple[str, str]] = set()
    owners_all: set[tuple[str, str]] = set()
    owners_s9: set[tuple[str, str]] = set()
    inv = Counter()
    bad_examples: dict[str, str] = {}

    for ch in chains:
        p = MC / f"panel_{ch}.csv"
        if not p.exists():
            check(False, f"[{ch}] panel exists", "missing")
            continue
        cf = census_file(ch, summary)
        cens_rows: dict[int, dict] = {}
        with cf.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                cens_rows[int(r["agent_id"])] = r

        c = Counter()
        pids: set[int] = set()
        owner_mismatch = a_mismatch = 0
        with p.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                aid = int(r["agent_id"])
                pids.add(aid)
                c["n"] += 1
                A, B, C = int(r["A"]), int(r["B"]), int(r["C"])
                D, E = z(r["D"]), z(r["E"])
                Dp, Ep = z(r["D_prime"]), z(r["E_prime"])
                F, G = z(r["F"]), int(r["G"])
                tier = r["agency_evidence_tier"]
                pub = int(r["protocol_use_binary"])
                s8, s9, s6 = int(r["S8mc"]), int(r["S9mc"]), int(r["S6mc"])
                tiers[tier] += 1
                c[f"tier_{tier}"] += 1
                c["ao"] += int(r["assumption_only"])
                c["pub"] += pub
                c["s8"] += s8
                c["s9"] += s9
                c["s6"] += s6
                owners_all.add((ch, r["owner"]))
                if s9:
                    host_col = r.get("host_groups") if "host_groups" in r else r.get("hosts")
                    host = (host_col or "").split("|")[0] or f"_none_{r['agent_id']}"
                    s10_clusters.add((ch, host))
                    owners_s9.add((ch, r["owner"]))

                cr = cens_rows.get(aid)
                if cr:
                    if (cr.get("owner") or "") != r["owner"]:
                        owner_mismatch += 1
                    # census sets has_token_uri on a raw non-empty test, the scorer
                    # needs a usable URI; a whitespace-only tokenURI is the gap
                    if "token_uri" in cr:
                        if (1 if (cr["token_uri"] or "").strip() else 0) != A:
                            a_mismatch += 1
                        elif int(cr.get("has_token_uri") or 0) != A:
                            c["ws_uri"] += 1
                    else:
                        # Without raw token_uri, check A <= has_token_uri and track whitespace gap.
                        if A > int(cr.get("has_token_uri") or 0):
                            a_mismatch += 1
                        elif int(cr.get("has_token_uri") or 0) != A:
                            c["ws_uri"] += 1

                # --- invariants ---
                if tier_of(A, B, C, D, E, r["b0_reason"]) != tier:
                    inv["tier_rule"] += 1
                    bad_examples.setdefault("tier_rule", f"{ch}/{aid} -> {tier}")
                if abs(score_of(A, B, C, D, E, F, G) - float(r["agency_score"])) > 1e-9:
                    inv["score_rule"] += 1
                    bad_examples.setdefault("score_rule", f"{ch}/{aid}")
                if pub != (1 if tier in ("moderate", "strong") else 0):
                    inv["pub_vs_tier"] += 1
                if int(r["assumption_only"]) != (1 if tier in ASSUMPTION_TIERS else 0):
                    inv["assumption_vs_tier"] += 1
                if int(r["claim_only"]) != (1 if tier == "weak" else 0):
                    inv["claim_vs_tier"] += 1
                if int(r["evidence_backed"]) != pub:
                    inv["evidence_vs_pub"] += 1
                if s8 != (1 if pub and Dp == 1 else 0):
                    inv["s8_rule"] += 1
                if s9 != (1 if pub and Ep == 1 else 0):
                    inv["s9_rule"] += 1
                if s9 and not s8:
                    inv["s9_implies_s8"] += 1
                if E == 1 and D != 1:
                    inv["E_implies_D"] += 1
                if Ep == 1 and Dp != 1:
                    inv["Eprime_implies_Dprime"] += 1
                if Dp == 1 and D != 1:
                    inv["Dprime_subset_D"] += 1
                if Ep == 1 and E != 1:
                    inv["Eprime_subset_E"] += 1
                if B == 0 and C != 0:
                    inv["B0_implies_C0"] += 1
                if B == 0 and (D is not None or E is not None):
                    inv["B0_implies_no_probe"] += 1
                if A == 0 and tier != "none":
                    inv["A0_implies_none"] += 1

        per_chain[ch] = c
        for k, v in c.items():
            tot[k] += v
        check(pids == cens[ch]["ids"] if ch in cens else False,
              f"[{ch}] panel covers exactly the census",
              f"panel={len(pids)} census={len(cens.get(ch, {}).get('ids', []))}")
        check(owner_mismatch == 0, f"[{ch}] owner matches census", f"{owner_mismatch} rows")
        check(a_mismatch == 0, f"[{ch}] A == non-empty tokenURI", f"{a_mismatch} rows")
        if c["ws_uri"]:
            print(f"  note  [{ch}] {c['ws_uri']} tokenURI are whitespace only: the census counts "
                  "them as a URI on raw non-emptiness, the panel does not")

    print("\n--- invariants over all rows ---")
    for name in (
        "tier_rule", "score_rule", "pub_vs_tier", "assumption_vs_tier", "claim_vs_tier",
        "evidence_vs_pub", "s8_rule", "s9_rule", "s9_implies_s8", "E_implies_D",
        "Eprime_implies_Dprime", "Dprime_subset_D", "Eprime_subset_E",
        "B0_implies_C0", "B0_implies_no_probe", "A0_implies_none",
    ):
        check(inv[name] == 0, f"invariant {name}", f"{inv[name]} violations, e.g. {bad_examples.get(name, '-')}")

    print("\n--- summary recomputed from panels ---")
    n = tot["n"]
    check(n == summary["n_agents"], "n_agents", f"{n} vs {summary['n_agents']}")
    for t, v in tiers.items():
        check(summary["tiers"].get(t) == v, f"tier {t}", f"{v} vs {summary['tiers'].get(t)}")
    check(round(100 * tot["ao"] / n, 3) == summary["assumption_only_pct"], "assumption_only_pct",
          f"{round(100 * tot['ao'] / n, 3)} vs {summary['assumption_only_pct']}")
    check(round(100 * tot["pub"] / n, 3) == summary["protocol_use_pct"], "protocol_use_pct",
          f"{round(100 * tot['pub'] / n, 3)} vs {summary['protocol_use_pct']}")
    check(tot["pub"] == summary["S0mc"]["n"], "S0mc n", f"{tot['pub']} vs {summary['S0mc']['n']}")
    check(tot["s8"] == summary["S8mc"]["n"], "S8mc n", f"{tot['s8']} vs {summary['S8mc']['n']}")
    check(tot["s9"] == summary["S9mc"]["n"], "S9mc n", f"{tot['s9']} vs {summary['S9mc']['n']}")
    check(tot["s6"] == summary["S6mc"]["n"], "S6mc n", f"{tot['s6']} vs {summary['S6mc']['n']}")
    check(len(s10_clusters) == summary["S10mc"]["clusters"], "S10mc clusters",
          f"{len(s10_clusters)} vs {summary['S10mc']['clusters']}")
    check(len(owners_s9) == summary["S10mc"]["owners_s9"], "S10mc owners_s9",
          f"{len(owners_s9)} vs {summary['S10mc']['owners_s9']}")
    check(len(owners_all) == summary["S10mc"]["owners_all"], "S10mc owners_all",
          f"{len(owners_all)} vs {summary['S10mc']['owners_all']}")

    print("\n--- per-chain block in summary ---")
    for ch, c in per_chain.items():
        sc = (summary.get("chains") or {}).get(ch) or {}
        check(sc.get("n") == c["n"], f"[{ch}] n", f"{c['n']} vs {sc.get('n')}")
        check(sc.get("S9mc_n") == c["s9"], f"[{ch}] S9mc_n", f"{c['s9']} vs {sc.get('S9mc_n')}")
    return {"per_chain": {k: dict(v) for k, v in per_chain.items()}, "totals": dict(tot)}


def audit_cards(summary: dict, sample: int = 400) -> None:
    """Recompute panel card_hash values against local uri_cache (working tree only)."""
    print(f"\n=== 5. card_hash vs cache (sample {sample}) ===")
    rows = []
    with (MC / "panel_all.csv").open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "card_hash" not in (reader.fieldnames or []):
            print("  skipped: the panel carries card_group, not card_hash, so this is an\n"
                  "  unpacked deposit. Run mc_verify_deposit.py instead.")
            return
        for r in reader:
            if r["B"] == "1" and r["card_hash"]:
                rows.append((r["chain"], r["agent_id"], r["card_hash"], r["token_uri"]))
    rnd = random.Random(7)
    pick = rnd.sample(rows, min(sample, len(rows)))
    checked = mism = nocache = refetched = 0
    for _ch, _aid, h, uri in pick:
        if not uri or uri.endswith("…") or len(uri) >= 300:
            continue  # panel truncates token_uri at 300 chars; can't rebuild the key
        p = CACHE / hashlib.sha1(uri.encode("utf-8", "replace")).hexdigest()[:2] / (
            hashlib.sha1(uri.encode("utf-8", "replace")).hexdigest() + ".json"
        )
        if not p.exists():
            nocache += 1
            continue
        card = (json.loads(p.read_text(encoding="utf-8")) or {}).get("card")
        if not isinstance(card, dict):
            nocache += 1
            continue
        if set(card) == {"error"}:
            # Shared cache entry overwritten by a subsequent failed re-fetch.
            refetched += 1
            continue
        calc = card_digest(card)
        checked += 1
        if calc != h:
            mism += 1
    check(mism == 0, "card_hash reproducible from cache", f"{mism} mismatched of {checked} checked")
    if nocache:
        print(f"  note  {nocache} of the sample skipped (URI truncated in panel or card absent)")
    if refetched:
        print(f"  note  {refetched} skipped: cache entry replaced by a rate-limited "
              f"re-fetch after this panel was built")


def audit_onchain(summary: dict, n: int) -> None:
    """The only external check: ask the chain what it says about a few agents."""
    print(f"\n=== 6. on-chain spot check ({n} agents) ===")
    from rpc_pool import load_pool, pad_uint

    SEL_OWNER, SEL_URI = "6352211e", "c87b56dd"
    IDENTITY_ADDR = None
    try:
        from rpc_pool import IDENTITY as IDENTITY_ADDR  # noqa: N811
    except ImportError:
        pass

    def decode_string(data: str) -> str:
        if not data or data in ("0x", "0x0"):
            return ""
        h = data[2:]
        try:
            off = int(h[0:64], 16) * 2
            ln = int(h[off : off + 64], 16) * 2
            return bytes.fromhex(h[off + 64 : off + 64 + ln]).decode("utf-8", "replace")
        except Exception:
            return ""

    rnd = random.Random(11)
    chains = sorted((summary.get("chains") or {}))
    picks: list[tuple[str, int]] = []
    per = max(1, n // len(chains))
    for ch in chains:
        cf = census_file(ch, summary)
        if not cf:
            continue
        total = (summary["chains"][ch] or {}).get("n") or 0
        if total:
            picks += [(ch, rnd.randrange(total)) for _ in range(per)]

    by_chain: dict[str, list[int]] = defaultdict(list)
    for ch, aid in picks:
        by_chain[ch].append(aid)

    ok = bad = skipped = 0
    for ch, aids in by_chain.items():
        cf = census_file(ch, summary)
        want: dict[int, dict] = {}
        with cf.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                a = int(r["agent_id"])
                if a in aids:
                    want[a] = r
        used = (summary.get("census_used") or {}).get(ch) or {}
        blk = used.get("observation_block")
        try:
            pool, _ = load_pool(ch, probe_id=0)
        except Exception as e:
            print(f"  skip  [{ch}] rpc unavailable: {type(e).__name__}")
            skipped += len(aids)
            continue
        for aid in sorted(want):
            row = want[aid]
            for tag in (hex(blk) if blk else "latest", "latest"):
                try:
                    o = pool.call("eth_call", [{"to": IDENTITY_ADDR, "data": "0x" + SEL_OWNER + pad_uint(aid)}, tag])
                    u = pool.call("eth_call", [{"to": IDENTITY_ADDR, "data": "0x" + SEL_URI + pad_uint(aid)}, tag])
                except Exception:
                    continue
                owner = ("0x" + o[-40:]).lower() if o and len(o) >= 42 else ""
                uri = decode_string(u or "")
                same_o = owner == (row["owner"] or "").lower()
                if "token_uri" in row:
                    same_u = uri.strip() == (row["token_uri"] or "").strip()
                else:
                    # An unpacked deposit has the shape of the field, not the field.
                    # Whoever can reach an archive node can still check the shape.
                    media, enc, nbytes = uri_shape(uri)
                    same_u = (media == row["uri_media_type"]
                              and enc == row["uri_encoding"]
                              and str(nbytes if uri else 0) == (row["uri_bytes"] or "0"))
                if same_o and same_u:
                    ok += 1
                else:
                    bad += 1
                    print(f"  diff  [{ch}/{aid}] @{tag} owner={'=' if same_o else 'X'} uri={'=' if same_u else 'X'}")
                break
            else:
                skipped += 1
    # printed unconditionally: bad==0 with ok==0 would be a vacuous pass
    print(f"  checked={ok + bad} match={ok} differ={bad} unreachable={skipped}")
    check(ok > 0, "on-chain check actually ran", f"{ok + bad} agents reached")
    check(bad == 0, "on-chain owner+tokenURI match census",
          f"{bad} differ, {ok} match, {skipped} unreachable")
    if skipped:
        print(f"  note  {skipped} skipped — free nodes prune historical state, this is expected")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--onchain", type=int, default=0, help="spot-check N agents against the chain")
    ap.add_argument("--cards", type=int, default=400)
    ap.add_argument("--label", default="mc_t1",
                    help="mc_t2 reads _enrich/multichain/mc_t2/ and never opens the baseline summary")
    args = ap.parse_args()
    global MC
    if args.label != "mc_t1":
        MC = MC_BASE / args.label
        if not (MC / "MC_SUMMARY.json").exists():
            raise SystemExit(f"no summary under {MC}")

    summary = load_summary()
    chains = sorted((summary.get("chains") or {}))
    print(f"auditing {args.label}: {summary['n_agents']} agents, {len(chains)} chains, "
          f"script {summary.get('script_version')}, codebook {summary.get('codebook_version')}")

    cens = audit_census(chains, summary)
    audit_panels(chains, summary, cens)
    audit_cards(summary, args.cards)
    if args.onchain:
        audit_onchain(summary, args.onchain)

    print("\n" + "=" * 60)
    print(f"checks passed: {CHECKS[0]}/{CHECKS[1]}")
    if PROBLEMS:
        print(f"\n{len(PROBLEMS)} problem(s):")
        for p in PROBLEMS:
            print(f"  - {p}")
    else:
        print("no discrepancies found")


if __name__ == "__main__":
    main()
