#!/usr/bin/env python3
"""Build the Zenodo v2.0.0 deposit package from an explicit allowlist.

Replaces registrant-authored strings and hostnames with structural metadata and
sequential integer surrogate keys (`uri_group`, `card_group`, `endpoint_group`,
`host_group` / `host_groups`).

Output: submission/Zenodo/dist_submission/ (archives, docs, SHA256SUMS.txt).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

csv.field_size_limit(1 << 30)  # token_uri carries inline base64 cards

ROOT = Path(__file__).resolve().parents[2]  # the working-tree root
sys.path.insert(0, str(ROOT / "_enrich" / "scripts"))
from mc_deposit_shape import (  # noqa: E402
    URI_COLUMNS, collapse_services, error_class, host_of, no_url, uri_shape,
)
CHAIN_DIR = ROOT / "_chain" / "multichain"
MC = ROOT / "_enrich" / "multichain"
MINT = ROOT / "_enrich" / "mint"
REP = ROOT / "_enrich" / "reputation"
CODING = ROOT / "_enrich" / "coding"
_ANALYSIS_ROOT = ROOT / "analysis_v2"
CERT_DIR = _ANALYSIS_ROOT / "p1_estimator" / "certainty_coding"
P1_DIR = _ANALYSIS_ROOT / "p1_estimator"
P2_DIR = _ANALYSIS_ROOT / "p2_network"
OUT_BASE = ROOT / "submission" / "Zenodo"
DIST = OUT_BASE / "dist_submission"

LABELS = ("mc_t1", "mc_t2")
CHAINS = (
    "arbitrum", "avalanche", "base", "bsc", "celo", "ethereum", "gnosis",
    "linea", "mantle", "metis", "optimism", "polygon", "scroll",
)

CYRILLIC = re.compile(r"[\u0400-\u04FF]")
# Optional gitignored marker list + generic absolute-path regex (bracketed to avoid self-match).
_EXTRA_MARKERS = ROOT / "_enrich" / "reports" / "local_markers.txt"
_extra = [re.escape(w) for w in _EXTRA_MARKERS.read_text(encoding="utf-8").split()] \
    if _EXTRA_MARKERS.is_file() else []
LOCAL_MARKERS = re.compile("|".join([r"[A-Za-z]:\\", r"/[h]ome/", r"/[U]sers/"] + _extra))

# Derived card-level column omitted from deposited panels.
DROPPED_PANEL_COLUMNS = ("name",)

errors: list[str] = []


class HostGroup:
    """Map distinct host strings to sequential 1-based integer surrogate IDs."""

    def __init__(self) -> None:
        self.map: dict[str, int] = {}

    def __call__(self, value: str) -> int | None:
        if not value:
            return None
        got = self.map.get(value)
        if got is None:
            got = self.map[value] = len(self.map) + 1
        return got


host_group = HostGroup()


CERTAINTY_PROTOCOL_MD = """# Certainty-stratum blind coding protocol (mc_t2 follow-up)

Frozen 2026-09-30 prior to receiving labels; labels returned 2026-10-02.

## Frame and composition

- **Certainty stratum (20 endpoints):** the 10 highest-weight live endpoints of stratum A
  (machine `agentish` = 1 at `mc_t2`; token weights 2,150, 831, 717, 236, 102, 36, 35, 31,
  30, 30, covering 54.1% of stratum-A token-endpoint links) and the 10 highest-weight live
  endpoints of stratum B (live, machine `agentish` = 0 at `mc_t2`; token weights 267, 137,
  87, 58, 53, 27, 19, 19, 16, 14, covering 3.9% of stratum-B links). All 20 had stored
  HTTP 200 response bodies from `mc_t2`, and none appeared in the August 300-endpoint sample.
- **Controls (20 endpoints):** 10 stratum-A and 10 stratum-B endpoints drawn from the August
  300-endpoint sample outside the double-coded overlap (`overlap_idx.json`; RNG seed
  `20261001`), unseen by coder 2. Controls conceal sheet composition and provide an
  independent agreement check against coder 1.

## Blinding and rubric

