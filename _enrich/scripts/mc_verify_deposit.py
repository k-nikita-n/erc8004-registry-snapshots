"""Offline integrity verification for the Zenodo v2.0.0 deposit (75 checks).

Verifies SHA-256 checksums, census/panel joins, structural stub invariants,
surrogate-key schemas, probe/repair layers, blind coding layers, 47-day
aggregates, and cross-snapshot consistency without external data or RPC access.

    python mc_verify_deposit.py
    MC_DIST=/path/to/dist python mc_verify_deposit.py

Exits non-zero if any check fails.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIST = Path(os.environ.get("MC_DIST")
            or ROOT / "submission" / "Zenodo" / "dist_submission")
if not DIST.is_dir():
    sys.exit(f"deposit directory not found: {DIST}\n"
             "Unpack the Zenodo record (or point MC_DIST at the directory that "
             "holds its files) and re-run.")
HERE = Path(__file__).resolve().parent

csv.field_size_limit(1 << 30)

sys.path.insert(0, str(HERE))
from mc_deposit_shape import SERVICE_VOCAB as PRODUCED_VOCAB  # noqa: E402

LABELS = ("mc_t1", "mc_t2")
PROTOCOL = frozenset({"mcp", "a2a", "oasf"})
# The builder's vocabulary plus the bucket it puts everything else in.
SERVICE_VOCAB = PRODUCED_VOCAB | {"other"}
STUB = ("uri_scheme", "uri_media_type", "uri_encoding", "uri_bytes")

passed = failed = 0
problems: list[str] = []


def check(ok: bool, what: str, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok    {what}")
    else:
        failed += 1
        problems.append(f"{what}: {detail}")
        print(f"  FAIL  {what}  {detail}")


def members(name: str) -> zipfile.ZipFile:
    return zipfile.ZipFile(DIST / name)


def rows_of(zf: zipfile.ZipFile, member: str) -> list[dict[str, str]]:
    return list(csv.DictReader(
        io.StringIO(zf.read(member).decode("utf-8"), newline="")))


def checksums() -> None:
    print("\n=== 1. checksums ===")
    listed = {}
    for line in (DIST / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, name = line.split(None, 1)
            listed[name.strip()] = digest
    present = {p.name for p in DIST.iterdir()
               if p.is_file() and p.name != "SHA256SUMS.txt"}
    check(present == set(listed), "every file is listed and every listing exists",
          f"only on disk {sorted(present - set(listed))}, "
          f"only listed {sorted(set(listed) - present)}")
    bad = []
    for name, digest in listed.items():
        h = hashlib.sha256()
        with (DIST / name).open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 22), b""):
                h.update(chunk)
        if h.hexdigest() != digest:
            bad.append(name)
    check(not bad, f"{len(listed)} checksums match", f"mismatched: {bad}")


def census(label: str) -> dict[str, list[dict[str, str]]]:
    """chain -> census rows, checked against the per-chain meta as we go."""
    out: dict[str, list[dict[str, str]]] = {}
    with members(f"census_{label}.zip") as zf:
        metas = {i.filename.split("/")[0]: json.loads(zf.read(i))
                 for i in zf.infolist() if "/meta_" in i.filename}
        for info in zf.infolist():
            if not info.filename.endswith(".csv"):
                continue
            chain = info.filename.split("/")[0]
            out[chain] = rows = rows_of(zf, info.filename)
            m = metas[chain]
            n_uri = sum(1 for r in rows if r["has_token_uri"] == "1")
            n_wallet = sum(1 for r in rows
                           if (r["agent_wallet"] or "").lower() not in
                           ("", (r["owner"] or "").lower()))
            ok = (m["population"] == len(rows) and m["n_has_token_uri"] == n_uri
                  and m["n_wallet_custom_neq_owner"] == n_wallet)
            if not ok:
                check(False, f"[{label}/{chain}] census agrees with its meta",
                      f"rows {len(rows)}/{m['population']}, uri {n_uri}/"
                      f"{m['n_has_token_uri']}, wallet {n_wallet}/"
                      f"{m['n_wallet_custom_neq_owner']}")
                return out
    check(True, f"[{label}] all 13 census files agree with their meta "
                f"on population, URI count and custom-wallet count")
    return out


def stubs(label: str, cens: dict[str, list[dict[str, str]]]) -> None:
    print(f"\n=== stub internal consistency, {label} ===")
    bad_empty = bad_group = bad_media = 0
    for rows in cens.values():
        for r in rows:
            has = r["has_token_uri"] == "1"
            if has != (int(r["uri_bytes"]) > 0):
                bad_empty += 1
            if has != bool(r["uri_group"]):
                bad_group += 1
            if (r["uri_scheme"] == "data") != bool(r["uri_media_type"]):
                bad_media += 1
    n = sum(len(v) for v in cens.values())
    check(bad_empty == 0, f"[{label}] has_token_uri agrees with uri_bytes > 0 on "
                          f"{n:,} rows", f"{bad_empty} disagree")
    check(bad_group == 0, f"[{label}] a group id is present exactly when the field is",
          f"{bad_group} disagree")
    check(bad_media == 0, f"[{label}] a media type is present exactly for data: URIs",
          f"{bad_media} disagree")


def partition(label: str, cens: dict[str, list[dict[str, str]]]) -> None:
    """Rows sharing a uri_group came from one identical field, so their stubs must
    be identical too. This is the check that a surrogate key is a partition and not
    a relabelling."""
    seen: dict[str, tuple] = {}
    impure = 0
    for rows in cens.values():
        for r in rows:
            g = r["uri_group"]
            if not g:
                continue
            shape = (r["uri_scheme"], r["uri_media_type"], r["uri_encoding"],
                     r["uri_bytes"])
            if seen.setdefault(g, shape) != shape:
                impure += 1
    check(impure == 0, f"[{label}] every uri_group block is uniform in all four stub "
                       f"fields ({len(seen):,} blocks)", f"{impure} rows break it")


def panels(label: str, cens: dict[str, list[dict[str, str]]]) -> list[dict[str, str]]:
    print(f"\n=== panel against census and against itself, {label} ===")
    with members(f"panels_{label}.zip") as zf:
        allrows = rows_of(zf, "panel_all.csv")
        per_chain = {i.filename[len("panel_"):-4]: rows_of(zf, i.filename)
                     for i in zf.infolist()
                     if i.filename.startswith("panel_") and i.filename != "panel_all.csv"}
        summary = json.loads(zf.read("MC_SUMMARY.json"))

    check(sum(len(v) for v in per_chain.values()) == len(allrows),
          f"[{label}] panel_all.csv is exactly the union of the 13 per-chain panels",
          f"{sum(len(v) for v in per_chain.values())} vs {len(allrows)}")
    check(summary["n_agents"] == len(allrows),
          f"[{label}] MC_SUMMARY.n_agents matches the panel",
          f"{summary['n_agents']} vs {len(allrows)}")

    mismatch = missing = 0
    for chain, rows in per_chain.items():
        cmap = {r["agent_id"]: r for r in cens.get(chain, [])}
        for r in rows:
            c = cmap.get(r["agent_id"])
            if c is None:
                missing += 1
            elif c["owner"] != r["owner"] or any(c[f] != r[f] for f in STUB):
                mismatch += 1
    check(missing == 0, f"[{label}] every panel row has its census row",
          f"{missing} without one")
    check(mismatch == 0, f"[{label}] owner and all four stub fields agree between "
                         f"census and panel on {len(allrows):,} rows",
          f"{mismatch} disagree")

    bad_c = sum(1 for r in allrows
                if int(r["C"]) != (1 if r["B"] == "1"
                                   and set(r["service_classes"].split("|")) & PROTOCOL
                                   else 0))
    check(bad_c == 0, f"[{label}] flag C is reproducible from service_classes on "
                      f"{len(allrows):,} rows", f"{bad_c} disagree")

    vocab = {t for r in allrows if r["service_classes"]
             for t in r["service_classes"].split("|")}
    check(vocab <= SERVICE_VOCAB, f"[{label}] service_classes uses the closed vocabulary",
          f"unexpected: {sorted(vocab - SERVICE_VOCAB)}")

    tiers = Counter(r["agency_evidence_tier"] for r in allrows)
    check(dict(tiers) == summary["tiers"],
          f"[{label}] the tier histogram matches MC_SUMMARY", f"{dict(tiers)}")
    check(sum(1 for r in allrows if r["protocol_use_binary"] == "1")
          == summary["protocol_use_n"],
          f"[{label}] protocol-use count matches MC_SUMMARY")

    # card_group is assigned whenever a JSON document parsed (B=1 or b0_reason=card_not_8004).
    orphan = sum(1 for r in allrows if r["card_group"]
                 and r["B"] != "1" and r["b0_reason"] != "card_not_8004")
    check(orphan == 0, f"[{label}] a card_group appears only where a card parsed",
          f"{orphan} rows have one after a failed fetch or unparsable body")
    return allrows


def probes(label: str, panel: list[dict[str, str]]) -> set[str]:
    print(f"\n=== probe layer, {label} ===")
    with members(f"panels_{label}.zip") as zf:
        pr = json.loads(zf.read("probes.json"))
    check(all(k == str(v["endpoint_group"]) for k, v in pr.items()),
          f"[{label}] probes.json is keyed by its own endpoint_group")
    leaked = sum(1 for v in pr.values()
                 for x in v.values() if isinstance(x, str) and "://" in x)
    check(leaked == 0, f"[{label}] no probe record carries a URL", f"{leaked} do")
    bad_hg = [k for k, v in pr.items()
              if "host" in v or (v.get("host_group") is not None
                                 and (not isinstance(v["host_group"], int) or v["host_group"] <= 0))]
    check(not bad_hg, f"[{label}] probe host_group values are positive integers or null "
                      f"with no raw hostnames ({len(pr):,} records)", f"{len(bad_hg)} bad")
    bad_err = [k for k, v in pr.items()
               if isinstance(v.get("error"), str) and v["error"].startswith("host_dead:")]
    check(not bad_err, f"[{label}] no host_dead error embeds a hostname", f"{len(bad_err)} do")
    # Cached probes from rewritten cards may hold host_groups absent from the panel.
    known = {h for r in panel if r["host_groups"] for h in r["host_groups"].split("|")}
    bad_panel_hg = [h for h in known if not h.isdigit() or int(h) <= 0]
    check(not bad_panel_hg, f"[{label}] all panel host_groups entries are positive integers "
                            f"({len(known):,} distinct)", f"{len(bad_panel_hg)} bad")
    orphan = {str(v["host_group"]) for v in pr.values()
              if v.get("host_group") is not None and str(v["host_group"]) not in known}
    print(f"        {len(orphan)} probed host_groups no longer named by any panel row")
    return {str(k) for k in pr}


def summaries(label: str) -> None:
    """Verify JSON summaries are URL-free and join probes.json on endpoint_group and host_group."""
    print(f"\n=== summary reports, {label} ===")
    with members(f"panels_{label}.zip") as zf:
        docs = {i.filename: zf.read(i.filename).decode("utf-8")
                for i in zf.infolist() if i.filename.endswith(".json")}
    by_gid = {k: v for k, v in json.loads(docs["probes.json"]).items()}

    leaky = [n for n, txt in docs.items()
             if n != "ipfs_recovery_report.json" and "://" in txt]
    check(not leaky, f"[{label}] no JSON report in the panels zip carries a URL",
          f"{leaky}")

    zipped = json.loads(docs["MC_SUMMARY.json"])
    loose = json.loads((DIST / f"MC_SUMMARY_{label}.json").read_text(encoding="utf-8"))
    check(loose == zipped, f"[{label}] the loose summary equals the zipped one")

    def joins(entries, what):
        bad = [e for e in entries
               if str(e.get("endpoint_group")) not in by_gid
               or "host" in e
               or by_gid[str(e["endpoint_group"])]["host_group"] != e.get("host_group")]
        check(not bad, f"[{label}] {what} joins the probe layer by endpoint_group and host_group "
                       f"({len(entries)} entries)", f"{len(bad)} do not")

    joins(zipped["S11mc"]["top_endpoints"], "the summary's top-endpoint list")
    joins(json.loads(docs["MC_ENDPOINT_UNIT.json"])["top_endpoints"],
          "the endpoint-unit top list")
    if "PROBE_METHOD_AUDIT.json" in docs:
        joins(json.loads(docs["PROBE_METHOD_AUDIT.json"]).get("rows", []),
              "the probe-method audit sample")


def repair() -> None:
    """Verify the mc_t1 s11 repair layer and recompute MC_ENDPOINT_UNIT_repaired.json counts."""
    print("\n=== s11 repair layer, mc_t1 ===")
    if not (DIST / "probes_s11_repair.json").is_file():
        check(False, "the s11 repair layer ships with the deposit",
              "probes_s11_repair.json missing")
        return
    rep = json.loads((DIST / "probes_s11_repair.json").read_text(encoding="utf-8"))
    unit = json.loads((DIST / "MC_ENDPOINT_UNIT_repaired.json").read_text(encoding="utf-8"))
    with members("panels_mc_t1.zip") as zf:
        t1 = json.loads(zf.read("probes.json"))
        summary = json.loads(zf.read("MC_SUMMARY.json"))

    check(all(k == str(v["endpoint_group"]) for k, v in rep.items()),
          "the repair file is keyed by its own endpoint_group")
    leaked = sum(1 for v in rep.values()
                 for x in v.values() if isinstance(x, str) and "://" in x)
    check(leaked == 0, "no repair record carries a URL", f"{leaked} do")
    bad_hg = [k for k, v in rep.items()
              if "host" in v or (v.get("host_group") is not None
                                 and (not isinstance(v["host_group"], int) or v["host_group"] <= 0))]
    check(not bad_hg, f"repair host_group values are positive integers with no raw hostnames "
                      f"({len(rep):,} records)", f"{len(bad_hg)} bad")
    check(len(rep) == unit["candidates_probed"],
          f"candidate count matches the repair summary ({unit['candidates_probed']:,})",
          f"file has {len(rep):,}")
    ag = sum(1 for v in rep.values() if v.get("agentish") == 1)
    check(ag == unit["measured_agentish_now"],
          f"agent-like-now recomputes from the records ({ag:,})",
          f"summary says {unit['measured_agentish_now']:,}")
    known = {k for k, v in t1.items() if v.get("agentish") == 1}
    ctrl = {k for k in rep if k in known}
    check(len(ctrl) == unit["control_survived_recovery"]["n"],
          f"the control group re-derives from the reconstruction ({len(ctrl):,})",
          f"summary says {unit['control_survived_recovery']['n']:,}")
    exact = [k for k in ctrl if t1[k].get("recovered") == "exact"]
    drift = [k for k in exact if rep[k].get("agentish") != 1]
    check(len(exact) == unit["control_survived_recovery"]["exact_provenance_n"]
          and not drift,
          f"zero drift on the {len(exact)} exactly-pinned controls",
          f"{len(drift)} of {len(exact)} flipped")
    check(unit["published_S11mc"] == summary["S11mc"]["distinct_live_endpoints"],
          "the pre-loss S11mc in the repair summary equals MC_SUMMARY's",
          f"{unit['published_S11mc']} vs {summary['S11mc']['distinct_live_endpoints']}")


def coding(probe_groups: set[str]) -> None:
    print("\n=== coding layer ===")
    with members("coding.zip") as zf:
        names = {i.filename for i in zf.infolist()}
        sheet = rows_of(zf, "coding_sheet.csv")
        c1 = rows_of(zf, "coder_1.csv")
        c2 = rows_of(zf, "coder_2.csv")
        overlap = json.loads(zf.read("overlap_idx.json"))["idx"]
        tmp = Path(tempfile.mkdtemp(prefix="mc_verify_"))
        zf.extractall(tmp)

    check("body_snippet" not in (sheet[0] if sheet else {}),
          "the response bodies are not shipped")
    check(all("endpoint_group" in r for r in sheet),
          "items are identified by endpoint_group")
    absent = {r["endpoint_group"] for r in sheet} - probe_groups
    check(not absent, f"every coded item joins to a probe record ({len(sheet)} items)",
          f"{len(absent)} do not")

    lab1 = {r["endpoint_group"]: r["label"].strip() for r in c1}
    lab2 = {r["endpoint_group"]: r["label"].strip() for r in c2}
    idx2ep = {int(r["idx"]): r["endpoint_group"] for r in sheet}
    pairs = [(lab1[idx2ep[i]], lab2[idx2ep[i]]) for i in overlap]
    # Cohen's kappa over binary decisions (0/1); U items counted separately.
    binary = [(a, b) for a, b in pairs if a in ("0", "1") and b in ("0", "1")]
    agree = sum(1 for a, b in binary if a == b)
    po = agree / len(binary)
    m1, m2 = Counter(a for a, _ in binary), Counter(b for _, b in binary)
    pe = sum(m1[c] * m2[c] for c in ("0", "1")) / (len(binary) ** 2)
    kappa = (po - pe) / (1 - pe)
    u = sum(1 for _a, b in pairs if b == "U")
    print(f"        {len(pairs)} overlap items, {u} coder-2 U, {len(binary)} binary "
          f"pairs, agreement {agree}/{len(binary)}, kappa = {kappa:.3f}")
    check(len(pairs) == 100 and round(kappa, 2) == 0.94,
          "Cohen's kappa recomputes to the reported 0.94 from the raw label files",
          f"got {kappa:.4f} on {len(binary)} binary pairs")

    before = {n: hashlib.sha256((tmp / n).read_bytes()).hexdigest()
              for n in ("adjudication.md", "consensus.csv")}
    out = Path(tempfile.mkdtemp(prefix="mc_verify_out_"))
    r = subprocess.run([sys.executable, str(HERE / "mc_adjudicate.py"),
                        "--in", str(tmp), "--out", str(out)],
                       capture_output=True, text=True)
    same = r.returncode == 0 and all(
        hashlib.sha256((out / n).read_bytes()).hexdigest() == before[n] for n in before)
    check(same, "re-running mc_adjudicate.py on the deposit reproduces "
                "adjudication.md and consensus.csv byte for byte",
          r.stderr.strip()[:200])
    check({"CODING_RUBRIC.md", "adjudication.md"} <= names,
          "the rubric and the journal ship with the labels")


def certainty_coding(probe_groups: set[str]) -> None:
    print("\n=== certainty-stratum coding layer (v2.0.0) ===")
    czip = DIST / "certainty_coding.zip"
    if not czip.is_file():
        check(False, "certainty_coding.zip ships with the deposit", "missing")
        return
    with members("certainty_coding.zip") as zf:
        names = {i.filename for i in zf.infolist()}
        rows = rows_of(zf, "certainty_labels.csv")
        raw_ans = {int(r["idx"]): r["label"].strip().upper()
                   for r in csv.DictReader(io.StringIO(zf.read("answers_coder2_set2.csv").decode("utf-8-sig")))}
        wts = json.loads(zf.read("weights_mc_t2.json"))
        hashes = json.loads(zf.read("PACKAGE_HASHES.json"))
    with members("panels_mc_t2.zip") as zf:
        probes_t2 = json.loads(zf.read("probes.json"))
    with members("coding.zip") as zf:
        aug_c1 = {r["endpoint_group"]: r["label"].strip() for r in rows_of(zf, "coder_1.csv")}

    check({"certainty_labels.csv", "answers_coder2_set2.csv", "weights_mc_t2.json",
           "PACKAGE_HASHES.json", "CERTAINTY_PROTOCOL.md"} <= names,
          "certainty_coding.zip carries all five expected files")
    leaked = sum(1 for r in rows for v in r.values() if isinstance(v, str) and "://" in v)
    check(leaked == 0, "certainty_labels.csv carries no URLs", f"{leaked} found")
    check(len(rows) == 40, "certainty_labels.csv has 40 items (20 certainty + 20 controls)",
          f"got {len(rows)}")
    bad_join = [r["idx"] for r in rows
                if r["endpoint_group"] not in probes_t2
                or "host" in r
                or str(probes_t2[r["endpoint_group"]]["host_group"]) != r.get("host_group")]
    check(not bad_join, "every certainty/control item joins mc_t2 probes.json on endpoint_group and host_group",
          f"{len(bad_join)} mismatch")
    bad_ans = [r["idx"] for r in rows if raw_ans.get(int(r["idx"])) != r["coder2_label"]]
    check(not bad_ans, "certainty_labels.csv matches unmodified answers_coder2_set2.csv on all 40 rows",
          f"{len(bad_ans)} mismatch")
    ctrls = [r for r in rows if r["role"] == "control"]
    agree = sum(1 for r in ctrls if r["coder2_label"] == aug_c1.get(r["endpoint_group"]))
    check(len(ctrls) == 20 and agree == 20,
          "all 20 control items agree 20/20 with coder 1 from coding.zip",
          f"{agree}/{len(ctrls)}")
    check(wts["checks"]["S9mc"] == 5990 and wts["checks"]["L0"] == 17683
          and len(wts["w_A"]) == 3147 and len(wts["w_B"]) == 16861
          and "sha256" in hashes,
          "weights_mc_t2.json totals and PACKAGE_HASHES.json match mc_t2")


def p2_network() -> None:
    print("\n=== 47-day re-probe and network aggregates (v2.0.0) ===")
    p = DIST / "p2_results.json"
    if not p.is_file():
        check(False, "p2_results.json ships with the deposit", "missing")
        return
    txt = p.read_text(encoding="utf-8")
    check("://" not in txt, "p2_results.json carries no URLs")
    d = json.loads(txt)
    check({"survival", "hosting", "tls_dns"} <= set(d),
          "p2_results.json carries survival, hosting, and tls_dns blocks")
    check(d["survival"]["t2_live_endpoints"] == 20008
          and d["survival"]["t2_agentlike_endpoints"] == 3147
          and d["tls_dns"]["hosts"] == 1287,
          "p2_results.json denominators match mc_t2")


def churn(t1: list[dict[str, str]], t2: list[dict[str, str]]) -> None:
    print("\n=== the two snapshots against each other ===")
    a = {(r["chain"], r["agent_id"]): r for r in t1}
    common = [(a[k], r) for r in t2 if (k := (r["chain"], r["agent_id"])) in a]
    check(len(common) == len(t1),
          f"every registration at t1 is still present at t2 ({len(common):,})",
          f"{len(t1) - len(common)} vanished")
    uri = sum(1 for x, y in common if x["uri_group"] != y["uri_group"])
    card = sum(1 for x, y in common if x["card_group"] != y["card_group"])
    new = len(t2) - len(common)
    print(f"        {new:,} new registrations, {uri:,} rewrote the field, "
          f"{card:,} changed card")
    check(uri <= card, "a field rewrite is rarer than a card change, as it must be "
                       "when most cards live behind a URL",
          f"field {uri}, card {card}")


def main() -> int:
    if not DIST.is_dir():
        print(f"no deposit at {DIST}")
        return 2
    print(f"deposit: {DIST}")
    checksums()
    panel = {}
    probe_groups: set[str] = set()
    for label in LABELS:
        print(f"\n=== 2. census, {label} ===")
        cens = census(label)
        stubs(label, cens)
        partition(label, cens)
        panel[label] = panels(label, cens)
        probe_groups |= probes(label, panel[label])
        summaries(label)
    repair()
    coding(probe_groups)
    certainty_coding(probe_groups)
    p2_network()
    churn(panel["mc_t1"], panel["mc_t2"])

    print("\n" + "=" * 62)
    print(f"checks passed: {passed}/{passed + failed}")
    for p in problems:
        print(" -", p)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
