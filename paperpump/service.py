"""Trading logic shared by the bot (and later the website)."""
import asyncio
import re
import time

from . import fill as fm
from .db import DB, DBConflict

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
        try:
            self.db.record_buy(tg_id, mint, q.symbol, f)
        except DBConflict as e:
            raise TradeError(str(e)) from e
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
        try:
            realized = self.db.record_sell(pos, f)
        except DBConflict as e:
            raise TradeError(str(e)) from e
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


def usd(v: float) -> str:
    """Compact USD: $950, $41.2K, $1.3M, $2.1B."""
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"${v / div:.1f}{suf}"
    return f"${v:,.0f}"


def buy_text(f: fm.Fill, q) -> str:
    """Short buy confirmation. q is the quote the fill used (after the simulated delay)."""
    entry_mc = q.market_cap * (f.sol / f.tokens) / q.price_sol if f.tokens and q.price_sol else q.market_cap
    extra = (f.naive / f.tokens - 1) * 100 if f.tokens else 0
    sym = q.symbol.lstrip("$")
    return (f"🟢 Bought ${sym}\n"
            f"MC: {usd(q.market_cap)} · Vol 24h: {usd(q.volume_24h)} · Liq: {usd(q.liquidity_usd)}\n\n"
            f"Spent: {f.sol:g} SOL\nReceived: {f.tokens:,.0f} ${sym}\nEntry MC: {usd(entry_mc)}\n"
            f"Fees {f.fee_sol:.4f} SOL · Impact {f.impact_pct:.2f}%\n"
            f"<i>A naive paper fill would show {extra:.1f}% more tokens.</i>")


def sell_text(symbol: str, percent: float, got_sol: float, realized: float, prefix: str = "") -> str:
    """Simple sell confirmation. Put in = cost basis of the sold portion (got back minus PnL)."""
    put_in = got_sol - realized
    pct = realized / put_in * 100 if put_in else 0.0
    symbol = symbol.lstrip("$")
    icon = "🟢" if realized >= 0 else "🔴"
    return (f"{prefix}{icon} Sold {percent:g}% of ${symbol}\n"
            f"Put in: {put_in:.2f} SOL\nGot back: {got_sol:.2f} SOL\n"
            f"PnL: <b>{realized:+.2f} SOL ({pct:+.1f}%)</b>")


PORTFOLIO_MAX_CHARS = 3800  # Telegram limit is 4096; leave room for the footer and HTML
PORTFOLIO_MAX_POSITIONS = 20  # keeps the button grid well under Telegram's limits


def portfolio_text(balance: float, sol_usd: float, positions, quotes) -> tuple[str, list[int]]:
    """Portfolio message plus the ids of the positions shown (in order, for the buttons)."""
    from html import escape
    usd_bal = f" (${balance * sol_usd:,.0f})" if sol_usd else ""
    lines = [f"💼 <b>Portfolio</b>\nBalance: <b>{balance:.3f} SOL</b>{usd_bal}"]
    shown: list[int] = []
    size = len(lines[0])
    for i, (p, q) in enumerate(zip(positions, quotes, strict=True), 1):
        sym = escape((p["symbol"] or "?").lstrip("$"))
        tp = f"+{p['tp_pct']:g}%" if p["tp_pct"] else "off"
        sl = f"-{p['sl_pct']:g}%" if p["sl_pct"] else "off"
        if q:
            val = p["tokens"] * q.price_sol
            d, pct = fm.pnl(p["cost_sol"], val)
            entry = (f" · Entry MC {usd(q.market_cap * (p['cost_sol'] / p['tokens']) / q.price_sol)}"
                     if p["tokens"] and q.price_sol else "")
            block = (f"\n{i}. {'🟢' if d >= 0 else '🔴'} <b>${sym}</b>  {pct:+.1f}% ({d:+.3f} SOL)\n"
                     f"   Cost {p['cost_sol']:.3f} → now {val:.3f} SOL{entry}\n"
                     f"   MC: {usd(q.market_cap)} · Vol 24h: {usd(q.volume_24h)} · Liq: {usd(q.liquidity_usd)}\n"
                     f"   TP {tp} · SL {sl}")
        else:
            block = (f"\n{i}. ⚪ <b>${sym}</b>\n   Cost {p['cost_sol']:.3f} SOL\n"
                     f"   Market data unavailable\n   TP {tp} · SL {sl}")
        if size + len(block) > PORTFOLIO_MAX_CHARS or len(shown) >= PORTFOLIO_MAX_POSITIONS:
            lines.append(f"\n…and {len(positions) - len(shown)} more (sell some to see them).")
            break
        lines.append(block)
        shown.append(p["id"])
        size += len(block) + 1
    if not positions:
        lines.append("\nNo open positions yet. Paste a contract address to buy something.")
    elif len(shown) > 1:
        lines.append("\n<i>Buttons below are in the same order as the list.</i>")
    return "\n".join(lines), shown


def leaderboard_text(rows) -> str:
    """Ranking is by realized PnL since each user's last reset; win rate and resets are display-only."""
    from html import escape
    medals = ["🥇", "🥈", "🥉"]
    out = []
    for i, r in enumerate(rows):
        n = r["n"]
        win = f"Win {r['wins'] / n * 100:.0f}% ({n} trade{'s' if n != 1 else ''})" if n else "No closed trades"
        out.append(f"{medals[i] if i < 3 else f'{i + 1}.'} {escape(r['username'] or 'anon')}  "
                   f"{r['pnl']:+.1f} SOL · {win} · Resets {r['resets']}")
    return "🏆 <b>Leaderboard</b> (profit from closed trades since last reset)\n" + ("\n".join(out) or "Empty")
