"""Bot messages must not contain bare domains (Telegram auto-links them) and must disable link previews."""
import ast
import asyncio
import re
from pathlib import Path

from paperpump import bot, ui
from tests.test_bot_handlers import make_ctx, make_update

DOMAIN = re.compile(r"\b[a-z0-9-]+\.(?:fun|com|io|xyz|org|net|app|so|gg|me|co|ai|sh|tv|dev|bot|gl|ly|to)\b", re.I)
SRC = Path(bot.__file__).parent


def bot_strings():
    for name in ("ui.py", "bot.py", "service.py"):
        tree = ast.parse((SRC / name).read_text())
        docs = {id(n.body[0].value) for n in ast.walk(tree)
                if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
                if not n.value.startswith(("http://", "https://")):  # real URLs (API/button) are fine
                    yield name, n.value


def test_no_bare_domains_in_bot_strings():
    bad = [(f, s) for f, s in bot_strings() if DOMAIN.search(s)]
    assert bad == []


def test_rendered_messages_have_no_bare_domains():
    for text in (ui.HELP_TEXT, ui.NUDGE, ui.menu_text(100), ui.SEARCH_PROMPT, ui.search_text("x", []),
                 ui.trending_text([])):
        assert not DOMAIN.search(text), text
    assert DOMAIN.search("see pump.fun") and not DOMAIN.search("Pump.fun-style".replace(".", ""))


def test_help_and_menu_disable_link_previews():
    ctx = make_ctx()
    for screen in ("hp", "menu"):
        upd, ev = make_update(text="x")
        asyncio.run(bot.SCREENS[screen](upd, ctx))
        opts = ev[-1][3]["link_preview_options"]
        assert opts.is_disabled is True and not DOMAIN.search(ev[-1][2][0])
