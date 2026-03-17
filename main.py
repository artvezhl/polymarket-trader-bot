from __future__ import annotations

import asyncio
import signal
import sys
from datetime import datetime
from pathlib import Path

from bot.notifications import (
    format_new_trade,
    format_position_resolved,
    format_positions_report,
    format_price_spike,
    format_status_report,
)
from bot.telegram_bot import TelegramBot
from database.db import Database
from trading.btc_feed import BtcFeed
from trading.btc_strategy import BtcStrategy
from trading.executor import TradeExecutor
from trading.portfolio import PortfolioManager
from trading.redeemer import Redeemer
from trading.scanner import MarketScanner
from utils.config import AppConfig, apply_db_overrides, load_config
from utils.logger import logger


class TradingEngine:
    def __init__(self, config: AppConfig):
        self.config = config
        self.db = Database()
        self.scanner = MarketScanner(config.trading)
        self.portfolio = PortfolioManager(self.db)
        self.executor = TradeExecutor(config, self.db)
        self.btc_feed = BtcFeed(exchange=config.strategy.btc_exchange)
        self.btc_strategy = BtcStrategy(
            config, self.db, self.executor, self.btc_feed
        )
        self.redeemer: Redeemer | None = None
        if config.secrets.proxy_address:
            self.redeemer = Redeemer(config.secrets)

        self.tg_bot = TelegramBot(
            config, self.db, self.portfolio, self.executor
        )
        self.tg_bot.scanner = self.scanner
        self.tg_bot.btc_strategy = self.btc_strategy
        self.tg_bot.btc_feed = self.btc_feed
        self.tg_bot.redeemer = self.redeemer
        self._shutdown = asyncio.Event()

    async def start(self) -> None:
        logger.info("Starting Polymarket Trading Bot...")

        await self.db.connect()

        db_values = await self.db.get_all_config()
        if db_values:
            apply_db_overrides(self.config, db_values)
            logger.info("Loaded %d config overrides from DB", len(db_values))

        app = self.tg_bot.build_app()
        await app.initialize()
        await app.start()
        if app.updater:
            await app.updater.start_polling(drop_pending_updates=True)

        await self.tg_bot.register_commands()
        logger.info("Telegram bot started")

        await self.btc_strategy.load_settings()
        self.btc_strategy.set_notify(self.tg_bot.send_message)

        tasks = [
            asyncio.create_task(self.btc_feed.start(), name="btc_feed"),
            asyncio.create_task(self._btc_strategy_loop(), name="btc_strategy"),
            # asyncio.create_task(self._scanner_loop(), name="scanner"),
            asyncio.create_task(self._position_monitor_loop(), name="position_monitor"),
            asyncio.create_task(self._status_reporter_loop(), name="status_reporter"),
            asyncio.create_task(self._price_monitor_loop(), name="price_monitor"),
            asyncio.create_task(self._positions_report_loop(), name="positions_report"),
        ]

        await self.tg_bot.send_message(
            "🚀 Бот запущен! Используйте /start\\_trading для начала торговли."
        )

        await self._shutdown.wait()

        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        await self.btc_feed.stop()
        await self.btc_strategy.stop()

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

        deposit = await self.executor.get_polymarket_balance()
        if deposit < self.config.trading.min_bet_usd:
            logger.debug("USDC balance too low ($%.2f), skipping", deposit)
            return

        slots = self.config.trading.max_open_positions - open_count
        for opp in opportunities[:slots]:
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
                free_usdc = await self.executor.get_polymarket_balance()
                balance = await self.portfolio.log_balance(free_usdc)
                open_count = await self.portfolio.get_open_positions_count()
                trades_today = await self.db.get_trades_count_today()
                today = datetime.now().replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                pnl_today = await self.db.get_pnl_since(today)

                msg = format_status_report(
                    balance, open_count, trades_today, pnl_today,
                    self.tg_bot.is_trading,
                )
                await self.tg_bot.send_message(msg)
            except Exception as e:
                logger.error("Status reporter error: %s", e)


    async def _btc_strategy_loop(self) -> None:
        await asyncio.sleep(5)
        while not self._shutdown.is_set():
            try:
                if self.tg_bot.is_trading:
                    await self.btc_strategy.run()
            except Exception as e:
                logger.error("BTC strategy error: %s", e)
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(), timeout=1
                )
                return
            except asyncio.TimeoutError:
                pass

    async def _price_monitor_loop(self) -> None:
        interval = self.config.trading.price_check_interval_sec
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(), timeout=interval
                )
                return
            except asyncio.TimeoutError:
                pass

            try:
                alerts = await self.portfolio.update_prices(
                    self.scanner, self.config.trading
                )
                for alert in alerts:
                    msg = format_price_spike(
                        alert["trade"],
                        alert["new_price"],
                        alert["multiplier"],
                    )
                    await self.tg_bot.send_message(msg)
                    logger.info(
                        "Price spike alert: %s (x%.1f)",
                        alert["trade"].question[:40],
                        alert["multiplier"],
                    )
            except Exception as e:
                logger.error("Price monitor error: %s", e)

    async def _positions_report_loop(self) -> None:
        interval = self.config.reporting.positions_report_interval_hours * 3600
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(), timeout=interval
                )
                return
            except asyncio.TimeoutError:
                pass

            try:
                trades = await self.portfolio.get_positions_report()
                if trades:
                    msg = format_positions_report(trades)
                    await self.tg_bot.send_message(msg)
            except Exception as e:
                logger.error("Positions report error: %s", e)


def main() -> None:
    Path("data").mkdir(exist_ok=True)
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
