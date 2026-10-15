"""Read-only market data from the public DexScreener API (no key needed)."""
import time
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
                 p.get("pairCreatedAt"), p.get("dexId", "?"), p.get("url", ""))


class DexScreener:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(timeout=10)
        self._sol = (0.0, 0.0)

    async def quote(self, mint: str) -> Quote | None:
        r = await self.client.get(f"{DEX}/token-pairs/v1/solana/{mint}")
        r.raise_for_status()
        return parse_pairs(mint, r.json())

    async def sol_usd(self) -> float:
        if time.time() - self._sol[1] < 60:
            return self._sol[0]
        r = await self.client.get(f"{DEX}/tokens/v1/solana/{WSOL}")
        r.raise_for_status()
        pairs = [p for p in r.json() if p.get("baseToken", {}).get("address") == WSOL and p.get("priceUsd")]
        best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
        self._sol = (float(best["priceUsd"]), time.time())
        return self._sol[0]
