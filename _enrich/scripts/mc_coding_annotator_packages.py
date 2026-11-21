#!/usr/bin/env python3
"""Render self-contained English HTML annotation sheets from the frozen sample.

Coder 1: 300 items; Coder 2: 100-item overlap (original idx preserved).

    python mc_coding_annotator_packages.py
"""
from __future__ import annotations

import csv
import html
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CODING = ROOT / "_enrich" / "coding"
PACKAGES = CODING / "packages"

SNIP_LIMIT = 2048


def _title_from_html(sn: str) -> str:
    m = re.search(r"<title[^>]*>([^<]{1,120})", sn, re.I)
    return m.group(1).strip() if m else ""


def _meta_content(sn: str, name: str) -> str:
    m = re.search(
        rf'<meta[^>]+(?:name|property)=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']{{1,200}})',
        sn,
        re.I,
    )
    if m:
        return m.group(1).strip()
    m = re.search(
        rf'<meta[^>]+content=["\']([^"\']{{1,200}})["\'][^>]+(?:name|property)=["\']{re.escape(name)}["\']',
        sn,
        re.I,
    )
    return m.group(1).strip() if m else ""


def classify(ct: str, snippet: str) -> str:
    """Format badge derived from Content-Type and body prefix (no interpretive hints)."""
    sn = snippet or ""
    sn_low = sn.lstrip().lower()
    ct_low = (ct or "").lower()
    if "application/json" in ct_low or sn.lstrip().startswith(("{", "[")):
        return "JSON"
    if "text/html" in ct_low or sn_low.startswith("<!doctype") or sn_low.startswith("<html"):
        if "_next/static" in sn or "data-next-head" in sn or "next/static/chunks" in sn:
            return "HTML · SPA shell"
        return "HTML"
    return "other"


def render_body(fmt: str, snippet: str) -> str:
    sn = (snippet or "").replace("\r", " ").replace("\n", " ")
    if not sn.strip():
        return "(empty body)"
    if fmt == "JSON":
        try:
            return json.dumps(json.loads(sn), ensure_ascii=False, indent=2)
        except Exception:
            return sn  # truncated mid-JSON — show as saved
    if fmt.startswith("HTML"):
        lines = []
        title = _title_from_html(sn)
        if title:
            lines.append(f"<title> {title}")
        desc = _meta_content(sn, "description") or _meta_content(sn, "og:description")
        if desc:
            lines.append(f"<meta description> {desc[:160]}{'…' if len(desc) > 160 else ''}")
        if fmt == "HTML · SPA shell":
            n_scripts = len(re.findall(r"<script\b", sn, re.I))
            lines.append("")
            lines.append(
                f"The first 2 KB contain script/style loader markup "
                f"(single-page-app shell, ~{n_scripts} <script> tags); "
                "no readable page content in this snippet."
            )
        else:
            clean = re.sub(r">\s*<", "> <", sn[:700])
            lines.append("")
            lines.append("Beginning of the HTML (shortened):")
            lines.append(clean + ("…" if len(sn) > 700 else ""))
        return "\n".join(lines) if lines else sn[:SNIP_LIMIT]
    return sn[:SNIP_LIMIT]


RUBRIC_HTML = """
<div class='steps'><b>Task.</b> For every item below, decide whether the shown HTTP response is a
<b>machine-readable agent interface</b> or not, and click one label.
<ol>
<li><b>1 — agent_endpoint.</b> The response itself is an artifact of an agent protocol. Any of:
an MCP server reply (JSON-RPC with <code>jsonrpc</code>/<code>result</code>/<code>serverInfo</code>,
a tool list, an <code>initialize</code> response, an MCP SSE stream);
an A2A agent card (object with <code>name</code> and <code>skills</code>/<code>capabilities</code>,
typically at <code>/.well-known/agent-card.json</code>);
an ERC-8004 registration card (<code>type</code> referencing <code>eip-8004</code> /
<code>registration-v1</code>); an OASF descriptor;
or another machine-readable agent-interface declaration — describe it in the note.</li>
<li><b>0 — not_agent.</b> A live response that is not an agent interface: a website or landing page,
documentation, a generic REST root, a health check such as <code>{"status":"ok"}</code>,
an error JSON, a login page, an empty body, or content where <code>mcp</code>/<code>a2a</code>
appears only as prose or inside a URL.</li>
<li><b>U — undecidable.</b> The decision is impossible <i>from this body</i>: the 2 KB cut hits
mid-document and the visible part carries no signal, the body is opaque or binary, or an auth wall
returns no content. Use sparingly: U means "this snippet cannot answer it", not "I am unsure".</li>
</ol></div>
<div class='steps'><b>Edge cases (from the pre-registered rubric).</b>
<table>
<tr><th>Case</th><th>Label</th></tr>
<tr><td>JSON with a <code>tools</code> key that lists shop products / utilities</td><td>0</td></tr>
<tr><td>The word <code>mcp</code> appears only inside a URL or a text description</td><td>0</td></tr>
<tr><td>Valid A2A card with empty <code>skills: []</code></td><td>1</td></tr>
<tr><td>MCP server answering GET with a hint like "use POST /messages"</td><td>1</td></tr>
<tr><td>Project landing page whose HTML happens to contain "a2a"</td><td>0</td></tr>
<tr><td>200 with body <code>{"error":"unauthorized"}</code></td><td>0</td></tr>
</table></div>
<div class='box'><b>How to work.</b> Judge <b>only the HTTP response shown</b> (what our GET
returned) — not whatever may exist elsewhere on the site. Do <b>not</b> visit the URLs; they are
listed as plain text for context only. Each item shows: URL, HTTP status, Content-Type, body
length, a truncation flag, and the first 2 KB of the body. Your clicks autosave in this browser —
you can close the page and continue later <i>in the same browser</i>. When finished, press
<b>Save CSV</b> in the bottom bar and return the file.</div>
"""


