from __future__ import annotations

from database.models import BalanceLog, Trade
from utils.config import TradingConfig


def format_new_trade(trade: Trade, deposit: float) -> str:
    pct = (trade.bet_usd / deposit * 100) if deposit > 0 else 0
    return (
        f"🟢 *Новая ставка:*\n"
        f"Рынок: _{trade.question}_\n"
        f"Вероятность: {trade.probability * 100:.1f}%\n"
        f"Ставка: ${trade.bet_usd:.2f} ({pct:.1f}% депозита)\n"
        f"Потенциальная выплата: ${trade.potential_payout:.2f}"
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
        return "📭 Нет открытых позиций"

    lines = ["📋 *Открытые позиции:*\n"]
    for i, t in enumerate(trades, 1):
        lines.append(
            f"{i}. _{t.question[:60]}_\n"
            f"   Исход: {t.outcome} | Вер: {t.probability * 100:.1f}% | "
            f"Ставка: ${t.bet_usd:.2f}"
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
        f"🔍 Проверка цен: *{cfg.price_check_interval_sec}с*\n"
        f"🚀 Алерт при росте: *×{cfg.price_spike_multiplier:.0f}*\n"
        f"🚫 Исключения: *{', '.join(cfg.skip_categories) or 'нет'}*\n\n"
        f"_Изменить:_ /set\\_max\\_prob, /set\\_bet\\_size,\n"
        f"/set\\_min\\_bet, /set\\_max\\_bet,\n"
        f"/set\\_max\\_positions, /set\\_liquidity,\n"
        f"/set\\_interval, /set\\_spike\\_mult"
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
    trade: Trade, sell_price: float, revenue: float, pnl: float
) -> str:
    pnl_icon = "✅" if pnl >= 0 else "❌"
    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
    mult = sell_price / trade.probability if trade.probability > 0 else 0

    return (
        f"{pnl_icon} *Позиция закрыта:*\n"
        f"Рынок: _{trade.question}_\n"
        f"Вход: ${trade.probability:.4f} → "
        f"Выход: ${sell_price:.4f} (×{mult:.1f})\n"
        f"Ставка: ${trade.bet_usd:.2f} | "
        f"Получено: ${revenue:.2f}\n"
        f"P&L: {pnl_str}"
    )


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
