#!/usr/bin/env python3
"""Free-RPC pool for multichain ERC-8004 census.

Provides:
  - CHAIN_RPCS: candidate free endpoints per chain
  - RpcPool: rotating caller with per-endpoint cooldown, backoff, keep-alive sessions
  - health_check(): probes what each endpoint actually supports

Requirements a usable endpoint must satisfy:
  1. eth_blockNumber
  2. eth_call pinned to a specific (recent) block  -> reproducibility
  3. Multicall3 aggregate3 with a 100-call payload  -> throughput
  4. (nice) JSON-RPC array batching
"""
from __future__ import annotations

import itertools
import json
import random
import threading
import time
from dataclasses import dataclass, field

import certifi
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

IDENTITY = "0x8004a169fb4a3325136eb29fa0ceb6d2e539a432"
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
SEL_OWNER = "6352211e"
SEL_AGGREGATE3 = "82ad56cb"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ERC8004-research/1.0"

CHAIN_RPCS: dict[str, list[str]] = {
    "ethereum": [
        "https://ethereum-rpc.publicnode.com",
        "https://ethereum.publicnode.com",
        "https://rpc.flashbots.net",
        "https://eth.drpc.org",
        "https://rpc.mevblocker.io",
        "https://eth.merkle.io",
        "https://cloudflare-eth.com",
        "https://eth.llamarpc.com",
        "https://rpc.payload.de",
        "https://eth.rpc.blxrbdn.com",
        "https://api.securerpc.com/v1",
        "https://eth-pokt.nodies.app",
        "https://rpc.therpc.io/ethereum",
    ],
    "bsc": [
        "https://bsc-rpc.publicnode.com",
        "https://bsc-dataseed1.defibit.io",
        "https://bsc-dataseed1.ninicoin.io",
        "https://bsc-dataseed2.defibit.io",
        "https://bsc-dataseed3.defibit.io",
        "https://bsc-dataseed4.defibit.io",
        "https://bsc-dataseed1.bnbchain.org",
        "https://bsc-dataseed2.bnbchain.org",
        "https://bsc-dataseed3.bnbchain.org",
        "https://bsc-dataseed4.bnbchain.org",
        "https://bsc.drpc.org",
        "https://binance.llamarpc.com",
        "https://bsc.meowrpc.com",
        "https://bsc-pokt.nodies.app",
    ],
    "base": [
        "https://base-rpc.publicnode.com",
        "https://mainnet.base.org",
        "https://base.llamarpc.com",
        "https://base.drpc.org",
        "https://base.meowrpc.com",
        "https://base-pokt.nodies.app",
        "https://1rpc.io/base",
    ],
    "arbitrum": [
        "https://arbitrum-one-rpc.publicnode.com",
        "https://arb1.arbitrum.io/rpc",
        "https://arbitrum.llamarpc.com",
        "https://arbitrum.drpc.org",
        "https://arbitrum-one.public.blastapi.io",
        "https://arb-pokt.nodies.app",
        "https://arbitrum.meowrpc.com",
    ],
    "optimism": [
        "https://optimism-rpc.publicnode.com",
        "https://mainnet.optimism.io",
        "https://optimism.llamarpc.com",
        "https://optimism.drpc.org",
        "https://op-pokt.nodies.app",
    ],
    "polygon": [
        "https://polygon-bor-rpc.publicnode.com",
        "https://polygon-rpc.com",
        "https://polygon.llamarpc.com",
        "https://polygon.drpc.org",
        "https://poly-pokt.nodies.app",
    ],
    "avalanche": [
        "https://avalanche-c-chain-rpc.publicnode.com",
        "https://api.avax.network/ext/bc/C/rpc",
        "https://avalanche.drpc.org",
        "https://avax-pokt.nodies.app/ext/bc/C/rpc",
    ],
    "gnosis": [
        "https://gnosis-rpc.publicnode.com",
        "https://rpc.gnosischain.com",
        "https://gnosis.drpc.org",
        "https://rpc.gnosis.gateway.fm",
    ],
    "celo": [
        "https://celo-rpc.publicnode.com",
        "https://forno.celo.org",
        "https://celo.drpc.org",
    ],
    "linea": [
        "https://linea-rpc.publicnode.com",
        "https://rpc.linea.build",
        "https://linea.drpc.org",
    ],
    "scroll": [
        "https://scroll-rpc.publicnode.com",
        "https://rpc.scroll.io",
        "https://scroll.drpc.org",
    ],
    "metis": [
        "https://metis-rpc.publicnode.com",
        "https://andromeda.metis.io/?owner=1088",
        "https://metis.drpc.org",
    ],
    "mantle": [
        "https://mantle-rpc.publicnode.com",
        "https://rpc.mantle.xyz",
        "https://mantle.drpc.org",
    ],
}


