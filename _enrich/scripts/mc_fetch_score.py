#!/usr/bin/env python3
"""Multichain: fetch unique tokenURIs → flags A–G → tier/score (CODEBOOK v1.1).

Rate-limited fetch with per-host pacing and retry (status `throttled`), never a
permanent circuit breaker; R_spec filters D′/E′ (S8mc/S9mc/S10mc); b0_reason
records why B≠1. Resumable URI cache under _enrich/multichain/uri_cache/.
G uses wallet≠owner + supportedTrust only (no cross-chain feedback census).
"""
from __future__ import annotations

import argparse
import atexit
import base64
import csv
import hashlib
import json
import os
import queue
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from r_spec import filter_probeable, is_probeable_endpoint

ROOT = Path(__file__).resolve().parents[2]
MC_ROOT = ROOT / "_chain" / "multichain"
OUT = ROOT / "_enrich" / "multichain"
CACHE = OUT / "uri_cache"
PROBE_BODIES = OUT / "probe_bodies"
REPORTS = ROOT / "_enrich" / "reports"
SCRIPT_VERSION = "mc_fetch_score_v1.7.2"  # IPFS: gateway HTML is not a final non_json
CODEBOOK_VERSION = "2026-08-09-v1.1"
SENSITIVITY_SPEC = "v1.3"

TIMEOUT = 8
UA = "ERC8004-mc-fetch/1.3"
CTX = ssl.create_default_context()
FETCH_WORKERS = 32  # lower: rate-limit friendly
PROBE_WORKERS = 48
PROBE_ROUND_GAP_S = 0.4  # spacing between repeated probes of the same endpoint
PROBE_BODY_SNIPPET = 2048  # bytes of response body kept for manual coding
# Per-host probe pacing and 429 retry parameters.
PROBE_HOST_MIN_INTERVAL_S = 0.4
PROBE_429_RETRIES = 3
PROBE_429_BACKOFF_S = 4.0
CACHE_MAX_AGE_S = 0  # 0 = cached cards never expire; set via --cache-max-age-h
# Log to run.log / PROBE_PROGRESS.json so non-interactive runs cannot block on stdout.
LOG_PATH: Path | None = None
_LOG_LOCK = threading.Lock()
_LOG_Q: queue.Queue[str | None] = queue.Queue(maxsize=256)


def _log_printer() -> None:
    while True:
        msg = _LOG_Q.get()
        if msg is None:
            return
        try:
            print(msg, flush=True)
        except Exception:
            pass


threading.Thread(target=_log_printer, name="log-printer", daemon=True).start()


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if LOG_PATH is not None:
        try:
            with _LOG_LOCK:
                with LOG_PATH.open("a", encoding="utf-8") as f:
                    f.write(f"{ts} {msg}\n")
        except Exception:
            pass
    try:
        _LOG_Q.put_nowait(msg)
    except queue.Full:
        pass