def sheet_js(idxs: list[int], storage_key: str, csv_name: str) -> str:
    return (
        "<script>\n"
        f"const IDXS = {json.dumps(idxs)};\n"
        f"const KEY = {json.dumps(storage_key)};\n"
        f"const CSV_NAME = {json.dumps(csv_name)};\n"
        + """
let state = {};
try { state = JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) { state = {}; }

function persist() { localStorage.setItem(KEY, JSON.stringify(state)); refreshBar(); }

function setLabel(idx, val) {
  state[idx] = state[idx] || {};
  state[idx].label = val;
  persist();
  document.querySelectorAll(`.ans[data-idx='${idx}'] button.lb`).forEach(b => {
    b.classList.toggle('on', b.dataset.val === val);
  });
}

function setNote(idx, val) {
  state[idx] = state[idx] || {};
  state[idx].note = val;
  persist();
}

function refreshBar() {
  let done = 0;
  for (const i of IDXS) if (state[i] && state[i].label) done++;
  document.getElementById('prog').textContent = `Labeled: ${done} of ${IDXS.length}`;
  document.getElementById('savebtn').disabled = done === 0;
}

function toCsv() {
  const esc = c => { c = String(c ?? ''); return /[",\\n;]/.test(c) ? '"' + c.replace(/"/g, '""') + '"' : c; };
  const rows = [['idx', 'label', 'note']];
  for (const i of IDXS) {
    const s = state[i] || {};
    rows.push([i, s.label || '', s.note || '']);
  }
  return rows.map(r => r.map(esc).join(',')).join('\\r\\n') + '\\r\\n';
}

async function saveCsv() {
  const csv = toCsv();
  if (window.showSaveFilePicker) {
    try {
      const h = await showSaveFilePicker({
        suggestedName: CSV_NAME,
        types: [{ description: 'CSV', accept: { 'text/csv': ['.csv'] } }],
      });
      const w = await h.createWritable();
      await w.write(csv);
      await w.close();
      flash('Saved. Please return this CSV file when all items are labeled.');
      return;
    } catch (e) { if (e.name === 'AbortError') return; }
  }
  const blob = new Blob(['\\ufeff' + csv], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = CSV_NAME;
  a.click();
  URL.revokeObjectURL(a.href);
  flash('Downloaded to your Downloads folder. Please return this CSV file.');
}

function flash(msg) {
  const el = document.getElementById('flash');
  el.textContent = msg;
  el.style.opacity = 1;
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.style.opacity = 0; }, 6000);
}

window.addEventListener('DOMContentLoaded', () => {
  for (const i of IDXS) {
    const s = state[i] || {};
    if (s.label) {
      document.querySelectorAll(`.ans[data-idx='${i}'] button.lb`).forEach(b => {
        b.classList.toggle('on', b.dataset.val === s.label);
      });
    }
    if (s.note) {
      const inp = document.querySelector(`.ans[data-idx='${i}'] input.note`);
      if (inp) inp.value = s.note;
    }
  }
  refreshBar();
});
</script>
"""
    )


