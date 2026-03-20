from __future__ import annotations

from database.models import BalanceLog, Trade
from trading.clob_positions import ClobOpenPosition
from utils.config import TradingConfig


def format_new_trade(trade: Trade, deposit: float) -> str:
    pct = (trade.bet_usd / deposit * 100) if deposit > 0 else 0
    fee_line = f"\nКомиссия: ${trade.fee_usd:.4f}" if trade.fee_usd > 0 else ""
    return (
        f"🟢 *Новая ставка:*\n"
        f"Рынок: _{trade.question}_\n"
        f"Вероятность: {trade.probability * 100:.1f}%\n"
        f"Ставка: ${trade.bet_usd:.2f} ({pct:.1f}% депозита)\n"
        f"Потенциальная выплата: ${trade.potential_payout:.2f}"
        f"{fee_line}"
    )


def format_position_resolved(trade: Trade, won: bool, pnl: float) -> str:
    if won:
        return (
            f"✅ *Позиция закрыта (WIN):*\n"
            f"Рынок: _{trade.question}_\n"
            f"Выплата: ${trade.potential_payout:.2f} "
            f"(ставка была ${trade.bet_usd:.2f})\n"
            f"Прибыль: +${pnl:.2f}"
        )
    return (
        f"❌ *Позиция закрыта (LOSS):*\n"
        f"Рынок: _{trade.question}_\n"
        f"Ставка: ${trade.bet_usd:.2f}\n"
        f"Убыток: -${abs(pnl):.2f}"
    )


def format_status_report(
    balance: BalanceLog,
    open_count: int,
    trades_today: int,
    pnl_today: float,
    is_trading: bool,
) -> str:
    status_icon = "🟢" if is_trading else "🔴"
    return (
        f"📊 *Статус:*\n"
        f"Торговля: {status_icon} {'активна' if is_trading else 'остановлена'}\n"
        f"Депозит: ${balance.free_usdc:.2f} USDC свободно\n"
        f"Открытых позиций: {open_count} "
        f"(стоимость ~${balance.positions_value:.2f})\n"
        f"Полный баланс: ~${balance.total_value:.2f}\n"
        f"Сделок сегодня: {trades_today}\n"
        f"P&L сегодня: {'+' if pnl_today >= 0 else ''}"
        f"${pnl_today:.2f}"
    )


def format_positions_list(trades: list[Trade]) -> str:
    if not trades:
        return "📭 Нет открытых позиций (только записи бота в БД)"

    lines = ["📋 *Открытые позиции (только БД бота):*\n"]
    for i, t in enumerate(trades, 1):
        lines.append(
            f"{i}. _{t.question[:60]}_\n"
            f"   Исход: {t.outcome} | Вер: {t.probability * 100:.1f}% | "
            f"Ставка: ${t.bet_usd:.2f}"
        )
    return "\n".join(lines)


def format_data_api_positions(
    positions: list[dict],
    error: str | None = None,
) -> str:
    """Позиции из Data API (реальные по кошельку)."""
    if error:
        return f"❌ Data API: `{error}`"
    if not positions:
        return "📭 Реальных позиций нет (Data API)"
    lines = ["📊 *Реальные позиции (Data API):*\n"]
    for i, p in enumerate(positions[:20], 1):
        title = (p.get("title") or "—")[:70]
        if len(str(p.get("title") or "")) > 70:
            title += "…"
        size = p.get("size") or 0
        outcome = p.get("outcome") or "—"
        lines.append(f"{i}. _{title}_\n   {outcome} | size: `{size}`")
    if len(positions) > 20:
        lines.append(f"\n_…и ещё {len(positions) - 20}_")
    return "\n".join(lines)