def write_json_atomic(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
# Soft throttle: after N consecutive fails, wait HOST_COOLDOWN_S before next try.
# Never permanently skip a host. IPFS gateways are never cooled down as a group.
HOST_FAIL_SOFT = 6
HOST_COOLDOWN_S = 8.0
HOST_MIN_INTERVAL_S = 0.05  # ~20 req/s/host max
# In retry pass: after this many consecutive fails on a host, mark remaining URIs
# fetch_failed (host_dead) instead of spinning for hours on SSL-dead hosts.
HOST_RETRY_GIVEUP = 8
RETRY_TIMEOUT = 4  # shorter probe when testing if a host is dead
IPFS_GATEWAYS = (
    "https://ipfs.io/ipfs/",
    "https://cloudflare-ipfs.com/ipfs/",
    "https://gateway.pinata.cloud/ipfs/",
    "https://dweb.link/ipfs/",
    "https://w3s.link/ipfs/",
)
# Never apply host cooldown to these keys (rotate gateways instead)
NO_COOLDOWN_HOSTS = frozenset({"ipfs"}) | {
    urllib.parse.urlparse(g).netloc.lower() for g in IPFS_GATEWAYS
}
# HTML / challenge pages from a gateway are retrieval failures, not card content.
_GATEWAY_JUNK_HEAD = (
    "<!doctype",
    "<html",
    "<head",
    "<!--",
)
_GATEWAY_JUNK_MARKERS = (
    "just a moment",
    "cf-browser-verification",
    "attention required",
    "checking your browser",
    "enable javascript and cookies",
    "cloudflare",
)
_GATEWAY_JUNK_STATUS = frozenset({401, 403, 429, 502, 503, 504, 520, 521, 522, 523, 524})
RUN_LABEL = "mc_t1"
AGENT_PROTOCOL = frozenset({"mcp", "a2a", "oasf"})
PROBE_SERVICES = frozenset({"mcp", "a2a", "oasf", "web"})
AGENTISH_KEYS = frozenset({"tools", "skills", "capabilities", "agentcard", "mcpservers"})
ALLOWLIST = frozenset(
    {
        "marketplace.olas.network",
        "gateway.autonolas.tech",
        "mesh.heurist.xyz",
        "mesh-data.heurist.xyz",
        "heurist.xyz",
        "useotto.xyz",
        "minara.ai",
        "x402.minara.ai",
        "app.virtuals.io",
        "acp.virtuals.io",
        "alphakek.ai",
        "api.alphakek.ai",
        "ethyai.app",
        "chat.ethyai.app",
    }
)
# B≠1 detail → still assumption_only
ASSUMPTION_TIERS = frozenset(
    {"none", "uri_unresolved", "fetch_failed", "throttled", "non_json", "card_not_8004"}
)

_host_state: dict[str, dict] = {}  # host -> {fails, next_ok, last_t}
_host_lock = threading.Lock()


def uri_hash(u: str) -> str:
    return hashlib.sha1(u.encode("utf-8", "replace")).hexdigest()


def cache_path(h: str) -> Path:
    return CACHE / h[:2] / f"{h}.json"


def cache_is_stale(h: str) -> bool:
    """True when the cached card is older than CACHE_MAX_AGE_S (0 = never stale).

    At T2 the card content itself is part of the measurement, so a warm cache
    from T1 would silently report T1 metadata under a T2 label.
    """
    if not CACHE_MAX_AGE_S:
        return False
    p = cache_path(h)
    try:
        return (time.time() - p.stat().st_mtime) > CACHE_MAX_AGE_S
    except OSError:
        return False


def load_cache(h: str) -> dict | None:
    p = cache_path(h)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_cache(h: str, obj: dict) -> None:
    p = cache_path(h)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def http_get_full(
    url: str, timeout: float | None = None
) -> tuple[int | None, bytes | None, str, str | None]:
    """Like http_get but also returns the Content-Type header."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json, */*"})
    except (ValueError, TypeError) as e:
        return None, None, "", f"bad_url: {e}"[:200]
    to = TIMEOUT if timeout is None else timeout
    try:
        with urllib.request.urlopen(req, timeout=to, context=CTX) as resp:
            ct = str(resp.headers.get("Content-Type") or "")
            return int(getattr(resp, "status", None) or resp.getcode()), resp.read(2_000_000), ct, None
    except urllib.error.HTTPError as e:
        try:
            body = e.read(200_000)
        except Exception:
            body = None
        ct = ""
        try:
            ct = str(e.headers.get("Content-Type") or "")
        except Exception:
            pass
        return e.code, body, ct, f"HTTP {e.code}"
    except Exception as e:
        return None, None, "", str(e)[:200]


def http_get(url: str, timeout: float | None = None) -> tuple[int | None, bytes | None, str | None]:
    status, body, _ct, err = http_get_full(url, timeout)
    return status, body, err


def _looks_like_gateway_junk(text: str, status: int | None) -> bool:
    if status in _GATEWAY_JUNK_STATUS:
        return True
    head = (text or "")[:400].lstrip().lower()
    if not head:
        return True
    if head.startswith(_GATEWAY_JUNK_HEAD):
        return True
    return any(m in head for m in _GATEWAY_JUNK_MARKERS)


def _cid_from_uri(uri: str) -> str | None:
    """ipfs://CID or a known HTTP gateway URL → CID. Anything else → None."""
    if not uri:
        return None
    low = uri.lower()
    if low.startswith("ipfs://"):
        cid = uri[7:]
        if cid.lower().startswith("ipfs/"):
            cid = cid[5:]
        cid = cid.split("?", 1)[0].strip("/")
        return cid or None
    try:
        p = urllib.parse.urlparse(uri)
    except Exception:
        return None
    path = p.path or ""
    if "/ipfs/" in path:
        cid = path.split("/ipfs/", 1)[1].split("?", 1)[0].strip("/")
        return cid or None
    host = (p.netloc or "").lower()
    for suf in (".ipfs.dweb.link", ".ipfs.cf-ipfs.com"):
        if host.endswith(suf):
            return host[: -len(suf)] or None
    return None


def _ipfs_gateway_urls(cid: str) -> list[tuple[str, str]]:
    gws = list(IPFS_GATEWAYS)
    off = int(hashlib.md5(cid.encode()).hexdigest(), 16) % len(gws)
    gws = gws[off:] + gws[:off]
    return [("ipfs", gw + cid) for gw in gws]


def cache_is_ipfs_html(rec: dict | None) -> bool:
    """True when a cached IPFS entry holds gateway HTML/challenge content instead of JSON."""
    if not rec:
        return False
    st = rec.get("uri_status")
    uri = str(rec.get("uri") or "")
    via = str(rec.get("resolved_via") or "")
    err = str(rec.get("error") or "")
    raw = str(rec.get("raw_head") or "")
    ipfsish = (
        uri.lower().startswith("ipfs://")
        or via == "ipfs"
        or via.startswith("ipfs:")
        or bool(_cid_from_uri(uri))
    )
    if not ipfsish:
        return False
    if st == "non_json":
        return True
    if st == "fetch_failed" and ("gateway_html" in err or _looks_like_gateway_junk(raw, rec.get("http_status"))):
        return True
    return False


def _host_key(uri: str) -> str:
    if uri.startswith("http://") or uri.startswith("https://"):
        try:
            netloc = (urllib.parse.urlparse(uri).netloc or "").lower()
        except Exception:
            return ""
        if not netloc or " " in netloc or '"' in netloc or "\n" in netloc:
            return ""
        return netloc
    if uri.lower().startswith("ipfs://"):
        return "ipfs"
    return ""


def _host_wait(host: str, min_interval: float = HOST_MIN_INTERVAL_S) -> str | None:
    """Block until host is allowed. Returns 'throttled' if soft-cooldown was applied."""
    if not host or host in NO_COOLDOWN_HOSTS:
        return None
    throttled = None
    while True:
        with _host_lock:
            st = _host_state.setdefault(host, {"fails": 0, "next_ok": 0.0, "last_t": 0.0})
            now = time.monotonic()
            wait = max(0.0, st["next_ok"] - now, st["last_t"] + min_interval - now)
            if wait <= 0:
                st["last_t"] = now
                return throttled
            if st["fails"] >= HOST_FAIL_SOFT and st["next_ok"] > now:
                throttled = "throttled"
        time.sleep(min(wait, 1.0))


def _host_note(host: str, ok: bool) -> None:
    if not host or host in NO_COOLDOWN_HOSTS:
        return
    with _host_lock:
        st = _host_state.setdefault(host, {"fails": 0, "next_ok": 0.0, "last_t": 0.0})
        if ok:
            st["fails"] = 0
            st["next_ok"] = 0.0
        else:
            st["fails"] = st.get("fails", 0) + 1
            if st["fails"] >= HOST_FAIL_SOFT:
                # exponential-ish cooldown, capped
                cool = min(60.0, HOST_COOLDOWN_S * (1.5 ** min(st["fails"] - HOST_FAIL_SOFT, 6)))
                st["next_ok"] = time.monotonic() + cool


def resolve_uri(
    uri: str,
    *,
    allow_throttle_skip: bool = False,
    skip_host_wait: bool = False,
    timeout: float | None = None,
) -> dict:
    """Return cache record: uri_status, card?, error?, resolved_via?"""
    if not uri or "\x00" in uri:
        return {"uri_status": "fetch_failed", "error": "empty_or_null_bytes"}
    for marker in ("data:", "https://", "http://", "ipfs://"):
        idx = uri.find(marker)
        if idx > 0:
            uri = uri[idx:]
            break
    host = _host_key(uri)

    if allow_throttle_skip and host and host not in NO_COOLDOWN_HOSTS:
        with _host_lock:
            st = _host_state.get(host, {})
            if st.get("fails", 0) >= HOST_FAIL_SOFT and st.get("next_ok", 0) > time.monotonic():
                return {"uri_status": "throttled", "error": f"host_soft_cooldown:{host}"}

    if uri.startswith("data:"):
        try:
            header, payload = uri.split(",", 1)
        except ValueError:
            return {"uri_status": "fetch_failed", "error": "bad data uri"}
        if ";base64" in header.lower():
            try:
                raw = base64.b64decode(payload)
            except Exception as e:
                return {"uri_status": "fetch_failed", "error": f"b64: {e}"}
        else:
            raw = urllib.parse.unquote_to_bytes(payload)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return {"uri_status": "fetch_failed", "error": "data not utf-8"}
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as e:
            return {"uri_status": "non_json", "error": str(e)[:120], "raw_head": text[:200]}
        if not isinstance(obj, dict):
            return {"uri_status": "non_json", "error": "not object"}
        return {"uri_status": "data", "card": obj, "resolved_via": "data:"}

    urls: list[tuple[str, str]] = []
    cid = _cid_from_uri(uri)
    if cid:
        urls = _ipfs_gateway_urls(cid)
    elif uri.startswith("http://") or uri.startswith("https://"):
        if not host:
            return {"uri_status": "fetch_failed", "error": "bad_http_host"}
        urls.append(("http", uri))
    else:
        return {"uri_status": "fetch_failed", "error": f"unsupported scheme: {uri[:48]}"}

    last = "no attempt"
    last_non_json: dict | None = None
    html_n = 0
    rotate = len(urls) > 1
    for via, url in urls:
        if not skip_host_wait:
            thr = _host_wait(host)
            if thr == "throttled" and allow_throttle_skip:
                return {"uri_status": "throttled", "error": f"host_soft_cooldown:{host}"}
        status, body, err = http_get(url, timeout=timeout)
        if body is None:
            last = err or "empty"
            _host_note(host, False)
            continue
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            last = "not utf-8"
            _host_note(host, False)
            continue
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            rec = {
                "uri_status": "non_json",
                "http_status": status,
                "error": "json parse",
                "resolved_via": via,
                "raw_head": text[:200],
            }
            if not rotate:
                _host_note(host, True)
                return rec
            if _looks_like_gateway_junk(text, status):
                last = f"gateway_html:{status}"
                html_n += 1
            else:
                last_non_json = rec
            _host_note(host, False)
            continue
        if not isinstance(obj, dict):
            rec = {
                "uri_status": "non_json",
                "http_status": status,
                "error": "not object",
                "resolved_via": via,
            }
            if not rotate:
                _host_note(host, True)
                return rec
            last_non_json = rec
            _host_note(host, False)
            continue
        _host_note(host, True)
        return {
            "uri_status": "resolved",
            "http_status": status,
            "card": obj,
            "resolved_via": f"{via}:{url[:120]}",
        }
    if last_non_json and html_n == 0:
        return last_non_json
    if html_n and last_non_json is None:
        return {"uri_status": "fetch_failed", "error": f"all_gateways_html n={html_n} last={last}"}
    if last_non_json:
        return last_non_json
    return {"uri_status": "fetch_failed", "error": last}


def schema_ok(card: dict) -> bool:
    t = str(card.get("type", "")).lower()
    if "eip-8004" not in t and "registration-v1" not in t:
        return False
    name, desc = card.get("name"), card.get("description")
    if not isinstance(name, str) or not name.strip():
        return False
    if not isinstance(desc, str) or not desc.strip():
        return False
    return True


def services_of(card: dict | None) -> list[dict]:
    if not card:
        return []
    s = card.get("services")
    if not isinstance(s, list):
        return []
    return [x for x in s if isinstance(x, dict)]


def probe_endpoints(services: list[dict]) -> list[str]:
    out, seen = [], set()
    for svc in services:
        if str(svc.get("name", "")).strip().lower() not in PROBE_SERVICES:
            continue
        ep = str(svc.get("endpoint", "")).strip()
        if not ep.startswith("http"):
            continue
        if ep not in seen:
            seen.add(ep)
            out.append(ep)
    return out


def agentish(body: bytes, ct: str) -> bool:
    try:
        text = body.decode("utf-8", "replace")
    except Exception:
        return False
    low = text.lower()
    if (ct or "").lower().startswith("application/json") or text.strip()[:1] in "{[":
        try:
            obj = json.loads(text)
        except Exception:
            obj = None
        if isinstance(obj, dict) and any(k.lower() in AGENTISH_KEYS for k in obj):
            return True
        if '"mcp"' in low or '"a2a"' in low:
            return True
        if re.search(r'"(tools|skills|capabilities|agentCard|mcpServers)"\s*:', text):
            return True
    return False


def save_probe_body(url: str, status: int | None, ct: str, body: bytes | None) -> str:
    """Persist a snapshot of the round-1 response for human coding of `agentish`.

    Kept outside probes.json: that file is rewritten on every checkpoint, so
    inlining ~2 KB per endpoint would cost gigabytes of rewrites per run.
    """
    h = hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()
    p = PROBE_BODIES / h[:2] / f"{h}.json"
    raw = body or b""
    rec = {
        "endpoint": url,
        "fetched_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "http_status": status,
        "content_type": ct,
        "body_len": len(raw),
        "body_sha256": hashlib.sha256(raw).hexdigest() if raw else "",
        "body_snippet": raw[:PROBE_BODY_SNIPPET].decode("utf-8", "replace"),
        "truncated": len(raw) > PROBE_BODY_SNIPPET,
    }
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except OSError:
        return ""
    return rec["body_sha256"]


def probe_fetch(url: str) -> tuple[int | None, bytes | None, str, str | None, int]:
    """Per-host paced HTTP GET with 429 retries; returns (status, body, ct, err, rate_limited)."""
    host = _host_key(url)
    status = body = None
    ct, err = "", None
    with _host_lock:
        st = _host_state.get(host) or {}
        if host and st.get("fails", 0) >= HOST_RETRY_GIVEUP:
            return None, None, "", f"host_dead:{host}", 0
    for attempt in range(PROBE_429_RETRIES + 1):
        _host_wait(host, PROBE_HOST_MIN_INTERVAL_S)
        status, body, ct, err = http_get_full(url, timeout=RETRY_TIMEOUT)
        if status == 429:
            _host_note(host, False)
            if attempt < PROBE_429_RETRIES:
                time.sleep(PROBE_429_BACKOFF_S * (attempt + 1))
                continue
            return status, body, ct, err, 1
        if status is not None:
            _host_note(host, True)
            return status, body, ct, err, 0
        _host_note(host, False)
        return status, body, ct, err, 0
    return status, body, ct, err, 1


def probe_one(url: str, rounds: int = 1, gap_s: float = PROBE_ROUND_GAP_S, save_body: bool = False) -> dict:
    """Probe an endpoint `rounds` times.

    Canonical live/agentish always come from round 1 so a multi-round T2 stays
    comparable with the single-round mc_t1 baseline. Majority vote is reported
    separately as *_mv (variant S6mc in SENSITIVITY_SPEC v1.3).
    """
    obs: list[dict] = []
    body_sha = ""
    throttled_rounds = 0
    for i in range(max(1, rounds)):
        if i:
            time.sleep(gap_s)
        status, body, ct, err, was_throttled = probe_fetch(url)
        throttled_rounds += was_throttled
        live = bool(body is not None and status and 200 <= status < 300)
        rec = {
            "live": int(live),
            # ct is deliberately NOT passed: mc_t1 scored with ct="" and
            # feeding the real header here would silently loosen E.
            "agentish": int(live and body is not None and agentish(body, "")),
            "http_status": status,
            "error": err or "",
        }
        obs.append(rec)
        if i == 0 and save_body:
            body_sha = save_probe_body(url, status, ct, body)
        # A host that did not answer will not start answering in 0.4s. Waiting
        # out two more timeouts only burns the pool; S6mc is still 0/3.
        if status is None and not was_throttled:
            while len(obs) < max(1, rounds):
                obs.append(dict(rec))
            break
    first = obs[0]
    n = len(obs)
    live_mv = int(sum(o["live"] for o in obs) * 2 > n)
    agentish_mv = int(sum(o["agentish"] for o in obs) * 2 > n)
    return {
        "endpoint": url,
        "live": first["live"],
        "agentish": first["agentish"],
        "http_status": first["http_status"],
        "error": first["error"],
        "probeable_rspec": int(is_probeable_endpoint(url)),
        "rounds": n,
        "live_mv": live_mv,
        "agentish_mv": agentish_mv,
        "live_votes": sum(o["live"] for o in obs),
        "agentish_votes": sum(o["agentish"] for o in obs),
        "unstable": int(live_mv != first["live"] or agentish_mv != first["agentish"]),
        "throttled_rounds": throttled_rounds,
        "body_sha256": body_sha,
    }


def host_of(u: str) -> str:
    if not u or u.startswith("data:") or u.lower().startswith("ipfs://"):
        return ""
    try:
        h = (urllib.parse.urlparse(u).netloc or "").lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def card_hash(card: dict) -> str:
    name = str(card.get("name", "")).strip().lower()
    desc = str(card.get("description", "")).strip().lower()
    eps = sorted(
        str(s.get("endpoint", "")).strip().lower()
        for s in services_of(card)
        if str(s.get("endpoint", "")).strip()
    )
    blob = json.dumps({"n": name, "d": desc, "e": eps}, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()


def tier_of(A, B, C, D, E, b0_reason: str = "uri_unresolved") -> str:
    if A != 1:
        return "none"
    if B != 1:
        # Prefer specific reason; keep uri_unresolved only as fallback
        if b0_reason in ("fetch_failed", "throttled", "non_json", "card_not_8004"):
            return b0_reason
        return "uri_unresolved"
    if E == 1 or (C == 1 and D == 1):
        return "strong"
    if C == 1:
        return "moderate"
    return "weak"


def score_of(A, B, C, D, E, F, G) -> float:
    s = (1 if A else 0) + (2 if B else 0) + (2 if C else 0) + (1 if D else 0) + (2 if E else 0)
    s += 0.5 if F else 0
    s += 1 if G else 0
    return min(10.0, round(s, 1))


def trust_ok(card: dict | None) -> bool:
    if not card:
        return False
    st = card.get("supportedTrust")
    if isinstance(st, list):
        return len(st) > 0
    if isinstance(st, str):
        return bool(st.strip())
    return bool(st)


def b0_reason_of(uri_rec: dict | None, card: dict | None) -> str:
    if not uri_rec:
        return "fetch_failed"
    st = uri_rec.get("uri_status", "fetch_failed")
    if st == "throttled":
        return "throttled"
    if st == "fetch_failed":
        return "fetch_failed"
    if st == "non_json":
        return "non_json"
    if st in ("resolved", "data"):
        if card and not schema_ok(card):
            return "card_not_8004"
        if not card:
            return "fetch_failed"
        return "card_not_8004"
    return "fetch_failed"


def cache_needs_refetch(rec: dict | None, mode: str) -> bool:
    if not rec or not rec.get("uri_status"):
        return True
    if mode == "none":
        return False
    st = rec.get("uri_status")
    err = str(rec.get("error", ""))
    if mode == "circuit":
        return st == "throttled" or (
            st == "fetch_failed"
            and (
                err.startswith("host_circuit_open:")
                or err.startswith("host_soft_cooldown:")
                or err.startswith("host_dead:")
                or "circuit" in err
            )
        )
    if mode == "throttled":
        return st == "throttled" or (
            st == "fetch_failed"
            and (
                err.startswith("host_circuit_open:")
                or err.startswith("host_soft_cooldown:")
                or err.startswith("host_dead:")
            )
        )
    if mode == "failed":
        return st in ("fetch_failed", "throttled")
    if mode == "ipfs_html":
        return cache_is_ipfs_html(rec)
    return False


CENSUS_USED: dict[str, dict] = {}


def _census_for_dir(d: Path) -> tuple[Path | None, dict]:
    """Prefer the census whose meta.label matches RUN_LABEL so a labelled
    rescore cannot silently pick a later (or earlier) file by mtime."""
    picked: list[tuple[Path, dict]] = []
    for mp in d.glob("meta_*.json"):
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if RUN_LABEL == "mc_t1":
            if m.get("label") not in (None, "", "mc_t1"):
                continue
        elif m.get("label") != RUN_LABEL:
            continue
        blk = m.get("observation_block")
        cf = d / f"census_{blk}.csv" if blk is not None else None
        if cf is not None and cf.exists():
            picked.append((cf, m))
    if picked:
        return max(picked, key=lambda x: x[1].get("observation_block") or 0)
    cens = sorted(d.glob("census_*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    metas = sorted(d.glob("meta_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not cens:
        return None, {}
    meta = json.loads(metas[0].read_text(encoding="utf-8")) if metas else {}
    return cens[0], meta


def load_censuses() -> list[dict]:
    rows = []
    for d in sorted(MC_ROOT.iterdir()):
        if not d.is_dir():
            continue
        cf, meta = _census_for_dir(d)
        if not cf:
            continue
        CENSUS_USED[d.name] = {
            "file": cf.name,
            "label": meta.get("label"),
            "observation_block": meta.get("observation_block"),
            "block_read_spread": meta.get("block_read_spread"),
            "timestamp_utc": meta.get("timestamp_utc"),
        }
        print(f"  census[{d.name}] {cf.name} label={meta.get('label')}", flush=True)
        with cf.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows.append(
                    {
                        "chain": d.name,
                        "agent_id": int(r["agent_id"]),
                        "owner": (r.get("owner") or "").lower(),
                        "token_uri": (r.get("token_uri") or "").strip(),
                        "agent_wallet": (r.get("agent_wallet") or "").lower(),
                        "observation_block": meta.get("observation_block"),
                    }
                )
    return rows


def _fetch_batch(todo: list[str], workers: int, label: str, allow_throttle_skip: bool) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not todo:
        return out

    def one(u: str) -> tuple[str, dict]:
        try:
            rec = resolve_uri(u, allow_throttle_skip=allow_throttle_skip)
        except Exception as e:
            rec = {"uri_status": "fetch_failed", "error": f"resolve_crash: {e}"[:200]}
        rec["uri"] = u[:500]
        save_cache(uri_hash(u), rec)
        return u, rec

    def rank(u: str) -> int:
        if u.startswith("data:"):
            return 0
        if u.startswith("http://") or u.startswith("https://"):
            return 1
        if u.lower().startswith("ipfs://"):
            return 2
        return 3

    todo = sorted(todo, key=rank)
    done = 0
    batch_size = max(workers * 4, 128)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i in range(0, len(todo), batch_size):
            batch = todo[i : i + batch_size]
            futs = [ex.submit(one, u) for u in batch]
            for fut in as_completed(futs):
                u, rec = fut.result()
                out[u] = rec
                done += 1
                if done % 500 == 0 or done == len(todo):
                    st = Counter(r.get("uri_status") for r in out.values())
                    cool = sum(
                        1
                        for h, s in _host_state.items()
                        if s.get("fails", 0) >= HOST_FAIL_SOFT
                        and s.get("next_ok", 0) > time.monotonic()
                    )
                    log(
                        f"{label} progress {done}/{len(todo)} statuses={dict(st)} "
                        f"hosts_cooling={cool}"
                    )
    return out


def phase_resolve(
    unique_uris: list[str],
    workers: int,
    refetch_mode: str = "none",
) -> dict[str, dict]:
    """uri -> cache record. Resumable. Ends with retry pass for throttled."""
    CACHE.mkdir(parents=True, exist_ok=True)
    print("resolve: indexing cache…", flush=True)
    cached_hashes = {p.stem for p in CACHE.rglob("*.json") if not p.name.endswith(".tmp")}
    print(f"resolve: cache_index={len(cached_hashes)}", flush=True)
    out: dict[str, dict] = {}
    todo = []
    for u in unique_uris:
        h = uri_hash(u)
        if h in cached_hashes and not cache_is_stale(h):
            cached = load_cache(h)
            if cached and cached.get("uri_status") and not cache_needs_refetch(cached, refetch_mode):
                out[u] = cached
                continue
        todo.append(u)
    print(
        f"resolve: cached={len(out)} todo={len(todo)} refetch_mode={refetch_mode}",
        flush=True,
    )

    # Pass 1: fetch, may mark throttled under soft cooldown
    got = _fetch_batch(todo, workers, "resolve", allow_throttle_skip=True)
    out.update(got)

    # Pass 2: retry throttled / soft-cooldown host-by-host with give-up
    retry = [
        u
        for u, r in out.items()
        if r.get("uri_status") == "throttled"
        or (
            r.get("uri_status") == "fetch_failed"
            and any(
                str(r.get("error", "")).startswith(p)
                for p in (
                    "host_circuit_open:",
                    "host_soft_cooldown:",
                    "host_dead:",
                )
            )
        )
    ]
    if retry:
        by_host: dict[str, list[str]] = defaultdict(list)
        for u in retry:
            by_host[_host_key(u) or "_none"].append(u)
        print(
            f"resolve: retry pass n={len(retry)} hosts={len(by_host)} "
            f"(give-up after {HOST_RETRY_GIVEUP} consecutive fails/host)",
            flush=True,
        )
        with _host_lock:
            _host_state.clear()

        for host, uris in sorted(by_host.items(), key=lambda x: -len(x[1])):
            print(f"retry host={host!r} n={len(uris)}", flush=True)
            # Quick liveness sample: no host-wait, short timeout
            sample_n = min(HOST_RETRY_GIVEUP, len(uris))
            sample_ok = 0
            for u in uris[:sample_n]:
                try:
                    rec = resolve_uri(
                        u,
                        allow_throttle_skip=False,
                        skip_host_wait=True,
                        timeout=RETRY_TIMEOUT,
                    )
                except Exception as e:
                    rec = {"uri_status": "fetch_failed", "error": f"resolve_crash: {e}"[:200]}
                rec["uri"] = u[:500]
                save_cache(uri_hash(u), rec)
                out[u] = rec
                if rec.get("uri_status") in ("resolved", "data", "non_json"):
                    sample_ok += 1
            if sample_ok == 0:
                for u2 in uris[sample_n:]:
                    rec = {
                        "uri_status": "fetch_failed",
                        "error": f"host_dead:{host}",
                        "uri": u2[:500],
                    }
                    save_cache(uri_hash(u2), rec)
                    out[u2] = rec
                print(
                    f"  give-up host={host!r}: 0/{sample_n} sample ok; "
                    f"marked_remaining={len(uris)-sample_n}",
                    flush=True,
                )
                continue
            # Host is alive — fetch the rest with normal waits
            print(f"  host live ({sample_ok}/{sample_n}); fetching rest…", flush=True)
            rest = uris[sample_n:]
            got = _fetch_batch(rest, max(4, workers // 4), f"retry:{host[:24]}", allow_throttle_skip=False)
            out.update(got)
            ok = sum(1 for r in ([out[u] for u in uris]) if r.get("uri_status") in ("resolved", "data", "non_json"))
            print(f"  done host={host!r} ok≈{ok}/{len(uris)}", flush=True)

    return out


def phase_probe(
    uri_recs: dict[str, dict],
    workers: int,
    *,
    out_dir: Path,
    rounds: int = 1,
    reuse: str = "auto",
    reuse_path: Path | None = None,
    save_bodies: bool = False,
) -> dict[str, dict]:
    """endpoint -> probe result.

    Probes ALL candidate endpoints (needed for raw D/E / S0mc). R_spec applied at score time.
    `reuse="auto"` keeps cached results only when they have at least `rounds`
    observations; `reuse="none"` forces a fresh probe (required at T2, where
    liveness is the measurement and stale results would be meaningless).
    """
    eps: set[str] = set()
    for rec in uri_recs.values():
        card = rec.get("card")
        if isinstance(card, dict):
            for ep in probe_endpoints(services_of(card)):
                eps.add(ep)
    with _host_lock:
        _host_state.clear()
    log(
        f"probe: unique endpoints={len(eps)} rounds={rounds} reuse={reuse} save_bodies={save_bodies}"
    )

    probes_path = out_dir / "probes.json"
    results: dict[str, dict] = {}
    src = reuse_path or probes_path
    if reuse != "none" and src.exists():
        try:
            prev = json.loads(src.read_text(encoding="utf-8"))
            if isinstance(prev, dict):
                for ep in eps:
                    p = prev.get(ep)
                    if not isinstance(p, dict) or "live" not in p:
                        continue
                    if int(p.get("rounds", 1)) < rounds:
                        continue
                    if save_bodies and "body_sha256" not in p:
                        continue  # cached before snapshots existed → re-probe
                    results[ep] = p
                log(f"probe: reused {len(results)}/{len(eps)} from {src.name}")
        except Exception as e:
            log(f"probe: reuse failed ({e}), probing fresh")

    reused_n = len(results)
    todo = [ep for ep in eps if ep not in results]
    # Prefer probeable first (R_spec), docs/github last — fail fast for S9mc
    todo.sort(key=lambda u: (0 if is_probeable_endpoint(u) else 1, u))
    log(f"probe: todo={len(todo)}")
    out_dir.mkdir(parents=True, exist_ok=True)
    progress_path = out_dir / "PROBE_PROGRESS.json"

    def flush(done: int) -> None:
        write_json_atomic(probes_path, results)
        write_json_atomic(
            progress_path,
            {
                "done_new": done,
                "todo": len(todo),
                "saved_total": len(results),
                "reused": reused_n,
                "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )
        log(f"probe progress {done}/{len(todo)} saved={len(results)}")

    if todo:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(probe_one, ep, rounds, PROBE_ROUND_GAP_S, save_bodies): ep for ep in todo}
            done = 0
            for fut in as_completed(futs):
                r = fut.result()
                results[r["endpoint"]] = r
                done += 1
                if done % 50 == 0 or done == len(todo):
                    flush(done)
    else:
        flush(0)
    write_json_atomic(probes_path, results)
    return results


def flags_for_agent(row: dict, uri_rec: dict | None, probes: dict[str, dict]) -> dict:
    A = 1 if row["token_uri"] else 0
    empty = {
        "A": A,
        "B": 0,
        "C": 0,
        "D": None,
        "E": None,
        "D_prime": None,
        "E_prime": None,
        "F": None,
        "G": 0,
        "uri_status": "absent" if A == 0 else "fetch_failed",
        "b0_reason": "none" if A == 0 else "fetch_failed",
        "hosts": "",
        "card_hash": "",
        "service_classes": "",
    }
    if A == 0:
        return empty
    if not uri_rec:
        return empty

    status = uri_rec.get("uri_status", "fetch_failed")
    card = uri_rec.get("card") if isinstance(uri_rec.get("card"), dict) else None
    B = 1 if card and schema_ok(card) else 0
    b0 = b0_reason_of(uri_rec, card) if B != 1 else ""
    svcs = services_of(card)
    names = [str(s.get("name", "")).strip().lower() for s in svcs]
    C = 1 if B and any(n in AGENT_PROTOCOL for n in names) else 0
    eps = probe_endpoints(svcs) if B else []
    eps_p = filter_probeable(eps) if eps else []

    def de(ep_list, live_key="live", agent_key="agentish"):
        if not ep_list:
            return None, None
        lives = [probes.get(ep, {}).get(live_key, 0) for ep in ep_list]
        agents = [probes.get(ep, {}).get(agent_key, 0) for ep in ep_list]
        return (1 if any(lives) else 0), (1 if any(agents) else 0)

    D, E = de(eps)
    Dp, Ep = de(eps_p)
    # majority vote across probe rounds — variant S6mc; falls back to round 1
    # when the probe file has a single round, so single-round runs are unchanged
    Dmv, Emv = de(eps_p, "live_mv", "agentish_mv") if eps_p else (None, None)
    if eps_p and not any("live_mv" in probes.get(ep, {}) for ep in eps_p):
        Dmv, Emv = Dp, Ep
    live_eps = [ep for ep in eps_p if probes.get(ep, {}).get("agentish", 0) == 1]

    hosts = []
    h0 = host_of(row["token_uri"])
    if h0:
        hosts.append(h0)
    for s in svcs:
        h = host_of(str(s.get("endpoint", "")))
        if h and h not in hosts:
            hosts.append(h)
    if not hosts:
        F = None
    else:
        F = 1 if any(h in ALLOWLIST for h in hosts) else 0
    w = row["agent_wallet"]
    owner = row["owner"]
    wallet_ok = bool(w) and w != "0x" + "0" * 40 and w != owner
    G = 1 if (wallet_ok or trust_ok(card)) else 0
    return {
        "A": A,
        "B": B,
        "C": C if B else 0,
        "D": D,
        "E": E,
        "D_prime": Dp,
        "E_prime": Ep,
        "D_mv": Dmv,
        "E_mv": Emv,
        "F": F,
        "G": G,
        "uri_status": status,
        "b0_reason": b0,
        "live_eps": live_eps,
        "hosts": "|".join(hosts),
        "card_hash": card_hash(card) if card else "",
        "service_classes": "|".join(sorted({n for n in names if n})),
    }


def write_panels(agents: list[dict], uri_recs: dict[str, dict], probes: dict[str, dict]) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    by_chain: dict[str, list] = defaultdict(list)
    global_tiers = Counter()
    b0_counts = Counter()
    chain_stats = {}
    s10_clusters: set[tuple[str, str]] = set()
    owners_all: set[tuple[str, str]] = set()
    owners_s9: set[tuple[str, str]] = set()
    tokens_per_live_ep: Counter = Counter()

    fields = [
        "chain",
        "agent_id",
        "owner",
        "A",
        "B",
        "C",
        "D",
        "E",
        "D_prime",
        "E_prime",
        "F",
        "G",
        "uri_status",
        "b0_reason",
        "agency_evidence_tier",
        "agency_score",
        "protocol_use_binary",
        "S8mc",
        "S9mc",
        "S6mc",
        "assumption_only",
        "claim_only",
        "evidence_backed",
        "hosts",
        "card_hash",
        "service_classes",
        "token_uri",
    ]

    for row in agents:
        uri = row["token_uri"]
        fl = flags_for_agent(row, uri_recs.get(uri) if uri else None, probes)

        def z(x):
            return 0 if x is None else int(x)

        A, B, C, D, E, F, G = (z(fl[k]) for k in "ABCDEFG")
        Dp, Ep = z(fl["D_prime"]), z(fl["E_prime"])
        tier = tier_of(A, B, C, D, E, fl.get("b0_reason") or "uri_unresolved")
        score = score_of(A, B, C, D, E, F, G)
        pub = 1 if tier in ("moderate", "strong") else 0  # S0mc
        s8 = 1 if pub and Dp == 1 else 0
        s9 = 1 if pub and Ep == 1 else 0
        s6 = 1 if pub and z(fl["E_mv"]) == 1 else 0
        if fl.get("b0_reason"):
            b0_counts[fl["b0_reason"]] += 1
        owners_all.add((row["chain"], row["owner"]))
        if s9:
            host = (fl["hosts"] or "").split("|")[0] or f"_none_{row['agent_id']}"
            s10_clusters.add((row["chain"], host))
            owners_s9.add((row["chain"], row["owner"]))
            for ep in fl.get("live_eps") or []:
                tokens_per_live_ep[ep] += 1
        rec = {
            "chain": row["chain"],
            "agent_id": row["agent_id"],
            "owner": row["owner"],
            "A": A,
            "B": B,
            "C": C,
            "D": "" if fl["D"] is None else D,
            "E": "" if fl["E"] is None else E,
            "D_prime": "" if fl["D_prime"] is None else Dp,
            "E_prime": "" if fl["E_prime"] is None else Ep,
            "F": "" if fl["F"] is None else F,
            "G": G,
            "uri_status": fl["uri_status"],
            "b0_reason": fl.get("b0_reason") or "",
            "agency_evidence_tier": tier,
            "agency_score": score,
            "protocol_use_binary": pub,
            "S8mc": s8,
            "S9mc": s9,
            "S6mc": s6,
            "assumption_only": 1 if tier in ASSUMPTION_TIERS else 0,
            "claim_only": 1 if tier == "weak" else 0,
            "evidence_backed": pub,
            "hosts": fl["hosts"],
            "card_hash": fl["card_hash"],
            "service_classes": fl["service_classes"],
            "token_uri": uri[:300],
        }
        by_chain[row["chain"]].append(rec)
        global_tiers[tier] += 1

    all_rows = []
    for chain, rows in sorted(by_chain.items()):
        path = OUT / f"panel_{chain}.csv"
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        n = len(rows)
        tiers = Counter(r["agency_evidence_tier"] for r in rows)
        ao = sum(r["assumption_only"] for r in rows)
        pu = sum(r["protocol_use_binary"] for r in rows)
        s8n = sum(r["S8mc"] for r in rows)
        s9n = sum(r["S9mc"] for r in rows)
        s6n = sum(r["S6mc"] for r in rows)
        chain_stats[chain] = {
            "n": n,
            "tiers": dict(tiers),
            "assumption_only_pct": round(100 * ao / n, 3) if n else 0,
            "protocol_use_pct": round(100 * pu / n, 3) if n else 0,
            "protocol_use_n": pu,
            "S0mc_n": pu,
            "S0mc_pct": round(100 * pu / n, 3) if n else 0,
            "S8mc_n": s8n,
            "S8mc_pct": round(100 * s8n / n, 3) if n else 0,
            "S9mc_n": s9n,
            "S9mc_pct": round(100 * s9n / n, 3) if n else 0,
            "S6mc_n": s6n,
            "S6mc_pct": round(100 * s6n / n, 3) if n else 0,
        }
        all_rows.extend(rows)
        print(
            f"[{chain}] n={n} S0mc={chain_stats[chain]['S0mc_pct']}% "
            f"S9mc={chain_stats[chain]['S9mc_pct']}%",
            flush=True,
        )

    with (OUT / "panel_all.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(all_rows)

    n = len(all_rows)
    ao = sum(r["assumption_only"] for r in all_rows)
    pu = sum(r["protocol_use_binary"] for r in all_rows)
    s8n = sum(r["S8mc"] for r in all_rows)
    s9n = sum(r["S9mc"] for r in all_rows)
    s6n = sum(r["S6mc"] for r in all_rows)
    probe_rounds = max((int(p.get("rounds", 1)) for p in probes.values()), default=1)
    unstable_eps = sum(1 for p in probes.values() if p.get("unstable"))
    # endpoints we failed to measure because the host rate-limited us; they score
    # as not-live, so this count is the size of the artefact, not of a finding
    throttled_eps = sum(1 for p in probes.values() if p.get("throttled_rounds"))
    # uri_unresolved aggregate for backward-compatible reporting
    uri_unres_agg = sum(
        global_tiers.get(t, 0)
        for t in ("uri_unresolved", "fetch_failed", "throttled", "non_json", "card_not_8004")
    )
    summary = {
        "script_version": SCRIPT_VERSION,
        "codebook_version": CODEBOOK_VERSION,
        "sensitivity_spec": SENSITIVITY_SPEC,
        "n_agents": n,
        "tiers": dict(global_tiers),
        "uri_unresolved_aggregate": uri_unres_agg,
        "b0_reason": dict(b0_counts),
        "assumption_only_pct": round(100 * ao / n, 3) if n else 0,
        "protocol_use_pct": round(100 * pu / n, 3) if n else 0,
        "protocol_use_n": pu,
        "S0mc": {"n": pu, "pct": round(100 * pu / n, 3) if n else 0},
        "S8mc": {"n": s8n, "pct": round(100 * s8n / n, 3) if n else 0},
        "S9mc": {"n": s9n, "pct": round(100 * s9n / n, 3) if n else 0},
        "S10mc": {
            "clusters": len(s10_clusters),
            "owners_s9": len(owners_s9),
            "owners_all": len(owners_all),
            "owners_s9_pct": round(100 * len(owners_s9) / len(owners_all), 3) if owners_all else 0,
        },
        "S6mc": {
            "n": s6n,
            "pct": round(100 * s6n / n, 3) if n else 0,
            "probe_rounds": probe_rounds,
            "unstable_endpoints": unstable_eps,
            "delta_vs_S9mc_pp": round(100 * (s6n - s9n) / n, 3) if n else 0,
        },
        "probe_quality": {
            "endpoints_lost_to_throttling": throttled_eps,
            "note": "429 after retries; counted as not-live, so a non-zero value biases D/E downwards",
        },
        "S11mc": {
            "distinct_live_endpoints": len(tokens_per_live_ep),
            "endpoints_single_token": sum(1 for v in tokens_per_live_ep.values() if v == 1),
            "tokens_on_shared_endpoint": sum(v for v in tokens_per_live_ep.values() if v > 1),
            "top_endpoints": tokens_per_live_ep.most_common(15),
        },
        "chains": chain_stats,
        "census_used": dict(CENSUS_USED),
        "g_note": "wallet≠owner OR supportedTrust; no multichain feedback layer",
        "r_spec": "D_prime/E_prime via r_spec.is_probeable_endpoint; S8mc/S9mc use primes",
    }
    (OUT / "MC_SUMMARY.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def write_report(
    summary: dict, uri_status: Counter, n_unique: int, elapsed: float, label: str = "mc_t1"
) -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    s0 = summary["S0mc"]
    s8 = summary["S8mc"]
    s9 = summary["S9mc"]
    s10 = summary["S10mc"]
    title = label
    lines = [
        f"# Multichain fetch + score ({title})",
        "",
        f"- Script: `{SCRIPT_VERSION}` / codebook `{CODEBOOK_VERSION}` / sensitivity `{SENSITIVITY_SPEC}`",
        f"- Agents: **{summary['n_agents']}**",
        f"- Unique URIs: **{n_unique}**",
        f"- URI status: `{dict(uri_status)}`",
        f"- b0_reason (B≠1): `{summary.get('b0_reason', {})}`",
        f"- Elapsed: {elapsed:.0f}s",
        f"- G note: {summary['g_note']}",
        f"- R_spec: {summary['r_spec']}",
        "",
        "## Pair headline (claims vs live)",
        "",
        "| Spec | N | % |",
        "|---|---:|---:|",
        f"| S0mc (claims) | {s0['n']} | {s0['pct']}% |",
        f"| S8mc (live 2xx, R_spec) | {s8['n']} | {s8['pct']}% |",
        f"| **S9mc (agent-ish, R_spec)** | **{s9['n']}** | **{s9['pct']}%** |",
        (
            f"| S6mc (majority of {summary['S6mc']['probe_rounds']} probe rounds) "
            f"| {summary['S6mc']['n']} | {summary['S6mc']['pct']}% "
            f"(Δ {summary['S6mc']['delta_vs_S9mc_pp']:+} pp, unstable eps {summary['S6mc']['unstable_endpoints']}) |"
        ),
        f"| S10mc clusters (chain,host) | {s10['clusters']} | — |",
        f"| **S11mc distinct live endpoints** | **{summary['S11mc']['distinct_live_endpoints']}** | — |",
        f"| owners with ≥1 S9mc | {s10['owners_s9']} / {s10['owners_all']} | {s10['owners_s9_pct']}% |",
        f"| tokens sharing an endpoint | {summary['S11mc']['tokens_on_shared_endpoint']} | — |",
        (
            f"| endpoints lost to throttling | {summary.get('probe_quality', {}).get('endpoints_lost_to_throttling', 0)} | "
            "— (measurement artefact: a 429 surviving the retries counts as not live) |"
        ),
        "",
        f"assumption_only (incl. split B≠1 tiers): {summary['assumption_only_pct']}%",
        "",
        "### Tiers (split B≠1)",
        "",
        "| Tier | N |",
        "|---|---:|",
    ]
    for t in (
        "none",
        "fetch_failed",
        "throttled",
        "non_json",
        "card_not_8004",
        "uri_unresolved",
        "weak",
        "moderate",
        "strong",
    ):
        if summary["tiers"].get(t, 0):
            lines.append(f"| {t} | {summary['tiers'].get(t, 0)} |")
    lines += [
        "",
        f"uri_unresolved aggregate (compat): {summary.get('uri_unresolved_aggregate', 0)}",
        "",
        "## Per chain",
        "",
        "| Chain | N | S0mc % | S9mc % | S9mc N |",
        "|---|---:|---:|---:|---:|",
    ]
    for chain, st in sorted(summary["chains"].items(), key=lambda x: -x[1]["n"]):
        lines.append(
            f"| {chain} | {st['n']} | {st['S0mc_pct']} | {st['S9mc_pct']} | {st['S9mc_n']} |"
        )
    lines += [
        "",
        "Artifacts: `_enrich/multichain/panel_*.csv`, `panel_all.csv`, `MC_SUMMARY.json`, `probes.json`",
        "",
    ]
    name = "MC_FETCH_SCORE.md" if label == "mc_t1" else f"MC_FETCH_SCORE_{label}.md"
    (REPORTS / name).write_text("\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch-workers", type=int, default=FETCH_WORKERS)
    ap.add_argument("--probe-workers", type=int, default=PROBE_WORKERS)
    ap.add_argument("--skip-fetch", action="store_true", help="use cache only (no network resolve)")
    ap.add_argument(
        "--refetch",
        choices=["none", "circuit", "throttled", "failed", "ipfs_html"],
        default="none",
        help="re-resolve cached URIs: circuit=old host_circuit_open; throttled=+throttled; "
        "failed=all fetch_failed; ipfs_html=IPFS/gateway HTML stored as non_json",
    )
    ap.add_argument("--limit-uris", type=int, default=0, help="debug: only first N unique URIs")
    ap.add_argument(
        "--label",
        default="mc_t1",
        help="run label; anything but mc_t1 writes panels and probes to _enrich/multichain/<label>/ "
        "(the URI cache is shared across labels)",
    )
    ap.add_argument("--probe-rounds", type=int, default=1, help="probes per endpoint; 3 enables S6mc majority vote")
    ap.add_argument(
        "--probe-reuse",
        choices=["auto", "none"],
        default="auto",
        help="auto=keep cached probes with enough rounds; none=always re-probe (use at T2)",
    )
    ap.add_argument(
        "--probe-seed",
        default="",
        help="path to an existing probes.json to seed from (e.g. the mc_t1 file)",
    )
    ap.add_argument(
        "--cache-max-age-h",
        type=float,
        default=0.0,
        help="re-fetch cards whose cache entry is older than N hours (0 = never)",
    )
    ap.add_argument(
        "--save-probe-bodies",
        action="store_true",
        help="store a 2KB snapshot of each round-1 response (required for manual coding of `agentish`)",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="ignore a stale RUN.lock left by a crashed or orphaned run",
    )
    args = ap.parse_args()

    global OUT, CACHE_MAX_AGE_S, PROBE_BODIES, LOG_PATH, RUN_LABEL
    RUN_LABEL = args.label
    CACHE_MAX_AGE_S = args.cache_max_age_h * 3600
    if args.label != "mc_t1":
        OUT = OUT / args.label
        OUT.mkdir(parents=True, exist_ok=True)
    PROBE_BODIES = OUT / "probe_bodies"
    LOG_PATH = OUT / "run.log"
    log(f"label={args.label} out={OUT} cache_max_age_h={args.cache_max_age_h} pid={os.getpid()}")

    # Guard output directory against concurrent runs.
    lock = OUT / "RUN.lock"
    if lock.exists() and not args.force:
        try:
            held = json.loads(lock.read_text(encoding="utf-8"))
        except Exception:
            held = {}
        print(
            f"REFUSING TO START: {lock} exists (pid={held.get('pid')} since {held.get('started_utc')}).\n"
            f"  Check for an orphan:  Get-CimInstance Win32_Process -Filter \"Name='python.exe'\"\n"
            f"  Kill the tree:        taskkill /PID <pid> /T /F\n"
            f"  Then delete the lock, or re-run with --force.",
            flush=True,
        )
        raise SystemExit(2)
    OUT.mkdir(parents=True, exist_ok=True)
    lock.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "label": args.label,
                "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        ),
        encoding="utf-8",
    )
    atexit.register(lambda: lock.unlink(missing_ok=True))

    t0 = time.time()
    print("loading censuses…", flush=True)
    agents = load_censuses()
    print(f"agents={len(agents)}", flush=True)

    unique = sorted({a["token_uri"] for a in agents if a["token_uri"]})
    if args.limit_uris:
        unique = unique[: args.limit_uris]
    print(f"unique_uris={len(unique)}", flush=True)

    if args.skip_fetch and args.refetch == "none":
        uri_recs = {}
        for u in unique:
            c = load_cache(uri_hash(u))
            if c:
                uri_recs[u] = c
        print(f"skip-fetch loaded cache={len(uri_recs)}", flush=True)
    else:
        uri_recs = phase_resolve(unique, args.fetch_workers, refetch_mode=args.refetch)

    uri_status = Counter(r.get("uri_status") for r in uri_recs.values())
    print(f"uri_status={dict(uri_status)}", flush=True)

    probes = phase_probe(
        uri_recs,
        args.probe_workers,
        out_dir=OUT,
        rounds=max(1, args.probe_rounds),
        reuse=args.probe_reuse,
        reuse_path=Path(args.probe_seed) if args.probe_seed else None,
        save_bodies=args.save_probe_bodies,
    )
    live_n = sum(1 for p in probes.values() if p.get("live"))
    probeable_live = sum(
        1 for p in probes.values() if p.get("live") and p.get("probeable_rspec")
    )
    print(f"probes live={live_n}/{len(probes)} probeable_live={probeable_live}", flush=True)

    summary = write_panels(agents, uri_recs, probes)
    summary["label"] = args.label
    elapsed = time.time() - t0
    write_report(summary, uri_status, len(uri_recs), elapsed, label=args.label)
    slim = {k: summary[k] for k in summary if k != "chains"}
    print(json.dumps(slim, indent=2), flush=True)
    print(f"DONE in {elapsed:.0f}s", flush=True)


if __name__ == "__main__":
    main()
