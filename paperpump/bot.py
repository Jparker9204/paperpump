"""Telegram bot entry point: python -m paperpump.bot"""
import logging

from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from . import config, ui
from .db import DB
from .fill import pnl
from .prices import DexScreener
from .service import MINT_RE, TradeError, Trader, fill_text

log = logging.getLogger("paperpump")


def T(ctx) -> Trader:
    return ctx.bot_data["trader"]


async def send(update: Update, text, kb=None, edit=False):
    """Edit the button's message in place when possible, otherwise send a new one."""
    q = update.callback_query
    if edit and q:
        try:
            return await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                             disable_web_page_preview=True)
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                              disable_web_page_preview=True)


def ensure_user(update, ctx):
    u = update.effective_user
    tr = T(ctx)
    tr.db.create_user(u.id, u.username or u.first_name, tr.cfg.start_balance)
    return tr.db.user(u.id)


# ---------- screens ----------
async def show_menu(update, ctx, edit=False):
    u = ensure_user(update, ctx)
    await send(update, ui.menu_text(u["balance"]), ui.menu_kb(), edit)


async def show_portfolio(update, ctx, edit=False):
    tr, u = T(ctx), ensure_user(update, ctx)
    sol_usd = await tr.prices.sol_usd()
    lines = [f"💼 <b>Portfolio</b>\nBalance: <b>{u['balance']:.3f} SOL</b> (${u['balance'] * sol_usd:,.0f})"]
    positions = tr.db.open_positions(u["tg_id"])
    for i, p in enumerate(positions, 1):
        q = await tr.prices.quote(p["mint"])
        val = p["tokens"] * q.price_sol if q else 0
        d, pct = pnl(p["cost_sol"], val)
        tp = f"+{p['tp_pct']:g}%" if p["tp_pct"] else "off"
        sl = f"-{p['sl_pct']:g}%" if p["sl_pct"] else "off"
        lines.append(f"\n{i}. {'🟢' if d >= 0 else '🔴'} <b>${ui.esc(p['symbol'])}</b>  {pct:+.1f}% ({d:+.3f} SOL)\n"
                     f"   Cost {p['cost_sol']:.3f} → now {val:.3f} SOL · TP {tp} · SL {sl}")
    if not positions:
        lines.append("\nNo open positions yet. Paste a contract address to buy something.")
    elif len(positions) > 1:
        lines.append("\n<i>Buttons below are in the same order as the list.</i>")
    await send(update, "\n".join(lines), ui.portfolio_kb([p["id"] for p in positions]), edit)


async def show_stats(update, ctx, edit=False):
    s = T(ctx).db.stats(ensure_user(update, ctx)["tg_id"])
    await send(update, f"📊 <b>Your stats</b>\nClosed trades: {s['closed']}\nWin rate: {s['win_rate']:.1f}%\n"
               f"Net PnL: <b>{s['net_pnl']:+.4f} SOL</b>\nAverage hold: {s['avg_hold_s'] / 60:.1f} min",
               ui.back_kb(), edit)


async def show_history(update, ctx, edit=False):
    rows = T(ctx).db.history(ensure_user(update, ctx)["tg_id"])
    out = [f"{'🟢 BUY ' if r['side'] == 'buy' else '🔴 SELL'} ${ui.esc(r['symbol'])} {r['sol']:.4f} SOL"
           + (f" · PnL {r['pnl_sol']:+.4f}" if r["pnl_sol"] is not None else "") for r in rows]
    await send(update, "📜 <b>Recent trades</b>\n" + ("\n".join(out) or "No trades yet."), ui.back_kb(), edit)


async def show_leaderboard(update, ctx, edit=False):
    rows = T(ctx).db.leaderboard()
    medals = ["🥇", "🥈", "🥉"]
    out = [f"{medals[i] if i < 3 else f'{i + 1}.'} {ui.esc(r['username'] or 'anon')}: {r['pnl']:+.3f} SOL"
           for i, r in enumerate(rows)]
    await send(update, "🏆 <b>Leaderboard</b> (profit from closed trades)\n" + ("\n".join(out) or "Empty"),
               ui.back_kb(), edit)


