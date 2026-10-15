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

    async def _quotes(self, mint):
        q = await self.prices.quote(mint)
        if not q:
            raise TradeError("No Solana/SOL pair found on DexScreener for that mint.")
        sol_usd = await self.prices.sol_usd()
        await self.sleep(self.cfg.delay_seconds)  # simulated landing delay
        q2 = await self.prices.quote(mint) or q
        return q, q2, sol_usd

    async def buy(self, tg_id, mint, sol):
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

    async def sell(self, tg_id, mint, percent, pos=None):
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
        """Close positions whose TP or SL hit. Returns list of (tg_id, symbol, reason, realized)."""
        hits = []
        for pos in self.db.open_positions():
            if pos["tp_pct"] is None and pos["sl_pct"] is None:
                continue
            q = await self.prices.quote(pos["mint"])
            if not q or pos["cost_sol"] <= 0:
                continue
            _, pct = fm.pnl(pos["cost_sol"], pos["tokens"] * q.price_sol)
            reason = ("TP" if pos["tp_pct"] is not None and pct >= pos["tp_pct"] else
                      "SL" if pos["sl_pct"] is not None and pct <= -abs(pos["sl_pct"]) else None)
            if reason:
                _, _, _, realized = await self.sell(pos["tg_id"], pos["mint"], 100, pos)
                hits.append((pos["tg_id"], pos["symbol"], reason, realized))
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
