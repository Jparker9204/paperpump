"""Read-only market data from the public DexScreener API (no key needed)."""
import asyncio
import time
from collections import deque
from dataclasses import dataclass

import httpx

DEX = "https://api.dexscreener.com"
WSOL = "So11111111111111111111111111111111111111112"


@dataclass
class Quote:
    mint: str
    name: str
    symbol: str
    price_usd: float
    price_sol: float
    liquidity_usd: float
    market_cap: float
    created_ms: int | None
    dex: str
    url: str
    volume_24h: float = 0.0
    pair_address: str = ""

    @property
    def age(self) -> str:
        if not self.created_ms:
            return "?"
        s = int(time.time() - self.created_ms / 1000)
        for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
            if s >= n:
                return f"{s // n}{unit}"
        return f"{s}s"


def parse_pairs(mint: str, data) -> Quote | None:
    pairs = data if isinstance(data, list) else (data or {}).get("pairs") or []
    pairs = [p for p in pairs if p.get("chainId") == "solana" and p.get("priceNative")
             and p.get("baseToken", {}).get("address") == mint
             and p.get("quoteToken", {}).get("address") == WSOL]
    if not pairs:
        return None
    p = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
    return Quote(mint, p["baseToken"].get("name", "?"), p["baseToken"].get("symbol", "?"),
                 float(p.get("priceUsd") or 0), float(p["priceNative"]),
                 float((p.get("liquidity") or {}).get("usd") or 0),
                 float(p.get("marketCap") or p.get("fdv") or 0),
                 p.get("pairCreatedAt"), p.get("dexId", "?"), p.get("url", ""),
                 float((p.get("volume") or {}).get("h24") or 0), p.get("pairAddress", ""))


class RateLimited(Exception):
    """DexScreener said 429 (or our own budget is used up) and there is no cached value to fall back on."""


class DexScreener:
    """DexScreener client with a short shared cache, request coalescing, a rate budget and 429 backoff.

    - Cache: quotes younger than `ttl` (default 1s) are reused for every user/page.
    - Coalescing: concurrent requests for the same mint(s) share one HTTP call.
    - Budget: at most `max_per_min` upstream calls per minute (DexScreener allows ~300/min);
      over budget or during a 429 backoff we serve the last known (stale) quote instead.
    """

    BATCH = 30  # DexScreener /tokens/v1 accepts up to 30 addresses per call

    def __init__(self, client: httpx.AsyncClient | None = None, ttl: float = 1.0, max_per_min: int = 240):
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(6.0, connect=3.0))
        self.ttl, self.max_per_min = ttl, max_per_min
        self._sol = (0.0, 0.0)
        self._cache: dict[str, tuple[float, Quote | None]] = {}
        self._inflight: dict[tuple, asyncio.Task] = {}
        self._calls: deque[float] = deque()
        self._backoff_until = 0.0
        self._backoff = 0.0

    # ----- upstream with budget + backoff -----
    async def _get(self, url: str):
        now = time.time()
        while self._calls and now - self._calls[0] > 60:
            self._calls.popleft()
        if now < self._backoff_until or len(self._calls) >= self.max_per_min:
            raise RateLimited("DexScreener rate limit, using cached data")
        self._calls.append(now)
        r = await self.client.get(url)
        if r.status_code == 429:
            self._backoff = min(60.0, (self._backoff or 2.0) * 2)
            self._backoff_until = time.time() + self._backoff
            raise RateLimited("DexScreener returned 429")
        r.raise_for_status()
        self._backoff = 0.0
        return r.json()

    async def _coalesce(self, key: tuple, factory):
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(factory())
            self._inflight[key] = task
            task.add_done_callback(lambda _t: self._inflight.pop(key, None))
        return await asyncio.shield(task)

    def _fresh(self, mint: str, max_age: float):
        hit = self._cache.get(mint)
        return hit if hit and time.time() - hit[0] < max_age else None

    async def quote(self, mint: str, max_age: float | None = None) -> Quote | None:
        """Latest quote; reuses one younger than max_age (default ttl). Pass 0 to force a fresh one."""
        max_age = self.ttl if max_age is None else max_age
        if hit := self._fresh(mint, max_age):
            return hit[1]

        async def fetch():
            try:
                q = parse_pairs(mint, await self._get(f"{DEX}/token-pairs/v1/solana/{mint}"))
            except RateLimited:
                if mint in self._cache:
                    return self._cache[mint][1]  # stale but better than nothing
                raise
            self._cache[mint] = (time.time(), q)
            return q
        return await self._coalesce(("one", mint), fetch)

    async def quotes(self, mints: list[str], max_age: float | None = None) -> dict[str, Quote | None]:
        """Many quotes with as few calls as possible (batches of 30 via /tokens/v1)."""
        max_age = self.ttl if max_age is None else max_age
        mints = list(dict.fromkeys(mints))
        out = {m: h[1] for m in mints if (h := self._fresh(m, max_age))}
        todo = [m for m in mints if m not in out]
        for i in range(0, len(todo), self.BATCH):
            chunk = tuple(sorted(todo[i:i + self.BATCH]))

            async def fetch(chunk=chunk):
                try:
                    data = await self._get(f"{DEX}/tokens/v1/solana/{','.join(chunk)}")
                except RateLimited:
                    return {m: self._cache.get(m, (0, None))[1] for m in chunk}
                now, res = time.time(), {}
                for m in chunk:
                    res[m] = parse_pairs(m, data)
                    self._cache[m] = (now, res[m])
                return res
            out.update(await self._coalesce(("many", chunk), fetch))
        return out

    async def sol_usd(self) -> float:
        if time.time() - self._sol[1] < 60:
            return self._sol[0]
        try:
            data = await self._get(f"{DEX}/tokens/v1/solana/{WSOL}")
        except RateLimited:
            if self._sol[0]:
                return self._sol[0]
            raise
        pairs = [p for p in data if p.get("baseToken", {}).get("address") == WSOL and p.get("priceUsd")]
        best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
        self._sol = (float(best["priceUsd"]), time.time())
        return self._sol[0]
