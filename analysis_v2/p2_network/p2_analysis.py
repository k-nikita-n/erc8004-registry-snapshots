#!/usr/bin/env python3
"""Aggregate survival, hosting concentration, and TLS/DNS statistics for mc_t3p.

Writes `p2_results.json` (aggregates only; no URLs, hostnames, or token IDs).
"""
from __future__ import annotations

import csv
import hashlib
import json
import statistics as st
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

csv.field_size_limit(10**9)
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "_enrich" / "scripts"))
from r_spec import is_probeable_endpoint  # noqa: E402

MC = ROOT / "_enrich" / "multichain"
T1, T2, T3 = MC / "probes.json", MC / "mc_t2" / "probes.json", MC / "mc_t3p" / "probes.json"
NET = MC / "mc_t3p" / "netlayer.internal.json"
CACHE = MC / "uri_cache"
CENSUS = ROOT / "_chain" / "multichain"
WEIGHTS = HERE / "endpoint_weights.internal.json"
OUT = HERE / "p2_results.json"
T1_DATE, T2_DATE = "2026-08-10", "2026-08-16"

AS_RULES = (("AMAZON", "AWS"), ("GOOGLE", "Google Cloud"), ("MICROSOFT", "Microsoft Azure"),
            ("CLOUDFLARE", "Cloudflare"), ("FASTLY", "Fastly"), ("AKAMAI", "Akamai/Linode"),
            ("LINODE", "Akamai/Linode"), ("DIGITALOCEAN", "DigitalOcean"), ("HETZNER", "Hetzner"),
            ("OVH", "OVH"), ("CHOOPA", "Vultr"), ("VULTR", "Vultr"), ("ORACLE", "Oracle Cloud"),
            ("ALIBABA", "Alibaba Cloud"), ("TENCENT", "Tencent Cloud"), ("CONTABO", "Contabo"),
            ("VERCEL", "Vercel"), ("NETLIFY", "Netlify"), ("FLY", "Fly.io"), ("RENDER", "Render"))
PAAS_HEADERS = (("x-vercel-id", "Vercel"), ("x-nf-request-id", "Netlify"), ("fly-request-id", "Fly.io"),
                ("x-render-origin-server", "Render"), ("x-railway-request-id", "Railway"),
                ("x-github-request-id", "GitHub Pages"))
FRONT_HEADERS = (("cf-ray", "Cloudflare"), ("x-amz-cf-id", "CloudFront"))


def card_of(uri):
    h = hashlib.sha1(uri.encode("utf-8", "replace")).hexdigest()
    p = CACHE / h[:2] / f"{h}.json"
    if not p.exists():
        return None
    try:
        c = json.loads(p.read_text(encoding="utf-8")).get("card")
        return c if isinstance(c, dict) else None
    except Exception:
        return None


