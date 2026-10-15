"""PaperPump website (all endpoints are async: the shared sqlite connection is only used from the event loop thread)

PaperPump website: FastAPI backend + plain HTML/JS pages in paperpump/static.

Run: .venv/bin/uvicorn paperpump.web:app --host 127.0.0.1 --port 8000
Shares the SQLite DB and trading logic (service.py) with the Telegram bot.
"""
import asyncio
import hmac
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import auth, config
from . import fill as fm
from .db import DB
from .prices import DexScreener
from .service import MINT_RE, TradeError, Trader

STATIC = Path(__file__).parent / "static"
PAGES = {"/": "trade.html", "/portfolio": "portfolio.html", "/history": "history.html",
         "/leaderboard": "leaderboard.html"}
OPEN_PATHS = {"/robots.txt", "/gate", "/login", "/static/app.css"}

GATE_HTML = """<!doctype html><html><head><meta charset=utf-8><meta name=robots content="noindex,nofollow">
<meta name=viewport content="width=device-width,initial-scale=1"><title>PaperPump</title>
<link rel=stylesheet href=/static/app.css></head><body class=center><form method=post action=/gate class="card gate">
<div class=logo>🧪 PaperPump</div><p class=mu>Private preview. Enter the password.</p>
<input type=password name=password autofocus placeholder=Password>{err}<button class="btn primary">Enter</button>
</form></body></html>"""


class BuyIn(BaseModel):
    mint: str
    sol: float = Field(gt=0, le=1000)


class SellIn(BaseModel):
    position_id: int
    percent: float = Field(gt=0, le=100)


class TpSlIn(BaseModel):
    position_id: int
    tp: float | None = Field(default=None, ge=0, le=10000)  # 0 / None = off
    sl: float | None = Field(default=None, ge=0, le=100)


