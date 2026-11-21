#!/usr/bin/env python3
"""Build the blind 40-item certainty-stratum coding package (20 heaviest + 20 controls).

Renders the HTML sheet via `_enrich/scripts/mc_coding_annotator_packages.py` (items 301-340)
and writes `package_coder2/`, `key.internal.csv`, and `PACKAGE_HASHES.json`.
"""
from __future__ import annotations

import csv
import hashlib
import html
import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
P1 = HERE.parent
ROOT = HERE.parents[2]                                  # repository root
CODING = ROOT / "_enrich" / "coding"
BODIES = ROOT / "_enrich" / "multichain" / "mc_t2" / "probe_bodies"
sys.path.insert(0, str(ROOT / "_enrich" / "scripts"))
import mc_coding_annotator_packages as pkg  # noqa: E402

SEED_CONTROLS = 20261001
SEED_SHUFFLE = 20261002
N_CONTROL = 10
IDX0 = 301
CODER = "coder2_set2"
OUT = HERE / "package_coder2"


def body_row(url: str) -> dict:
    h = hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()
    b = json.loads((BODIES / h[:2] / f"{h}.json").read_text(encoding="utf-8"))
    return {"endpoint": url, "http_status": b.get("http_status", ""), "content_type": b.get("content_type", ""),
            "body_len": b.get("body_len", ""), "truncated": b.get("truncated", ""),
            "body_snippet": (b.get("body_snippet") or "").replace("\r", " ").replace("\n", " ")[:2048]}


def build(rows: list[dict]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    idxs = [int(r["idx"]) for r in rows]
    csv_name = f"answers_{CODER}.csv"
    parts = ["<!doctype html><meta charset='utf-8'>",
             f"<title>Agent-interface annotation — Coder 2, supplementary set ({len(rows)} items)</title>",
             pkg.STYLE,
             f"<h1>Agent-interface annotation — Coder 2 · supplementary set · {len(rows)} items</h1>",
             pkg.RUBRIC_HTML]
    for r in rows:
        idx = int(r["idx"])
        trunc = str(r["truncated"]) in ("True", "1", "true")
        ct = str(r.get("content_type") or "")
        snippet = (r.get("body_snippet") or "")[:pkg.SNIP_LIMIT]
        fmt = pkg.classify(ct, snippet)
        parts += [f"<h3>#{idx}</h3>",
                  f"<div class='ep'>{html.escape(r['endpoint'])}</div>",
                  f"<div class='m'>HTTP {r['http_status']} · {html.escape(ct)} · "
                  f"{r['body_len']} bytes{' · snippet truncated at 2 KB' if trunc else ''}</div>",
                  f"<div class='fmt'>{html.escape(fmt)}</div>",
                  f"<pre>{html.escape(pkg.render_body(fmt, snippet))}</pre>",
                  f"<div class='ans' data-idx='{idx}'>"
                  f"<button class='lb' data-val='1' onclick=\"setLabel({idx},'1')\">1 — agent</button>"
                  f"<button class='lb' data-val='0' onclick=\"setLabel({idx},'0')\">0 — not agent</button>"
                  f"<button class='lb' data-val='U' onclick=\"setLabel({idx},'U')\">U — undecidable</button>"
                  f"<input class='note' type='text' placeholder='note (optional; recommended for borderline calls)' "
                  f"oninput=\"setNote({idx},this.value)\"></div><hr>"]
    parts += [f"<div id='bar'><span id='prog'>Labeled: 0 of {len(rows)}</span>"
              "<button id='savebtn' onclick='saveCsv()' disabled>Save CSV</button>"
              "<span id='flash'></span></div>",
              pkg.sheet_js(idxs, f"erc8004_coding_{CODER}_v1", csv_name)]
    (OUT / "annotation_sheet.html").write_text("\n".join(parts), encoding="utf-8")
    with (OUT / csv_name).open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["idx", "label", "note"])
        for i in idxs:
            w.writerow([i, "", ""])


def main() -> None:
    frame = json.loads((P1 / "certainty_frame_mc_t2.internal.json").read_text(encoding="utf-8"))
    sheet = list(csv.DictReader((CODING / "coding_sheet.csv").open(encoding="utf-8")))
    stratum = {r["endpoint"]: r["stratum"] for r in csv.DictReader((CODING / "sample.csv").open(encoding="utf-8"))}
    overlap = set(json.loads((CODING / "overlap_idx.json").read_text(encoding="utf-8"))["idx"])

    items = []
    for s in ("A", "B"):
        for r in frame[s]:
            assert r["body_file"] and not r["in_coding_sample"], r["rank"]
            items.append({**body_row(r["endpoint"]), "role": "certainty", "stratum": s,
                          "weight_rank": r["rank"], "weight": r["weight"]})

    rng = random.Random(SEED_CONTROLS)
    for s in ("A", "B"):
        pool = sorted((r for r in sheet if stratum[r["endpoint"]] == s and int(r["idx"]) not in overlap),
                      key=lambda r: int(r["idx"]))
        for r in rng.sample(pool, N_CONTROL):
            items.append({k: r[k] for k in ("endpoint", "http_status", "content_type", "body_len", "truncated",
                                            "body_snippet")}
                         | {"role": "control", "stratum": s, "weight_rank": "", "weight": "",
                            "august_idx": r["idx"]})

    assert len({i["endpoint"] for i in items}) == len(items) == 40
    random.Random(SEED_SHUFFLE).shuffle(items)
    for k, it in enumerate(items):
        it["idx"] = IDX0 + k

    forbidden = {"role", "stratum", "weight_rank", "weight", "august_idx", "agentish", "machine_agentish"}
    shown = [{k: it[k] for k in ("idx", "endpoint", "http_status", "content_type", "body_len", "truncated",
                                  "body_snippet")} for it in items]
    assert not forbidden & set(shown[0]), "blindness violation"
    build(shown)

    with (HERE / "key.internal.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["idx", "endpoint", "role", "stratum", "weight_rank", "weight", "august_idx"])
        for it in items:
            w.writerow([it["idx"], it["endpoint"], it["role"], it["stratum"], it["weight_rank"], it["weight"],
                        it.get("august_idx", "")])

    page = (OUT / "annotation_sheet.html").read_text(encoding="utf-8").lower()
    for word in ("certainty", "take-all", "take_all", "heaviest", "stratum", "agentish"):
        assert word not in page, f"coder-facing sheet mentions {word!r}"

    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in (OUT / "annotation_sheet.html", OUT / f"answers_{CODER}.csv", HERE / "key.internal.csv")}
    (HERE / "PACKAGE_HASHES.json").write_text(json.dumps({"built": "2026-09-30", "seeds": {
        "controls": SEED_CONTROLS, "shuffle": SEED_SHUFFLE}, "sha256": hashes}, indent=1), encoding="utf-8")

    roles = {(it["role"], it["stratum"]) for it in items}
    print(f"items: {len(items)}  idx {IDX0}-{IDX0 + len(items) - 1}  groups: {sorted(roles)}")
    print(f"certainty: {sum(it['role'] == 'certainty' for it in items)}  controls: "
          f"{sum(it['role'] == 'control' for it in items)}  snippet chars min "
          f"{min(len(it['body_snippet']) for it in items)}")
    for p in sorted(OUT.iterdir()):
        print(f"  package_coder2/{p.name}  {p.stat().st_size:,} bytes")
    print(json.dumps(hashes, indent=1))


if __name__ == "__main__":
    main()
