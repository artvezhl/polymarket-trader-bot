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
- When `admin_ids` in `config.yaml` is empty, **all Telegram users** can use commands (initial setup behavior). Set specific IDs to restrict access.
- All secrets must be in `.env` (see `.env.example`). Required: `TELEGRAM_BOT_TOKEN`, `PRIVATE_KEY`, `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_API_PASSPHRASE`. These are also available as Cursor Cloud secrets.
- To generate `.env` from Cloud secrets, write it from env vars: the secrets `TELEGRAM_BOT_TOKEN`, `PRIVATE_KEY`, `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_API_PASSPHRASE` are injected as environment variables. Create `.env` with a heredoc that expands them.
- SQLite database `bot.db` is created automatically on first run.
- Virtual environment lives in `.venv/`. Activate with `source .venv/bin/activate`.
- `python3.12-venv` system package is required to create the venv (pre-installed in Cloud snapshot).
- Telegram Web sessions in the VM do **not persist** across restarts (QR code login required each time). For testing bot commands in the Cloud VM, either use Telegram on your phone/desktop, or run command handlers programmatically.
- The bot runs as a long-lived process (`python main.py`). Start it in the background with `python main.py &` and check logs via `tail -f logs/bot.log`.
