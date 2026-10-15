# PaperPump 🧪

Telegram bot for **paper trading** Solana memecoins: live DexScreener prices, fake SOL, and realistic fills (fees, price impact, delay). No wallet connection and no real funds.

## Setup
```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env      # then put your @BotFather token in TELEGRAM_BOT_TOKEN
python -m paperpump.bot
```
Get a token: in Telegram, message **@BotFather**, send `/newbot`, and copy the token into `.env`. Never commit `.env`.

## Commands
`/start` · `/buy <mint> <sol>` · `/sell <mint> <percent>` · `/portfolio` · `/history` · `/stats` · `/tp <mint> <percent>` · `/sl <mint> <percent>` · `/reset` · `/leaderboard`.
You can also paste a contract address to get a token card with Buy buttons.

## Dev
```bash
pytest -q
ruff check .
```
See PLAN.md for the roadmap and the fill model.

Disclaimer: simulation only, not financial advice.

## Website (M4)
```bash
# .env needs SESSION_SECRET, optional SITE_PASSWORD, and BASE_URL (the public https URL)
.venv/bin/uvicorn paperpump.web:app --host 127.0.0.1 --port 8000
```
Log in by tapping **🌐 Open website** in the bot, which sends a one-time link that expires in 10 minutes. The bot and the website are separate processes that share `paperpump.db`. The database runs in WAL mode, and every trade is a `BEGIN IMMEDIATE` transaction that re-checks the balance and position inside the transaction, so the two processes can never overspend or sell the same position twice.
Pages: `/` (trade, with a live DexScreener chart), `/portfolio`, `/history`, `/leaderboard`. Every page is set to noindex, and `robots.txt` disallows everything.