STYLE = (
    "<style>"
    "body{font:15px/1.45 Segoe UI,sans-serif;max-width:980px;margin:1.5rem auto;padding:0 1rem 6rem;color:#111}"
    "pre{background:#f4f5f7;padding:.75rem;border-radius:6px;white-space:pre-wrap;word-break:break-word;"
    "max-height:22rem;overflow:auto;font-size:13px;line-height:1.4}"
    ".m{color:#555;font-size:12px}.ep{color:#334155;font-size:12px;word-break:break-all;font-family:Consolas,monospace}"
    ".fmt{display:inline-block;background:#e8eef7;color:#1a3a6b;padding:.15rem .45rem;border-radius:4px;"
    "font-size:11px;font-weight:600;margin:.3rem 0}"
    "h1{font-size:1.35rem}h3{margin:1.8rem 0 .2rem}hr{border:0;border-top:1px solid #e5e5e5}"
    ".box{background:#fff8e6;border:1px solid #ecd9a0;padding:.8rem 1rem;border-radius:6px;margin:1rem 0;font-size:14px}"
    ".steps{background:#eef6ee;border:1px solid #bcd9bc;padding:.8rem 1rem;border-radius:6px;margin:1rem 0;font-size:14px}"
    ".steps li{margin:.35rem 0}.steps table{border-collapse:collapse;margin:.4rem 0;font-size:13px}"
    ".steps th,.steps td{border:1px solid #bcd9bc;padding:.25rem .6rem;text-align:left}"
    "code{background:#eee;padding:0 .25rem}"
    ".ans{background:#f0f4ff;border:1px solid #c7d4f2;border-radius:6px;padding:.6rem .8rem;margin:.6rem 0}"
    "button.lb{font:600 14px Segoe UI,sans-serif;padding:.4rem .9rem;margin-right:.4rem;border-radius:6px;"
    "border:1.5px solid #94a3b8;background:#fff;cursor:pointer}"
    "button.lb.on{background:#1d4ed8;border-color:#1d4ed8;color:#fff}"
    "input.note{width:100%;margin-top:.5rem;padding:.35rem .5rem;border:1px solid #cbd5e1;border-radius:5px;font:13px Segoe UI}"
    "#bar{position:fixed;left:0;right:0;bottom:0;background:#111827;color:#f9fafb;display:flex;gap:1rem;"
    "align-items:center;padding:.6rem 1.2rem;font-size:14px;flex-wrap:wrap}"
    "#bar button{font:600 13px Segoe UI;padding:.45rem 1rem;border-radius:6px;border:0;cursor:pointer;"
    "background:#22c55e;color:#052e12}#bar button:disabled{background:#4b5563;color:#9ca3af;cursor:default}"
    "#flash{font-size:12.5px;color:#bbf7d0;opacity:0;transition:opacity .4s}</style>"
)


def build_package(coder: str, rows: list[dict], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    idxs = [int(r["idx"]) for r in rows]
    csv_name = f"answers_{coder}.csv"

    display = coder.replace("coder", "Coder ")
    parts = [
        "<!doctype html><meta charset='utf-8'>",
        f"<title>Agent-interface annotation — {display} ({len(rows)} items)</title>",
        STYLE,
        f"<h1>Agent-interface annotation — {display} · {len(rows)} items</h1>",
        RUBRIC_HTML,
    ]
    for r in rows:
        idx = int(r["idx"])
        trunc = str(r["truncated"]) in ("True", "1", "true")
        ct = str(r.get("content_type") or "")
        snippet = (r.get("body_snippet") or "")[:SNIP_LIMIT]
        fmt = classify(ct, snippet)
        parts.append(f"<h3>#{idx}</h3>")
        parts.append(f"<div class='ep'>{html.escape(r['endpoint'])}</div>")
        parts.append(
            f"<div class='m'>HTTP {r['http_status']} · {html.escape(ct)} · "
            f"{r['body_len']} bytes{' · snippet truncated at 2 KB' if trunc else ''}</div>"
        )
        parts.append(f"<div class='fmt'>{html.escape(fmt)}</div>")
        parts.append(f"<pre>{html.escape(render_body(fmt, snippet))}</pre>")
        parts.append(
            f"<div class='ans' data-idx='{idx}'>"
            f"<button class='lb' data-val='1' onclick=\"setLabel({idx},'1')\">1 — agent</button>"
            f"<button class='lb' data-val='0' onclick=\"setLabel({idx},'0')\">0 — not agent</button>"
            f"<button class='lb' data-val='U' onclick=\"setLabel({idx},'U')\">U — undecidable</button>"
            f"<input class='note' type='text' placeholder='note (optional; recommended for borderline calls)' "
            f"oninput=\"setNote({idx},this.value)\"></div><hr>"
        )
    parts.append(
        f"<div id='bar'><span id='prog'>Labeled: 0 of {len(rows)}</span>"
        "<button id='savebtn' onclick='saveCsv()' disabled>Save CSV</button>"
        "<span id='flash'></span></div>"
    )
    parts.append(sheet_js(idxs, f"erc8004_coding_{coder}_v1", csv_name))
    (out_dir / "annotation_sheet.html").write_text("\n".join(parts), encoding="utf-8")

    with (out_dir / csv_name).open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["idx", "label", "note"])
        for i in idxs:
            w.writerow([i, "", ""])


def main() -> None:
    rows = list(csv.DictReader((CODING / "coding_sheet.csv").open(encoding="utf-8")))
    assert len(rows) == 300, f"expected 300 rows, got {len(rows)}"
    overlap = set(json.loads((CODING / "overlap_idx.json").read_text(encoding="utf-8"))["idx"])
    assert len(overlap) == 100

    forbidden = {"stratum", "agentish", "machine_agentish", "label_machine"}
    leak = forbidden & set(rows[0].keys())
    assert not leak, f"blindness violation: coding sheet exposes {leak}"

    build_package("coder1", rows, PACKAGES / "coder1")
    build_package("coder2", [r for r in rows if int(r["idx"]) in overlap], PACKAGES / "coder2")

    print(f"coder1: 300 items -> {PACKAGES / 'coder1'}")
    print(f"coder2: {len(overlap)} overlap items (original idx kept) -> {PACKAGES / 'coder2'}")
    for d in ("coder1", "coder2"):
        for p in sorted((PACKAGES / d).iterdir()):
            print(f"  {d}/{p.name}  {p.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
