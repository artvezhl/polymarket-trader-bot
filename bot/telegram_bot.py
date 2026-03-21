from __future__ import annotations

from datetime import datetime, timedelta
from functools import wraps
from typing import Any, Callable, Coroutine

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from bot.notifications import (
    SCAN_PAGE_SIZE,
    format_clob_positions_list,
    format_close_list,
    format_close_result,
    format_data_api_positions,
    format_history,
    format_pnl,
    format_positions_list,
    format_scan_result,
    format_settings,
    format_status_report,
)
from database.db import Database
from trading.clob_positions import fetch_clob_open_positions
from trading.data_api import fetch_positions_async
from trading.executor import TradeExecutor
from trading.portfolio import PortfolioManager
from trading.redeemer import Redeemer, redeem_all_pending
from trading.scanner import MarketScanner
from trading.wallet_watch import (
    is_valid_wallet_address,
    normalize_wallet_address,
)
from utils.config import AppConfig
from utils.logger import logger

HandlerFunc = Callable[..., Coroutine[Any, Any, None]]

BOT_COMMANDS = [
    BotCommand("start", "Запуск бота и приветствие"),
    BotCommand("status", "Текущий статус (вкл/выкл, параметры)"),
    BotCommand("balance", "Баланс: свободные USDC + позиции"),
    BotCommand("positions", "Позиции: Data API + CLOB + БД"),
    BotCommand("start_trading", "Запустить торговлю"),
    BotCommand("stop_trading", "Остановить торговлю"),
    BotCommand("settings", "Все текущие настройки"),
    BotCommand("set_max_prob", "Макс. вероятность (0-1)"),
    BotCommand("set_bet_size", "Размер ставки (доля депозита)"),
    BotCommand("set_min_bet", "Мин. ставка в $"),
    BotCommand("set_max_bet", "Макс. ставка в $"),
    BotCommand("set_max_positions", "Лимит открытых позиций"),
    BotCommand("set_liquidity", "Мин. ликвидность рынка в $"),
    BotCommand("set_interval", "Интервал сканирования (сек)"),
    BotCommand("set_spike_mult", "Множитель алерта цены"),
    BotCommand("set_min_days", "Мин. дней до закрытия события"),
    BotCommand("set_skip_words", "Исключить по ключевым словам"),
    BotCommand("close", "Закрыть позицию"),
    BotCommand("auto_close", "Вкл/выкл авто-закрытие по профиту"),
    BotCommand("set_take_profit", "Установить % take profit"),
    BotCommand("set_stop_loss", "Установить % stop loss"),
    BotCommand("reverse", "Развернуть сигнал стратегии"),
    BotCommand("hedge", "Вкл/выкл хеджирование позиций"),
    BotCommand("set_hedge", "Настроить триггер и размер хеджа"),
    BotCommand("strategy", "Статус BTC 5-min стратегии"),
    BotCommand("edge", "Текущие сигналы и edge"),
    BotCommand("scan", "Сканировать рынки (показать кол-во)"),
    BotCommand("sync", "Синхронизировать с CLOB API"),
    BotCommand("redeem", "Зачислить выигрыши на счёт"),
    BotCommand("watch_add", "Подписаться на сделки кошелька Polymarket"),
    BotCommand("watch_list", "Список отслеживаемых кошельков"),
    BotCommand("watch_remove", "Отписаться от кошелька"),
    BotCommand("fees", "Статистика по комиссиям"),
    BotCommand("report", "Полный отчёт"),
    BotCommand("history", "Последние 20 сделок"),
    BotCommand("pnl", "P&L за день / неделю / всё время"),
]