async def show_reset(update, ctx, edit=False):
    ensure_user(update, ctx)
    await send(update, "🔄 Start over with a fresh 100 SOL? Open positions will be closed.\n(Once per day.)",
               ui.reset_confirm_kb(), edit)


async def show_help(update, ctx, edit=False):
    await send(update, ui.HELP_TEXT, ui.back_kb(), edit)


SCREENS = {"menu": show_menu, "pf": show_portfolio, "st": show_stats, "hi": show_history,
           "lb": show_leaderboard, "rs": show_reset, "hp": show_help}


# ---------- actions ----------
async def do_buy(update, ctx, mint, sol):
    ensure_user(update, ctx)
    try:
        q, f, sol_usd = await T(ctx).buy(update.effective_user.id, mint, sol)
        await send(update, fill_text(f, ui.esc(q.symbol), sol_usd), ui.after_trade_kb())
    except TradeError as e:
        await send(update, f"❌ {ui.esc(e)}", ui.back_kb())


async def token_card(update, ctx, mint):
    q = await T(ctx).prices.quote(mint)
    if not q:
        return await send(update, "Couldn't find that coin on DexScreener (it may be too new). Try again in a minute.",
                          ui.back_kb())
    await send(update, f"🪙 <b>{ui.esc(q.name)}</b> (${ui.esc(q.symbol)})\nPrice: ${q.price_usd:.8g}\n"
               f"Market cap: ${q.market_cap:,.0f}\nLiquidity: ${q.liquidity_usd:,.0f}\nAge: {q.age} · {ui.esc(q.dex)}\n"
               f"<code>{mint}</code>", ui.token_kb(mint))


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.effective_message.text or "").strip()
    if text in ui.MENU_LABELS:
        ctx.user_data.pop("custom_mint", None)
        return await SCREENS[ui.MENU_LABELS[text]](update, ctx)
    if MINT_RE.match(text):
        ctx.user_data.pop("custom_mint", None)
        return await token_card(update, ctx, text)
    mint = ctx.user_data.get("custom_mint")
    if mint:
        amt = ui.parse_amount(text)
        if amt is None:
            return await send(update, "Please type a number of SOL, like <b>0.3</b>.", ui.back_kb())
        ctx.user_data.pop("custom_mint", None)
        return await do_buy(update, ctx, mint, amt)
    await send(update, ui.NUDGE, ui.menu_kb())


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split("|")
    kind = parts[0]
    tr, uid = T(ctx), update.effective_user.id
    if kind == "m":
        await q.answer()
        return await SCREENS.get(parts[1], show_menu)(update, ctx, edit=True)
    if kind == "b":
        await q.answer("Simulating fill…")
        return await do_buy(update, ctx, parts[2], float(parts[1]))
    if kind == "c":
        await q.answer()
        ctx.user_data["custom_mint"] = parts[1]
        return await send(update, "✏️ How much SOL do you want to spend? Type a number, like <b>0.3</b>.", ui.back_kb())
    if kind == "rs":
        await q.answer()
        try:
            tr.reset(uid)
            return await show_menu(update, ctx, edit=True)
        except TradeError as e:
            return await send(update, f"❌ {ui.esc(e)}", ui.back_kb(), edit=True)
    pos = tr.db.position(int(parts[1]), uid)
    if not pos:
        await q.answer("That position is already closed.")
        return await show_portfolio(update, ctx, edit=True)
    if kind == "s":
        await q.answer("Simulating fill…")
        try:
            _, f, sol_usd, realized = await tr.sell(uid, pos["mint"], float(parts[2]), pos)
            await send(update, fill_text(f, ui.esc(pos["symbol"]), sol_usd) + f"\nRealized PnL: <b>{realized:+.4f} SOL</b>",
                       ui.after_trade_kb(), edit=True)
        except TradeError as e:
            await send(update, f"❌ {ui.esc(e)}", ui.back_kb(), edit=True)
    elif kind in ("tpm", "slm"):
        await q.answer()
        k = kind[:2]
        what = "Take profit: auto-sell when you're up" if k == "tp" else "Stop loss: auto-sell when you're down"
        await send(update, f"🎯 <b>${ui.esc(pos['symbol'])}</b>\n{what}…", ui.tpsl_kb(k, pos["id"]), edit=True)
    elif kind in ("tp", "sl"):
        pct = float(parts[2])
        if pct:
            tr.db.set_tpsl(pos["id"], **{kind: pct})
        else:
            tr.db.clear_tpsl(pos["id"], kind)
        await q.answer(f"{kind.upper()} {'set' if pct else 'cleared'}")
        await show_portfolio(update, ctx, edit=True)