def create_app(cfg: config.Config, db: DB, prices, sleep=asyncio.sleep) -> FastAPI:
    app = FastAPI(title="PaperPump", docs_url=None, redoc_url=None, openapi_url=None)
    trader = Trader(db, prices, cfg, sleep=sleep)
    secret = cfg.session_secret
    secure = cfg.base_url.startswith("https://")

    def set_cookie(resp, name, value, max_age):
        resp.set_cookie(name, value, max_age=max_age, httponly=True, secure=secure, samesite="lax")

    def gate_ok(request: Request) -> bool:
        if not cfg.site_password:
            return True
        return hmac.compare_digest(request.cookies.get("pp_gate", ""), auth.gate_cookie(secret, cfg.site_password))

    @app.middleware("http")
    async def headers_and_gate(request: Request, call_next):
        path = request.url.path
        if path not in OPEN_PATHS and not gate_ok(request):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "locked"}, status_code=401)
            return RedirectResponse("/gate", status_code=303)
        resp = await call_next(request)
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    async def current_user(request: Request):
        got = auth.verify(secret, request.cookies.get("pp_session", ""), "session")
        u = db.user(got[0]) if got else None
        if not u:
            raise HTTPException(401, "Not logged in. Open the website from the Telegram bot menu.")
        return u

    @app.get("/robots.txt", response_class=PlainTextResponse)
    async def robots():
        return "User-agent: *\nDisallow: /\n"

    @app.get("/gate", response_class=HTMLResponse)
    async def gate_page():
        return GATE_HTML.format(err="")

    @app.post("/gate")
    async def gate_submit(password: str = Form("")):
        if cfg.site_password and hmac.compare_digest(password.encode(), cfg.site_password.encode()):
            resp = RedirectResponse("/", status_code=303)
            set_cookie(resp, "pp_gate", auth.gate_cookie(secret, cfg.site_password), 30 * 86400)
            return resp
        return HTMLResponse(GATE_HTML.format(err="<p class=r>Wrong password.</p>"), status_code=401)

    @app.get("/login")
    async def login(t: str = ""):
        got = auth.verify(secret, t, "login")
        if not got or not db.user(got[0]) or not db.use_nonce(got[1]):
            return HTMLResponse("<p>This login link is invalid, expired or already used. "
                                "Tap 🌐 Open website in the bot to get a new one.</p>", status_code=401)
        resp = RedirectResponse("/", status_code=303)
        set_cookie(resp, "pp_session", auth.sign(secret, "session", got[0], auth.SESSION_TTL), auth.SESSION_TTL)
        if cfg.site_password:  # a valid bot link also unlocks the preview gate
            set_cookie(resp, "pp_gate", auth.gate_cookie(secret, cfg.site_password), 30 * 86400)
        return resp

    @app.post("/logout")
    async def logout():
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie("pp_session")
        return resp

    def _page(fname):
        async def page():
            return FileResponse(STATIC / fname)
        return page

    for route, fname in PAGES.items():
        app.add_api_route(route, _page(fname), methods=["GET"], include_in_schema=False)

    @app.get("/static/{name}")
    async def static(name: str):
        f = (STATIC / name).resolve()
        if f.parent != STATIC.resolve() or not f.is_file():
            raise HTTPException(404)
        return FileResponse(f)

    # ---------------- API ----------------
    async def sol_usd():
        try:
            return await asyncio.wait_for(prices.sol_usd(), 8)
        except Exception:
            return 0.0

    def err(e: Exception):
        raise HTTPException(400, str(e)) from e

    @app.get("/api/me")
    async def me(u=Depends(current_user)):
        return {"username": u["username"], "balance": u["balance"], "sol_usd": await sol_usd()}

    def token_json(q):
        return {"mint": q.mint, "name": q.name, "symbol": q.symbol.lstrip("$"), "price_usd": q.price_usd,
                "price_sol": q.price_sol, "market_cap": q.market_cap, "liquidity_usd": q.liquidity_usd,
                "volume_24h": q.volume_24h, "age": q.age, "dex": q.dex, "url": q.url,
                "pair_address": q.pair_address, "holders": None}  # DexScreener doesn't provide holders

    @app.get("/api/token/{mint}")
    async def token(mint: str, u=Depends(current_user)):
        if not MINT_RE.match(mint):
            raise HTTPException(400, "That doesn't look like a Solana contract address.")
        try:
            return token_json(await trader._q(mint))
        except TradeError as e:
            err(e)

    @app.get("/api/quotes")
    async def quotes(mints: str, u=Depends(current_user)):
        """Live prices for several coins in one call (shared ~1s cache, batched upstream). Max 30."""
        wanted = [m for m in dict.fromkeys(mints.split(",")) if MINT_RE.match(m)][:30]
        try:
            got = await asyncio.wait_for(prices.quotes(wanted), 8)
        except Exception as e:
            raise HTTPException(503, "Price service is busy, retrying…") from e
        return {m: ({"price_sol": q.price_sol, "price_usd": q.price_usd, "market_cap": q.market_cap,
                     "liquidity_usd": q.liquidity_usd, "volume_24h": q.volume_24h, "age": q.age} if q else None)
                for m, q in got.items()}

    @app.get("/api/quote")
    async def quote(mint: str, side: str = "buy", amount: float = 0.0, u=Depends(current_user)):
        """Realistic-fill preview (no delay, nothing saved)."""
        try:
            q = await trader._q(mint)
            reserve = fm.sol_reserve(q.liquidity_usd, await sol_usd())
            if side == "buy":
                f = fm.simulate_buy(amount, q.price_sol, q.price_sol, reserve, cfg.fee_rate, cfg.priority_fee)
            else:
                pos = db.open_position(u["tg_id"], mint)
                if not pos:
                    raise TradeError("No open position in that coin.")
                f = fm.simulate_sell(pos["tokens"] * amount / 100, q.price_sol, q.price_sol, reserve,
                                     cfg.fee_rate, cfg.priority_fee)
        except (TradeError, ValueError) as e:
            err(e)
        return {"side": side, "sol": f.sol, "tokens": f.tokens, "fee_sol": f.fee_sol, "impact_pct": f.impact_pct,
                "naive": f.naive, "delay_s": cfg.delay_seconds}

    @app.post("/api/buy")
    async def buy(body: BuyIn, u=Depends(current_user)):
        try:
            q, f, _ = await trader.buy(u["tg_id"], body.mint, body.sol)
        except TradeError as e:
            err(e)
        return {"symbol": q.symbol.lstrip("$"), "sol": f.sol, "tokens": f.tokens, "fee_sol": f.fee_sol,
                "impact_pct": f.impact_pct, "drift_pct": f.drift_pct, "balance": db.user(u["tg_id"])["balance"]}

    @app.post("/api/sell")
    async def sell(body: SellIn, u=Depends(current_user)):
        pos = db.position(body.position_id, u["tg_id"])
        if not pos:
            raise HTTPException(404, "Position not found or already closed.")
        try:
            _, f, _, realized = await trader.sell(u["tg_id"], pos["mint"], body.percent, pos)
        except TradeError as e:
            err(e)
        put_in = f.sol - realized
        return {"symbol": (pos["symbol"] or "?").lstrip("$"), "percent": body.percent, "put_in": put_in,
                "got_back": f.sol, "pnl_sol": realized, "pnl_pct": realized / put_in * 100 if put_in else 0,
                "balance": db.user(u["tg_id"])["balance"]}

    @app.post("/api/tpsl")
    async def tpsl(body: TpSlIn, u=Depends(current_user)):
        pos = db.position(body.position_id, u["tg_id"])
        if not pos:
            raise HTTPException(404, "Position not found or already closed.")
        for kind, v in (("tp", body.tp), ("sl", body.sl)):
            if v is None:
                continue
            if v:
                db.set_tpsl(pos["id"], **{kind: v})
            else:
                db.clear_tpsl(pos["id"], kind)
        p = db.position(pos["id"], u["tg_id"])
        return {"tp_pct": p["tp_pct"], "sl_pct": p["sl_pct"]}

    @app.get("/api/portfolio")
    async def portfolio(u=Depends(current_user)):
        positions = db.open_positions(u["tg_id"])
        res = await asyncio.gather(*(trader._q(p["mint"]) for p in positions), return_exceptions=True)
        out = []
        for p, q in zip(positions, res, strict=True):
            q = None if isinstance(q, BaseException) else q
            row = {"id": p["id"], "mint": p["mint"], "symbol": (p["symbol"] or "?").lstrip("$"), "tokens": p["tokens"],
                   "cost_sol": p["cost_sol"], "tp_pct": p["tp_pct"], "sl_pct": p["sl_pct"], "opened_at": p["opened_at"],
                   "value_sol": None, "pnl_sol": None, "pnl_pct": None, "market": None}
            # Whole-position summary: bought = remaining cost + cost of the parts already sold
            sold = db.sold_sol(p["id"])
            bought = p["cost_sol"] + sold - p["realized_sol"]
            row.update(realized_sol=p["realized_sol"], sold_sol=sold, bought_sol=bought,
                       total_pnl_sol=None, total_pnl_pct=None)
            if q:
                val = p["tokens"] * q.price_sol
                d, pct = fm.pnl(p["cost_sol"], val)
                total = val + sold - bought
                row.update(total_pnl_sol=total, total_pnl_pct=total / bought * 100 if bought else 0.0)
                row.update(value_sol=val, pnl_sol=d, pnl_pct=pct, market={
                    "market_cap": q.market_cap, "volume_24h": q.volume_24h, "liquidity_usd": q.liquidity_usd,
                    "entry_mc": q.market_cap * (p["cost_sol"] / p["tokens"]) / q.price_sol if p["tokens"] else None})
            out.append(row)
        return {"balance": u["balance"], "sol_usd": await sol_usd(), "positions": out}

    @app.get("/api/history")
    async def history(u=Depends(current_user)):
        return [dict(r) for r in db.history(u["tg_id"], 50)]

    @app.get("/api/stats")
    async def stats(u=Depends(current_user)):
        return db.stats(u["tg_id"])

    @app.get("/api/leaderboard")
    async def leaderboard(u=Depends(current_user)):
        return [{"username": r["username"], "pnl": r["pnl"], "trades": r["n"], "wins": r["wins"],
                 "resets": r["resets"], "me": r["tg_id"] == u["tg_id"]} for r in db.leaderboard(25)]

    return app


def _make_default() -> FastAPI:
    cfg = config.load(require_token=False)
    if not cfg.session_secret:
        raise SystemExit("SESSION_SECRET missing in .env")
    return create_app(cfg, DB(cfg.db_path), DexScreener())


def __getattr__(name):  # lazy so tests can import this module without a .env
    if name == "app":
        globals()["app"] = _make_default()
        return globals()["app"]
    raise AttributeError(name)
