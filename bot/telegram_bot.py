from __future__ import annotations

from datetime import datetime, timedelta
from functools import wraps
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from bot.notifications import (
    format_history,
    format_pnl,
    format_positions_list,
    format_status_report,
)
from database.db import Database
from trading.portfolio import PortfolioManager
from utils.config import AppConfig
from utils.logger import logger

if TYPE_CHECKING:
    pass

HandlerFunc = Callable[..., Coroutine[Any, Any, None]]


class TelegramBot:
    def __init__(
        self,
        config: AppConfig,
        db: Database,
        portfolio: PortfolioManager,
    ):
        self.config = config
        self.db = db
        self.portfolio = portfolio
        self.is_trading = False
        self._app: Application | None = None  # type: ignore[type-arg]

    def _admin_only(self, func: HandlerFunc) -> HandlerFunc:
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            user_id = update.effective_user.id if update.effective_user else 0
            if self.config.telegram.admin_ids and user_id not in self.config.telegram.admin_ids:
                if update.message:
                    await update.message.reply_text("⛔ Доступ запрещён")
                return
            await func(update, context)

        return wrapper

    def _register_handlers(self, app: Application) -> None:  # type: ignore[type-arg]
        commands: list[tuple[str, HandlerFunc]] = [
            ("start", self._cmd_start),
            ("status", self._cmd_status),
            ("balance", self._cmd_balance),
            ("positions", self._cmd_positions),
            ("start_trading", self._cmd_start_trading),
            ("stop_trading", self._cmd_stop_trading),
            ("set_max_prob", self._cmd_set_max_prob),
            ("set_bet_size", self._cmd_set_bet_size),
            ("set_max_positions", self._cmd_set_max_positions),
            ("report", self._cmd_report),
            ("history", self._cmd_history),
            ("pnl", self._cmd_pnl),
        ]
        for name, handler in commands:
            app.add_handler(CommandHandler(name, self._admin_only(handler)))

    def build_app(self) -> Application:  # type: ignore[type-arg]
        app = Application.builder().token(self.config.telegram.bot_token).build()
        self._register_handlers(app)
        self._app = app
        return app

    async def send_message(self, text: str) -> None:
        if not self._app or not self.config.telegram.admin_ids:
            return
        for admin_id in self.config.telegram.admin_ids:
            try:
                await self._app.bot.send_message(
                    chat_id=admin_id, text=text, parse_mode="Markdown"
                )
            except Exception as e:
                logger.error("Failed to send message to %d: %s", admin_id, e)

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(  # type: ignore[union-attr]
            "👋 *Polymarket Trading Bot*\n\n"
            "Используйте /status для просмотра текущего состояния.\n"
            "Используйте /start\\_trading для запуска торговли.",
            parse_mode="Markdown",
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        open_count = await self.portfolio.get_open_positions_count()
        trades_today = await self.db.get_trades_count_today()
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        pnl_today = await self.db.get_pnl_since(today)

        balance = await self.portfolio.log_balance(0)

        text = format_status_report(balance, open_count, trades_today, pnl_today, self.is_trading)
        text += (
            f"\n\n⚙️ *Настройки:*\n"
            f"Max вероятность: {self.config.trading.max_probability * 100:.1f}%\n"
            f"Размер ставки: {self.config.trading.bet_size_pct * 100:.1f}%\n"
            f"Max позиций: {self.config.trading.max_open_positions}\n"
            f"Интервал сканирования: {self.config.trading.scan_interval_sec}с"
        )
        await update.message.reply_text(text, parse_mode="Markdown")  # type: ignore[union-attr]

    async def _cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        balance = await self.portfolio.log_balance(0)
        await update.message.reply_text(  # type: ignore[union-attr]
            f"💰 *Баланс:*\n"
            f"Свободно: ${balance.free_usdc:.2f} USDC\n"
            f"В позициях: ~${balance.positions_value:.2f}\n"
            f"Итого: ~${balance.total_value:.2f}",
            parse_mode="Markdown",
        )

    async def _cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        trades = await self.db.get_open_trades()
        text = format_positions_list(trades)
        await update.message.reply_text(text, parse_mode="Markdown")  # type: ignore[union-attr]

    async def _cmd_start_trading(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        self.is_trading = True
        await update.message.reply_text(  # type: ignore[union-attr]
            "🟢 Торговля запущена!"
        )
        logger.info("Trading started by admin")

    async def _cmd_stop_trading(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        self.is_trading = False
        await update.message.reply_text(  # type: ignore[union-attr]
            "🔴 Торговля остановлена. Позиции не закрыты."
        )
        logger.info("Trading stopped by admin")

    async def _cmd_set_max_prob(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not context.args:
            await update.message.reply_text(  # type: ignore[union-attr]
                "Использование: /set_max_prob 0.03"
            )
            return
        try:
            value = float(context.args[0])
            if not 0 < value < 1:
                raise ValueError
            self.config.trading.max_probability = value
            await update.message.reply_text(  # type: ignore[union-attr]
                f"✅ Max вероятность: {value * 100:.1f}%"
            )
        except ValueError:
            await update.message.reply_text(  # type: ignore[union-attr]
                "❌ Укажите число от 0 до 1, например 0.03"
            )

    async def _cmd_set_bet_size(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not context.args:
            await update.message.reply_text(  # type: ignore[union-attr]
                "Использование: /set_bet_size 0.01"
            )
            return
        try:
            value = float(context.args[0])
            if not 0 < value <= 1:
                raise ValueError
            self.config.trading.bet_size_pct = value
            await update.message.reply_text(  # type: ignore[union-attr]
                f"✅ Размер ставки: {value * 100:.1f}%"
            )
        except ValueError:
            await update.message.reply_text(  # type: ignore[union-attr]
                "❌ Укажите число от 0 до 1, например 0.01"
            )

    async def _cmd_set_max_positions(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not context.args:
            await update.message.reply_text(  # type: ignore[union-attr]
                "Использование: /set_max_positions 30"
            )
            return
        try:
            value = int(context.args[0])
            if value < 1:
                raise ValueError
            self.config.trading.max_open_positions = value
            await update.message.reply_text(  # type: ignore[union-attr]
                f"✅ Max позиций: {value}"
            )
        except ValueError:
            await update.message.reply_text(  # type: ignore[union-attr]
                "❌ Укажите целое число >= 1"
            )

    async def _cmd_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._cmd_status(update, context)

    async def _cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        trades = await self.db.get_recent_trades(20)
        text = format_history(trades)
        await update.message.reply_text(text, parse_mode="Markdown")  # type: ignore[union-attr]

    async def _cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        week_ago = today - timedelta(days=7)

        pnl_today = await self.db.get_pnl_since(today)
        pnl_week = await self.db.get_pnl_since(week_ago)
        pnl_total = await self.db.get_total_pnl()

        text = format_pnl(pnl_today, pnl_week, pnl_total)
        await update.message.reply_text(text, parse_mode="Markdown")  # type: ignore[union-attr]
