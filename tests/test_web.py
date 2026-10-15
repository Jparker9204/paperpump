import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from paperpump import auth
from paperpump.db import DB
from paperpump.prices import Quote
from paperpump.web import create_app

MINT = "DemoMint1111111111111111111111111111111pump"
SECRET = "test-secret-" + "x" * 20


class FakePrices:
    price = 1e-6

    async def quote(self, mint, max_age=3.0):
        return Quote(mint, "Demo", "DEMO", self.price * 150, self.price, 20000, 50000, None, "pumpswap", "",
                     210000, "PairAddr111")

    async def sol_usd(self):
        return 150.0


def cfg(password=""):
    return SimpleNamespace(start_balance=100.0, fee_rate=0.01, priority_fee=0.001, delay_seconds=0,
                           reset_cooldown_hours=24, base_url="https://example.test", session_secret=SECRET,
                           site_password=password)


async def nosleep(_):
    pass


@pytest.fixture
def env():
    db, prices = DB(), FakePrices()
    db.create_user(1, "alice", 100)
    db.create_user(2, "bob", 100)
    client = TestClient(create_app(cfg(), db, prices, sleep=nosleep), base_url="https://testserver")
    return client, db, prices


def login(client, tg_id):
    url = auth.login_url("", SECRET, tg_id)
    r = client.get(url, follow_redirects=False)
    assert r.status_code == 303
    return r


# ---------- auth ----------
def test_sign_verify_roundtrip_and_tamper():
    t = auth.sign(SECRET, "login", 42, 60)
    tg, nonce = auth.verify(SECRET, t, "login")
    assert tg == 42 and nonce
    assert auth.verify(SECRET, t, "session") is None          # wrong kind
    assert auth.verify("other-secret", t, "login") is None    # wrong secret
    body, sig = t.split(".")
    forged = auth._b64(auth._unb64(body).replace(b"|42|", b"|43|")) + "." + sig
    assert auth.verify(SECRET, forged, "login") is None       # tampered
    assert auth.verify(SECRET, "garbage", "login") is None


def test_token_expiry():
    t = auth.sign(SECRET, "login", 1, 60, now=1000)
    assert auth.verify(SECRET, t, "login", now=1059)
    assert auth.verify(SECRET, t, "login", now=1061) is None


def test_login_link_is_one_time(env):
    client, _, _ = env
    url = auth.login_url("", SECRET, 1)
    assert client.get(url, follow_redirects=False).status_code == 303
    assert "pp_session" in client.cookies
    client.cookies.clear()
    assert client.get(url, follow_redirects=False).status_code == 401  # reused
    assert client.get("/api/me").status_code == 401


def test_login_unknown_user_rejected(env):
    client, _, _ = env
    assert client.get(auth.login_url("", SECRET, 999), follow_redirects=False).status_code == 401


def test_api_requires_session(env):
    client, _, _ = env
    for path in ("/api/me", "/api/portfolio", "/api/history", "/api/stats", "/api/leaderboard", f"/api/token/{MINT}"):
        assert client.get(path).status_code == 401


def test_password_gate():
    db = DB()
    db.create_user(1, "alice", 100)
    c = TestClient(create_app(cfg("hunter2-long-password"), db, FakePrices(), sleep=nosleep), base_url="https://testserver")
    assert c.get("/", follow_redirects=False).headers["location"] == "/gate"
    assert c.get("/api/me").status_code == 401
    assert c.get("/robots.txt").text == "User-agent: *\nDisallow: /\n"
    assert c.post("/gate", data={"password": "nope"}).status_code == 401
    assert c.post("/gate", data={"password": "hunter2-long-password"}, follow_redirects=False).status_code == 303
    assert c.get("/", follow_redirects=False).status_code == 200
    # a valid bot login link also unlocks the gate
    c2 = TestClient(c.app, base_url="https://testserver")
    login(c2, 1)
    assert c2.get("/api/me").status_code == 200


def test_pages_noindex(env):
    client, _, _ = env
    r = client.get("/")
    assert r.status_code == 200 and 'name="robots" content="noindex' in r.text
    assert r.headers["x-robots-tag"].startswith("noindex")
    assert client.get("/static/../web.py").status_code == 404


# ---------- trading API ----------
def test_buy_sell_flow(env):
    client, db, prices = env
    login(client, 1)
    assert client.get("/api/me").json()["balance"] == 100
    tok = client.get(f"/api/token/{MINT}").json()
    assert tok["pair_address"] == "PairAddr111" and tok["volume_24h"] == 210000
    q = client.get(f"/api/quote?mint={MINT}&side=buy&amount=0.5").json()
    assert q["tokens"] > 0 and q["impact_pct"] > 0 and q["fee_sol"] == pytest.approx(0.006)
    b = client.post("/api/buy", json={"mint": MINT, "sol": 0.5}).json()
    assert b["balance"] == pytest.approx(99.499)
    pf = client.get("/api/portfolio").json()
    pid = pf["positions"][0]["id"]
    assert pf["positions"][0]["market"]["market_cap"] == 50000
    assert client.post("/api/tpsl", json={"position_id": pid, "tp": 50, "sl": 20}).json() == {"tp_pct": 50, "sl_pct": 20}
    prices.price = 2e-6
    s = client.post("/api/sell", json={"position_id": pid, "percent": 100}).json()
    assert s["pnl_sol"] > 0 and s["put_in"] == pytest.approx(0.501)
    assert client.get("/api/portfolio").json()["positions"] == []
    assert len(client.get("/api/history").json()) == 2
    assert client.get("/api/stats").json()["closed"] == 1
    lb = client.get("/api/leaderboard").json()
    assert lb[0]["username"] == "alice" and lb[0]["me"]