- Items 301-340 were shuffled (`seed = 20261002`) and rendered with the same annotation
  tool (`mc_coding_annotator_packages.py`) under the unchanged `CODING_RUBRIC.md` frozen on
  2026-08-10.
- Coder 2 viewed the stored 2 KB response snippet, HTTP status, content type, and endpoint
  URL string as plain text (with explicit instructions not to follow links), and saw no
  machine flag, stratum, weight, or role label. The author neither coded nor adjudicated
  any item in this set.
- `PACKAGE_HASHES.json` records the pre-coding SHA-256 digests (2026-09-30) of the coding
  sheet, blank answer file, and internal key mapping.

## Quality gate and raw outcomes (2026-10-02)

- All 40/40 items were labeled; pre-coding hashes matched on receipt.
- **Control agreement vs. coder 1:** 20/20 decisive pairs agreed (1.00; 0 undecidable),
  passing the pre-specified 0.80 floor.
- **Certainty stratum A (n = 10):** 9 labeled `1` (agent-like), 1 labeled `U` (weight 31).
- **Certainty stratum B (n = 10):** 10 labeled `0` (not agent-like).

## Files in `certainty_coding.zip`

- `certainty_labels.csv` — 40 rows keyed by integer surrogates `endpoint_group` and
  `host_group` (matching `panels_mc_t2.zip/probes.json` and `coding.zip`), with `role`,
  `stratum`, `weight_rank`, `weight`, `august_idx`, `coder2_label`, `coder1_control_label`,
  `note`.
- `answers_coder2_set2.csv` — unmodified raw coder-2 output (`idx,label,note`).
- `weights_mc_t2.json` — per-stratum token-weight vectors (`w_A`, `w_B`) and August random
  sample `(weight, label)` pairs (`sample_A`, `sample_B`); counts only, no URLs or hosts.
