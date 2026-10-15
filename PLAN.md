# PaperPump Plan

PaperPump is a memecoin paper-trading tool: real Solana prices, fake money. It starts as a Telegram bot, and a website comes later (see `../memecoin-papertrade-research/mockup/trade.html`).

**Hard rules:** no wallet connection, no private keys, and no real funds, ever. Any "project coin" or token is deferred until we get legal advice.

## v1 scope
- Telegram bot with 100 paper SOL per user
- Buy and sell any Solana token that has a SOL pair on DexScreener
- Realistic fill breakdown on every trade, compared with a naive fill
- Portfolio, history, stats, TP/SL, reset, leaderboard

## Architecture (simple on purpose)
```
Telegram <-long polling-> bot.py (python-telegram-bot)
                              |
                         service.py  (trading rules, shared with the future website)
                          /       \
                 fill.py (math)   prices.py (DexScreener HTTP, read-only)
                          \
                        db.py (SQLite file)
```
- Python 3.12, python-telegram-bot v21, sqlite3 (no ORM yet; we can switch to SQLAlchemy when the website arrives)
- Later: a FastAPI backend that serves the web UI. It uses the same DB and the same `service.py`, plus the **Telegram Login Widget** so the website account is your Telegram account.

## Data model
| Table | Key fields |
|---|---|
| users | tg_id, username, balance (paper SOL), created_at, last_reset |
| positions | user, mint, symbol, tokens, cost_sol, opened_at, tp_pct, sl_pct, status (open/closed/reset), realized_sol |
| trades | user, position, side, sol, tokens, fee_sol, impact_pct, naive, pnl_sol, ts |
| leaderboard | a query, not a table: realized PnL of closed positions since the user's last reset |

## Fill model
1. **Fees:** 1% of each swap (pump.fun-like) plus a priority fee (default 0.001 SOL) on every buy and sell.
2. **Price impact:** treat the pool as constant product `x*y=k`. The SOL reserve is about half of DexScreener's USD liquidity divided by the SOL price, so `tokens_out = T*in/(R+in)`. This is an approximation. Real bonding curves and CLMM pools differ.
3. **Delay:** take a quote, wait N seconds (default 1.5), quote again, and fill at the later price. The difference is shown as "drift".
4. **Paper vs realistic:** every reply shows what a naive "fill at the displayed price" tool would have given you.

## Milestones
- **M1 Bot core** ✅: start, buy, sell, portfolio, history, token card with buy buttons
- **M2 TP/SL + stats** ✅: per-position TP/SL with a background checker, /stats, /reset with cooldown, basic global leaderboard
- **M3 Group leaderboards:** add the bot to a group for weekly competitions per group
- **M4 Website:** FastAPI + the trade.html UI, Telegram login, same DB
- **M5 Backtesting/replay:** record pump.fun launches, replay a token's first minutes, run rule-based strategies over history
- **Later, monetization (optional):** credit packs (e.g. extra resets), a paid tier (more history and analytics), and clearly labelled **"Promoted"** placements that always carry this disclaimer: *"Promoted coins are paid advertisements. PaperPump does not endorse or vet them. Not financial advice."*

## Known limitations
- Only pairs quoted in SOL (wSOL) are used, so for big tokens the deepest pool may be a USDC pair and impact will be overstated.
- Brand-new pump.fun coins may not be on DexScreener for the first seconds or minutes.
- DexScreener rate limits are about 300 requests/min on the pair endpoints, so add caching before growing.