def format_clob_positions_list(
    positions: list[ClobOpenPosition],
    clob_error: str | None = None,
) -> str:
    """Позиции по нетто из Polymarket CLOB (все сделки аккаунта)."""
    if clob_error:
        return (
            "❌ *Не удалось загрузить историю сделок CLOB*\n"
            f"`{clob_error}`\n\n"
            "Часто: *POLYMARKET_API_SECRET* не валидный base64 (без кавычек и "
            "переносов), или ключи API не от этого кошелька. "
            "Перевыпустите ключи в Polymarket / проверьте `.env`."
        )
    if not positions:
        return (
            "📭 По истории CLOB нет ненулевых позиций "
            "(нетто по всем токенам ≈ 0)."
        )

    lines = [
        "📊 *Все позиции (Polymarket CLOB, нетто по token):*\n",
        "_Источник: история сделок `get_trades`, не только БД бота._\n",
    ]
    for i, p in enumerate(positions, 1):
        status = "🔒 закрыт" if p.market_closed else "⏳ открыт"
        price_s = f"{p.current_price * 100:.1f}¢" if p.current_price > 0 else "—"
        val_s = f" ~${p.notional_usd:.2f}" if p.notional_usd > 0 else ""
        q = p.question[:70] + "…" if len(p.question) > 70 else p.question
        lines.append(
            f"{i}. _{q}_\n"
            f"   {status} | {p.outcome} | shares: `{p.shares:.4f}` | "
            f"цена: {price_s}{val_s}"
        )
    return "\n".join(lines)


def format_history(trades: list[Trade]) -> str:
    if not trades:
        return "📭 Нет истории сделок"

    lines = ["📜 *Последние сделки:*\n"]
    for t in trades:
        status_icon = {"open": "🔵", "won": "✅", "lost": "❌"}.get(t.status.value, "⚪")
        pnl_str = f" | PnL: ${t.pnl:+.2f}" if t.status.value != "open" else ""
        lines.append(
            f"{status_icon} _{t.question[:50]}_\n"
            f"   ${t.bet_usd:.2f} | {t.probability * 100:.1f}%{pnl_str}"
        )
    return "\n".join(lines)


def format_settings(cfg: TradingConfig) -> str:
    return (
        f"⚙️ *Настройки торговли:*\n\n"
        f"📉 Макс. вероятность: *{cfg.max_probability * 100:.1f}%*\n"
        f"💵 Размер ставки: *{cfg.bet_size_pct * 100:.1f}%* от депозита\n"
        f"📏 Мин. ставка: *${cfg.min_bet_usd:.2f}*\n"
        f"📏 Макс. ставка: *${cfg.max_bet_usd:.2f}*\n"
        f"💧 Мин. ликвидность: *${cfg.min_liquidity:,.0f}*\n"
        f"📊 Макс. позиций: *{cfg.max_open_positions}*\n"
        f"⏱ Интервал скана: *{cfg.scan_interval_sec}с*\n"
        f"📅 Мин. до закрытия: *{cfg.min_end_date_days} дн.*\n"
        f"🔍 Проверка цен: *{cfg.price_check_interval_sec}с*\n"
        f"🚀 Алерт при росте: *×{cfg.price_spike_multiplier:.0f}*\n"
        f"🚫 Исключения: *{', '.join(cfg.skip_keywords) or 'нет'}*\n\n"
        f"_Изменить:_ /set\\_max\\_prob, /set\\_bet\\_size,\n"
        f"/set\\_min\\_bet, /set\\_max\\_bet,\n"
        f"/set\\_max\\_positions, /set\\_liquidity,\n"
        f"/set\\_interval, /set\\_spike\\_mult,\n"
        f"/set\\_min\\_days, /set\\_skip\\_words"
    )


def format_price_spike(trade: Trade, new_price: float, multiplier: float) -> str:
    return (
        f"🚀 *Рост цены позиции (×{multiplier:.1f}):*\n"
        f"Рынок: _{trade.question}_\n"
        f"Вход: ${trade.probability:.4f} → Сейчас: ${new_price:.4f}\n"
        f"Ставка: ${trade.bet_usd:.2f} → "
        f"Стоимость: ~${trade.shares * new_price:.2f}\n"
        f"Unrealized P&L: +${trade.shares * new_price - trade.bet_usd:.2f}\n\n"
        f"Закрыть: /close (см. список)"
    )


