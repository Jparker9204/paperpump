import asyncio
from types import SimpleNamespace

import pytest

from paperpump import fill as fm
from paperpump.db import DB
from paperpump.prices import WSOL, Quote, parse_pairs
from paperpump.service import TradeError, Trader

MINT = "DemoMint1111111111111111111111111111111pump"
CFG = SimpleNamespace(start_balance=100.0, fee_rate=0.01, priority_fee=0.001, delay_seconds=0,
                      reset_cooldown_hours=24)


class FakePrices:
    def __init__(self, price=1e-6, liq=20000):
        self.price, self.liq = price, liq

    async def quote(self, mint, max_age=3.0):
        return Quote(mint, "Demo", "DEMO", self.price * 150, self.price, self.liq, 50000, None, "pumpswap", "", 210000)

    async def sol_usd(self):
        return 150.0


async def nosleep(_):
    pass


def run(c):
    return asyncio.run(c)


def test_buy_constant_product():
    f = fm.simulate_buy(1, 1e-6, 1e-6, reserve_sol=100)
    assert f.naive == pytest.approx(1e6)
    expected = (100 / 1e-6) * 0.99 / (100 + 0.99)
    assert f.tokens == pytest.approx(expected)
    assert f.impact_pct == pytest.approx(0.99 / 100.99 * 100, rel=1e-6)
    assert f.fee_sol == pytest.approx(0.011)


def test_buy_no_liquidity_and_drift():
    f = fm.simulate_buy(1, 1e-6, 1.1e-6, reserve_sol=0)
    assert f.impact_pct == pytest.approx(0)
    assert f.drift_pct == pytest.approx(10)
    assert f.tokens == pytest.approx(0.99 / 1.1e-6)


def test_sell_fees_and_impact():
    f = fm.simulate_sell(1e6, 1e-6, 1e-6, reserve_sol=100)
    gross = 100 * 1e6 / (1e8 + 1e6)
    assert f.sol == pytest.approx(gross * 0.99 - 0.001)
    assert f.impact_pct > 0


def test_invalid_inputs():
    with pytest.raises(ValueError):
        fm.simulate_buy(0, 1, 1, 1)


def test_pnl():
    assert fm.pnl(1, 1.5) == (0.5, 50.0)
    assert fm.pnl(0, 1)[1] == 0


def test_sol_reserve():
    assert fm.sol_reserve(30000, 150) == 100
    assert fm.sol_reserve(0, 150) == 0


def test_parse_pairs_picks_deepest_sol_pair():
    data = [
        {"chainId": "solana", "priceNative": "0.000001", "priceUsd": "0.00015", "liquidity": {"usd": 5},
         "baseToken": {"address": MINT, "symbol": "A", "name": "A"}, "quoteToken": {"address": WSOL}},
        {"chainId": "solana", "priceNative": "0.000002", "priceUsd": "0.0003", "liquidity": {"usd": 9000},
         "baseToken": {"address": MINT, "symbol": "A", "name": "A"}, "quoteToken": {"address": WSOL}},
        {"chainId": "solana", "priceNative": "9", "liquidity": {"usd": 1e9},
         "baseToken": {"address": MINT}, "quoteToken": {"address": "USDC"}},
    ]
    q = parse_pairs(MINT, data)
    assert q.price_sol == 2e-6 and q.liquidity_usd == 9000
    assert parse_pairs(MINT, []) is None


def make():
    db = DB()
    t = Trader(db, FakePrices(), CFG, sleep=nosleep)
    db.create_user(1, "alice", 100)
    return db, t


def test_buy_sell_roundtrip_db():
    db, t = make()
    run(t.buy(1, MINT, 1))
    assert db.user(1)["balance"] == pytest.approx(98.999)
    pos = db.open_position(1, MINT)
    assert pos["cost_sol"] == pytest.approx(1.001)
    t.prices.price = 2e-6  # token doubles
    _, f, _, realized = run(t.sell(1, MINT, 50))
    pos = db.open_position(1, MINT)
    assert pos["tokens"] == pytest.approx(f.tokens)  # half left
    assert realized > 0
    run(t.sell(1, MINT, 100))
    assert db.open_position(1, MINT) is None
    s = db.stats(1)
    assert s["closed"] == 1 and s["win_rate"] == 100 and s["net_pnl"] > 0.8
    assert len(db.history(1)) == 3


def test_insufficient_balance_and_bad_mint():
    _, t = make()
    with pytest.raises(TradeError):
        run(t.buy(1, MINT, 1000))
    with pytest.raises(TradeError):
        run(t.buy(1, "not-a-mint", 1))
    with pytest.raises(TradeError):
        run(t.buy(2, MINT, 1))  # no account


def test_tp_and_sl_trigger():
    db, t = make()
    run(t.buy(1, MINT, 1))
    db.set_tpsl(db.open_position(1, MINT)["id"], tp=50, sl=30)
    assert run(t.check_tpsl()) == []
    t.prices.price = 0.5e-6
    hits = run(t.check_tpsl())
    assert hits[0][2] == "SL" and hits[0][3] < 0
    assert db.open_position(1, MINT) is None


