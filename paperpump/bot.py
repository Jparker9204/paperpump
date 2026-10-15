"""Telegram bot entry point: python -m paperpump.bot"""
import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from . import config
from .db import DB
from .fill import pnl
from .prices import DexScreener
from .service import MINT_RE, TradeError, Trader, fill_text

log = logging.getLogger("paperpump")


def T(ctx) -> Trader:
    return ctx.bot_data["trader"]


async def reply(update: Update, text: str, **kw):
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, **kw)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    tr = T(ctx)
    new = tr.db.create_user(u.id, u.username or u.first_name, tr.cfg.start_balance)
    bal = tr.db.user(u.id)["balance"]
    await reply(update, ("Welcome to <b>PaperPump</b> 🧪\n" if new else "Welcome back!\n") +
                f"Paper balance: <b>{bal:.2f} SOL</b>\n\nPaste a Solana contract address, or use\n"
                "/buy &lt;mint&gt; &lt;sol&gt; · /sell &lt;mint&gt; &lt;%&gt; · /portfolio · /history · /stats\n"
                "/tp &lt;mint&gt; &lt;%&gt; · /sl &lt;mint&gt; &lt;%&gt; · /leaderboard · /reset\n\n"
                "<i>Simulated trading only. No wallet, no real funds, not financial advice.</i>")


async def do_buy(update, ctx, mint, sol):
    try:
        q, f, sol_usd = await T(ctx).buy(update.effective_user.id, mint, sol)
        await reply(update, fill_text(f, html.escape(q.symbol), sol_usd))
    except TradeError as e:
        await reply(update, f"❌ {html.escape(str(e))}")


async def buy(update, ctx):
    try:
        mint, sol = ctx.args[0], float(ctx.args[1])
    except (IndexError, ValueError):
        return await reply(update, "Usage: /buy &lt;mint&gt; &lt;sol&gt;")
    await do_buy(update, ctx, mint, sol)


async def sell(update, ctx):
    try:
        mint, pct = ctx.args[0], float(ctx.args[1].rstrip("%")) if len(ctx.args) > 1 else 100.0
    except (IndexError, ValueError):
        return await reply(update, "Usage: /sell &lt;mint&gt; &lt;percent&gt;")
    try:
        q, f, sol_usd, realized = await T(ctx).sell(update.effective_user.id, mint, pct)
        await reply(update, fill_text(f, html.escape(q.symbol), sol_usd) + f"\nRealized PnL: <b>{realized:+.4f} SOL</b>")
    except TradeError as e:
        await reply(update, f"❌ {html.escape(str(e))}")


async def portfolio(update, ctx):
    tr, uid = T(ctx), update.effective_user.id
    u = tr.db.user(uid)
    if not u:
        return await reply(update, "Run /start first.")
    sol_usd = await tr.prices.sol_usd()
    lines = [f"💼 Balance: <b>{u['balance']:.3f} SOL</b> (${u['balance'] * sol_usd:,.0f})"]
    for p in tr.db.open_positions(uid):
        q = await tr.prices.quote(p["mint"])
        val = p["tokens"] * q.price_sol if q else 0
        d, pct = pnl(p["cost_sol"], val)
        icon = "🟢" if d >= 0 else "🔴"
        lines.append(f"{icon} ${html.escape(p['symbol'] or '?')}: cost {p['cost_sol']:.3f} → {val:.3f} SOL "
                     f"({pct:+.1f}%) TP {p['tp_pct'] or '-'} / SL {p['sl_pct'] or '-'}\n<code>{p['mint']}</code>")
    if len(lines) == 1:
        lines.append("No open positions.")
    await reply(update, "\n".join(lines))


async def history(update, ctx):
    rows = T(ctx).db.history(update.effective_user.id)
    if not rows:
        return await reply(update, "No trades yet.")
    out = []
    for r in rows:
        p = f" PnL {r['pnl_sol']:+.4f}" if r["pnl_sol"] is not None else ""
        out.append(f"{r['side'].upper()} ${html.escape(r['symbol'] or '?')} {r['sol']:.4f} SOL{p}")
    await reply(update, "📜 Last trades\n" + "\n".join(out))