# ---------- slash commands (kept for power users, not advertised) ----------
async def cmd_start(update, ctx):
    ensure_user(update, ctx)
    await update.effective_message.reply_text("👋", reply_markup=ui.reply_keyboard())
    await show_menu(update, ctx)


async def cmd_buy(update, ctx):
    try:
        await do_buy(update, ctx, ctx.args[0], float(ctx.args[1]))
    except (IndexError, ValueError):
        await send(update, ui.NUDGE, ui.menu_kb())


async def cmd_sell(update, ctx):
    ensure_user(update, ctx)
    try:
        mint, pct = ctx.args[0], float(ctx.args[1].rstrip("%")) if len(ctx.args) > 1 else 100.0
        q, f, sol_usd, realized = await T(ctx).sell(update.effective_user.id, mint, pct)
        await send(update, fill_text(f, ui.esc(q.symbol), sol_usd) + f"\nRealized PnL: <b>{realized:+.4f} SOL</b>",
                   ui.after_trade_kb())
    except (IndexError, ValueError):
        await show_portfolio(update, ctx)
    except TradeError as e:
        await send(update, f"❌ {ui.esc(e)}", ui.back_kb())


async def cmd_tpsl(update, ctx):
    kind = update.effective_message.text.split()[0].lstrip("/").split("@")[0]
    ensure_user(update, ctx)
    try:
        pos = T(ctx).db.open_position(update.effective_user.id, ctx.args[0])
        pct = abs(float(ctx.args[1].rstrip("%")))
    except (IndexError, ValueError):
        return await show_portfolio(update, ctx)
    if not pos:
        return await send(update, "No open position in that coin.", ui.back_kb())
    T(ctx).db.set_tpsl(pos["id"], **{kind: pct})
    await show_portfolio(update, ctx)


async def tpsl_job(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        for tg_id, sym, reason, realized in await T(ctx).check_tpsl():
            label = "Take profit" if reason == "TP" else "Stop loss"
            await ctx.bot.send_message(tg_id, f"🎯 {label} hit on ${ui.esc(sym)}: sold, {realized:+.4f} SOL (paper)",
                                       reply_markup=ui.after_trade_kb())
    except Exception:
        log.exception("tp/sl check failed")


async def post_init(app: Application):
    await app.bot.set_my_commands([BotCommand("start", "Open PaperPump"), BotCommand("menu", "Main menu"),
                                   BotCommand("help", "How it works")])


def main():
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # keeps the bot token out of logs
    cfg = config.load()
    app = Application.builder().token(cfg.token).post_init(post_init).build()
    app.bot_data["trader"] = Trader(DB(cfg.db_path), DexScreener(), cfg)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", lambda u, c: show_menu(u, c)))
    app.add_handler(CommandHandler("help", lambda u, c: show_help(u, c)))
    app.add_handler(CommandHandler("buy", cmd_buy))
    app.add_handler(CommandHandler("sell", cmd_sell))
    app.add_handler(CommandHandler(["tp", "sl"], cmd_tpsl))
    for name, key in [("portfolio", "pf"), ("stats", "st"), ("history", "hi"), ("leaderboard", "lb"), ("reset", "rs")]:
        app.add_handler(CommandHandler(name, lambda u, c, k=key: SCREENS[k](u, c)))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.job_queue.run_repeating(tpsl_job, interval=cfg.tpsl_interval, first=10)
    app.run_polling()


if __name__ == "__main__":
    main()
