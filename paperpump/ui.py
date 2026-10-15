"""Texts and keyboards (pure functions, no Telegram network calls) so they are easy to test."""
import html

from telegram import InlineKeyboardButton as B
from telegram import InlineKeyboardMarkup, ReplyKeyboardMarkup

PORTFOLIO, STATS, HISTORY, LEADERBOARD, RESET, HELP = (
    "💼 Portfolio", "📊 Stats", "📜 History", "🏆 Leaderboard", "🔄 Reset", "❓ Help")
MENU_LABELS = {PORTFOLIO: "pf", STATS: "st", HISTORY: "hi", LEADERBOARD: "lb", RESET: "rs", HELP: "hp"}
BUY_AMOUNTS = ("0.1", "0.25", "0.5", "1")
TP_PRESETS = (25, 50, 100)
SL_PRESETS = (10, 20, 30)
WEB = "🌐 Open website"
NUDGE = "Paste a contract address or tap a button below."

HELP_TEXT = (
    "❓ <b>How PaperPump works</b>\n\n"
    "You get 100 pretend SOL to practise memecoin trading with real, live prices.\n\n"
    "1. Copy a coin's contract address (from DexScreener, pump.fun, etc.) and paste it here.\n"
    "2. Tap a Buy button to buy with pretend SOL.\n"
    "3. Open 💼 Portfolio to sell, or set a take-profit (auto-sell when up) or stop-loss (auto-sell when down).\n\n"
    "Every trade shows a <i>realistic fill</i>: fees, price impact and a short delay, just like real trading. "
    "That way your results aren't too good to be true.\n\n"
    "🔄 Reset gives you a fresh 100 SOL (once a day).\n"
    "No wallet, no real money, not financial advice."
)


def reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[PORTFOLIO, STATS, HISTORY], [LEADERBOARD, RESET, HELP]],
                               resize_keyboard=True, is_persistent=True)


def back_row():
    return [B("⬅️ Menu", callback_data="m|menu")]


def menu_text(balance: float) -> str:
    return (f"🧪 <b>PaperPump</b>: practice memecoin trading with fake SOL.\n"
            f"Balance: <b>{balance:.2f} SOL</b>\n\nPaste a coin's contract address to trade.")


def menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [B(PORTFOLIO, callback_data="m|pf"), B(STATS, callback_data="m|st")],
        [B(HISTORY, callback_data="m|hi"), B(LEADERBOARD, callback_data="m|lb")],
        [B(RESET, callback_data="m|rs"), B(HELP, callback_data="m|hp")],
        [B(WEB, callback_data="m|web")],
    ])


def web_link_kb(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[B("🌐 Open PaperPump website", url=url)], back_row()])


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([back_row()])


def token_kb(mint: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [B(f"Buy {a} SOL", callback_data=f"b|{a}|{mint}") for a in BUY_AMOUNTS[:2]],
        [B(f"Buy {a} SOL", callback_data=f"b|{a}|{mint}") for a in BUY_AMOUNTS[2:]],
        [B("✏️ Custom amount", callback_data=f"c|{mint}")],
        back_row(),
    ])


def after_trade_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[B(PORTFOLIO, callback_data="m|pf")], back_row()])


def position_kb(pid: int) -> list[list[B]]:
    return [
        [B(f"Sell {p}%", callback_data=f"s|{pid}|{p}") for p in (25, 50, 100)],
        [B("🎯 Set TP", callback_data=f"tpm|{pid}"), B("🛡 Set SL", callback_data=f"slm|{pid}")],
    ]


def portfolio_kb(pids: list[int]) -> InlineKeyboardMarkup:
    rows = []
    for pid in pids:
        rows += position_kb(pid)
    rows.append([B("🔄 Refresh", callback_data="m|pf")])
    rows.append(back_row())
    return InlineKeyboardMarkup(rows)


def tpsl_kb(kind: str, pid: int) -> InlineKeyboardMarkup:
    presets = TP_PRESETS if kind == "tp" else SL_PRESETS
    sign = "+" if kind == "tp" else "-"
    return InlineKeyboardMarkup([
        [B(f"{sign}{p}%", callback_data=f"{kind}|{pid}|{p}") for p in presets],
        [B("Clear", callback_data=f"{kind}|{pid}|0")],
        [B("⬅️ Portfolio", callback_data="m|pf")],
    ])


def reset_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[B("Yes, reset to 100 SOL", callback_data="rs|yes")], back_row()])


def parse_amount(text: str) -> float | None:
    try:
        v = float(text.strip().replace(",", ".").removesuffix("SOL").removesuffix("sol").strip())
    except ValueError:
        return None
    return v if v > 0 else None


def esc(s) -> str:
    return html.escape(str(s or "?"))
