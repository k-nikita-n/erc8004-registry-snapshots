"""One-way structural helpers for deposit sanitization.

Derive structural metadata from `token_uri`, service declarations, and probe
error strings (scheme, media type, encoding, byte length, closed service
vocabulary, error classes without URLs or hostnames).
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

# Allowlist regexes for `data:` URI headers (media type and encoding parameters).
DATA_MEDIA = re.compile(r"^data:([A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+)")
DATA_PARAM = re.compile(r";([A-Za-z0-9_.+=-]{1,40})(?=[;,]|$)")
URL_START = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")

URI_COLUMNS = ("uri_media_type", "uri_encoding", "uri_bytes", "uri_group")

# Closed vocabulary for normalized `service_classes` (all other tokens map to `other`).
SERVICE_VOCAB = frozenset({"mcp", "a2a", "oasf", "web", "api", "agentwallet", "x402"})


def uri_shape(value: str) -> tuple[str, str, int]:
    """Return (media_type, encoding_params, byte_length) for a tokenURI without content."""
    n = len(value.encode("utf-8", "replace"))
    if not value.lower().startswith("data:"):
        return ("", "", n)
    head = value.split(",", 1)[0]
    m = DATA_MEDIA.match(head)
    params = [p.lower() for p in DATA_PARAM.findall(head)]
    if "," not in value:
        params.append("malformed")
    return (m.group(1).lower() if m else "unparsed", ";".join(params), n)


def collapse_services(value: str) -> str:
    if not value.strip():
        return ""
    kept = {t if t in SERVICE_VOCAB else "other"
            for t in (p.strip().lower() for p in value.split("|")) if t}
    return "|".join(sorted(kept))


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


# Ordered longest-evidence-first; the first pattern found in the message names the class.
ERROR_CLASSES = (
    ("certificate has expired", "tls_certificate_expired"),
    ("Hostname mismatch", "tls_hostname_mismatch"),
    ("self-signed certificate", "tls_self_signed"),
    ("key too weak", "tls_key_too_weak"),
    ("CERTIFICATE_VERIFY_FAILED", "tls_verify_failed"),
    ("TLSV1_UNRECOGNIZED_NAME", "tls_unrecognized_name"),
    ("ALERT_INTERNAL_ERROR", "tls_internal_error"),
    ("ALERT_HANDSHAKE_FAILURE", "tls_handshake_failure"),
    ("handshake operation timed out", "tls_handshake_timeout"),
    ("getaddrinfo failed", "dns_failure"),
    ("Name or service not known", "dns_failure"),
    ("no host given", "no_host"),
    ("control characters", "malformed_url"),
    ("timed out", "timeout"),
    ("10061", "connection_refused"),
    ("10060", "connection_timeout"),
    ("10054", "connection_reset"),
    ("ConnectionResetError", "connection_reset"),
)
HTTP_STATUS = re.compile(r"^HTTP (\d{3})$")


def error_class(value: str) -> str:
    """Map raw network/TLS error strings to a closed, URL-free error class."""
    v = (value or "").strip()
    if not v or HTTP_STATUS.match(v):
        return v
    if v.startswith("host_dead"):
        return "host_dead"
    for probe, name in ERROR_CLASSES:
        if probe in v:
            return name
    return "other"


def no_url(value):
    """Truncate a string at the first embedded URL scheme."""
    if not isinstance(value, str):
        return value
    m = URL_START.search(value)
    if m is None:
        return value
    return value[:m.start()].rstrip(" '\"(<[") + " <url removed>"
