import asyncio

import pytest

from paperpump.prices import WSOL, DexScreener, RateLimited

A = "MintA111111111111111111111111111111111pump"
B = "MintB111111111111111111111111111111111pump"


def pair(mint, price):
    return {"chainId": "solana", "priceNative": str(price), "priceUsd": str(price * 150), "liquidity": {"usd": 1000},
            "baseToken": {"address": mint, "symbol": mint[:5], "name": mint[:5]}, "quoteToken": {"address": WSOL}}


class Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class Client:
    def __init__(self, status=200, delay=0.05):
        self.urls, self.status, self.delay = [], status, delay

    async def get(self, url):
        self.urls.append(url)
        await asyncio.sleep(self.delay)
        mints = url.rsplit("/", 1)[1].split(",")
        return Resp([pair(m, 1e-6) for m in mints], self.status)


def test_concurrent_requests_coalesce_into_one_fetch():
    c = Client()
    d = DexScreener(client=c)

    async def go():
        return await asyncio.gather(*(d.quote(A) for _ in range(20)))
    res = asyncio.run(go())
    assert len(c.urls) == 1 and all(q.price_sol == 1e-6 for q in res)


def test_batch_endpoint_one_call_for_many_mints():
    c = Client()
    d = DexScreener(client=c)
    out = asyncio.run(d.quotes([A, B, A]))
    assert set(out) == {A, B} and len(c.urls) == 1 and "/tokens/v1/solana/" in c.urls[0]
    asyncio.run(d.quote(A))  # served from the batch's cache
    assert len(c.urls) == 1


def test_429_backoff_serves_stale_then_recovers():
    c = Client()
    d = DexScreener(client=c, ttl=0)
    asyncio.run(d.quote(A))
    c.status = 429
    assert asyncio.run(d.quote(A)).price_sol == 1e-6  # 429 -> stale cached value
    n = len(c.urls)
    asyncio.run(d.quote(A))                            # inside backoff window: no upstream call
    assert len(c.urls) == n and d._backoff_until > 0
    with pytest.raises(RateLimited):
        asyncio.run(d.quote(B))                        # nothing cached for B


def test_budget_caps_upstream_calls():
    c = Client(delay=0)
    d = DexScreener(client=c, ttl=0, max_per_min=5)

    async def go():
        for _ in range(20):
            await d.quote(A)
    asyncio.run(go())
    assert len(c.urls) == 5