class TelegramBot:
    def __init__(
        self,
        config: AppConfig,
        db: Database,
        portfolio: PortfolioManager,
        executor: TradeExecutor | None = None,
    ):
        self.config = config
        self.db = db
        self.portfolio = portfolio
        self.executor = executor
        self.scanner: MarketScanner | None = None
        self.btc_strategy = None
        self.btc_feed = None
        self.redeemer: Redeemer | None = None
        self.is_trading = False
        self._scan_cache: dict[int, tuple[int, list]] = {}
        self._app: Application | None = None  # type: ignore[type-arg]

    def _admin_only(self, func: HandlerFunc) -> HandlerFunc:
        @wraps(func)
        async def wrapper(
            update: Update, context: ContextTypes.DEFAULT_TYPE
        ) -> None:
            user_id = (
                update.effective_user.id if update.effective_user else 0
            )
            if (
                self.config.telegram.admin_ids
                and user_id not in self.config.telegram.admin_ids
            ):
                if update.message:
                    await update.message.reply_text("⛔ Доступ запрещён")
                return
            await func(update, context)

        return wrapper

    def _register_handlers(
        self, app: Application  # type: ignore[type-arg]
    ) -> None:
        commands: list[tuple[str, HandlerFunc]] = [
            ("start", self._cmd_start),
            ("status", self._cmd_status),
            ("balance", self._cmd_balance),
            ("positions", self._cmd_positions),
            ("start_trading", self._cmd_start_trading),
            ("stop_trading", self._cmd_stop_trading),
            ("settings", self._cmd_settings),
            ("set_max_prob", self._cmd_set_max_prob),
            ("set_bet_size", self._cmd_set_bet_size),
            ("set_min_bet", self._cmd_set_min_bet),
            ("set_max_bet", self._cmd_set_max_bet),
            ("set_max_positions", self._cmd_set_max_positions),
            ("set_liquidity", self._cmd_set_liquidity),
            ("set_interval", self._cmd_set_interval),
            ("set_spike_mult", self._cmd_set_spike_mult),
            ("set_min_days", self._cmd_set_min_days),
            ("set_skip_words", self._cmd_set_skip_words),
            ("close", self._cmd_close),
            ("auto_close", self._cmd_auto_close),
            ("set_take_profit", self._cmd_set_take_profit),
            ("set_stop_loss", self._cmd_set_stop_loss),
            ("reverse", self._cmd_reverse),
            ("hedge", self._cmd_hedge),
            ("set_hedge", self._cmd_set_hedge),
            ("strategy", self._cmd_strategy),
            ("edge", self._cmd_edge),
            ("scan", self._cmd_scan),
            ("sync", self._cmd_sync),
            ("redeem", self._cmd_redeem),
            ("watch_add", self._cmd_watch_add),
            ("watch_list", self._cmd_watch_list),
            ("watch_remove", self._cmd_watch_remove),
            ("fees", self._cmd_fees),
            ("report", self._cmd_report),
            ("history", self._cmd_history),
            ("pnl", self._cmd_pnl),
        ]
        for name, handler in commands:
            app.add_handler(
                CommandHandler(name, self._admin_only(handler))
            )
        app.add_handler(
            CallbackQueryHandler(
                self._admin_only(self._cb_scan_page),
                pattern=r"^scan_page:\d+$",
            )
        )

    def build_app(self) -> Application:  # type: ignore[type-arg]
        app = (
            Application.builder()
            .token(self.config.telegram.bot_token)
            .build()
        )
        self._register_handlers(app)
        self._app = app
        return app

    async def register_commands(self) -> None:
        if self._app:
            await self._app.bot.set_my_commands(BOT_COMMANDS)
            logger.info(
                "Bot commands menu registered (%d commands)",
                len(BOT_COMMANDS),
            )

    async def send_message(
        self, text: str, parse_mode: str | None = "Markdown"
    ) -> None:
        if not self._app or not self.config.telegram.admin_ids:
            return
        for admin_id in self.config.telegram.admin_ids:
            try:
                kwargs: dict = {"chat_id": admin_id, "text": text}
                if parse_mode is not None:
                    kwargs["parse_mode"] = parse_mode
                await self._app.bot.send_message(**kwargs)
            except Exception as e:
                logger.error(
                    "Failed to send message to %d: %s", admin_id, e
                )

    # ── Information commands ─────────────────────────────────────

    async def _cmd_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await update.message.reply_text(  # type: ignore[union-attr]
            "👋 *Polymarket Trading Bot*\n\n"
            "📊 /status — текущее состояние\n"
            "⚙️ /settings — все настройки\n"
            "▶️ /start\\_trading — запустить торговлю\n"
            "⏹ /stop\\_trading — остановить торговлю\n\n"
            "Нажмите / для списка всех команд.",
            parse_mode="Markdown",
        )

    async def _get_free_usdc(self) -> float:
        if self.executor:
            return await self.executor.get_polymarket_balance()
        return 0.0

    async def _cmd_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        open_count = await self.portfolio.get_open_positions_count()
        trades_today = await self.db.get_trades_count_today()
        today = datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        pnl_today = await self.db.get_pnl_since(today)
        free_usdc = await self._get_free_usdc()
        balance = await self.portfolio.log_balance(free_usdc)

        text = format_status_report(
            balance, open_count, trades_today, pnl_today, self.is_trading
        )
        await update.message.reply_text(  # type: ignore[union-attr]
            text, parse_mode="Markdown"
        )

    async def _cmd_balance(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        free_usdc = await self._get_free_usdc()
        balance = await self.portfolio.log_balance(free_usdc)
        await update.message.reply_text(  # type: ignore[union-attr]
            f"💰 *Баланс Polymarket:*\n"
            f"Свободно: ${balance.free_usdc:.2f} USDC\n"
            f"В позициях: ~${balance.positions_value:.2f}\n"
            f"Итого: ~${balance.total_value:.2f}",
            parse_mode="Markdown",
        )

    async def _cmd_positions(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        addr = self.config.secrets.proxy_address or ""
        if not addr and self.config.secrets.private_key:
            from eth_account import Account
            addr = Account.from_key(self.config.secrets.private_key).address
        if addr:
            await update.message.reply_text(  # type: ignore[union-attr]
                "⏳ Загружаю позиции из Data API…"
            )
            try:
                data_positions = await fetch_positions_async(addr)
                text = format_data_api_positions(data_positions)
            except Exception as e:
                logger.error("fetch_positions_async: %s", e)
                text = format_data_api_positions([], str(e))
            await update.message.reply_text(  # type: ignore[union-attr]
                text, parse_mode="Markdown"
            )

        if self.executor:
            await update.message.reply_text(  # type: ignore[union-attr]
                "⏳ Загружаю позиции из Polymarket CLOB…"
            )
            try:
                snap = await fetch_clob_open_positions(self.executor)
                main_text = format_clob_positions_list(
                    snap.positions, snap.clob_error
                )
            except Exception as e:
                logger.error("fetch_clob_open_positions: %s", e)
                main_text = format_clob_positions_list([], str(e))
            await update.message.reply_text(  # type: ignore[union-attr]
                main_text, parse_mode="Markdown"
            )
        elif not addr:
            await update.message.reply_text(  # type: ignore[union-attr]
                "⚠️ Нет POLYMARKET_PROXY_ADDRESS и PRIVATE_KEY — "
                "показываю только БД бота."
            )

        trades = await self.db.get_open_trades()
        if trades:
            db_text = format_positions_list(trades)
            await update.message.reply_text(  # type: ignore[union-attr]
                db_text, parse_mode="Markdown"
            )
        elif not self.executor:
            await update.message.reply_text(  # type: ignore[union-attr]
                format_positions_list([]), parse_mode="Markdown"
            )

    async def _cmd_settings(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        text = format_settings(self.config.trading)
        await update.message.reply_text(  # type: ignore[union-attr]
            text, parse_mode="Markdown"
        )

    # ── Trading control ──────────────────────────────────────────

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

    # ── Setting commands ─────────────────────────────────────────

    async def _persist(self, db_key: str, value: float | int | str) -> None:
        await self.db.set_config(db_key, str(value))

    async def _cmd_set_max_prob(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        cur = self.config.trading.max_probability
        if not context.args:
            await update.message.reply_text(  # type: ignore[union-attr]
                f"Макс. вероятность: *{cur * 100:.1f}%*\n"
                f"Изменить: /set\\_max\\_prob 0.03",
                parse_mode="Markdown",
            )
            return
        try:
            value = float(context.args[0])
            if not 0 < value < 1:
                raise ValueError
            self.config.trading.max_probability = value
            await self._persist("trading.max_probability", value)
            await update.message.reply_text(  # type: ignore[union-attr]
                f"✅ Макс. вероятность: {cur * 100:.1f}% → "
                f"*{value * 100:.1f}%* 💾",
                parse_mode="Markdown",
            )
            logger.info("max_probability changed: %.4f -> %.4f", cur, value)
        except ValueError:
            await update.message.reply_text(  # type: ignore[union-attr]
                "❌ Укажите число от 0 до 1, например: /set\\_max\\_prob 0.03",
                parse_mode="Markdown",
            )

    async def _cmd_set_bet_size(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        cur = self.config.trading.bet_size_pct
        if not context.args:
            await update.message.reply_text(  # type: ignore[union-attr]
                f"Размер ставки: *{cur * 100:.1f}%* от депозита\n"
                f"Изменить: /set\\_bet\\_size 0.02",
                parse_mode="Markdown",
            )
            return
        try:
            value = float(context.args[0])
            if not 0 < value <= 1:
                raise ValueError
            self.config.trading.bet_size_pct = value
            await self._persist("trading.bet_size_pct", value)
            await update.message.reply_text(  # type: ignore[union-attr]
                f"✅ Размер ставки: {cur * 100:.1f}% → "
                f"*{value * 100:.1f}%* 💾",
                parse_mode="Markdown",
            )
            logger.info("bet_size_pct changed: %.4f -> %.4f", cur, value)
        except ValueError:
            await update.message.reply_text(  # type: ignore[union-attr]
                "❌ Укажите число от 0 до 1, например: /set\\_bet\\_size 0.02",
                parse_mode="Markdown",
            )

    async def _cmd_set_min_bet(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        cur = self.config.trading.min_bet_usd
        if not context.args:
            await update.message.reply_text(  # type: ignore[union-attr]
                f"Мин. ставка: *${cur:.2f}*\n"
                f"Изменить: /set\\_min\\_bet 2.0",
                parse_mode="Markdown",
            )
            return
        try:
            value = float(context.args[0])
            if value <= 0:
                raise ValueError
            if value > self.config.trading.max_bet_usd:
                await update.message.reply_text(  # type: ignore[union-attr]
                    f"❌ Мин. ставка не может быть больше макс. "
                    f"(${self.config.trading.max_bet_usd:.2f})"
                )
                return
            self.config.trading.min_bet_usd = value
            await self._persist("trading.min_bet_usd", value)
            await update.message.reply_text(  # type: ignore[union-attr]
                f"✅ Мин. ставка: ${cur:.2f} → *${value:.2f}* 💾",
                parse_mode="Markdown",
            )
            logger.info("min_bet_usd changed: %.2f -> %.2f", cur, value)
        except ValueError:
            await update.message.reply_text(  # type: ignore[union-attr]
                "❌ Укажите положительное число, например: /set\\_min\\_bet 2.0",
                parse_mode="Markdown",
            )

    async def _cmd_set_max_bet(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        cur = self.config.trading.max_bet_usd
        if not context.args:
            await update.message.reply_text(  # type: ignore[union-attr]
                f"Макс. ставка: *${cur:.2f}*\n"
                f"Изменить: /set\\_max\\_bet 15.0",
                parse_mode="Markdown",
            )
            return
        try:
            value = float(context.args[0])
            if value <= 0:
                raise ValueError
            if value < self.config.trading.min_bet_usd:
                await update.message.reply_text(  # type: ignore[union-attr]
                    f"❌ Макс. ставка не может быть меньше мин. "
                    f"(${self.config.trading.min_bet_usd:.2f})"
                )
                return
            self.config.trading.max_bet_usd = value
            await self._persist("trading.max_bet_usd", value)
            await update.message.reply_text(  # type: ignore[union-attr]
                f"✅ Макс. ставка: ${cur:.2f} → *${value:.2f}* 💾",
                parse_mode="Markdown",
            )
            logger.info("max_bet_usd changed: %.2f -> %.2f", cur, value)
        except ValueError:
            await update.message.reply_text(  # type: ignore[union-attr]
                "❌ Укажите положительное число, например: /set\\_max\\_bet 15.0",
                parse_mode="Markdown",
            )

    async def _cmd_set_max_positions(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        cur = self.config.trading.max_open_positions
        if not context.args:
            await update.message.reply_text(  # type: ignore[union-attr]
                f"Макс. позиций: *{cur}*\n"
                f"Изменить: /set\\_max\\_positions 30",
                parse_mode="Markdown",
            )
            return
        try:
            value = int(context.args[0])
            if value < 1:
                raise ValueError
            self.config.trading.max_open_positions = value
            await self._persist("trading.max_open_positions", value)
            await update.message.reply_text(  # type: ignore[union-attr]
                f"✅ Макс. позиций: {cur} → *{value}* 💾",
                parse_mode="Markdown",
            )
            logger.info("max_open_positions changed: %d -> %d", cur, value)
        except ValueError:
            await update.message.reply_text(  # type: ignore[union-attr]
                "❌ Укажите целое число >= 1, например: /set\\_max\\_positions 30",
                parse_mode="Markdown",
            )

    async def _cmd_set_liquidity(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        cur = self.config.trading.min_liquidity
        if not context.args:
            await update.message.reply_text(  # type: ignore[union-attr]
                f"Мин. ликвидность: *${cur:,.0f}*\n"
                f"Изменить: /set\\_liquidity 10000",
                parse_mode="Markdown",
            )
            return
        try:
            value = float(context.args[0])
            if value < 0:
                raise ValueError
            self.config.trading.min_liquidity = value
            await self._persist("trading.min_liquidity", value)
            await update.message.reply_text(  # type: ignore[union-attr]
                f"✅ Мин. ликвидность: ${cur:,.0f} → *${value:,.0f}* 💾",
                parse_mode="Markdown",
            )
            logger.info("min_liquidity changed: %.0f -> %.0f", cur, value)
        except ValueError:
            await update.message.reply_text(  # type: ignore[union-attr]
                "❌ Укажите число >= 0, например: /set\\_liquidity 10000",
                parse_mode="Markdown",
            )

    async def _cmd_set_interval(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        cur = self.config.trading.scan_interval_sec
        if not context.args:
            await update.message.reply_text(  # type: ignore[union-attr]
                f"Интервал сканирования: *{cur}с*\n"
                f"Изменить: /set\\_interval 120",
                parse_mode="Markdown",
            )
            return
        try:
            value = int(context.args[0])
            if value < 10:
                raise ValueError
            self.config.trading.scan_interval_sec = value
            await self._persist("trading.scan_interval_sec", value)
            await update.message.reply_text(  # type: ignore[union-attr]
                f"✅ Интервал: {cur}с → *{value}с* 💾",
                parse_mode="Markdown",
            )
            logger.info("scan_interval_sec changed: %d -> %d", cur, value)
        except ValueError:
            await update.message.reply_text(  # type: ignore[union-attr]
                "❌ Укажите целое число >= 10, например: /set\\_interval 120",
                parse_mode="Markdown",
            )

    async def _cmd_set_spike_mult(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        cur = self.config.trading.price_spike_multiplier
        if not context.args:
            await update.message.reply_text(  # type: ignore[union-attr]
                f"Алерт при росте цены: *×{cur:.0f}*\n"
                f"Изменить: /set\\_spike\\_mult 5",
                parse_mode="Markdown",
            )
            return
        try:
            value = float(context.args[0])
            if value < 1.1:
                raise ValueError
            self.config.trading.price_spike_multiplier = value
            await self._persist("trading.price_spike_multiplier", value)
            await update.message.reply_text(  # type: ignore[union-attr]
                f"✅ Алерт при росте: ×{cur:.0f} → *×{value:.0f}* 💾",
                parse_mode="Markdown",
            )
            logger.info(
                "price_spike_multiplier changed: %.1f -> %.1f", cur, value
            )
        except ValueError:
            await update.message.reply_text(  # type: ignore[union-attr]
                "❌ Укажите число > 1, например: /set\\_spike\\_mult 5",
                parse_mode="Markdown",
            )

    async def _cmd_set_min_days(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        cur = self.config.trading.min_end_date_days
        if not context.args:
            await update.message.reply_text(  # type: ignore[union-attr]
                f"Мин. дней до закрытия: *{cur}*\n"
                f"Изменить: /set\\_min\\_days 7",
                parse_mode="Markdown",
            )
            return
        try:
            value = int(context.args[0])
            if value < 0:
                raise ValueError
            self.config.trading.min_end_date_days = value
            await self._persist("trading.min_end_date_days", value)
            await update.message.reply_text(  # type: ignore[union-attr]
                f"✅ Мин. дней: {cur} → *{value}* 💾",
                parse_mode="Markdown",
            )
        except ValueError:
            await update.message.reply_text(  # type: ignore[union-attr]
                "❌ Укажите число >= 0: /set\\_min\\_days 7",
                parse_mode="Markdown",
            )

    async def _cmd_set_skip_words(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        cur = self.config.trading.skip_keywords
        if not context.args:
            words = ", ".join(cur) if cur else "нет"
            await update.message.reply_text(  # type: ignore[union-attr]
                f"🚫 Исключённые слова: *{words}*\n\n"
                f"Добавить: /set\\_skip\\_words NBA NHL crypto\n"
                f"Очистить: /set\\_skip\\_words clear",
                parse_mode="Markdown",
            )
            return

        import json

        if context.args[0].lower() == "clear":
            self.config.trading.skip_keywords = []
            await self._persist(
                "trading.skip_keywords",
                json.dumps([]),  # type: ignore[arg-type]
            )
            await update.message.reply_text(  # type: ignore[union-attr]
                "✅ Фильтр слов очищен 💾"
            )
            return

        words = [w.lower() for w in context.args]
        merged = list(set(cur + words))
        self.config.trading.skip_keywords = merged
        await self._persist(
            "trading.skip_keywords",
            json.dumps(merged),  # type: ignore[arg-type]
        )
        added = [w for w in words if w not in cur]
        await update.message.reply_text(  # type: ignore[union-attr]
            f"✅ Добавлено: *{', '.join(added)}*\n"
            f"Всего исключений: *{', '.join(merged)}* 💾",
            parse_mode="Markdown",
        )

    # ── Close position ───────────────────────────────────────────

    async def _cmd_close(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        trades = await self.db.get_open_trades()

        if not context.args:
            text = format_close_list(trades)
            await update.message.reply_text(  # type: ignore[union-attr]
                text, parse_mode="Markdown"
            )
            return

        try:
            idx = int(context.args[0])
        except ValueError:
            await update.message.reply_text(  # type: ignore[union-attr]
                "❌ Укажите номер позиции: /close 3"
            )
            return

        if idx < 1 or idx > len(trades):
            await update.message.reply_text(  # type: ignore[union-attr]
                f"❌ Позиция #{idx} не найдена. "
                f"Доступно: 1-{len(trades)}"
            )
            return

        if not self.executor:
            await update.message.reply_text(  # type: ignore[union-attr]
                "❌ Executor не настроен. Проверьте API ключи."
            )
            return

        trade = trades[idx - 1]
        await update.message.reply_text(  # type: ignore[union-attr]
            f"⏳ Закрываю позицию #{idx}: "
            f"_{trade.question[:50]}_...",
            parse_mode="Markdown",
        )

        result = await self.executor.close_position(trade)
        if result:
            text = format_close_result(
                result["trade"],
                result["sell_price"],
                result["revenue"],
                result["pnl"],
                result.get("fee", 0.0),
            )
            await self.send_message(text)
        else:
            await update.message.reply_text(  # type: ignore[union-attr]
                "❌ Не удалось закрыть позицию. "
                "Возможно, нет ликвидности или проблема с API."
            )

    # ── Auto-close ───────────────────────────────────────────────

    async def _cmd_auto_close(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self.btc_strategy:
            await update.message.reply_text(
                "❌ Стратегия не инициализирована"
            )  # type: ignore[union-attr]
            return

        self.btc_strategy.auto_close_enabled = (
            not self.btc_strategy.auto_close_enabled
        )
        state = self.btc_strategy.auto_close_enabled
        await self.btc_strategy.save_setting("auto_close", state)
        icon = "🟢" if state else "🔴"
        await update.message.reply_text(  # type: ignore[union-attr]
            f"{icon} Авто-закрытие: *{'вкл' if state else 'выкл'}*\n"
            f"Take profit: *{self.btc_strategy.take_profit_pct * 100:.1f}%*\n"
            f"Stop loss: *{self.btc_strategy.stop_loss_pct * 100:.1f}%*\n\n"
            f"Изменить: /set\\_take\\_profit, /set\\_stop\\_loss",
            parse_mode="Markdown",
        )

    async def _cmd_set_take_profit(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self.btc_strategy:
            await update.message.reply_text(
                "❌ Стратегия не инициализирована"
            )  # type: ignore[union-attr]
            return

        cur = self.btc_strategy.take_profit_pct
        if not context.args:
            await update.message.reply_text(  # type: ignore[union-attr]
                f"Take profit: *{cur * 100:.1f}%*\n"
                f"Изменить: /set\\_take\\_profit 10",
                parse_mode="Markdown",
            )
            return
        try:
            value = float(context.args[0]) / 100
            if value <= 0:
                raise ValueError
            self.btc_strategy.take_profit_pct = value
            await self.btc_strategy.save_setting("take_profit_pct", value)
            await update.message.reply_text(  # type: ignore[union-attr]
                f"✅ Take profit: {cur * 100:.1f}% → "
                f"*{value * 100:.1f}%* 💾",
                parse_mode="Markdown",
            )
        except ValueError:
            await update.message.reply_text(  # type: ignore[union-attr]
                "❌ Укажите % > 0, например: /set\\_take\\_profit 10",
                parse_mode="Markdown",
            )

    async def _cmd_set_stop_loss(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self.btc_strategy:
            await update.message.reply_text(
                "❌ Стратегия не инициализирована"
            )  # type: ignore[union-attr]
            return

        cur = self.btc_strategy.stop_loss_pct
        if not context.args:
            await update.message.reply_text(  # type: ignore[union-attr]
                f"Stop loss: *{cur * 100:.1f}%*\n"
                f"Изменить: /set\\_stop\\_loss 5",
                parse_mode="Markdown",
            )
            return
        try:
            value = float(context.args[0]) / 100
            if value <= 0:
                raise ValueError
            self.btc_strategy.stop_loss_pct = value
            await self.btc_strategy.save_setting("stop_loss_pct", value)
            await update.message.reply_text(  # type: ignore[union-attr]
                f"✅ Stop loss: {cur * 100:.1f}% → "
                f"*{value * 100:.1f}%* 💾",
                parse_mode="Markdown",
            )
        except ValueError:
            await update.message.reply_text(  # type: ignore[union-attr]
                "❌ Укажите % > 0, например: /set\\_stop\\_loss 5",
                parse_mode="Markdown",
            )

    async def _cmd_reverse(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self.btc_strategy:
            await update.message.reply_text(
                "❌ Стратегия не инициализирована"
            )  # type: ignore[union-attr]
            return

        self.btc_strategy.reverse_signal = (
            not self.btc_strategy.reverse_signal
        )
        state = self.btc_strategy.reverse_signal
        await self.btc_strategy.save_setting("reverse_signal", state)
        icon = "🔄" if state else "➡️"
        mode = "mean-reversion" if state else "momentum"
        await update.message.reply_text(  # type: ignore[union-attr]
            f"{icon} Сигнал: *{'развёрнут' if state else 'прямой'}*\n"
            f"Режим: *{mode}*\n\n"
            f"_Прямой:_ модель предсказывает направление\n"
            f"_Развёрнут:_ ставка против модели (mean-reversion)",
            parse_mode="Markdown",
        )

    async def _cmd_hedge(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self.btc_strategy:
            await update.message.reply_text(
                "❌ Стратегия не инициализирована"
            )  # type: ignore[union-attr]
            return

        self.btc_strategy.hedge_enabled = (
            not self.btc_strategy.hedge_enabled
        )
        state = self.btc_strategy.hedge_enabled
        await self.btc_strategy.save_setting("hedge_enabled", state)
        icon = "🟢" if state else "🔴"
        await update.message.reply_text(  # type: ignore[union-attr]
            f"🛡 {icon} Хеджирование: *{'вкл' if state else 'выкл'}*\n"
            f"Триггер: при профите *{self.btc_strategy.hedge_trigger_pct * 100:.0f}%*\n"
            f"Размер: *{self.btc_strategy.hedge_ratio * 100:.0f}%* от прибыли\n\n"
            f"Настроить: /set\\_hedge 5 50\n"
            f"_(триггер 5%, размер 50% от прибыли)_",
            parse_mode="Markdown",
        )

    async def _cmd_set_hedge(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self.btc_strategy:
            await update.message.reply_text(
                "❌ Стратегия не инициализирована"
            )  # type: ignore[union-attr]
            return

        if not context.args or len(context.args) < 2:
            await update.message.reply_text(  # type: ignore[union-attr]
                f"🛡 *Настройки хеджа:*\n"
                f"Триггер: *{self.btc_strategy.hedge_trigger_pct * 100:.0f}%*\n"
                f"Размер: *{self.btc_strategy.hedge_ratio * 100:.0f}%* от прибыли\n\n"
                f"Использование: /set\\_hedge <триггер%> <размер%>\n"
                f"Пример: /set\\_hedge 5 50",
                parse_mode="Markdown",
            )
            return
        try:
            trigger = float(context.args[0]) / 100
            ratio = float(context.args[1]) / 100
            if trigger <= 0 or ratio <= 0 or ratio > 1:
                raise ValueError
            self.btc_strategy.hedge_trigger_pct = trigger
            self.btc_strategy.hedge_ratio = ratio
            await self.btc_strategy.save_setting("hedge_trigger_pct", trigger)
            await self.btc_strategy.save_setting("hedge_ratio", ratio)
            await update.message.reply_text(  # type: ignore[union-attr]
                f"✅ Хедж: триггер *{trigger * 100:.0f}%*, "
                f"размер *{ratio * 100:.0f}%* от прибыли 💾",
                parse_mode="Markdown",
            )
        except ValueError:
            await update.message.reply_text(  # type: ignore[union-attr]
                "❌ Формат: /set\\_hedge <триггер> <размер>\n"
                "Пример: /set\\_hedge 5 50",
                parse_mode="Markdown",
            )

    # ── BTC Strategy ──────────────────────────────────────────────

    async def _cmd_strategy(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        cfg = self.config.strategy
        feed_status = "🔴 offline"
        btc_price = ""
        if self.btc_feed and self.btc_feed.is_ready:
            feed_status = "🟢 online"
            btc_price = f" (${self.btc_feed.price:,.0f})"

        markets_count = 0
        if self.btc_strategy:
            try:
                markets = await self.btc_strategy.find_active_markets()
                markets_count = len(markets)
            except Exception:
                pass

        sig_mode = "mean-reversion 🔄" if (
            self.btc_strategy and self.btc_strategy.reverse_signal
        ) else "momentum ➡️"
        await update.message.reply_text(  # type: ignore[union-attr]
            f"📈 *BTC 5-Min Strategy:*\n"
            f"Режим: *{cfg.mode}* ({sig_mode})\n"
            f"BTC feed: {feed_status}{btc_price}\n"
            f"Активных рынков: *{markets_count}*\n"
            f"Edge порог: *{cfg.edge_threshold * 100:.1f}%*\n"
            f"Размер позиции: *{cfg.trade_size_pct * 100:.1f}%*\n"
            f"Take profit: *{cfg.take_profit_pct * 100:.1f}%*\n"
            f"Stop loss: *{cfg.stop_loss_pct * 100:.1f}%*\n"
            f"Kelly: *{cfg.kelly_fraction:.2f}*\n"
            f"Интервал: *{cfg.update_interval_ms}ms*",
            parse_mode="Markdown",
        )

    async def _cmd_edge(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self.btc_feed or not self.btc_feed.is_ready:
            await update.message.reply_text(  # type: ignore[union-attr]
                "⏳ BTC feed ещё не готов. Подождите ~10 сек."
            )
            return

        from trading.signals import SignalEngine

        signals = SignalEngine(self.btc_feed)
        sig = signals.all_signals()

        lines = [
            f"📊 *BTC Сигналы:*\n"
            f"💲 Price: *${sig['price']:,.2f}*\n"
            f"📐 Microprice: ${sig['microprice']:,.2f}\n"
            f"📊 Imbalance: {sig['imbalance']:+.3f}\n"
            f"📈 Volatility: {sig['volatility']:.4f}\n"
            f"🔄 Momentum: {sig['momentum']:.6f}\n"
            f"📉 Drift: {sig['drift']:.4f}\n"
        ]

        if self.btc_strategy:
            try:
                markets = await self.btc_strategy.find_active_markets()
                if markets:
                    lines.append(f"\n*Рынки ({len(markets)}):*")
                    for m in markets[:5]:
                        from trading.probability import (
                            compute_edge,
                            final_probability,
                        )

                        t = m.time_left_frac
                        p_up = final_probability(
                            sig["price"], m.strike,
                            sig["volatility"], sig["drift"], t,
                            sig["bid_volume"], sig["ask_volume"],
                            sig["microprice"], sig["mid_price"],
                            self.btc_feed.recent_returns(20),
                        )
                        edge_up = compute_edge(p_up, m.up_price)
                        edge_dn = compute_edge(1 - p_up, m.down_price)
                        best = max(edge_up, edge_dn)
                        icon = "🟢" if best >= 0.03 else "⚪"
                        lines.append(
                            f"{icon} {m.question[:35]} "
                            f"({m.time_left:.0f}s)\n"
                            f"   Up: {p_up * 100:.0f}% vs "
                            f"{m.up_price * 100:.0f}% "
                            f"(edge {edge_up * 100:+.1f}%)\n"
                            f"   Dn: {(1 - p_up) * 100:.0f}% vs "
                            f"{m.down_price * 100:.0f}% "
                            f"(edge {edge_dn * 100:+.1f}%)"
                        )
                else:
                    lines.append("\n_Нет активных 5-мин BTC рынков_")
            except Exception:
                pass

        await update.message.reply_text(  # type: ignore[union-attr]
            "\n".join(lines), parse_mode="Markdown"
        )

    # ── Scan ─────────────────────────────────────────────────────

    def _scan_keyboard(
        self, page: int, total: int
    ) -> InlineKeyboardMarkup | None:
        total_pages = max(1, (total + SCAN_PAGE_SIZE - 1) // SCAN_PAGE_SIZE)
        if total_pages <= 1:
            return None

        buttons: list[InlineKeyboardButton] = []
        if page > 0:
            buttons.append(
                InlineKeyboardButton("⬅️", callback_data=f"scan_page:{page - 1}")
            )
        buttons.append(
            InlineKeyboardButton(
                f"{page + 1}/{total_pages}", callback_data="scan_page:noop"
            )
        )
        if page < total_pages - 1:
            buttons.append(
                InlineKeyboardButton("➡️", callback_data=f"scan_page:{page + 1}")
            )
        return InlineKeyboardMarkup([buttons])

    async def _cmd_scan(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self.scanner:
            await update.message.reply_text(  # type: ignore[union-attr]
                "❌ Сканер не инициализирован"
            )
            return

        await update.message.reply_text(  # type: ignore[union-attr]
            "🔎 Сканирую рынки..."
        )

        existing_ids = await self.portfolio.get_existing_market_ids()
        markets = await self.scanner.fetch_markets()
        opportunities = self.scanner.filter_markets(markets, existing_ids)

        user_id = update.effective_user.id if update.effective_user else 0
        self._scan_cache[user_id] = (len(markets), opportunities)

        text = format_scan_result(
            len(markets), opportunities, self.config.trading, page=0
        )
        kb = self._scan_keyboard(0, len(opportunities))
        await update.message.reply_text(  # type: ignore[union-attr]
            text, parse_mode="Markdown", reply_markup=kb
        )

    async def _cb_scan_page(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if not query:
            return
        await query.answer()

        data = query.data or ""
        if data == "scan_page:noop":
            return

        page = int(data.split(":")[1])
        user_id = query.from_user.id if query.from_user else 0
        cached = self._scan_cache.get(user_id)
        if not cached:
            await query.edit_message_text("⏳ Кэш истёк. Запустите /scan заново.")
            return

        total_markets, opportunities = cached
        text = format_scan_result(
            total_markets, opportunities, self.config.trading, page=page
        )
        kb = self._scan_keyboard(page, len(opportunities))
        await query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=kb
        )

    # ── Sync & Fees ───────────────────────────────────────────────

    async def _cmd_sync(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self.executor:
            await update.message.reply_text(  # type: ignore[union-attr]
                "❌ Executor не настроен."
            )
            return

        await update.message.reply_text(  # type: ignore[union-attr]
            "🔄 Синхронизирую с Polymarket..."
        )

        try:
            result = await self.executor.sync_positions(self.db)
            open_now = await self.db.get_open_trades()
            extra = ""
            if result.get("clob_error"):
                extra = f"\n⚠️ CLOB trades: `{result['clob_error']}`"
            await update.message.reply_text(  # type: ignore[union-attr]
                f"✅ *Синхронизация завершена:*\n"
                f"Сделок в CLOB: {result['clob_trades']}\n"
                f"Открытых в БД: {len(open_now)}\n"
                f"Закрыто/resolved: {result['resolved']}"
                f"{extra}",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error("Sync failed: %s", e)
            await update.message.reply_text(  # type: ignore[union-attr]
                f"❌ Ошибка: {e}"
            )

    async def _cmd_redeem(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self.redeemer:
            await update.message.reply_text(  # type: ignore[union-attr]
                "❌ Redeemer не настроен: нужен `PRIVATE_KEY` и либо "
                "`POLYMARKET_PROXY_ADDRESS` (Safe), либо `POLYMARKET_SIG_TYPE=0` (EOA).",
                parse_mode="Markdown",
            )
            return

        await update.message.reply_text(  # type: ignore[union-attr]
            "💰 Запуск redeem: сначала **Polymarket CLOB** (нетто-позиции), "
            "затем записи WON в БД.",
            parse_mode="Markdown",
        )

        summary = await redeem_all_pending(
            self.db,
            self.redeemer,
            self.executor,
            max_trades=100,
        )
        if summary.total == 0 and not summary.errors:
            text = (
                "📭 Нет кандидатов на redeem "
                "(нет выигрышного нетто по CLOB и нет WON в БД)."
            )
            await self.send_message(text, parse_mode=None)
            await update.message.reply_text(text)  # type: ignore[union-attr]
            return

        lines = [
            f"✅ Успешных redeem: {summary.succeeded} из {summary.total} попыток",
        ]
        if summary.errors:
            lines.append("")
            lines.append("Ошибки:")
            for err in summary.errors[:8]:
                lines.append(f"• {err}")
            if len(summary.errors) > 8:
                lines.append(f"… и ещё {len(summary.errors) - 8}")
        text = "\n".join(lines)
        await self.send_message(text, parse_mode=None)
        await update.message.reply_text(  # type: ignore[union-attr]
            text[:4000]
        )

    async def _cmd_watch_add(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        args = context.args or []
        if not args or not is_valid_wallet_address(args[0]):
            await update.message.reply_text(  # type: ignore[union-attr]
                "Использование: `/watch_add 0x… [метка]`",
                parse_mode="Markdown",
            )
            return
        addr = normalize_wallet_address(args[0])
        label = " ".join(args[1:]).strip() if len(args) > 1 else ""
        await self.db.add_watched_wallet(addr, label)
        await update.message.reply_text(  # type: ignore[union-attr]
            f"✅ Кошелёк добавлен: `{addr}`"
            + (f" ({label})" if label else "")
            + "\nПервый опрос пометит текущие сделки без уведомлений.",
            parse_mode="Markdown",
        )

    async def _cmd_watch_list(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        rows = await self.db.list_watched_wallets()
        if not rows:
            await update.message.reply_text(  # type: ignore[union-attr]
                "Список пуст. `/watch_add 0x…`",
                parse_mode="Markdown",
            )
            return
        lines = ["*Отслеживаемые кошельки:*"]
        for addr, label, init in rows:
            status = "🟢" if init else "⏳"
            lab = f" — _{label}_" if label else ""
            lines.append(f"{status} `{addr}`{lab}")
        await update.message.reply_text(  # type: ignore[union-attr]
            "\n".join(lines), parse_mode="Markdown"
        )

    async def _cmd_watch_remove(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        args = context.args or []
        if not args or not is_valid_wallet_address(args[0]):
            await update.message.reply_text(  # type: ignore[union-attr]
                "Использование: `/watch_remove 0x…`",
                parse_mode="Markdown",
            )
            return
        addr = normalize_wallet_address(args[0])
        removed = await self.db.remove_watched_wallet(addr)
        if removed:
            await update.message.reply_text(  # type: ignore[union-attr]
                f"✅ Удалён `{addr}`", parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(  # type: ignore[union-attr]
                "Такого адреса в списке нет."
            )

    async def _cmd_fees(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        today = datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        week_ago = today - timedelta(days=7)

        fees_today = await self.db.get_fees_since(today)
        fees_week = await self.db.get_fees_since(week_ago)
        fees_total = await self.db.get_total_fees()

        total_pnl = await self.db.get_total_pnl()
        net_pnl = total_pnl - fees_total

        await update.message.reply_text(  # type: ignore[union-attr]
            f"💸 *Комиссии:*\n"
            f"Сегодня: ${fees_today:.4f}\n"
            f"Неделя: ${fees_week:.4f}\n"
            f"Всё время: ${fees_total:.4f}\n\n"
            f"📊 *Итого с учётом комиссий:*\n"
            f"Gross P&L: ${total_pnl:.2f}\n"
            f"Комиссии: -${fees_total:.4f}\n"
            f"Net P&L: ${net_pnl:.2f}",
            parse_mode="Markdown",
        )

    # ── Reports ──────────────────────────────────────────────────

    async def _cmd_report(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self._cmd_status(update, context)

    async def _cmd_history(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        trades = await self.db.get_recent_trades(20)
        text = format_history(trades)
        await update.message.reply_text(  # type: ignore[union-attr]
            text, parse_mode="Markdown"
        )

    async def _cmd_pnl(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        today = datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        week_ago = today - timedelta(days=7)

        pnl_today = await self.db.get_pnl_since(today)
        pnl_week = await self.db.get_pnl_since(week_ago)
        pnl_total = await self.db.get_total_pnl()

        text = format_pnl(pnl_today, pnl_week, pnl_total)
        await update.message.reply_text(  # type: ignore[union-attr]
            text, parse_mode="Markdown"
        )
