"""Trading logic shared by the bot (and later the website)."""
import asyncio
import re
import time

from . import fill as fm
from .db import DB

MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


class TradeError(Exception):
    pass


class Trader:
    def __init__(self, db: DB, prices, cfg, sleep=asyncio.sleep):
        self.db, self.prices, self.cfg, self.sleep = db, prices, cfg, sleep
        self._locks: dict[int, asyncio.Lock] = {}

    def lock(self, tg_id) -> asyncio.Lock:
        """One trade at a time per user (updates run concurrently, so double taps can't overspend)."""
        return self._locks.setdefault(tg_id, asyncio.Lock())

    async def _q(self, mint, fresh=False):
        try:
            return await asyncio.wait_for(self.prices.quote(mint, **({"max_age": 0} if fresh else {})), 8)
        except TradeError:
            raise
        except Exception as e:
            raise TradeError("Price service is slow or unavailable right now. Try again.") from e

    async def _quotes(self, mint):
        q = await self._q(mint)
        if not q:
            raise TradeError("No Solana/SOL pair found on DexScreener for that mint.")
        try:
            sol_usd = await asyncio.wait_for(self.prices.sol_usd(), 8)
        except Exception as e:
            raise TradeError("Price service is slow or unavailable right now. Try again.") from e
        await self.sleep(self.cfg.delay_seconds)  # simulated landing delay (asyncio.sleep: non-blocking)
        q2 = await self._q(mint, fresh=True) or q
        return q, q2, sol_usd

    async def buy(self, tg_id, mint, sol):
        async with self.lock(tg_id):
            return await self._buy(tg_id, mint, sol)

    async def sell(self, tg_id, mint, percent, pos=None):
        async with self.lock(tg_id):
            if pos is not None:  # re-read: it may have changed while we waited for the lock
                pos = self.db.position(pos["id"], tg_id)
                if not pos:
                    raise TradeError("That position is already closed.")
            return await self._sell(tg_id, mint, percent, pos)

    async def _buy(self, tg_id, mint, sol):
        if not MINT_RE.match(mint):
            raise TradeError("That doesn't look like a Solana mint address.")
        u = self.db.user(tg_id)
        if not u:
            raise TradeError("Run /start first.")
        if sol <= 0 or sol + self.cfg.priority_fee > u["balance"]:
            raise TradeError(f"Not enough paper SOL (balance {u['balance']:.3f}).")
        q, q2, sol_usd = await self._quotes(mint)
        f = fm.simulate_buy(sol, q.price_sol, q2.price_sol, fm.sol_reserve(q2.liquidity_usd, sol_usd),
                            self.cfg.fee_rate, self.cfg.priority_fee)
        self.db.record_buy(tg_id, mint, q.symbol, f)
        return q2, f, sol_usd

    async def _sell(self, tg_id, mint, percent, pos=None):
        pos = pos or self.db.open_position(tg_id, mint)
        if not pos:
            raise TradeError("No open position in that token.")
        if not 0 < percent <= 100:
            raise TradeError("Percent must be 1-100.")
        q, q2, sol_usd = await self._quotes(pos["mint"])
        tokens = pos["tokens"] * percent / 100
        f = fm.simulate_sell(tokens, q.price_sol, q2.price_sol, fm.sol_reserve(q2.liquidity_usd, sol_usd),
                             self.cfg.fee_rate, self.cfg.priority_fee)
        realized = self.db.record_sell(pos, f)
        return q2, f, sol_usd, realized

    def reset(self, tg_id, now=None):
        now = now or time.time()
        u = self.db.user(tg_id)
        if not u:
            raise TradeError("Run /start first.")
        last = u["last_reset"]
        wait = (last + self.cfg.reset_cooldown_hours * 3600 - now) if last is not None else 0
        if wait > 0:
            raise TradeError(f"Reset cooldown: try again in {wait / 3600:.1f}h.")
        self.db.reset(tg_id, self.cfg.start_balance, now)

    async def check_tpsl(self):
        """Close positions whose TP or SL hit. Returns list of (tg_id, symbol, reason, realized, got_sol)."""
        hits = []
        watched = [p for p in self.db.open_positions() if p["tp_pct"] is not None or p["sl_pct"] is not None]
        mints = list({p["mint"] for p in watched})
        res = await asyncio.gather(*(self._q(m) for m in mints), return_exceptions=True)
        quotes = {m: r for m, r in zip(mints, res, strict=True) if not isinstance(r, BaseException)}
        for pos in watched:
            q = quotes.get(pos["mint"])
            if not q or pos["cost_sol"] <= 0:
                continue
            _, pct = fm.pnl(pos["cost_sol"], pos["tokens"] * q.price_sol)
            reason = ("TP" if pos["tp_pct"] is not None and pct >= pos["tp_pct"] else
                      "SL" if pos["sl_pct"] is not None and pct <= -abs(pos["sl_pct"]) else None)
            if reason:
                try:
                    _, f, _, realized = await self.sell(pos["tg_id"], pos["mint"], 100, pos)
                except TradeError:
                    continue
                hits.append((pos["tg_id"], pos["symbol"], reason, realized, f.sol))
        return hits


def fill_text(f: fm.Fill, symbol: str, sol_usd: float) -> str:
    if f.side == "buy":
        head = f"🧪 PAPER BUY {f.sol:g} SOL → {f.tokens:,.0f} ${symbol}"
        naive = f"Naive paper fill: {f.naive:,.0f} tokens ({(f.naive / f.tokens - 1) * 100:+.2f}% vs realistic)"
    else:
        head = f"🧪 PAPER SELL {f.tokens:,.0f} ${symbol} → {f.sol:.4f} SOL (${f.sol * sol_usd:,.2f})"
        naive = f"Naive paper fill: {f.naive:.4f} SOL ({(f.naive / f.sol - 1) * 100 if f.sol > 0 else 0:+.2f}% vs realistic)"
    return (f"{head}\n\n<b>Realistic fill</b>\n"
            f"• Price impact: {f.impact_pct:.2f}%\n"
            f"• Fees: {f.fee_sol:.4f} SOL (1% swap + {f.priority_fee:g} priority)\n"
            f"• Simulated delay drift: {f.drift_pct:+.2f}%\n"
            f"• {naive}\n<i>Paper trade, no real funds.</i>")


def sell_text(symbol: str, percent: float, got_sol: float, realized: float, prefix: str = "") -> str:
    """Simple sell confirmation. Put in = cost basis of the sold portion (got back minus PnL)."""
    put_in = got_sol - realized
    pct = realized / put_in * 100 if put_in else 0.0
    icon = "🟢" if realized >= 0 else "🔴"
    return (f"{prefix}{icon} Sold {percent:g}% of ${symbol}\n"
            f"Put in: {put_in:.2f} SOL\nGot back: {got_sol:.2f} SOL\n"
            f"PnL: <b>{realized:+.2f} SOL ({pct:+.1f}%)</b>")
