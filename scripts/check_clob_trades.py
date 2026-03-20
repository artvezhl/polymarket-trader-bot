"""One-off: load .env and print CLOB get_trades count (no secrets)."""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.db import Database
from trading.clob_account import fetch_all_clob_trades
from trading.executor import TradeExecutor
from utils.config import load_config


async def main() -> None:
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cfg = load_config()
    pk = bool(cfg.secrets.private_key)
    ak = bool(cfg.secrets.polymarket_api_key)
    px = bool(cfg.secrets.proxy_address)
    print("env_ok", {"private_key_set": pk, "api_key_set": ak, "proxy_set": px})
    if not pk or not ak:
        print("skip_clob_missing_creds")
        return
    ex = TradeExecutor(cfg, Database())
    r = await fetch_all_clob_trades(ex.client)
    if r.error:
        print("fetch_error", r.error)
    print("trades_count", len(r.trades))
    if r.trades:
        print("first_trade_keys", sorted(r.trades[0].keys())[:25])


if __name__ == "__main__":
    asyncio.run(main())
