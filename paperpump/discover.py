"""Shared discovery feed (Trending / New pairs / Gainers), built ONCE per refresh for all users.

Upstream cost per refresh: 3 list calls (token-boosts latest + top, token-profiles latest) plus one
/tokens/v1 batch call per 30 tokens (MAX_TOKENS=90 -> at most 3), so <= 6 calls. With REFRESH=45s that is
<= 8 calls/min, all through DexScreener's shared rate budget. Users only ever read the cached list.
"""
import asyncio
import time

from .prices import DEX, WSOL, RateLimited

TIMEFRAMES = ("m5", "h1", "h6", "h24")
TABS = ("trending", "new", "gainers")
MIN_LIQ = 1000.0
MAX_TOKENS = 90
REFRESH = 45.0
LIST_URLS = (f"{DEX}/token-boosts/top/v1", f"{DEX}/token-boosts/latest/v1", f"{DEX}/token-profiles/latest/v1")
FILTER_KEYS = ("min_mc", "max_mc", "min_liq", "min_vol", "min_age_m", "max_age_m", "min_change")
PRESETS = {
    "all": {},
    "fresh": {"max_age_m": 360, "min_liq": 5000},
    "safer": {"min_liq": 50000, "min_mc": 250000, "min_vol": 50000},
    "micro": {"max_mc": 250000, "min_liq": 2000},
    "pumping": {"min_change": 20, "min_vol": 10000},
}


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _icon(p) -> str:
    url = (p.get("info") or {}).get("imageUrl") or ""
    return url if url.startswith("https://") and "dexscreener.com/" in url else ""


def build_rows(addresses: list[str], pairs: list[dict], now_ms: float | None = None) -> list[dict]:
    """One row per token: its highest-liquidity Solana/SOL pair with >= MIN_LIQ. Keeps list order (rank)."""
    now_ms = now_ms or time.time() * 1000
    best: dict[str, dict] = {}
    for p in pairs or []:
        mint = (p.get("baseToken") or {}).get("address")
        liq = _f((p.get("liquidity") or {}).get("usd"))
        if (p.get("chainId") != "solana" or not mint or not p.get("priceNative") or liq < MIN_LIQ
                or (p.get("quoteToken") or {}).get("address") != WSOL):
            continue
        if mint not in best or liq > _f(best[mint]["liquidity"]["usd"]):
            best[mint] = p
    rows = []
    for rank, mint in enumerate(dict.fromkeys(addresses)):
        p = best.get(mint)
        if not p:
            continue
        created = p.get("pairCreatedAt")
        tx = p.get("txns") or {}
        rows.append({
            "mint": mint, "rank": rank, "name": p["baseToken"].get("name") or "?",
            "symbol": (p["baseToken"].get("symbol") or "?").lstrip("$"), "icon": _icon(p),
            "age_m": max(0.0, (now_ms - created) / 60000) if created else None,
            "market_cap": _f(p.get("marketCap") or p.get("fdv")), "liquidity_usd": _f(p["liquidity"]["usd"]),
            "volume": {t: _f((p.get("volume") or {}).get(t)) for t in TIMEFRAMES},
            "change": {t: _f((p.get("priceChange") or {}).get(t)) for t in TIMEFRAMES},
            "txns": {t: int(_f((tx.get(t) or {}).get("buys")) + _f((tx.get(t) or {}).get("sells"))) for t in TIMEFRAMES},
        })
    return rows


def clean_filters(raw: dict | None) -> dict:
    out = {}
    for k in FILTER_KEYS:
        v = (raw or {}).get(k)
        if v is None or v == "":
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v == v and abs(v) < 1e15 and (v >= 0 or k == "min_change"):
            out[k] = v
    return out


def passes(r: dict, f: dict, tf: str) -> bool:
    age = r["age_m"]
    checks = (
        ("min_mc", lambda v: r["market_cap"] >= v), ("max_mc", lambda v: r["market_cap"] <= v),
        ("min_liq", lambda v: r["liquidity_usd"] >= v), ("min_vol", lambda v: r["volume"][tf] >= v),
        ("min_age_m", lambda v: age is not None and age >= v), ("max_age_m", lambda v: age is not None and age <= v),
        ("min_change", lambda v: r["change"][tf] >= v),
    )
    return all(fn(f[k]) for k, fn in checks if k in f)


def view(rows: list[dict], tab: str = "trending", tf: str = "h1", filters: dict | None = None, limit: int = 60):
    tab = tab if tab in TABS else "trending"
    tf = tf if tf in TIMEFRAMES else "h1"
    f = clean_filters(filters)
    out = [r for r in rows if passes(r, f, tf)]
    if tab == "trending":  # activity in the timeframe, boosted rank as tiebreak
        out.sort(key=lambda r: (-(r["volume"][tf] + r["txns"][tf] * 50), r["rank"]))
    elif tab == "new":
        out.sort(key=lambda r: r["age_m"] if r["age_m"] is not None else 1e12)
    else:
        out.sort(key=lambda r: -r["change"][tf])
    return out[:limit]


class Discover:
    """Single shared, cached feed. Refreshed at most once per REFRESH seconds (single-flight); requests
    in between (and during a refresh, or when rate-limited) get the cached list."""

    def __init__(self, dex, refresh: float = REFRESH):
        self.dex, self.refresh_s = dex, refresh
        self.rows: list[dict] = []
        self.updated = 0.0
        self._task: asyncio.Task | None = None
        self._next = 0.0

    async def _build(self):
        lists = await asyncio.gather(*(self.dex._get(u) for u in LIST_URLS), return_exceptions=True)
        addrs = []
        for data in lists:
            if isinstance(data, list):
                addrs += [t.get("tokenAddress") for t in data if t.get("chainId") == "solana" and t.get("tokenAddress")]
        addrs = list(dict.fromkeys(addrs))[:MAX_TOKENS]
        if not addrs:
            if any(isinstance(x, RateLimited) for x in lists):
                raise RateLimited("discover lists rate limited")
            return
        pairs = []
        for i in range(0, len(addrs), 30):
            try:
                pairs += await self.dex._get(f"{DEX}/tokens/v1/solana/{','.join(addrs[i:i + 30])}") or []
            except RateLimited:
                break
        if pairs:
            self.rows, self.updated = build_rows(addrs, pairs), time.time()

    async def get(self) -> list[dict]:
        now = time.time()
        if now >= self._next and (self._task is None or self._task.done()):
            self._next = now + self.refresh_s  # at most one refresh per interval, success or not
            self._task = asyncio.ensure_future(self._build())
            self._task.add_done_callback(lambda t: t.cancelled() or t.exception())  # errors -> keep cache
        if not self.rows and self._task and not self._task.done():  # first load: wait for it
            try:
                await asyncio.wait_for(asyncio.shield(self._task), 10)
            except Exception:
                pass
        return self.rows