def format_positions_report(trades: list[Trade]) -> str:
    if not trades:
        return "📭 Нет открытых позиций"

    total_cost = sum(t.bet_usd for t in trades)
    total_value = sum(t.current_value for t in trades)
    total_pnl = total_value - total_cost

    def _pnl_str(v: float) -> str:
        return f"+${v:.2f}" if v >= 0 else f"-${abs(v):.2f}"

    lines = [
        f"📋 *Позиции по цене* ({len(trades)} шт.):\n"
        f"Стоимость: ${total_value:.2f} | "
        f"PnL: {_pnl_str(total_pnl)}\n"
    ]
    for i, t in enumerate(trades, 1):
        if t.price_multiplier >= 2:
            icon = "🔥"
        elif t.price_multiplier >= 1.1:
            icon = "⬆️"
        elif t.price_multiplier >= 0.9:
            icon = "➡️"
        else:
            icon = "⬇️"

        lines.append(
            f"{i}. {icon} _{t.question[:50]}_\n"
            f"   ${t.probability:.3f}→${t.current_price:.3f} "
            f"(×{t.price_multiplier:.1f}) | "
            f"{_pnl_str(t.unrealized_pnl)}"
        )
    return "\n".join(lines)


def format_close_list(trades: list[Trade]) -> str:
    if not trades:
        return "📭 Нет открытых позиций для закрытия"

    lines = ["📋 *Выберите позицию для закрытия:*\n"]
    for i, t in enumerate(trades, 1):
        val = f"${t.current_value:.2f}" if t.current_price > 0 else "?"
        lines.append(
            f"{i}. _{t.question[:50]}_\n"
            f"   Ставка: ${t.bet_usd:.2f} | Стоимость: {val}\n"
            f"   Закрыть: /close {i}"
        )
    return "\n".join(lines)


def format_close_result(
    trade: Trade, sell_price: float, revenue: float, pnl: float,
    fee: float = 0.0,
) -> str:
    pnl_icon = "✅" if pnl >= 0 else "❌"
    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
    mult = sell_price / trade.probability if trade.probability > 0 else 0
    fee_line = f"\nКомиссии: ${fee:.4f}" if fee > 0 else ""

    return (
        f"{pnl_icon} *Позиция закрыта:*\n"
        f"Рынок: _{trade.question}_\n"
        f"Вход: ${trade.probability:.4f} → "
        f"Выход: ${sell_price:.4f} (×{mult:.1f})\n"
        f"Ставка: ${trade.bet_usd:.2f} | "
        f"Получено: ${revenue:.2f}\n"
        f"P&L: {pnl_str} (после комиссий)"
        f"{fee_line}"
    )


SCAN_PAGE_SIZE = 10


def format_scan_result(
    total_markets: int,
    opportunities: list,
    config: "TradingConfig",
    page: int = 0,
) -> str:
    total = len(opportunities)
    start = page * SCAN_PAGE_SIZE
    end = start + SCAN_PAGE_SIZE
    page_items = opportunities[start:end]
    total_pages = max(1, (total + SCAN_PAGE_SIZE - 1) // SCAN_PAGE_SIZE)

    lines = [
        f"🔎 *Результат сканирования:*\n"
        f"Всего рынков: {total_markets}\n"
        f"Подходящих: *{total}*\n"
        f"_Фильтры:_ вер. ≤ {config.max_probability * 100:.1f}%, "
        f"ликв. ≥ ${config.min_liquidity:,.0f}\n"
    ]

    if page_items:
        lines.append(f"📄 *Стр. {page + 1}/{total_pages}* "
                      f"({start + 1}-{min(end, total)} из {total}):\n")
        for i, opp in enumerate(page_items, start + 1):
            lines.append(
                f"{i}. [{opp.probability * 100:.1f}%] "
                f"_{opp.question[:42]}_\n"
                f"   лик: ${opp.liquidity:,.0f}"
            )
    else:
        lines.append("Нет результатов на этой странице.")

    return "\n".join(lines)


def format_pnl(pnl_today: float, pnl_week: float, pnl_total: float) -> str:
    def _fmt(v: float) -> str:
        if v >= 0:
            return f"+${v:.2f}"
        return f"-${abs(v):.2f}"

    return (
        f"💰 *P&L:*\n"
        f"Сегодня: {_fmt(pnl_today)}\n"
        f"Неделя: {_fmt(pnl_week)}\n"
        f"Всё время: {_fmt(pnl_total)}"
    )
