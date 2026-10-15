from paperpump import ui
from paperpump.db import DB

MINT = "DemoMint1111111111111111111111111111111pump"


def all_data(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def test_callback_data_fits_telegram_limit():
    long_mint = "A" * 44
    for kb in (ui.token_kb(long_mint), ui.portfolio_kb([999999, 1]), ui.tpsl_kb("tp", 999999), ui.menu_kb()):
        assert all(len(d.encode()) <= 64 for d in all_data(kb))


def test_menu_has_all_items_and_no_command_list():
    data = all_data(ui.menu_kb())
    assert set(data) == {f"m|{k}" for k in ui.MENU_LABELS.values()} | {"m|web"}
    text = ui.menu_text(100) + ui.HELP_TEXT
    assert "/buy" not in text and "/sell" not in text
    labels = [b.text for row in ui.reply_keyboard().keyboard for b in row]
    assert set(labels) == set(ui.MENU_LABELS)


def test_token_and_portfolio_buttons():
    data = all_data(ui.token_kb(MINT))
    assert [d for d in data if d.startswith("b|")] == [f"b|{a}|{MINT}" for a in ui.BUY_AMOUNTS]
    assert f"c|{MINT}" in data and "m|menu" in data
    pf = all_data(ui.portfolio_kb([7]))
    assert {"s|7|25", "s|7|50", "s|7|100", "tpm|7", "slm|7", "m|pf", "m|menu"} <= set(pf)
    assert {"sl|7|10", "sl|7|20", "sl|7|30", "sl|7|0"} <= set(all_data(ui.tpsl_kb("sl", 7)))


def test_parse_amount():
    assert ui.parse_amount("0.3") == 0.3
    assert ui.parse_amount("0,5 SOL") == 0.5
    assert ui.parse_amount("abc") is None
    assert ui.parse_amount("-1") is None


def test_position_lookup_is_scoped_to_owner_and_clear_tpsl():
    db = DB()
    db.create_user(1, "a", 100)
    pid = db.c.execute("INSERT INTO positions(tg_id,mint,symbol,tokens,cost_sol,opened_at,tp_pct)"
                       " VALUES(1,?,?,1,1,0,50)", (MINT, "X")).lastrowid
    assert db.position(pid, 1) is not None
    assert db.position(pid, 2) is None  # other users can't touch it
    db.clear_tpsl(pid, "tp")
    assert db.position(pid, 1)["tp_pct"] is None