def pad_uint(n: int) -> str:
    return f"{n:064x}"


def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=0, backoff_factor=0)
    adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update(
        {
            "Content-Type": "application/json",
            "User-Agent": UA,
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
        }
    )
    s.verify = certifi.where()
    return s


def build_aggregate3(calls: list[tuple[str, str]]) -> str:
    """calls = [(target, calldata_hex_no_0x)] -> aggregate3 calldata."""
    n = len(calls)
    offsets, bodies = [], []
    running = 32 * n
    for target, calldata in calls:
        offsets.append(pad_uint(running))
        pad_len = (64 - (len(calldata) % 64)) % 64
        body = (
            pad_uint(int(target, 16))
            + pad_uint(1)
            + pad_uint(96)
            + pad_uint(len(calldata) // 2)
            + calldata
            + "0" * pad_len
        )
        bodies.append(body)
        running += len(body) // 2
    return "0x" + SEL_AGGREGATE3 + pad_uint(32) + pad_uint(n) + "".join(offsets) + "".join(bodies)


def decode_aggregate3(result: str, n: int) -> list[tuple[bool, str]]:
    if not result or result == "0x":
        return [(False, "")] * n
    h = result[2:]
    arr_off = int(h[0:64], 16) * 2
    count = int(h[arr_off : arr_off + 64], 16)
    base = arr_off + 64
    out: list[tuple[bool, str]] = []
    for i in range(count):
        tup_off = int(h[base + i * 64 : base + (i + 1) * 64], 16) * 2 + base
        success = int(h[tup_off : tup_off + 64], 16) == 1
        data_off = int(h[tup_off + 64 : tup_off + 128], 16) * 2 + tup_off
        dlen = int(h[data_off : data_off + 64], 16)
        raw = h[data_off + 64 : data_off + 64 + dlen * 2]
        out.append((success, "0x" + raw))
    while len(out) < n:
        out.append((False, ""))
    return out


def decode_string(data: str) -> str:
    if not data or data in ("0x", "0x0"):
        return ""
    h = data[2:] if data.startswith("0x") else data
    if len(h) < 128:
        return ""
    try:
        offset = int(h[0:64], 16) * 2
        slen = int(h[offset : offset + 64], 16)
        raw = h[offset + 64 : offset + 64 + slen * 2]
        return bytes.fromhex(raw).decode("utf-8", "replace")
    except Exception:
        return ""


@dataclass
class Endpoint:
    url: str
    ok_call_at_block: bool = False
    ok_multicall: bool = False
    ok_batch: bool = False
    max_multicall: int = 0
    latency_ms: float = 0.0
    cooldown_until: float = 0.0
    fails: int = 0
    session: requests.Session = field(default_factory=make_session)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def healthy(self) -> bool:
        return self.ok_call_at_block and self.ok_multicall


class RpcPool:
    """Round-robin RPC pool with per-endpoint cooldown; each Session is lock-guarded."""

    def __init__(self, endpoints: list[Endpoint], timeout: int = 40):
        self.endpoints = [e for e in endpoints if e.healthy()] or endpoints
        self.timeout = timeout
        self._lock = threading.Lock()
        self._cycle = itertools.cycle(range(len(self.endpoints)))

    def _pick(self) -> Endpoint:
        now = time.time()
        with self._lock:
            for _ in range(len(self.endpoints) * 2):
                ep = self.endpoints[next(self._cycle)]
                if ep.cooldown_until <= now:
                    return ep
            return random.choice(self.endpoints)

    def call(self, method: str, params: list, retries: int = 8):
        last = None
        for attempt in range(retries):
            ep = self._pick()
            try:
                payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
                with ep.lock:
                    r = ep.session.post(ep.url, data=payload, timeout=self.timeout)
                if r.status_code != 200:
                    ep.cooldown_until = time.time() + min(60, 2 ** attempt)
                    last = f"HTTP {r.status_code}"
                    continue
                o = r.json()
                if "error" in o:
                    last = o["error"]
                    ep.cooldown_until = time.time() + min(30, 1.5 ** attempt)
                    continue
                ep.fails = 0
                return o.get("result")
            except Exception as e:
                last = e
                ep.cooldown_until = time.time() + min(60, 2 ** attempt)
        raise RuntimeError(f"RPC pool exhausted for {method}: {last}")

    def multicall(self, calls: list[tuple[str, str]], block: str) -> list[tuple[bool, str]]:
        data = build_aggregate3(calls)
        res = self.call("eth_call", [{"to": MULTICALL3, "data": data}, block])
        return decode_aggregate3(res, len(calls))


def probe_endpoint(url: str, probe_id: int = 0, chunk: int = 100) -> Endpoint:
    ep = Endpoint(url=url)
    t0 = time.time()
    try:
        r = ep.session.post(
            url,
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}),
            timeout=25,
        )
        if r.status_code != 200:
            ep.latency_ms = -1
            return ep
        head = int(r.json()["result"], 16)
    except Exception:
        ep.latency_ms = -1
        return ep
    ep.latency_ms = round((time.time() - t0) * 1000, 1)

    block = hex(max(1, head - 5))
    # 2: pinned eth_call
    try:
        r = ep.session.post(
            url,
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_call",
                    "params": [{"to": IDENTITY, "data": "0x" + SEL_OWNER + pad_uint(probe_id)}, block],
                }
            ),
            timeout=25,
        )
        o = r.json()
        ep.ok_call_at_block = r.status_code == 200 and "error" not in o and (o.get("result") or "0x") != "0x"
    except Exception:
        pass

    # 3: multicall3 with `chunk` calls
    try:
        calls = [(IDENTITY, SEL_OWNER + pad_uint(probe_id + i)) for i in range(chunk)]
        r = ep.session.post(
            url,
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_call",
                    "params": [{"to": MULTICALL3, "data": build_aggregate3(calls)}, block],
                }
            ),
            timeout=40,
        )
        o = r.json()
        if r.status_code == 200 and "error" not in o:
            dec = decode_aggregate3(o.get("result") or "0x", chunk)
            if sum(1 for ok, _ in dec if ok) >= chunk // 2:
                ep.ok_multicall = True
                ep.max_multicall = chunk
    except Exception:
        pass

    # 4: json-rpc array batch
    try:
        payload = [
            {"jsonrpc": "2.0", "id": i, "method": "eth_chainId", "params": []} for i in range(3)
        ]
        r = ep.session.post(url, data=json.dumps(payload), timeout=25)
        ep.ok_batch = r.status_code == 200 and isinstance(r.json(), list)
    except Exception:
        pass

    return ep


def load_pool(chain: str, probe_id: int = 0) -> tuple[RpcPool, list[Endpoint]]:
    from concurrent.futures import ThreadPoolExecutor

    urls = CHAIN_RPCS[chain]
    with ThreadPoolExecutor(max_workers=min(12, len(urls))) as ex:
        eps = list(ex.map(lambda u: probe_endpoint(u, probe_id), urls))
    healthy = [e for e in eps if e.healthy()]
    return RpcPool(healthy or eps), eps