- `PACKAGE_HASHES.json` — pre-coding SHA-256 freeze manifest and RNG seeds.
- `CERTAINTY_PROTOCOL.md` — this document.
"""


def uri_index(census_files: list[tuple[str, str, Path]]) -> dict[str, dict[tuple[str, str], tuple]]:
    """Map label -> (chain, agent_id) -> (scheme, media_type, encoding, bytes, uri_group)."""
    groups: dict[bytes, int] = {}
    out: dict[str, dict[tuple[str, str], tuple]] = {}
    for label, chain, path in census_files:
        rows = out.setdefault(label, {})
        with path.open(encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                uri = r.get("token_uri") or ""
                if uri:
                    key = hashlib.sha1(uri.encode("utf-8", "replace")).digest()
                    gid = groups.get(key)
                    if gid is None:
                        gid = groups[key] = len(groups) + 1
                else:
                    gid = ""
                media, enc, n = uri_shape(uri)
                rows[(chain, r["agent_id"])] = (
                    sys.intern(r.get("uri_scheme") or ""), sys.intern(media),
                    sys.intern(enc), n, gid,
                )
    print(f"  uri groups: {len(groups):,} distinct values across both snapshots")
    return out


def card_index(panel_files: list[Path]) -> dict[str, int]:
    """card_hash -> group id, assigned across both snapshots."""
    groups: dict[str, int] = {}
    for path in panel_files:
        with path.open(encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                h = (r.get("card_hash") or "").strip()
                if h and h not in groups:
                    groups[h] = len(groups) + 1
    print(f"  card groups: {len(groups):,} distinct cards across both snapshots")
    return groups


def endpoint_index(probe_paths: list[Path], coding_paths: list[Path],
                   extra_json_paths: list[Path] = ()) -> dict[str, int]:
    """Map endpoint URL -> 1-based endpoint_group across probes, coding, and repair layers."""
    groups: dict[str, int] = {}

    def add(ep: str) -> None:
        ep = ep.strip()
        if ep and ep not in groups:
            groups[ep] = len(groups) + 1

    for p in probe_paths:
        for ep in json.loads(p.read_text(encoding="utf-8")):
            add(ep)
    for p in coding_paths:
        with p.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                add(r.get("endpoint") or "")
    n_core = len(groups)
    for p in extra_json_paths:
        for ep in json.loads(p.read_text(encoding="utf-8")):
            add(ep)
    print(f"  endpoint groups: {n_core:,} distinct endpoints across probes and coding"
          + (f" (+{len(groups) - n_core:,} repair-only)" if len(groups) > n_core else ""))
    return groups


def strip_probes(data: bytes, eps: dict[str, int]) -> bytes:
    """Re-key the probe records by endpoint_group and host_group; drop URL and hostname."""
    out = {}
    for url, rec in json.loads(data.decode("utf-8")).items():
        gid = eps[url.strip()]
        kept = {k: error_class(v) if k == "error" else no_url(v)
                for k, v in rec.items() if k != "endpoint"}
        out[str(gid)] = {"endpoint_group": gid, "host_group": host_group(host_of(url)), **kept}
    return json.dumps(out, ensure_ascii=False, indent=1).encode("utf-8")


def _grouped_top(entries: list, eps: dict[str, int]) -> list:
    """[[url, n], ...] -> [{endpoint_group, host_group, tokens}, ...], joinable to probes.json."""
    out = []
    for url, n in entries:
        gid = eps.get(str(url).strip())
        if gid is None:
            raise SystemExit("top endpoint missing from the probe layer")
        out.append({"endpoint_group": gid, "host_group": host_group(host_of(str(url))), "tokens": n})
    return out


def strip_summary(data: bytes, eps: dict[str, int]) -> bytes:
    """MC_SUMMARY: name the S11mc top endpoints by endpoint_group and host_group, not by URL."""
    s = json.loads(data.decode("utf-8"))
    if isinstance(s.get("S11mc"), dict) and "top_endpoints" in s["S11mc"]:
        s["S11mc"]["top_endpoints"] = _grouped_top(s["S11mc"]["top_endpoints"], eps)
    return json.dumps(s, ensure_ascii=False, indent=2).encode("utf-8")


def strip_endpoint_unit(data: bytes, eps: dict[str, int]) -> bytes:
    """The S11mc companion report gets the same treatment as the summary."""
    d = json.loads(data.decode("utf-8"))
    if "top_endpoints" in d:
        d["top_endpoints"] = _grouped_top(d["top_endpoints"], eps)
    return json.dumps(d, ensure_ascii=False, indent=2).encode("utf-8")


def strip_probe_audit(data: bytes, eps: dict[str, int]) -> bytes:
    """Replace URLs in method-audit rows with endpoint_group and host_group."""
    d = json.loads(data.decode("utf-8"))
    rows = []
    for r in d.get("rows", []):
        url = str(r.get("endpoint", "")).strip()
        gid = eps.get(url)
        if gid is None:
            raise SystemExit("audit endpoint missing from the probe layer")
        rest = {k: no_url(v) for k, v in r.items() if k not in ("endpoint", "wk_url")}
        rows.append({"endpoint_group": gid, "host_group": host_group(host_of(url)), **rest})
    d["rows"] = rows
    return json.dumps(d, ensure_ascii=False, indent=2).encode("utf-8")


def strip_coding(data: bytes, eps: dict[str, int]) -> bytes:
    """Drop body_snippet and replace endpoint URLs with endpoint_group and host_group."""
    def head(h):
        out = []
        for c in h:
            if c == "body_snippet":
                continue
            out.extend(("endpoint_group", "host_group") if c == "endpoint" else (c,))
        return out

    def row(h, r):
        out = []
        for c, v in zip(h, r):
            if c == "body_snippet":
                continue
            if c == "endpoint":
                hg = host_group(host_of(v))
                out.extend((eps[v.strip()], "" if hg is None else hg))
            else:
                out.append(v)
        return out

    return _rewrite(data, head, row, encoding="utf-8-sig")


def _rewrite(data: bytes, header_fn, row_fn, encoding: str = "utf-8") -> bytes:
    reader = csv.reader(io.StringIO(data.decode(encoding), newline=""))
    header = next(reader)
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(header_fn(header))
    for row in reader:
        writer.writerow(row_fn(header, row))
    return buf.getvalue().encode("utf-8")


def strip_census(data: bytes, chain: str, rows: dict[tuple[str, str], tuple]) -> bytes:
    """Replace the census `token_uri` with its structure and a group id."""
    def head(h):
        i = h.index("token_uri")
        return h[:i] + list(URI_COLUMNS) + h[i + 1:]

    def row(h, r):
        i = h.index("token_uri")
        _scheme, media, enc, n, gid = rows[(chain, r[h.index("agent_id")])]
        return r[:i] + [media, enc, n, gid] + r[i + 1:]

    return _rewrite(data, head, row)


def strip_panel(data: bytes, rows: dict[tuple[str, str], tuple],
                cards: dict[str, int]) -> bytes:
    """Drop `name`; replace `token_uri`, `card_hash`, and `hosts` with structural/surrogate columns."""
    def head(h):
        out = []
        for c in h:
            if c in DROPPED_PANEL_COLUMNS:
                continue
            if c == "card_hash":
                out.append("card_group")
            elif c == "hosts":
                out.append("host_groups")
            elif c == "token_uri":
                out.extend(("uri_scheme",) + URI_COLUMNS)
            else:
                out.append(c)
        return out

    def row(h, r):
        key = (r[h.index("chain")], r[h.index("agent_id")])
        scheme, media, enc, n, gid = rows[key]
        out = []
        for c, v in zip(h, r):
            if c in DROPPED_PANEL_COLUMNS:
                continue
            if c == "card_hash":
                out.append(cards.get(v.strip(), "") if v.strip() else "")
            elif c == "service_classes":
                out.append(collapse_services(v))
            elif c == "hosts":
                out.append("|".join(str(host_group(t)) for t in v.split("|") if t))
            elif c == "token_uri":
                out.extend((scheme, media, enc, n, gid))
            else:
                out.append(v)
        return out

    return _rewrite(data, head, row)


def build_certainty_zip(eps: dict[str, int], coding_tmpdir: Path) -> None:
    """Build certainty_coding.zip using endpoint_group and host_group surrogates."""
    ans_path = need(CERT_DIR / "package_coder2" / "answers_coder2_set2.csv")
    key_path = need(CERT_DIR / "key.internal.csv")
    weights_path = need(P1_DIR / "weights_mc_t2.json")
    hashes_path = need(CERT_DIR / "PACKAGE_HASHES.json")

    with zipfile.ZipFile(DIST / "panels_mc_t2.zip") as zf:
        probes_t2 = json.loads(zf.read("probes.json"))
    aug_sheet = {
        int(r["idx"]): r
        for r in csv.DictReader((coding_tmpdir / "coding_sheet.csv").open(encoding="utf-8"))
    }
    aug_c1 = {
        r["endpoint_group"]: r["label"].strip()
        for r in csv.DictReader((coding_tmpdir / "coder_1.csv").open(encoding="utf-8"))
    }
    ans = {
        int(r["idx"]): (r["label"].strip().upper(), (r.get("note") or "").strip())
        for r in csv.DictReader(ans_path.open(encoding="utf-8-sig"))
    }
    key_rows = list(csv.DictReader(key_path.open(encoding="utf-8")))

    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow([
        "idx", "endpoint_group", "host_group", "role", "stratum",
        "weight_rank", "weight", "august_idx",
        "coder2_label", "coder1_control_label", "note",
    ])
    ctrl_agree = ctrl_total = 0
    for kr in key_rows:
        idx = int(kr["idx"])
        ep = kr["endpoint"].strip()
        gid = str(eps[ep])
        assert gid in probes_t2, f"Missing certainty gid {gid} in mc_t2 probes"
        hg = probes_t2[gid]["host_group"]
        assert isinstance(hg, int) and hg > 0, f"Invalid host_group {hg!r} for gid {gid}"
        c2_lab, note = ans[idx]
        assert c2_lab in ("0", "1", "U")
        c1_lab = ""
        aug_idx = (kr.get("august_idx") or "").strip()
        if kr["role"] == "control":
            assert aug_idx and str(aug_sheet[int(aug_idx)]["endpoint_group"]) == gid
            c1_lab = aug_c1[gid]
            ctrl_total += 1
            if c1_lab == c2_lab:
                ctrl_agree += 1
        writer.writerow([
            idx, gid, hg, kr["role"], kr["stratum"],
            kr["weight_rank"], kr["weight"], aug_idx,
            c2_lab, c1_lab, note,
        ])

    certainty_csv_bytes = buf.getvalue().encode("utf-8")
    assert b"://" not in certainty_csv_bytes
    fixed_dt = (2026, 10, 2, 12, 0, 0)
    czip = DIST / "certainty_coding.zip"
    with zipfile.ZipFile(czip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for arc, blob in [
            ("certainty_labels.csv", certainty_csv_bytes),
            ("answers_coder2_set2.csv", ans_path.read_bytes()),
            ("weights_mc_t2.json", weights_path.read_bytes()),
            ("PACKAGE_HASHES.json", hashes_path.read_bytes()),
            ("CERTAINTY_PROTOCOL.md", CERTAINTY_PROTOCOL_MD.encode("utf-8")),
        ]:
            info = zipfile.ZipInfo(arc, fixed_dt)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, blob)
    print(f"  certainty_coding.zip: 5 files, {czip.stat().st_size / 1e3:.1f} KB "
          f"(controls {ctrl_agree}/{ctrl_total})")


def prepare_coding(entries: list[tuple[Path, str]], eps: dict[str, int],
                   tmpdir: Path) -> list[tuple[Path, str]]:
    """Sanitize coding CSVs and regenerate adjudication.md and consensus.csv via mc_adjudicate."""
    out = []
    for src, arc in entries:
        dst = tmpdir / arc
        dst.write_bytes(strip_coding(src.read_bytes(), eps) if arc.endswith(".csv")
                        else src.read_bytes())
        out.append((dst, arc))
    import mc_adjudicate
    print("  coding layer:", end=" ")
    mc_adjudicate.run(tmpdir, tmpdir)
    return out


def need(p: Path) -> Path:
    if not p.is_file():
        errors.append(f"MISSING {p.relative_to(ROOT)}")
    return p


def check_doc(p: Path) -> Path:
    """Shipped text docs must be English and free of local-path markers."""
    need(p)
    if p.is_file():
        txt = p.read_text(encoding="utf-8")
        if CYRILLIC.search(txt):
            errors.append(f"CYRILLIC in shipped doc: {p.name}")
        if LOCAL_MARKERS.search(txt):
            errors.append(f"LOCAL PATH MARKER in shipped doc: {p.name}")
    return p


def census_blocks() -> dict[tuple[str, str], int]:
    """(label, chain) -> observation block from census metadata."""
    out: dict[tuple[str, str], int] = {}
    for chain in CHAINS:
        for meta in sorted((CHAIN_DIR / chain).glob("meta_*.json")):
            m = json.loads(meta.read_text(encoding="utf-8"))
            key = (m.get("label"), chain)
            if key[0] in LABELS:
                if key in out:
                    errors.append(f"DUPLICATE census meta for {key}: block {out[key]} vs {m['observation_block']}")
                out[key] = int(m["observation_block"])
    for label in LABELS:
        missing = [c for c in CHAINS if (label, c) not in out]
        if missing:
            errors.append(f"census meta missing for {label}: {missing}")
    return out


def reputation_blocks(label: str) -> dict[str, int]:
    """chain -> canonical head block, from the run log (retry duplicates stay out)."""
    run = json.loads(need(REP / f"run_reputation_{label}.json").read_text(encoding="utf-8"))
    out = {c["chain"]: int(c["block_read"]) for c in run["chains"]}
    missing = [c for c in CHAINS if c not in out]
    if missing:
        errors.append(f"reputation run {label} missing chains: {missing}")
    return out


def build_zip(name: str, entries: list[tuple[Path, str]], transform) -> None:
    dest = DIST / name
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for src, arc in entries:
            data = transform(name, arc, src)
            if data is None:
                z.write(src, arc)
                continue
            info = zipfile.ZipInfo(arc, time.localtime(src.stat().st_mtime)[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    print(f"  {name}: {len(entries)} files, {dest.stat().st_size / 1e6:.1f} MB")


def main() -> None:
    print(f"output: {DIST.name}/")

    DIST.mkdir(exist_ok=True)
    blocks = census_blocks()
    plan: dict[str, list[tuple[Path, str]]] = {}

    # census
    for label in LABELS:
        entries = [(need(CHAIN_DIR / f"run_{label}.json"), f"run_{label}.json")]
        for chain in CHAINS:
            b = blocks.get((label, chain))
            if b is None:
                continue
            for fn in (f"census_{b}.csv", f"meta_{b}.json"):
                entries.append((need(CHAIN_DIR / chain / fn), f"{chain}/{fn}"))
        plan[f"census_{label}.zip"] = entries

    # panels (t1 lives at the multichain root, t2 in mc_t2/)
    for label, base, extra in (
        ("mc_t1", MC, []),
        ("mc_t2", MC / "mc_t2", ["ipfs_recovery_report.json", "PROBE_METHOD_AUDIT.json"]),
    ):
        names = [f"panel_{c}.csv" for c in CHAINS] + [
            "panel_all.csv", "probes.json", "MC_SUMMARY.json", "MC_ENDPOINT_UNIT.json", *extra,
        ]
        plan[f"panels_{label}.zip"] = [(need(base / n), n) for n in names]

    # mint (same observation blocks as the census)
    for label in LABELS:
        entries = []
        for chain in CHAINS:
            b = blocks.get((label, chain))
            if b is None:
                continue
            for stem in ("mints", "block_ts", "ts_quality", "receipts", "meta"):
                ext = "csv" if stem == "mints" else "json"
                fn = f"{stem}_{b}.{ext}"
                src = MINT / chain / fn
                if stem == "receipts" and not src.is_file():
                    # Incremental scans with zero new mints don't rewrite the receipts
                    # sample; the file of the previous watermark (meta.incremental_from)
                    # covers every mint transaction. Allowed only in that exact case.
                    meta = json.loads((MINT / chain / f"meta_{b}.json").read_text(encoding="utf-8"))
                    prev = meta.get("incremental_from")
                    prev_file = MINT / chain / f"receipts_{prev}.json"
                    if meta.get("new_tokens") == 0 and prev_file.is_file():
                        entries.append((prev_file, f"{chain}/receipts_{prev}.json"))
                        continue
                entries.append((need(src), f"{chain}/{fn}"))
        plan[f"mint_{label}.zip"] = entries

    # reputation (canonical head block per run log)
    for label in LABELS:
        entries = [
            (need(REP / f"run_reputation_{label}.json"), f"run_reputation_{label}.json"),
            (need(REP / "deployment_check.json"), "deployment_check.json"),
        ]
        for chain, b in reputation_blocks(label).items():
            for stem in ("clients", "summary", "meta"):
                ext = "json" if stem == "meta" else "csv"
                fn = f"{stem}_head{b}.{ext}"
                entries.append((need(REP / label / chain / fn), f"{chain}/{fn}"))
        plan[f"reputation_{label}.zip"] = entries

    # coding (+ English rubric)
    coding_names = [
        "coding_sheet.csv", "sample.csv", "coder_1.csv", "coder_2.csv",
        "consensus.csv", "overlap_idx.json",
    ]
    plan["coding.zip"] = [(need(CODING / n), n) for n in coding_names]
    for doc in ("adjudication.md", "CODING_RUBRIC.md"):
        plan["coding.zip"].append((check_doc(CODING / doc), doc))

    loose: list[tuple[Path, str]] = [
        (check_doc(OUT_BASE / "README_SUBMISSION.md"), "README.md"),
        (check_doc(ROOT / "_enrich" / "reports" / "DATA_DICTIONARY_SUBMISSION_EN.md"), "DATA_DICTIONARY.md"),
        (need(MC / "MC_SUMMARY.json"), "MC_SUMMARY_mc_t1.json"),
        (need(MC / "mc_t2" / "MC_SUMMARY.json"), "MC_SUMMARY_mc_t2.json"),
        (need(MC / "probes_s11_repair.json"), "probes_s11_repair.json"),
        (need(MC / "MC_ENDPOINT_UNIT_repaired.json"), "MC_ENDPOINT_UNIT_repaired.json"),
        (need(P2_DIR / "p2_results.json"), "p2_results.json"),
    ]

    if errors:
        print("BUILD ABORTED:")
        for e in errors:
            print(" -", e)
        sys.exit(1)

    total_in = sum(src.stat().st_size for es in plan.values() for src, _ in es)
    print(f"allowlist ok: {sum(len(e) for e in plan.values())} files in zips, "
          f"{len(loose)} loose, {total_in / 1e6:.0f} MB raw")

    census_files = [
        (label, arc.split("/")[0], src)
        for label in LABELS
        for src, arc in plan[f"census_{label}.zip"]
        if "/census_" in arc and arc.endswith(".csv")
    ]
    panel_files = [
        src for label in LABELS
        for src, arc in plan[f"panels_{label}.zip"]
        if arc.startswith("panel_") and arc.endswith(".csv")
    ]
    probe_paths = [src for label in LABELS
                   for src, arc in plan[f"panels_{label}.zip"] if arc == "probes.json"]
    coding_paths = [src for src, arc in plan["coding.zip"] if arc.endswith(".csv")]
    print("indexing:")
    rows_by_label = uri_index(census_files)
    cards = card_index(panel_files)
    eps = endpoint_index(probe_paths, coding_paths,
                         [MC / "probes_s11_repair.json"])
    for p in panel_files:
        with p.open(encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                for t in (r.get("hosts") or "").split("|"):
                    if t:
                        host_group(t)
    for ep in eps:
        host_group(host_of(ep))
    print(f"  host groups: {len(host_group.map):,} distinct host values across both snapshots")
    tmpdir = Path(tempfile.mkdtemp(prefix="mc_coding_"))
    plan["coding.zip"] = prepare_coding(plan["coding.zip"], eps, tmpdir)

    def transform(zip_name, arc, src):
        if arc == "probes.json":
            return strip_probes(src.read_bytes(), eps)
        if arc == "MC_SUMMARY.json":
            return strip_summary(src.read_bytes(), eps)
        if arc == "MC_ENDPOINT_UNIT.json":
            return strip_endpoint_unit(src.read_bytes(), eps)
        if arc == "PROBE_METHOD_AUDIT.json":
            return strip_probe_audit(src.read_bytes(), eps)
        if not arc.endswith(".csv"):
            return None
        label = zip_name.removesuffix(".zip").split("_", 1)[-1]
        if "/census_" in arc:
            return strip_census(src.read_bytes(), arc.split("/")[0],
                                rows_by_label[label])
        if arc.startswith("panel_"):
            return strip_panel(src.read_bytes(), rows_by_label[label], cards)
        return None

    for name, entries in plan.items():
        build_zip(name, entries, transform)
    build_certainty_zip(eps, tmpdir)
    for src, out_name in loose:
        data = src.read_bytes()
        # Loose summaries match the copies inside the panels zips.
        if out_name.startswith("MC_SUMMARY_"):
            data = strip_summary(data, eps)
        elif out_name == "probes_s11_repair.json":
            data = strip_probes(data, eps)
        elif out_name == "MC_ENDPOINT_UNIT_repaired.json":
            data = strip_endpoint_unit(data, eps)
        (DIST / out_name).write_bytes(data)
        print(f"  {out_name}: {len(data) / 1e3:.0f} KB")

    sums = []
    for f in sorted(DIST.iterdir()):
        if f.name == "SHA256SUMS.txt" or not f.is_file():
            continue
        h = hashlib.sha256()
        with open(f, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 22), b""):
                h.update(chunk)
        sums.append(f"{h.hexdigest()}  {f.name}")
    (DIST / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")
    print(f"SHA256SUMS.txt: {len(sums)} entries")
    print("dist total:", f"{sum(f.stat().st_size for f in DIST.iterdir() if f.is_file()) / 1e6:.1f} MB")
    if tmpdir is not None:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
