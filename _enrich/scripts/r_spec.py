#!/usr/bin/env python3
"""R_spec — probeable-endpoint filter for S8mc/S9mc/S10mc.

Frozen on 2026-08-10, before the mc_t2 snapshot; this module is the operative
definition of the filter. Does not alter the D/E flags for S0 / S0mc.
"""
from __future__ import annotations

import re
import urllib.parse

DOC_HOSTS = frozenset(
    {
        "github.com",
        "gitlab.com",
        "bitbucket.org",
        "npmjs.com",
        "pypi.org",
        "modelcontextprotocol.io",
        "a2a-protocol.org",
        "json-schema.org",
    }
)

# path-prefix (lowercase) on any host — protocol homes / pure docs
SPEC_PATH_PREFIXES = (
    "https://github.com/agntcy/oasf",
)

TEMPLATE_RE = re.compile(
    r"(\{agentId\}|\{agent_id\}|\{id\}|\{\{|\}|%s|%d)",
    re.IGNORECASE,
)


def _norm_host(host: str) -> str:
    h = (host or "").lower().strip()
    if h.startswith("www."):
        h = h[4:]
    return h


def is_doc_host(host: str) -> bool:
    h = _norm_host(host)
    if not h:
        return True
    if h in DOC_HOSTS:
        return True
    if h.startswith("docs."):
        return True
    if h.endswith(".readthedocs.io"):
        return True
    return False


def is_spec_home_url(url: str) -> bool:
    u = (url or "").strip().lower()
    if not u:
        return False
    for p in SPEC_PATH_PREFIXES:
        if u.startswith(p):
            return True
    return False


def has_unresolved_template(url: str) -> bool:
    try:
        decoded = urllib.parse.unquote(url or "")
    except Exception:
        decoded = url or ""
    return bool(TEMPLATE_RE.search(decoded))


def is_probeable_endpoint(url: str) -> bool:
    """True iff URL may be used for D′/E′ under R_spec."""
    u = (url or "").strip()
    if not (u.startswith("http://") or u.startswith("https://")):
        return False
    if has_unresolved_template(u):
        return False
    if is_spec_home_url(u):
        return False
    try:
        host = urllib.parse.urlparse(u).netloc
    except Exception:
        return False
    if is_doc_host(host):
        return False
    return True


def filter_probeable(urls: list[str]) -> list[str]:
    out, seen = [], set()
    for u in urls:
        if not is_probeable_endpoint(u):
            continue
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


if __name__ == "__main__":
    cases = [
        ("https://api.example.com/mcp", True),
        ("https://github.com/agntcy/oasf/", False),
        ("https://github.com/foo/bar", False),
        ("https://platform.example.com/api/v1/a2a/agents/{agentId}/card", False),
        ("https://docs.example.com/mcp", False),
        ("https://mesh.example.org/mcp", True),
        ("ipfs://QmFoo", False),
    ]
    fail = 0
    for url, expect in cases:
        got = is_probeable_endpoint(url)
        ok = got == expect
        print(("OK " if ok else "FAIL"), expect, got, url)
        fail += not ok
    raise SystemExit(fail)