def endpoint_weights(t2: dict) -> dict:
    """S9mc tokens per flagged live endpoint and L0 tokens per live unflagged endpoint (mc_t2 walk)."""
    if WEIGHTS.exists():
        return json.loads(WEIGHTS.read_text(encoding="utf-8"))
    frame = {e: r for e, r in t2.items() if r.get("probeable_rspec") == 1 and r.get("live") == 1}
    live_A = {e for e, r in frame.items() if r.get("agentish") == 1}
    live_B = {e for e, r in frame.items() if r.get("agentish") == 0}
    uri_by_key = {}
    for d in sorted(CENSUS.iterdir()):
        if d.is_dir() and (cens := sorted(d.glob("census_*.csv"))):
            with cens[-1].open(encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    if u := (r.get("token_uri") or "").strip():
                        uri_by_key[(d.name, r["agent_id"])] = u
    wA, wB = Counter(), Counter()
    for p in sorted((MC / "mc_t2").glob("panel_*.csv")):
        if p.name == "panel_all.csv":
            continue
        chain = p.stem[len("panel_"):]
        with p.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                u = uri_by_key.get((chain, r["agent_id"]))
                c = card_of(u) if u else None
                if not c:
                    continue
                eps = {str(s.get("endpoint", "")).strip() for s in (c.get("services") or []) if isinstance(s, dict)}
                if r.get("S9mc") == "1":
                    for e in eps & live_A:
                        wA[e] += 1
                else:
                    for e in eps & live_B:
                        wB[e] += 1
    out = {"A": dict(wA), "B": dict(wB)}
    WEIGHTS.write_text(json.dumps(out), encoding="utf-8")
    return out


def hhi(counter: Counter) -> float:
    tot = sum(counter.values())
    return round(sum((v / tot) ** 2 for v in counter.values()) * 10000, 0) if tot else float("nan")


def topk(counter: Counter, k: int) -> float:
    tot = sum(counter.values())
    return round(sum(v for _, v in counter.most_common(k)) / tot, 4) if tot else float("nan")


def provider_of(net: dict | None) -> tuple[str, str, str]:
    """(provider, AS label, fronting)."""
    if not net or net.get("dns_error"):
        return "unresolved", "unresolved", ""
    hdr = net.get("headers") or {}
    names = set(net.get("header_names") or [])
    as_list = net.get("as") or []
    as_label = f"AS{as_list[0]['asn']} {as_list[0]['org']}" if as_list else "no AS match"
    as_org = (as_list[0]["org"] if as_list else "").upper()
    front = next((lab for h, lab in FRONT_HEADERS if h in names), "")
    for h, lab in PAAS_HEADERS:
        if h in names:
            return lab, as_label, front
    for needle, lab in AS_RULES:
        if needle in as_org:
            return lab, as_label, front
    return ("other" if as_list else "no AS match"), as_label, front


def main() -> None:
    t1 = json.loads(T1.read_text(encoding="utf-8"))
    t2 = json.loads(T2.read_text(encoding="utf-8"))
    t3_raw = json.loads(T3.read_text(encoding="utf-8"))
    repair_path = MC / "mc_t3p" / "probes_breaker_repair.json"
    repair = json.loads(repair_path.read_text(encoding="utf-8")) if repair_path.exists() else {}
    t3 = {**t3_raw, **repair}
    net = json.loads(NET.read_text(encoding="utf-8")) if NET.exists() else {}
    W = endpoint_weights(t2)
    wA = W["A"]

    probed = [r.get("probed_utc") for r in t3_raw.values() if r.get("probed_utc")]
    t3_mid = sorted(probed)[len(probed) // 2] if probed else None
    days_23 = (datetime.fromisoformat(t3_mid.replace("Z", "+00:00")) -
               datetime.fromisoformat(T2_DATE).replace(tzinfo=timezone.utc)).days if t3_mid else None
    days_12 = (datetime.fromisoformat(T2_DATE) - datetime.fromisoformat(T1_DATE)).days

    def frame(p, e):
        r = p.get(e) or {}
        return r.get("probeable_rspec") == 1

    # ---- survival ----
    live2 = [e for e, r in t2.items() if frame(t2, e) and r.get("live")]
    ag2 = [e for e in live2 if t2[e].get("agentish")]
    s = {}
    s["t2_live_endpoints"] = len(live2)
    s["t2_live_still_live_t3p"] = round(sum(bool((t3.get(e) or {}).get("live")) for e in live2) / len(live2), 4)
    s["t2_agentlike_endpoints"] = len(ag2)
    s["t2_agentlike_still_agentlike_t3p"] = round(sum(bool((t3.get(e) or {}).get("agentish")) for e in ag2) / len(ag2), 4)
    s["t2_agentlike_still_live_t3p"] = round(sum(bool((t3.get(e) or {}).get("live")) for e in ag2) / len(ag2), 4)
    tokw = sum(wA.get(e, 0) for e in ag2)
    s["t2_agentlike_token_weighted_still_agentlike"] = round(
        sum(wA.get(e, 0) for e in ag2 if (t3.get(e) or {}).get("agentish")) / tokw, 4)
    for name, sel in (("single_token", lambda w: w == 1), ("platform_ge10_tokens", lambda w: w >= 10)):
        grp = [e for e in ag2 if sel(wA.get(e, 0))]
        s[f"agentlike_{name}_n"] = len(grp)
        s[f"agentlike_{name}_still_agentlike"] = (round(sum(bool((t3.get(e) or {}).get("agentish")) for e in grp)
                                                        / len(grp), 4) if grp else None)
    dead2 = [e for e, r in t2.items() if frame(t2, e) and not r.get("live")]
    s["t2_not_live_now_live_t3p"] = round(sum(bool((t3.get(e) or {}).get("live")) for e in dead2) / len(dead2), 4)
    s["days_t2_to_t3p"] = days_23
    live1 = [e for e, r in t1.items() if frame(t1, e) and r.get("live") and e in t2]
    is_breaker = lambda r: str((r or {}).get("error", "")).startswith("host_dead")
    s["breaker_repair"] = {
        "targets_t2_live_cut_at_t3p": len(repair),
        "repair_now_live": sum(1 for r in repair.values() if r.get("live")),
        "repair_now_agentlike": sum(1 for r in repair.values() if r.get("agentish")),
        "repair_breaker_again": sum(1 for r in repair.values() if is_breaker(r)),
        "raw_t2_live_still_live": round(sum(bool((t3_raw.get(e) or {}).get("live")) for e in live2) / len(live2), 4),
        "raw_t2_agentlike_still_agentlike": round(sum(bool((t3_raw.get(e) or {}).get("agentish")) for e in ag2) / len(ag2), 4),
        "t2_agentlike_cut_by_breaker_after_repair": sum(1 for e in ag2 if is_breaker(t3.get(e))),
    }
    s["t1_live_in_t2_frame"] = len(live1)
    s["t1_live_still_live_t2"] = round(sum(bool(t2[e].get("live")) for e in live1) / len(live1), 4) if live1 else None
    nb1 = [e for e in live1 if not is_breaker(t2[e])]
    s["t1_live_still_live_t2_excluding_t2_breaker"] = round(sum(bool(t2[e].get("live")) for e in nb1) / len(nb1), 4)
    s["days_t1_to_t2"] = days_12
    s["hazard_note"] = "loss per window is not proportional to its length (early churn); no exponential half-life"
    b2 = [e for e, r in t2.items() if frame(t2, e) and is_breaker(r)]
    answered = [e for e in b2 if not is_breaker(t3.get(e))]
    s["t2_breaker_cut_followup_at_t3p"] = {
        "t2_breaker_cut_endpoints": len(b2),
        "answered_at_t3p": len(answered),
        "live_at_t3p": sum(1 for e in answered if (t3.get(e) or {}).get("live")),
        "agentlike_at_t3p": sum(1 for e in answered if (t3.get(e) or {}).get("agentish")),
        "cut_again_at_t3p": len(b2) - len(answered),
    }

    # ---- hosting (live agent-like endpoints at t3p; also at t2 for comparison) ----
    def host_key(e):
        u = urlsplit(e)
        return f"{(u.hostname or '').lower()}:{u.port or (443 if u.scheme == 'https' else 80)}:{int(u.scheme == 'https')}"

    hosting = {}
    for label, eps in (("t3p_live_agentlike", [e for e in ag2 if (t3.get(e) or {}).get("agentish")]),
                       ("t2_live_agentlike", ag2),
                       ("t3p_live_unflagged", [e for e, r in t3.items() if frame(t2, e) and r.get("live")
                                               and not r.get("agentish")])):
        by_ep, by_tok, by_as, fronted = Counter(), Counter(), Counter(), Counter()
        hosts_by_prov = defaultdict(set)
        for e in eps:
            prov, as_label, front = provider_of(net.get(host_key(e)))
            by_ep[prov] += 1
            by_tok[prov] += wA.get(e, 0)
            by_as[as_label] += 1
            hosts_by_prov[prov].add(host_key(e))
            fronted[front or "none"] += 1
        by_host = Counter({p: len(h) for p, h in hosts_by_prov.items()})
        hosting[label] = {
            "n_endpoints": len(eps), "n_hosts": sum(by_host.values()),
            "providers_by_endpoints": dict(by_ep.most_common(12)),
            "providers_by_hosts": dict(by_host.most_common(12)),
            "providers_by_s9_tokens": dict(by_tok.most_common(12)) if label != "t3p_live_unflagged" else None,
            "top1_top3_top5_share_endpoints": [topk(by_ep, 1), topk(by_ep, 3), topk(by_ep, 5)],
            "top1_top3_top5_share_hosts": [topk(by_host, 1), topk(by_host, 3), topk(by_host, 5)],
            "hhi_providers_endpoints": hhi(by_ep), "hhi_providers_hosts": hhi(by_host),
            "hhi_as_endpoints": hhi(by_as), "distinct_as": len(by_as),
            "cdn_fronting_endpoints": dict(fronted),
        }

    # ---- TLS / DNS over all probeable declared hosts ----
    eps_per_host = Counter(host_key(e) for e in t2 if is_probeable_endpoint(e))
    tls = {"hosts": len(net), "dns_failure_hosts": sum(1 for r in net.values() if r.get("dns_error"))}
    https = {k: r for k, r in net.items() if r.get("tls") and not r.get("dns_error")}
    ok = {k: r for k, r in https.items() if r.get("tls_version")}
    tls["https_resolving_hosts"] = len(https)
    tls["handshake_ok_share_hosts"] = round(len(ok) / len(https), 4) if https else None
    tls["handshake_ok_share_endpoints"] = round(sum(eps_per_host[k] for k in ok) / sum(eps_per_host[k] for k in https), 4) if https else None
    tls["versions_hosts"] = dict(Counter(r["tls_version"] for r in ok.values()))
    tls["issuers_hosts"] = dict(Counter(r.get("cert_issuer_o") or r.get("cert_issuer_cn") or "?" for r in ok.values()).most_common(10))
    tls["errors_hosts"] = dict(Counter(r.get("tls_error") for r in https.values() if r.get("tls_error")))
    ms = [r["tls_ms"] for r in ok.values() if "tls_ms" in r]
    tls["handshake_ms_median"] = round(st.median(ms), 1) if ms else None
    live_hosts = {host_key(e) for e, r in t3.items() if frame(t2, e) and r.get("live")}
    lh = [net[k] for k in live_hosts if k in net]
    tls["live_hosts_t3p"] = len(lh)
    tls["live_hosts_with_hsts"] = sum(1 for r in lh if "strict-transport-security" in (r.get("header_names") or []))
    tls["live_hosts_http3_alt_svc"] = sum(1 for r in lh if "h3" in str((r.get("headers") or {}).get("alt-svc", "")))

    out = {"generated": "2026-10-02, mc_t3p 47-day endpoint re-probe and network-layer aggregates",
           "t3p_probe_median_utc": t3_mid, "survival": s, "hosting": hosting, "tls_dns": tls}
    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
