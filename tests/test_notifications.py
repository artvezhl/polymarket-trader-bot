from __future__ import annotations

from datetime import datetime

from bot.notifications import (
    format_history,
    format_new_trade,
    format_pnl,
    format_position_resolved,
    format_positions_list,
    format_status_report,
)
from database.models import BalanceLog, Trade, TradeStatus


def _make_trade(**kwargs) -> Trade:
    defaults = {
        "id": 1,
        "market_id": "m1",
        "question": "Will X happen?",
        "probability": 0.03,
        "bet_usd": 2.50,
        "potential_payout": 83.33,
        "outcome": "Yes",
        "status": TradeStatus.OPEN,
        "created_at": datetime.now(),
        "token_id": "tok1",
    }
    defaults.update(kwargs)
    return Trade(**defaults)


class TestFormatNewTrade:
    def test_basic(self):
        trade = _make_trade()
        msg = format_new_trade(trade, deposit=250.0)
        assert "🟢" in msg
        assert "Новая ставка" in msg
        assert "$2.50" in msg
        assert "3.0%" in msg

    def test_zero_deposit(self):
        trade = _make_trade()
        msg = format_new_trade(trade, deposit=0)
        assert "0.0% депозита" in msg


class TestFormatPositionResolved:
    def test_win(self):
        trade = _make_trade(potential_payout=83.33, bet_usd=2.50)
        msg = format_position_resolved(trade, won=True, pnl=80.83)
        assert "✅" in msg
        assert "WIN" in msg
        assert "+$80.83" in msg

    def test_loss(self):
        trade = _make_trade(bet_usd=2.50)
        msg = format_position_resolved(trade, won=False, pnl=-2.50)
        assert "❌" in msg
        assert "LOSS" in msg
        assert "$2.50" in msg


class TestFormatStatusReport:
    def test_trading_active(self):
        balance = BalanceLog(
            id=1, free_usdc=87.50, positions_value=57.50,
            total_value=145.0, timestamp=datetime.now(),
        )
        msg = format_status_report(balance, 23, 12, -3.20, True)
        assert "🟢" in msg
        assert "активна" in msg
        assert "$87.50" in msg
        assert "23" in msg

    def test_trading_stopped(self):
        balance = BalanceLog(
            id=1, free_usdc=100.0, positions_value=0.0,
            total_value=100.0, timestamp=datetime.now(),
        )
        msg = format_status_report(balance, 0, 0, 0.0, False)
        assert "🔴" in msg
        assert "остановлена" in msg


class TestFormatPositionsList:
    def test_empty(self):
        msg = format_positions_list([])
        assert "Нет открытых позиций" in msg

    def test_with_trades(self):
        trades = [_make_trade(id=1), _make_trade(id=2, question="Another?")]
        msg = format_positions_list(trades)
        assert "Открытые позиции" in msg
        assert "Will X happen?" in msg
        assert "Another?" in msg


class TestFormatHistory:
    def test_empty(self):
        msg = format_history([])
        assert "Нет истории" in msg

    def test_with_mixed_statuses(self):
        trades = [
            _make_trade(status=TradeStatus.OPEN),
            _make_trade(status=TradeStatus.WON, pnl=50.0),
            _make_trade(status=TradeStatus.LOST, pnl=-2.0),
        ]
        msg = format_history(trades)
        assert "🔵" in msg
        assert "✅" in msg
        assert "❌" in msg


class TestFormatPnl:
    def test_mixed_values(self):
        msg = format_pnl(pnl_today=5.0, pnl_week=-10.0, pnl_total=100.0)
        assert "+$5.00" in msg
        assert "-$10.00" in msg
        assert "+$100.00" in msg