async def stats(update, ctx):
    s = T(ctx).db.stats(update.effective_user.id)
    await reply(update, f"📊 Closed trades: {s['closed']}\nWin rate: {s['win_rate']:.1f}%\n"
                f"Net PnL: <b>{s['net_pnl']:+.4f} SOL</b>\nAvg hold: {s['avg_hold_s'] / 60:.1f} min")


async def tpsl(update, ctx):
    kind = update.effective_message.text.split()[0].lstrip("/").split("@")[0]
    try:
        mint, pct = ctx.args[0], abs(float(ctx.args[1].rstrip("%")))
    except (IndexError, ValueError):
        return await reply(update, f"Usage: /{kind} &lt;mint&gt; &lt;percent&gt;  e.g. /{kind} MINT {100 if kind == 'tp' else 30}")
    tr = T(ctx)
    pos = tr.db.open_position(update.effective_user.id, mint)
    if not pos:
        return await reply(update, "No open position in that token.")
    tr.db.set_tpsl(pos["id"], **{kind: pct})
    await reply(update, f"✅ {kind.upper()} set at {'+' if kind == 'tp' else '-'}{pct:g}% for ${html.escape(pos['symbol'] or '?')}")


async def reset(update, ctx):
    try:
        T(ctx).reset(update.effective_user.id)
        await reply(update, f"🔄 Reset. Balance is {T(ctx).cfg.start_balance:g} paper SOL. Open positions were closed.")
    except TradeError as e:
        await reply(update, f"❌ {html.escape(str(e))}")


async def leaderboard(update, ctx):
    rows = T(ctx).db.leaderboard()
    medals = ["🥇", "🥈", "🥉"]
    out = [f"{medals[i] if i < 3 else f'{i + 1}.'} {html.escape(r['username'] or 'anon')}: {r['pnl']:+.3f} SOL ({r['n']} trades)"
           for i, r in enumerate(rows)]
    await reply(update, "🏆 Leaderboard (realized PnL since last reset)\n" + ("\n".join(out) or "Empty"))


async def token_card(update, ctx):
    mint = update.effective_message.text.strip()
    if not MINT_RE.match(mint):
        return
    q = await T(ctx).prices.quote(mint)
    if not q:
        return await reply(update, "No Solana/SOL pair found on DexScreener for that address.")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"Buy {a}", callback_data=f"b|{a}|{mint}")
                                for a in ("0.1", "0.25", "0.5", "1")]])
    await reply(update, f"🪙 <b>{html.escape(q.name)}</b> (${html.escape(q.symbol)})\n"
                f"Price: ${q.price_usd:.8g}\nMC: ${q.market_cap:,.0f}\nLiquidity: ${q.liquidity_usd:,.0f}\n"
                f"Age: {q.age} · {html.escape(q.dex)}\n<code>{mint}</code>", reply_markup=kb)


async def on_button(update, ctx):
    qd = update.callback_query
    await qd.answer("Simulating fill…")
    _, amt, mint = qd.data.split("|")
    await do_buy(update, ctx, mint, float(amt))


async def tpsl_job(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        for tg_id, sym, reason, realized in await T(ctx).check_tpsl():
            await ctx.bot.send_message(tg_id, f"🎯 {reason} hit on ${sym}: closed, realized {realized:+.4f} SOL (paper)")
    except Exception:
        log.exception("tp/sl check failed")


def main():
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # keeps the bot token out of logs
    cfg = config.load()
    app = Application.builder().token(cfg.token).build()
    app.bot_data["trader"] = Trader(DB(cfg.db_path), DexScreener(), cfg)
    for name, fn in [("start", start), ("buy", buy), ("sell", sell), ("portfolio", portfolio),
                     ("history", history), ("stats", stats), ("tp", tpsl), ("sl", tpsl),
                     ("reset", reset), ("leaderboard", leaderboard)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^b\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, token_card))
    app.job_queue.run_repeating(tpsl_job, interval=cfg.tpsl_interval, first=10)
    app.run_polling()


if __name__ == "__main__":
    main()
