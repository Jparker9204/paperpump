"""Discovery feed: row building/filters/sorting, shared single-flight cache, web endpoints, bot button."""
import asyncio
import time

from fastapi.testclient import TestClient

from paperpump import bot, ui
from paperpump import discover as dv
from paperpump.db import DB
from paperpump.prices import WSOL, DexScreener
from paperpump.service import Trader
from paperpump.web import create_app
from tests.test_bot_handlers import CFG, make_update
from tests.test_web import cfg, login, nosleep

NOW = 1_800_000_000_000
A, B, C = "A" * 43 + "p", "B" * 43 + "p", "C" * 43 + "p"


def pair(mint, liq, mc, chg, vol, age_m, quote=WSOL, chain="solana", tx=10):
    tf = dict.fromkeys(dv.TIMEFRAMES)
    return {"chainId": chain, "priceNative": "0.000001", "liquidity": {"usd": liq}, "marketCap": mc,
            "pairCreatedAt": NOW - age_m * 60000, "baseToken": {"address": mint, "symbol": mint[:3], "name": "N"},
            "quoteToken": {"address": quote}, "info": {"imageUrl": "https://cdn.dexscreener.com/x.png"},
            "priceChange": {k: chg for k in tf}, "volume": {k: vol for k in tf},
            "txns": {k: {"buys": tx, "sells": tx} for k in tf}}


PAIRS = [pair(A, 50000, 900000, 5, 100000, 3000), pair(A, 2000, 900000, 5, 1, 3000),
         pair(B, 5000, 80000, 120, 20000, 30), pair(C, 500, 1000, 999, 9e9, 1),       # C: junk liquidity
         pair("D" * 44, 9e6, 1, 1, 1, 1, chain="base"), pair("E" * 44, 9e6, 1, 1, 1, 1, quote="USDC")]


def rows():
    return dv.build_rows([A, B, C, "D" * 44, "E" * 44], PAIRS, NOW)


def test_build_rows_filters_and_dedupes():
    r = rows()
    assert [x["mint"] for x in r] == [A, B]
    a = r[0]
    assert a["liquidity_usd"] == 50000 and a["txns"]["h1"] == 20 and round(a["age_m"]) == 3000
    assert a["icon"].startswith("https://cdn.dexscreener.com/")
    assert dv.build_rows([A], [dict(pair(A, 5000, 1, 1, 1, 1), info={"imageUrl": "javascript:x"})], NOW)[0]["icon"] == ""


def test_view_tabs_and_filters():
    r = rows()
    assert [x["mint"] for x in dv.view(r, "trending", "h1")] == [A, B]
    assert [x["mint"] for x in dv.view(r, "new", "h1")] == [B, A]
    assert [x["mint"] for x in dv.view(r, "gainers", "m5")] == [B, A]
    assert [x["mint"] for x in dv.view(r, "trending", "h24", {"max_mc": 100000})] == [B]
    assert [x["mint"] for x in dv.view(r, "trending", "h1", {"min_liq": 10000, "min_age_m": 60})] == [A]
    assert [x["mint"] for x in dv.view(r, "trending", "h1", {"max_age_m": 60, "min_change": 100})] == [B]
    assert dv.view(r, "bogus", "bogus", {"min_vol": 1e12}) == []
    assert dv.clean_filters({"min_mc": "5", "max_mc": -1, "evil": 1, "min_change": -50, "min_liq": "x"}) == \
        {"min_mc": 5.0, "min_change": -50.0}


class Resp:
    status_code = 200

    def __init__(self, d):
        self.d = d

    def raise_for_status(self):
        pass

    def json(self):
        return self.d


class Client:
    def __init__(self):
        self.urls = []

    async def get(self, url):
        self.urls.append(url)
        await asyncio.sleep(0.02)
        if "/tokens/v1/" in url:
            return Resp([pair(m, 5000, 1e5, 10, 1e4, 10) for m in url.rsplit("/", 1)[1].split(",")])
        return Resp([{"chainId": "solana", "tokenAddress": f"T{i:042d}p"} for i in range(50)]
                    + [{"chainId": "ethereum", "tokenAddress": "0xabc"}])


def test_feed_is_shared_single_flight_and_bounded():
    c = Client()
    feed = dv.Discover(DexScreener(client=c), refresh=45)

    async def go():
        res = await asyncio.gather(*(feed.get() for _ in range(25)))  # 25 users at once
        for _ in range(10):
            await feed.get()  # within the refresh window -> cache only
        return res
    res = asyncio.run(go())
    # 3 lists + 2 batches (50 unique tokens) for everyone, never per user
    assert len(c.urls) == 5 and all(len(r) == 50 for r in res)
    assert len(c.urls) <= 3 + dv.MAX_TOKENS // 30
    assert 60 / dv.REFRESH * (3 + dv.MAX_TOKENS // 30) < 10  # worst case < 10 calls/min


def test_failed_refresh_keeps_cache_and_does_not_hammer():
    c = Client()
    feed = dv.Discover(DexScreener(client=c, max_per_min=0), refresh=45)  # budget exhausted
    assert asyncio.run(feed.get()) == []
    asyncio.run(feed.get())
    assert c.urls == []


class FakeFeed:
    refresh_s = 45

    def __init__(self):
        self.calls, self.updated = 0, time.time()

    async def get(self):
        self.calls += 1
        return rows()


def test_web_discover_and_saved_filters():
    db, ff = DB(), FakeFeed()
    db.create_user(1, "alice", 100)
    db.create_user(2, "bob", 100)
    client = TestClient(create_app(cfg(), db, None, sleep=nosleep, feed=ff), base_url="https://testserver")
    assert client.get("/api/discover").status_code == 401
    login(client, 1)
    d = client.get("/api/discover?tab=new&tf=m5").json()
    assert [r["mint"] for r in d["rows"]] == [B, A] and d["total"] == 2 and "safer" in d["presets"]
    assert client.post("/api/discover/filters", json={"max_mc": 100000, "junk": 1}).json() == {"max_mc": 100000}
    d = client.get("/api/discover").json()
    assert [r["mint"] for r in d["rows"]] == [B] and d["filters"] == {"max_mc": 100000}
    assert db.get_pref(2, "discover_filters") is None  # per user
    assert client.post("/api/discover/filters", json={}).json() == {}
    assert len(client.get("/api/discover").json()["rows"]) == 2
    assert "mountDiscover(" in client.get("/discover").text


def test_bot_trending_button_top5():
    assert any(b.callback_data == "m|tr" for row in ui.menu_kb().inline_keyboard for b in row)
    ctx = type("Ctx", (), {"bot_data": {"trader": Trader(DB(), None, CFG), "discover": FakeFeed()},
                           "user_data": {}, "args": []})()
    upd, ev = make_update(data="m|tr")
    asyncio.run(bot.on_button(upd, ctx))
    data = [b.callback_data for row in ev[-1][3]["reply_markup"].inline_keyboard for b in row]
    assert data[:2] == [f"tk|{A}", f"tk|{B}"] and "Trending" in ev[-1][2][0]
    many = [dict(rows()[0], mint=f"M{i}") for i in range(9)]
    assert sum(b.callback_data.startswith("tk|") for r in ui.trending_kb(many[:5]).inline_keyboard for b in r) == 5
