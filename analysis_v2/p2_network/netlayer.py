#!/usr/bin/env python3
"""Per-host network-layer pass for mc_t3p (DNS, offline IP-to-ASN, TLS handshake, HEAD headers).

Reads probeable mc_t2 hosts; writes host-level results to `mc_t3p/netlayer.internal.json`
(kept local; only aggregate distributions are published in `p2_results.json`).
"""
from __future__ import annotations

import bisect
import gzip
import ipaddress
import json
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "_enrich" / "scripts"))
from r_spec import is_probeable_endpoint  # noqa: E402

MC = ROOT / "_enrich" / "multichain"
T2 = MC / "mc_t2" / "probes.json"
T3 = MC / "mc_t3p" / "probes.json"
OUT = MC / "mc_t3p" / "netlayer.internal.json"
ASN_URL = "https://iptoasn.com/data/ip2asn-combined.tsv.gz"
ASN_FILE = HERE / "data" / "ip2asn-combined.tsv.gz"
UA = "ERC8004-mc-fetch/1.3"
TIMEOUT = 6
WORKERS = 32
HEADER_KEYS = ("server", "via", "x-powered-by", "cf-ray", "x-vercel-id", "x-amz-cf-id", "x-amzn-requestid",
               "x-served-by", "x-cache", "fly-request-id", "x-nf-request-id", "x-render-origin-server",
               "x-railway-request-id", "x-github-request-id", "x-goog-generation", "x-cloud-trace-context",
               "x-azure-ref", "x-ms-request-id", "alt-svc", "strict-transport-security")


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class AsnTable:
    def __init__(self, path: Path):
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            req = urllib.request.Request(ASN_URL, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                path.write_bytes(r.read())
        self.v4: tuple[list[int], list[int], list[tuple]] = ([], [], [])
        self.v6: tuple[list[int], list[int], list[tuple]] = ([], [], [])
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 5 or parts[2] == "0":
                    continue
                a, b = ipaddress.ip_address(parts[0]), ipaddress.ip_address(parts[1])
                t = self.v4 if a.version == 4 else self.v6
                t[0].append(int(a))
                t[1].append(int(b))
                t[2].append((int(parts[2]), parts[3], parts[4]))

    def lookup(self, ip: str):
        a = ipaddress.ip_address(ip)
        starts, ends, info = self.v4 if a.version == 4 else self.v6
        i = bisect.bisect_right(starts, int(a)) - 1
        if i >= 0 and int(a) <= ends[i]:
            asn, cc, org = info[i]
            return {"asn": asn, "cc": cc, "org": org}
        return None


def tls_class(e: Exception) -> str:
    s = f"{type(e).__name__}: {e}"
    for needle, cls in (("certificate has expired", "expired"), ("Hostname mismatch", "hostname_mismatch"),
                        ("self-signed", "self_signed"), ("self signed", "self_signed"),
                        ("unable to get local issuer", "untrusted_chain"), ("CERTIFICATE_VERIFY_FAILED", "verify_failed"),
                        ("UNRECOGNIZED_NAME", "unrecognized_name"), ("HANDSHAKE_FAILURE", "handshake_failure"),
                        ("timed out", "timeout"), ("refused", "refused"), ("reset", "reset"),
                        ("getaddrinfo", "dns_failure"), ("WRONG_VERSION_NUMBER", "not_tls")):
        if needle.lower() in s.lower():
            return cls
    return "other"


def one_host(host: str, port: int, tls: bool, live_ep: str | None, asn: AsnTable) -> dict:
    rec: dict = {"host": host, "port": port, "tls": tls, "checked_utc": utc()}
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        ips = sorted({i[4][0] for i in infos})
        rec["ipv4"] = [ip for ip in ips if ":" not in ip]
        rec["ipv6"] = [ip for ip in ips if ":" in ip]
        rec["as"] = [x for x in (asn.lookup(ip) for ip in (rec["ipv4"] or rec["ipv6"])[:4]) if x]
    except Exception as e:
        rec["dns_error"] = tls_class(e) if "getaddrinfo" in str(e) else type(e).__name__
        return rec
    if tls:
        ctx = ssl.create_default_context()
        t0 = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=TIMEOUT) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as ss:
                    rec["tls_ms"] = round(1000 * (time.perf_counter() - t0), 1)
                    rec["tls_version"] = ss.version()
                    rec["tls_cipher"] = (ss.cipher() or ("",))[0]
                    cert = ss.getpeercert() or {}
                    issuer = {k: v for part in cert.get("issuer", ()) for k, v in part}
                    rec["cert_issuer_o"] = issuer.get("organizationName", "")
                    rec["cert_issuer_cn"] = issuer.get("commonName", "")
                    rec["cert_not_before"] = cert.get("notBefore", "")
                    rec["cert_not_after"] = cert.get("notAfter", "")
        except Exception as e:
            rec["tls_error"] = tls_class(e)
    if live_ep:
        try:
            req = urllib.request.Request(live_ep, method="HEAD", headers={"User-Agent": UA, "Accept": "application/json, */*"})
            with urllib.request.urlopen(req, timeout=TIMEOUT + 2, context=ssl.create_default_context()) as r:
                rec["head_status"], hdrs = r.status, r.headers
        except urllib.error.HTTPError as e:
            rec["head_status"], hdrs = e.code, e.headers
        except Exception as e:
            rec["head_error"] = tls_class(e)
            hdrs = None
        if hdrs is not None:
            rec["header_names"] = sorted({k.lower() for k in hdrs.keys()})
            rec["headers"] = {k: str(hdrs.get(k))[:120] for k in HEADER_KEYS if hdrs.get(k) is not None}
    return rec


def main() -> None:
    t2 = json.loads(T2.read_text(encoding="utf-8"))
    t3 = json.loads(T3.read_text(encoding="utf-8"))
    assert len(t3) == len(t2), "run reprobe.py to completion first"
    hosts: dict[tuple[str, int, bool], str | None] = {}
    for ep in sorted(t2):
        if not is_probeable_endpoint(ep):
            continue
        u = urlsplit(ep)
        if u.scheme not in ("http", "https") or not u.hostname:
            continue
        key = (u.hostname.lower(), u.port or (443 if u.scheme == "https" else 80), u.scheme == "https")
        hosts.setdefault(key, None)
        if (t3.get(ep) or {}).get("live") and hosts[key] is None:
            hosts[key] = ep
    results = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    todo = [k for k in hosts if f"{k[0]}:{k[1]}:{int(k[2])}" not in results]
    print(f"hosts={len(hosts)} live_at_t3p={sum(v is not None for v in hosts.values())} todo={len(todo)}", flush=True)
    asn = AsnTable(ASN_FILE)
    print(f"asn table: v4={len(asn.v4[0])} v6={len(asn.v6[0])} ranges", flush=True)
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(one_host, h, p, t, hosts[(h, p, t)], asn): (h, p, t) for h, p, t in todo}
        for fut in as_completed(futs):
            h, p, t = futs[fut]
            results[f"{h}:{p}:{int(t)}"] = fut.result()
            done += 1
            if done % 100 == 0 or done == len(todo):
                tmp = OUT.with_suffix(".tmp")
                tmp.write_text(json.dumps(results), encoding="utf-8")
                tmp.replace(OUT)
                print(f"netlayer progress {done}/{len(todo)}", flush=True)
    print(f"DONE hosts={len(results)}", flush=True)


if __name__ == "__main__":
    main()
