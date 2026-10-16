"""Coin search: parsing/filtering, shared cache + rate budget, web endpoint and bot buttons."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from paperpump import bot, ui
from paperpump.db import DB
from paperpump.prices import WSOL, DexScreener, RateLimited, parse_search
from paperpump.service import Trader
from paperpump.web import create_app
from tests.test_bot_handlers import CFG, make_update
from tests.test_web import SECRET, cfg, login, nosleep

A = "MintA111111111111111111111111111111111pump"
B = "MintB111111111111111111111111111111111pump"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def p(mint, liq, chain="solana", quote=WSOL, name="Dog"):
    return {"chainId": chain, "priceNative": "0.000001", "priceUsd": "0.00015", "liquidity": {"usd": liq},
            "marketCap": 50000, "volume": {"h24": 1234}, "dexId": "raydium", "pairAddress": f"P{mint[:4]}{liq}",
            "baseToken": {"address": mint, "symbol": name.upper(), "name": name}, "quoteToken": {"address": quote}}


SAMPLE = {"pairs": [
    p(A, 5000), p(A, 90000), p(A, 20000),           # duplicates: keep 90k
    p(B, 3000, name="Cat"),
    p("JunkJunk1111111111111111111111111111111", 200),  # under $1k liquidity
    p("EthToken", 999999, chain="ethereum"),           # not Solana
    p("UsdcQuoted111111111111111111111111111111", 50000, quote=USDC),  # not tradable in the buy view
]}


def test_parse_search_filters_dedupes_and_sorts():
    res = parse_search(SAMPLE)
    assert [q.mint for q in res] == [A, B]
    assert res[0].liquidity_usd == 90000 and res[0].volume_24h == 1234 and res[0].market_cap == 50000


def test_parse_search_handles_empty():
    assert parse_search({}) == [] and parse_search({"pairs": None}) == []


class Resp:
    status_code = 200

    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class Client:
    def __init__(self):
        self.urls = []

    async def get(self, url):
        self.urls.append(url)
        await asyncio.sleep(0.02)
        return Resp(SAMPLE)


def test_search_uses_cache_coalescing_and_budget():
    c = Client()
    d = DexScreener(client=c, max_per_min=2)

    async def go():
        r = await asyncio.gather(*(d.search("Dog") for _ in range(10)))
        await d.search("  dog ")  # normalized -> cached
        return r
    res = asyncio.run(go())
    assert len(c.urls) == 1 and "latest/dex/search?q=dog" in c.urls[0]
    assert all([q.mint for q in r] == [A, B] for r in res)
    # result quotes warm the shared quote cache: opening a result costs no extra call
    assert asyncio.run(d.quote(A)).liquidity_usd == 90000 and len(c.urls) == 1
    # shared budget: one more call allowed, then RateLimited (no stale value for a new query)
    asyncio.run(d.search("cat"))
    with pytest.raises(RateLimited):
        asyncio.run(d.search("frog"))
    assert len(c.urls) == 2


def test_search_too_short_makes_no_call():
    c = Client()
    assert asyncio.run(DexScreener(client=c).search(" a ")) == [] and c.urls == []


class FakePrices:
    def __init__(self):
        self.queries = []

    async def search(self, q):
        self.queries.append(q)
        return parse_search(SAMPLE)

    async def quote(self, mint, max_age=3.0):
        return parse_search(SAMPLE)[0]

    async def sol_usd(self):
        return 150.0


def test_web_search_endpoint():
    fp = FakePrices()
    db = DB()
    db.create_user(1, "alice", 100)
    client = TestClient(create_app(cfg(), db, fp, sleep=nosleep), base_url="https://testserver")
    assert client.get("/api/search?q=dog").status_code == 401
    login(client, 1)
    r = client.get("/api/search?q=dog").json()
    assert [x["mint"] for x in r] == [A, B]
    assert set(r[0]) >= {"name", "symbol", "market_cap", "liquidity_usd", "volume_24h"}
    assert client.get("/api/search?q=d").json() == [] and fp.queries == ["dog"]
    page = client.get("/").text
    assert 'id="results"' in page and "/api/search" in page
    assert SECRET not in page


def ctx_with(prices):
    return type("Ctx", (), {"bot_data": {"trader": Trader(DB(), prices, CFG)}, "user_data": {}, "args": []})()


def test_bot_search_flow_buttons_open_coin_card():
    ctx = ctx_with(FakePrices())
    assert any(b.callback_data == "m|sr" for row in ui.menu_kb().inline_keyboard for b in row)
    upd, ev = make_update(data="m|sr")
    asyncio.run(bot.on_button(upd, ctx))
    assert ctx.user_data.get("searching") and ui.SEARCH_PROMPT in str(ev[-1])
    upd, ev = make_update(text="dog")
    asyncio.run(bot.on_text(upd, ctx))
    kb = ev[-1][3]["reply_markup"]
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert data[:2] == [f"tk|{A}", f"tk|{B}"] and "searching" not in ctx.user_data
    upd, ev = make_update(data=f"tk|{A}")
    asyncio.run(bot.on_button(upd, ctx))
    assert f"b|0.1|{A}" in [b.callback_data for row in ev[-1][3]["reply_markup"].inline_keyboard for b in row]


def test_bot_search_caps_at_five_and_reply_label():
    from paperpump.prices import Quote
    qs = [Quote(f"M{i}", "N", "S", 1, 1, 5000, 1, None, "d", "") for i in range(9)]
    rows = ui.search_kb(qs).inline_keyboard
    assert sum(b.callback_data.startswith("tk|") for r in rows for b in r) == 5
    assert all(len(b.callback_data.encode()) <= 64 for r in rows for b in r)
    assert ui.MENU_LABELS[ui.SEARCH] == "sr"