def test_cannot_touch_other_users_positions(env):
    client, db, _ = env
    login(client, 2)
    client.post("/api/buy", json={"mint": MINT, "sol": 0.5})
    pid = db.open_positions(2)[0]["id"]
    client.cookies.clear()
    login(client, 1)
    assert client.post("/api/sell", json={"position_id": pid, "percent": 100}).status_code == 404
    assert client.post("/api/tpsl", json={"position_id": pid, "tp": 50}).status_code == 404


def test_validation_and_overspend(env):
    client, _, _ = env
    login(client, 1)
    assert client.post("/api/buy", json={"mint": MINT, "sol": -1}).status_code == 422
    assert client.post("/api/buy", json={"mint": "bad", "sol": 1}).status_code == 400
    assert client.post("/api/buy", json={"mint": MINT, "sol": 500}).status_code == 400
    assert client.get("/api/token/not-a-mint").status_code == 400


def test_db_guard_blocks_overspend_across_processes(tmp_path):
    """Two DB connections (= bot + web processes) can't overspend: the balance check is inside the write tx."""
    from paperpump import fill as fm
    from paperpump.db import DBConflict
    path = str(tmp_path / "shared.db")
    a, b = DB(path), DB(path)
    a.create_user(1, "x", 1.0)
    f = fm.simulate_buy(0.8, 1e-6, 1e-6, 0)
    a.record_buy(1, MINT, "X", f)
    with pytest.raises(DBConflict):
        b.record_buy(1, MINT, "X", f)   # second "process" sees the new balance
    assert a.c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    pos = b.open_position(1, MINT)
    s = fm.simulate_sell(pos["tokens"], 1e-6, 1e-6, 0)
    a.record_sell(pos, s)
    with pytest.raises(DBConflict):
        b.record_sell(pos, s)           # already closed by the other process


def test_bot_web_button_sends_login_link():
    from unittest.mock import AsyncMock

    from paperpump import bot
    from paperpump.service import Trader
    db = DB()
    ctx = SimpleNamespace(bot_data={"trader": Trader(db, FakePrices(), cfg(), sleep=nosleep)}, user_data={})
    msg = SimpleNamespace(reply_text=AsyncMock())
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=7, username="u", first_name="u"), effective_message=msg,
                          callback_query=None)
    asyncio.run(bot.show_web(upd, ctx))
    kb = msg.reply_text.await_args.kwargs["reply_markup"]
    url = kb.inline_keyboard[0][0].url
    assert url.startswith("https://example.test/login?t=")
    assert auth.verify(SECRET, url.split("t=")[1], "login")[0] == 7


def test_portfolio_position_summary_fields(env):
    client, db, prices = env
    login(client, 1)
    client.post("/api/buy", json={"mint": MINT, "sol": 1.0})
    pid = client.get("/api/portfolio").json()["positions"][0]["id"]
    prices.price = 2e-6
    sold = client.post("/api/sell", json={"position_id": pid, "percent": 50}).json()
    p = client.get("/api/portfolio").json()["positions"][0]
    assert p["bought_sol"] == pytest.approx(1.001)
    assert p["sold_sol"] == pytest.approx(sold["got_back"])
    assert p["realized_sol"] == pytest.approx(sold["pnl_sol"])
    assert p["total_pnl_sol"] == pytest.approx(p["value_sol"] + p["sold_sol"] - p["bought_sol"])
    assert p["total_pnl_pct"] == pytest.approx(p["total_pnl_sol"] / 1.001 * 100)


def test_trade_page_has_no_fill_breakdown(env):
    client, _, _ = env
    html = client.get("/").text
    assert "Realistic fill estimate" not in html and "No position yet" in html


def test_pages_are_live(env):
    client, _, _ = env
    js = client.get("/static/app.js").text
    assert "visibilitychange" in js and "function live(" in js and "setTimeout(tick" in js
    for page in ("/", "/portfolio", "/history", "/leaderboard"):
        assert "live(" in client.get(page).text, page
    trade = client.get("/").text
    assert "live(refreshToken, 3000)" in trade and "setInterval" not in trade


def test_quote_cache_shared_between_requests():
    import time as _t

    from paperpump.prices import DexScreener

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return []

    class FakeClient:
        calls = 0

        async def get(self, url):
            FakeClient.calls += 1
            return FakeResp()

    d = DexScreener(client=FakeClient())

    async def go():
        for _ in range(5):
            await d.quote(MINT)
    asyncio.run(go())
    assert FakeClient.calls == 1  # 5 polls within 3s -> 1 upstream request
    d._cache[MINT] = (_t.time() - 10, None)
    asyncio.run(d.quote(MINT))
    assert FakeClient.calls == 2
