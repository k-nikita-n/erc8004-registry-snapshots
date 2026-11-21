"""Cross-check deposited Ethereum tokenURI structural fields against a public reference.

Compares `uri_shape(token_uri)` on `agents_core.csv` (doi:10.7910/DVN/HJZW8Q,
tokens 0-9,999; place under `_raw/extracted/`) against the deposited columns.

    python _enrich/scripts/mc_stub_crosscheck.py
    MC_DIST=/path/to/deposit python _enrich/scripts/mc_stub_crosscheck.py
"""
from __future__ import annotations

import csv
import io
import json
import os
import sys
import zipfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "_raw" / "extracted" / "agents_core.csv"
DIST = Path(os.environ.get("MC_DIST")
            or ROOT / "submission" / "Zenodo" / "dist_submission")
OUT = ROOT / "_enrich" / "reports" / "STUB_CROSSCHECK.json"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mc_census import uri_scheme  # noqa: E402
from mc_deposit_shape import uri_shape  # noqa: E402

csv.field_size_limit(1 << 30)

FIELDS = ("uri_scheme", "uri_media_type", "uri_encoding", "uri_bytes")


def reference_rows() -> dict[str, tuple[str, str, str, int]]:
    """agent_id -> the four stub values, derived from the published field."""
    out = {}
    with REFERENCE.open(encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            uri = r.get("token_uri") or ""
            media, enc, n = uri_shape(uri)
            out[str(r["agent_id"]).strip()] = (uri_scheme(uri), media, enc, n)
    return out


def deposited(label: str) -> tuple[dict[str, tuple[str, str, str, int]], int]:
    """agent_id -> the four stub values as shipped, plus the observation block."""
    out: dict[str, tuple[str, str, str, int]] = {}
    block = 0
    with zipfile.ZipFile(DIST / f"census_{label}.zip") as zf:
        for info in zf.infolist():
            if not info.filename.startswith("ethereum/"):
                continue
            if info.filename.endswith(".json") and "/meta_" in info.filename:
                block = int(json.loads(zf.read(info))["observation_block"])
                continue
            if not info.filename.endswith(".csv"):
                continue
            for r in csv.DictReader(
                    io.StringIO(zf.read(info).decode("utf-8"), newline="")):
                out[str(r["agent_id"]).strip()] = (
                    r["uri_scheme"], r["uri_media_type"], r["uri_encoding"],
                    int(r["uri_bytes"] or 0),
                )
    return out, block


def compare(ref, dep) -> dict:
    common = sorted(set(ref) & set(dep), key=int)
    agree = 0
    per_field = Counter()
    both_empty = 0
    for aid in common:
        a, b = ref[aid], dep[aid]
        if a == b:
            agree += 1
            if a[3] == 0:
                both_empty += 1
            continue
        for i, name in enumerate(FIELDS):
            if a[i] != b[i]:
                per_field[name] += 1
    return {
        "common_agents": len(common),
        "agree_on_all_four": agree,
        "disagree": len(common) - agree,
        "agreement_pct": round(100.0 * agree / len(common), 4) if common else 0.0,
        "of_which_both_report_an_empty_field": both_empty,
        "disagreement_by_field": dict(per_field.most_common()),
    }


def main() -> int:
    if not REFERENCE.is_file():
        print(f"reference file not found: {REFERENCE}")
        print("download agents_core.csv from doi:10.7910/DVN/HJZW8Q")
        return 1
    if not DIST.is_dir():
        print(f"submission build not found: {DIST}")
        return 1

    ref = reference_rows()
    inline = sum(1 for v in ref.values() if v[0] == "data" and "base64" in v[2])
    print(f"reference: {len(ref):,} registrations, {inline} of them an inline base64 card")

    report = {
        "note": "The stub derivation, re-run over a third party's verbatim publication "
                "of the same field. No value from either dataset is recorded here.",
        "reference": {"doi": "10.7910/DVN/HJZW8Q", "rows": len(ref),
                      "inline_base64_cards": inline},
        "snapshots": {},
    }
    for label in ("mc_t1", "mc_t2"):
        dep, block = deposited(label)
        res = compare(ref, dep)
        res["our_ethereum_block"] = block
        report["snapshots"][label] = res
        print(f"\n{label} (ethereum block {block:,})")
        print(f"  agents in both:        {res['common_agents']:,}")
        print(f"  agree on all four:     {res['agree_on_all_four']:,} "
              f"({res['agreement_pct']}%)")
        print(f"  disagree:              {res['disagree']:,}")
        for field, n in res["disagreement_by_field"].items():
            print(f"      {field:<16} {n:,}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwritten: {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
