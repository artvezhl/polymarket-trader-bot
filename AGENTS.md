# AGENTS.md

## Cursor Cloud specific instructions

**Project:** Polymarket Trading Bot with Telegram management (Python 3.12, async).

### Services

| Service | Description | How to run |
|---|---|---|
| Trading Bot | Main application — scanner + executor + Telegram bot | `python main.py` (requires `.env` with secrets) |

### Quick reference

- **Lint:** `ruff check .`
- **Tests:** `python -m pytest tests/ -v`
- **Run bot:** `python main.py` (needs `TELEGRAM_BOT_TOKEN` and Polymarket API credentials in `.env`)

### Non-obvious caveats

- The Gamma API returns `outcomes`, `outcomePrices`, and `clobTokenIds` as **JSON-encoded strings** (e.g. `'["Yes", "No"]'`), not native lists. The parser in `trading/scanner.py` handles both JSON strings and CSV formats.
- `py-clob-client` is synchronous — all CLOB calls are wrapped in `asyncio.to_thread()` inside `trading/executor.py`.
- The bot **does not start trading automatically** on launch. An admin must send `/start_trading` via Telegram.
- All secrets must be in `.env` (see `.env.example`). Required: `TELEGRAM_BOT_TOKEN`, `PRIVATE_KEY`, `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_API_PASSPHRASE`.
- SQLite database `bot.db` is created automatically on first run.
- Virtual environment lives in `.venv/`. Activate with `source .venv/bin/activate`.
