#!/usr/bin/env python3
"""Однократный запуск redeem (CLOB + WON из БД) для тестирования."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.db import Database
from trading.executor import TradeExecutor
from trading.redeemer import redeem_all_pending
from utils.config import load_config


async def main() -> None:
    config = load_config()
    if not config.secrets.private_key:
        print("PRIVATE_KEY не задан в .env")
        return
    db = Database()
    await db.connect()
    from trading.redeemer import Redeemer

    redeemer = Redeemer(config.secrets)
    executor = TradeExecutor(config, db)
    print("Запуск redeem_all_pending...")
    summary = await redeem_all_pending(db, redeemer, executor, max_trades=100)
    await db.close()
    print(f"Готово: успешно {summary.succeeded} из {summary.total} попыток")
    for err in summary.errors:
        print(f"  Ошибка: {err}")


if __name__ == "__main__":
    asyncio.run(main())