def test_reset_cooldown_and_leaderboard():
    db, t = make()
    db.create_user(2, "bob", 100)
    run(t.buy(2, MINT, 1))
    t.prices.price = 3e-6
    run(t.sell(2, MINT, 100))
    lb = db.leaderboard()
    assert lb[0]["username"] == "bob"
    t.reset(1, now=1000)
    assert db.user(1)["balance"] == 100
    with pytest.raises(TradeError):
        t.reset(1, now=2000)
    t.reset(1, now=1000 + 25 * 3600)


def test_sell_text_simple():
    from paperpump.service import sell_text
    t = sell_text("FROG", 100, 0.46, -0.04)
    assert t.splitlines()[0] == "🔴 Sold 100% of $FROG"
    assert "Put in: 0.50 SOL" in t and "Got back: 0.46 SOL" in t and "-0.04 SOL (-8.0%)" in t
    assert "impact" not in t.lower() and "fee" not in t.lower() and "naive" not in t.lower()
    assert sell_text("X", 50, 1.5, 0.5).startswith("🟢 Sold 50% of $X")


def test_tpsl_hit_reports_got_back():
    db, t = make()
    run(t.buy(1, MINT, 1))
    db.set_tpsl(db.open_position(1, MINT)["id"], tp=50)
    t.prices.price = 2e-6
    hit = run(t.check_tpsl())[0]
    assert hit[2] == "TP" and hit[4] - hit[3] == pytest.approx(1.001)  # put in = cost basis


def test_usd_compact():
    from paperpump.service import usd
    assert [usd(950), usd(41200), usd(1.3e6), usd(2.1e9)] == ["$950", "$41.2K", "$1.3M", "$2.1B"]


def test_buy_text_and_volume_parsing():
    from paperpump.service import buy_text
    q, f, _ = run(make()[1].buy(1, MINT, 0.5))
    t = buy_text(f, q)
    assert t.startswith("🟢 Bought $DEMO") and "MC: $50.0K · Vol 24h: $210.0K · Liq: $20.0K" in t
    assert "Spent: 0.5 SOL" in t and "Entry MC:" in t and "Impact" in t and "drift" not in t.lower()
    data = [{"chainId": "solana", "priceNative": "1", "baseToken": {"address": MINT}, "quoteToken": {"address": WSOL},
             "volume": {"h24": 1234.5}}]
    assert parse_pairs(MINT, data).volume_24h == 1234.5


def _pos(i, sym="FROG", tokens=1e6, cost=0.5):
    return {"id": i, "symbol": sym, "tokens": tokens, "cost_sol": cost, "tp_pct": 100, "sl_pct": None, "mint": MINT}


def test_portfolio_text_market_line_and_unavailable():
    from paperpump.service import portfolio_text
    q = run(FakePrices(price=1e-6).quote(MINT))
    q.volume_24h = 210000
    text, shown = portfolio_text(10, 150, [_pos(1), _pos(2, "DOG")], [q, None])
    assert "   MC: $50.0K · Vol 24h: $210.0K · Liq: $20.0K" in text
    assert "Entry MC $25.0K" in text  # paid 0.5 SOL for 1M tokens = 5e-7 vs price 1e-6
    assert "$DOG" in text and "   Market data unavailable" in text
    assert shown == [1, 2]


def test_portfolio_text_stays_under_telegram_limit():
    from paperpump.service import portfolio_text
    q = run(FakePrices().quote(MINT))
    positions = [_pos(i, "X" * 30) for i in range(200)]
    text, shown = portfolio_text(10, 150, positions, [q] * 200)
    assert len(text) < 4096 and 0 < len(shown) < 200 and "more" in text


def test_leaderboard_winrate_and_resets():
    from paperpump.service import leaderboard_text
    db, t = make()
    t.reset(1, now=1000)
    t.reset(1, now=1000 + 25 * 3600)
    for price in (3e-6, 0.5e-6, 2e-6):  # win, loss, win
        t.prices.price = 1e-6
        run(t.buy(1, MINT, 1))
        t.prices.price = price
        run(t.sell(1, MINT, 100))
    r = db.leaderboard()[0]
    assert (r["n"], r["wins"], r["resets"]) == (3, 2, 2)
    line = leaderboard_text(db.leaderboard()).splitlines()[1]
    assert line.startswith("🥇 alice  +") and "Win 67% (3 trades) · Resets 2" in line


def test_migration_keeps_old_rows(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.executescript("CREATE TABLE users(tg_id INTEGER PRIMARY KEY, username TEXT, balance REAL NOT NULL,"
                    " created_at REAL NOT NULL, last_reset REAL);"
                    "INSERT INTO users VALUES(1,'old',50,0,NULL),(2,'reset_before',100,0,123);")
    c.commit()
    c.close()
    db = DB(str(path))
    assert db.user(1)["reset_count"] == 0 and db.user(2)["reset_count"] == 1
    assert db.user(1)["balance"] == 50
    DB(str(path))  # running the migration twice is harmless
    assert db.create_user(3, "new", 100)
