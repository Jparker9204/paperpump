"""Telegram bot entry point: python -m paperpump.bot"""
import asyncio
import logging
import time

from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from . import auth, config, ui
from .db import DB
from .prices import DexScreener
from .service import MINT_RE, TradeError, Trader, buy_text, leaderboard_text, portfolio_text, sell_text

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
    try:
        sol_usd = await asyncio.wait_for(tr.prices.sol_usd(), 8)
    except Exception:
        sol_usd = 0.0
    positions = tr.db.open_positions(u["tg_id"])
    res = await asyncio.gather(*(tr._q(p["mint"]) for p in positions), return_exceptions=True)
    quotes = [None if isinstance(q, BaseException) else q for q in res]
    text, shown = portfolio_text(u["balance"], sol_usd, positions, quotes)
    await send(update, text, ui.portfolio_kb(shown), edit)


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
    text = leaderboard_text(T(ctx).db.leaderboard())
    await send(update, text, ui.back_kb(), edit)


async def show_reset(update, ctx, edit=False):
    ensure_user(update, ctx)
    await send(update, "🔄 Start over with a fresh 100 SOL? Open positions will be closed.\n(Once per day.)",
               ui.reset_confirm_kb(), edit)


async def show_help(update, ctx, edit=False):
    await send(update, ui.HELP_TEXT, ui.back_kb(), edit)


async def show_web(update, ctx, edit=False):
    u, cfg = ensure_user(update, ctx), T(ctx).cfg
    if not (cfg.base_url and cfg.session_secret):
        return await send(update, "The website isn't available right now.", ui.back_kb(), edit)
    url = auth.login_url(cfg.base_url, cfg.session_secret, u["tg_id"])
    # Always a new message (never edited into an old one) so the link is easy to find. Valid 10 min, one use.
    await send(update, "🌐 Your personal login link is below.\nIt works once and expires in 10 minutes. "
               "Don't share it.", ui.web_link_kb(url))


SCREENS = {"menu": show_menu, "pf": show_portfolio, "st": show_stats, "hi": show_history,
           "lb": show_leaderboard, "rs": show_reset, "hp": show_help,
           "web": show_web}


# ---------- actions ----------
async def do_buy(update, ctx, mint, sol):
    ensure_user(update, ctx)
    try:
        q, f, _ = await T(ctx).buy(update.effective_user.id, mint, sol)
        q.symbol = ui.esc(q.symbol)
        await send(update, buy_text(f, q), ui.after_trade_kb())
    except TradeError as e:
        await send(update, f"❌ {ui.esc(e)}", ui.back_kb())


async def token_card(update, ctx, mint):
    try:
        q = await T(ctx)._q(mint)
    except TradeError as e:
        return await send(update, f"❌ {ui.esc(e)}", ui.back_kb())
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
    parts = (q.data or "").split("|")
    kind = parts[0]
    # Answer right away (Telegram only accepts answers for ~15s, and it stops the button spinner).
    try:
        await q.answer("Simulating fill…" if kind in ("b", "s") else None)
    except TelegramError as e:
        log.warning("answer_callback_query failed: %s", e)
    tr, uid = T(ctx), update.effective_user.id
    if kind == "m":
        return await SCREENS.get(parts[1], show_menu)(update, ctx, edit=True)
    if kind == "b":
        return await do_buy(update, ctx, parts[2], float(parts[1]))
    if kind == "c":
        ctx.user_data["custom_mint"] = parts[1]
        return await send(update, "✏️ How much SOL do you want to spend? Type a number, like <b>0.3</b>.", ui.back_kb())
    if kind == "rs":
        try:
            tr.reset(uid)
            return await show_menu(update, ctx, edit=True)
        except TradeError as e:
            return await send(update, f"❌ {ui.esc(e)}", ui.back_kb(), edit=True)
    pos = tr.db.position(int(parts[1]), uid)
    if not pos:
        return await show_portfolio(update, ctx, edit=True)
    if kind == "s":
        try:
            _, f, sol_usd, realized = await tr.sell(uid, pos["mint"], float(parts[2]), pos)
            await send(update, sell_text(ui.esc(pos["symbol"]), float(parts[2]), f.sol, realized),
                       ui.after_trade_kb(), edit=True)
        except TradeError as e:
            await send(update, f"❌ {ui.esc(e)}", ui.back_kb(), edit=True)
    elif kind in ("tpm", "slm"):
        k = kind[:2]
        what = "Take profit: auto-sell when you're up" if k == "tp" else "Stop loss: auto-sell when you're down"
        await send(update, f"🎯 <b>${ui.esc(pos['symbol'])}</b>\n{what}…", ui.tpsl_kb(k, pos["id"]), edit=True)
    elif kind in ("tp", "sl"):
        pct = float(parts[2])
        if pct:
            tr.db.set_tpsl(pos["id"], **{kind: pct})
        else:
            tr.db.clear_tpsl(pos["id"], kind)
        await show_portfolio(update, ctx, edit=True)


# ---------- slash commands (kept for power users, not advertised) ----------
async def cmd_start(update, ctx):
    ensure_user(update, ctx)
    # Sets the always-visible bottom keyboard. Not emoji-only (Telegram renders those huge).
    await update.effective_message.reply_text("Your quick buttons are at the bottom of the chat.",
                                              reply_markup=ui.reply_keyboard())
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
        await send(update, sell_text(ui.esc(q.symbol), pct, f.sol, realized),
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
        for tg_id, sym, reason, realized, got in await T(ctx).check_tpsl():
            label = "Take profit" if reason == "TP" else "Stop loss"
            await ctx.bot.send_message(tg_id, sell_text(ui.esc(sym), 100, got, realized, prefix=f"🎯 {label} hit\n"),
                                       parse_mode=ParseMode.HTML,
                                       reply_markup=ui.after_trade_kb())
    except Exception:
        log.exception("tp/sl check failed")


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("handler error", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await ctx.bot.send_message(update.effective_chat.id, "Something went wrong, try again.",
                                       reply_markup=ui.back_kb())
        except TelegramError:
            pass


async def loop_watchdog():
    """Logs a warning if something blocks the event loop (helps catch freezes)."""
    while True:
        t = time.monotonic()
        await asyncio.sleep(1)
        lag = time.monotonic() - t - 1
        if lag > 0.5:
            log.warning("event loop was blocked for %.1fs", lag)


async def post_init(app: Application):
    app.bot_data["watchdog"] = asyncio.get_running_loop().create_task(loop_watchdog())
    await app.bot.set_my_commands([BotCommand("start", "Open PaperPump"), BotCommand("menu", "Main menu"),
                                   BotCommand("help", "How it works")])


def main():
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # keeps the bot token out of logs
    cfg = config.load()
    app = Application.builder().token(cfg.token).post_init(post_init).concurrent_updates(True).build()
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
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(tpsl_job, interval=cfg.tpsl_interval, first=10)
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
