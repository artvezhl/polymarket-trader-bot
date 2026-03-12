from __future__ import annotations

import asyncio
import signal
import sys

from bot.notifications import format_new_trade, format_position_resolved, format_status_report
from bot.telegram_bot import TelegramBot
from database.db import Database
from trading.executor import TradeExecutor
from trading.portfolio import PortfolioManager
from trading.scanner import MarketScanner
from utils.config import AppConfig, load_config
from utils.logger import logger


class TradingEngine:
    def __init__(self, config: AppConfig):
        self.config = config
        self.db = Database()
        self.scanner = MarketScanner(config.trading)
        self.portfolio = PortfolioManager(self.db)
        self.executor = TradeExecutor(config, self.db)
        self.tg_bot = TelegramBot(config, self.db, self.portfolio)
        self._shutdown = asyncio.Event()

    async def start(self) -> None:
        logger.info("Starting Polymarket Trading Bot...")

        await self.db.connect()

        app = self.tg_bot.build_app()
        await app.initialize()
        await app.start()
        if app.updater:
            await app.updater.start_polling(drop_pending_updates=True)

        logger.info("Telegram bot started")

        tasks = [
            asyncio.create_task(self._scanner_loop(), name="scanner"),
            asyncio.create_task(self._position_monitor_loop(), name="position_monitor"),
            asyncio.create_task(self._status_reporter_loop(), name="status_reporter"),
        ]

        await self.tg_bot.send_message(
            "🚀 Бот запущен! Используйте /start\\_trading для начала торговли."
        )

        await self._shutdown.wait()

        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        if app.updater:
            await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await self.db.close()

        logger.info("Bot stopped")

    def stop(self) -> None:
        self._shutdown.set()

    async def _scanner_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                if self.tg_bot.is_trading:
                    await self._run_scan_cycle()
            except Exception as e:
                logger.error("Scanner error: %s", e)

            try:
                await asyncio.wait_for(
                    self._shutdown.wait(),
                    timeout=self.config.trading.scan_interval_sec,
                )
                return
            except asyncio.TimeoutError:
                pass

    async def _run_scan_cycle(self) -> None:
        open_count = await self.portfolio.get_open_positions_count()
        if open_count >= self.config.trading.max_open_positions:
            logger.debug("Max positions reached (%d), skipping scan", open_count)
            return

        existing_ids = await self.portfolio.get_existing_market_ids()
        opportunities = await self.scanner.scan(existing_ids)

        slots = self.config.trading.max_open_positions - open_count
        for opp in opportunities[:slots]:
            deposit = 1000.0
            trade = await self.executor.execute_trade(opp, deposit)
            if trade:
                msg = format_new_trade(trade, deposit)
                await self.tg_bot.send_message(msg)

    async def _position_monitor_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                resolved = await self.portfolio.check_resolved_markets()
                for r in resolved:
                    msg = format_position_resolved(r["trade"], r["won"], r["pnl"])
                    await self.tg_bot.send_message(msg)
            except Exception as e:
                logger.error("Position monitor error: %s", e)

            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=300)
                return
            except asyncio.TimeoutError:
                pass

    async def _status_reporter_loop(self) -> None:
        interval = self.config.reporting.status_interval_min * 60
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass

            try:
                balance = await self.portfolio.log_balance(0)
                open_count = await self.portfolio.get_open_positions_count()
                trades_today = await self.db.get_trades_count_today()
                from datetime import datetime

                today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                pnl_today = await self.db.get_pnl_since(today)

                msg = format_status_report(
                    balance, open_count, trades_today, pnl_today, self.tg_bot.is_trading
                )
                await self.tg_bot.send_message(msg)
            except Exception as e:
                logger.error("Status reporter error: %s", e)


def main() -> None:
    config = load_config()

    if not config.telegram.bot_token:
        logger.error("TELEGRAM_BOT_TOKEN not set. Check .env file.")
        sys.exit(1)

    if not config.secrets.private_key:
        logger.warning("PRIVATE_KEY not set — trading will fail. Set it in .env.")

    engine = TradingEngine(config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, engine.stop)

    try:
        loop.run_until_complete(engine.start())
    except KeyboardInterrupt:
        engine.stop()
        loop.run_until_complete(asyncio.sleep(1))
    finally:
        loop.close()


if __name__ == "__main__":
    main()
