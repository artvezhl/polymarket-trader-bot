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
    format_close_list,
    format_close_result,
    format_history,
    format_pnl,
    format_positions_list,
    format_scan_result,
    format_settings,
    format_status_report,
)
from database.db import Database
from trading.executor import TradeExecutor
from trading.portfolio import PortfolioManager
from trading.scanner import MarketScanner
from utils.config import AppConfig
from utils.logger import logger
from utils.wallet import WalletManager

HandlerFunc = Callable[..., Coroutine[Any, Any, None]]

BOT_COMMANDS = [
    BotCommand("start", "Запуск бота и приветствие"),
    BotCommand("status", "Текущий статус (вкл/выкл, параметры)"),
    BotCommand("balance", "Баланс: свободные USDC + позиции"),
    BotCommand("positions", "Список открытых позиций"),
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
    BotCommand("close", "Закрыть позицию"),
    BotCommand("scan", "Сканировать рынки (показать кол-во)"),
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
        wallet: WalletManager | None = None,
    ):
        self.config = config
        self.db = db
        self.portfolio = portfolio
        self.executor = executor
        self.wallet = wallet
        self.scanner: MarketScanner | None = None
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
            ("close", self._cmd_close),
            ("scan", self._cmd_scan),
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

    async def send_message(self, text: str) -> None:
        if not self._app or not self.config.telegram.admin_ids:
            return
        for admin_id in self.config.telegram.admin_ids:
            try:
                await self._app.bot.send_message(
                    chat_id=admin_id,
                    text=text,
                    parse_mode="Markdown",
                )
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
        if self.wallet:
            return await self.wallet.get_usdc_balance()
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
        wallet_info = ""
        if self.wallet:
            wallet_info = f"\n🔑 Кошелёк: `{self.wallet.address}`"
        await update.message.reply_text(  # type: ignore[union-attr]
            f"💰 *Баланс:*\n"
            f"Свободно: ${balance.free_usdc:.2f} USDC\n"
            f"В позициях: ~${balance.positions_value:.2f}\n"
            f"Итого: ~${balance.total_value:.2f}"
            f"{wallet_info}",
            parse_mode="Markdown",
        )

    async def _cmd_positions(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        trades = await self.db.get_open_trades()
        text = format_positions_list(trades)
        await update.message.reply_text(  # type: ignore[union-attr]
            text, parse_mode="Markdown"
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

    async def _persist(self, db_key: str, value: float | int) -> None:
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
            )
            await update.message.reply_text(  # type: ignore[union-attr]
                text, parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(  # type: ignore[union-attr]
                "❌ Не удалось закрыть позицию. "
                "Возможно, нет ликвидности или проблема с API."
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
