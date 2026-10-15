"""Run the real handlers with mocked Telegram objects and time them."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from paperpump import bot
from paperpump.db import DB
from paperpump.prices import Quote
from paperpump.service import Trader

MINT = "DemoMint1111111111111111111111111111111pump"
CFG = SimpleNamespace(start_balance=100.0, fee_rate=0.01, priority_fee=0.001, delay_seconds=0.2,
                      reset_cooldown_hours=24)


class SlowPrices:
    """Every price call takes 0.3s, like a slow API."""
    async def quote(self, mint, max_age=3.0):
        await asyncio.sleep(0.3)
        return Quote(mint, "Demo", "DEMO", 1.5e-4, 1e-6, 20000, 50000, None, "pumpswap", "")

    async def sol_usd(self):
        return 150.0


def make_update(uid=1, text=None, data=None, events=None):
    events = events if events is not None else []
    t0 = time.monotonic()

    def rec(name):
        async def f(*a, **k):
            events.append((name, time.monotonic() - t0, a, k))
        return f
    msg = SimpleNamespace(text=text, reply_text=AsyncMock(side_effect=rec("reply")))
    cq = None
    if data:
        cq = SimpleNamespace(data=data, answer=AsyncMock(side_effect=rec("answer")),
                             edit_message_text=AsyncMock(side_effect=rec("edit")))
    upd = MagicMock(spec=bot.Update)
    upd.effective_user = SimpleNamespace(id=uid, username="tester", first_name="T")
    upd.effective_chat = SimpleNamespace(id=uid)
    upd.effective_message = msg
    upd.callback_query = cq
    return upd, events


def make_ctx():
    trader = Trader(DB(), SlowPrices(), CFG)
    return SimpleNamespace(bot_data={"trader": trader}, user_data={}, args=[], bot=AsyncMock())


def run(c):
    return asyncio.run(c)


def test_start_is_fast_and_has_no_emoji_only_message():
    ctx = make_ctx()
    upd, ev = make_update(text="/start")
    t = time.monotonic()
    run(bot.cmd_start(upd, ctx))
    assert time.monotonic() - t < 0.1
    texts = [e[2][0] for e in ev if e[0] == "reply"]
    assert len(texts) == 2 and "PaperPump" in texts[1]
    assert all(len(x.strip()) > 3 for x in texts)  # nothing emoji-only


def test_menu_help_and_reset_buttons_respond_quickly():
    ctx = make_ctx()
    for data in ("m|menu", "m|hp", "m|rs", "m|st", "m|hi", "m|lb", "rs|yes"):
        upd, ev = make_update(data=data)
        t = time.monotonic()
        run(bot.on_button(upd, ctx))
        assert time.monotonic() - t < 0.1, data
        assert ev[0][0] == "answer" and any(e[0] in ("edit", "reply") for e in ev), data


def test_callback_answered_before_slow_buy():
    ctx = make_ctx()
    upd, ev = make_update(data=f"b|0.5|{MINT}")
    run(bot.on_button(upd, ctx))
    assert ev[0][0] == "answer" and ev[0][1] < 0.05
    assert ev[-1][1] >= 0.2  # fill reply comes after the simulated delay
    assert "PAPER BUY" in ev[-1][2][0]


def test_handlers_run_concurrently():
    ctx = make_ctx()

    async def both():
        u1, e1 = make_update(uid=1, data=f"b|0.1|{MINT}")
        u2, e2 = make_update(uid=2, data="m|hp")
        t = time.monotonic()
        await asyncio.gather(bot.on_button(u1, ctx), bot.on_button(u2, ctx))
        return e2, time.monotonic() - t
    e2, total = run(both())
    assert e2[-1][1] < 0.1  # help not stuck behind the slow buy
    assert total < 1.5


def test_double_tap_cannot_overspend():
    ctx = make_ctx()
    tr = ctx.bot_data["trader"]
    tr.db.create_user(1, "t", 1.0)

    async def taps():
        await asyncio.gather(*(bot.on_button(make_update(data=f"b|0.6|{MINT}")[0], ctx) for _ in range(2)))
    run(taps())
    assert tr.db.user(1)["balance"] >= 0


def test_error_handler_replies():
    upd, _ = make_update(text="x")
    ctx = SimpleNamespace(error=RuntimeError("boom"), bot=AsyncMock())
    run(bot.on_error(upd, ctx))
    assert ctx.bot.send_message.await_args.args[1] == "Something went wrong, try again."
