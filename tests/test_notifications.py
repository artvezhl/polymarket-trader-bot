from __future__ import annotations

from datetime import datetime

from bot.notifications import (
    format_close_list,
    format_close_result,
    format_history,
    format_new_trade,
    format_pnl,
    format_position_resolved,
    format_positions_list,
    format_positions_report,
    format_price_spike,
    format_settings,
    format_status_report,
)
from database.models import BalanceLog, Trade, TradeStatus
from utils.config import TradingConfig


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


class TestTradeProperties:
    def test_shares(self):
        trade = _make_trade(potential_payout=83.33)
        assert trade.shares == 83.33

    def test_current_value(self):
        trade = _make_trade(potential_payout=100.0, current_price=0.15)
        assert trade.current_value == 15.0

    def test_unrealized_pnl(self):
        trade = _make_trade(bet_usd=3.0, potential_payout=100.0, current_price=0.10)
        assert trade.unrealized_pnl == 7.0

    def test_price_multiplier(self):
        trade = _make_trade(probability=0.02, current_price=0.20)
        assert trade.price_multiplier == 10.0

    def test_price_multiplier_zero_prob(self):
        trade = _make_trade(probability=0.0, current_price=0.10)
        assert trade.price_multiplier == 0.0


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


class TestFormatSettings:
    def test_default_settings(self):
        cfg = TradingConfig()
        msg = format_settings(cfg)
        assert "Настройки торговли" in msg
        assert "5.0%" in msg
        assert "1.0%" in msg
        assert "$1.00" in msg
        assert "$10.00" in msg
        assert "$5,000" in msg
        assert "50" in msg
        assert "60с" in msg
        assert "120с" in msg
        assert "×10" in msg
        assert "нет" in msg

    def test_custom_settings(self):
        cfg = TradingConfig(
            max_probability=0.03,
            bet_size_pct=0.02,
            min_bet_usd=2.0,
            max_bet_usd=20.0,
            min_liquidity=15000,
            max_open_positions=30,
            scan_interval_sec=120,
            skip_categories=["Sports", "Entertainment"],
        )
        msg = format_settings(cfg)
        assert "3.0%" in msg
        assert "2.0%" in msg
        assert "$2.00" in msg
        assert "$20.00" in msg
        assert "$15,000" in msg
        assert "30" in msg
        assert "120с" in msg
        assert "Sports, Entertainment" in msg


class TestFormatPriceSpike:
    def test_basic(self):
        trade = _make_trade(probability=0.03, bet_usd=2.50, potential_payout=83.33)
        msg = format_price_spike(trade, new_price=0.30, multiplier=10.0)
        assert "🚀" in msg
        assert "×10.0" in msg
        assert "$0.0300" in msg
        assert "$0.3000" in msg


class TestFormatPositionsReport:
    def test_empty(self):
        msg = format_positions_report([])
        assert "Нет открытых позиций" in msg

    def test_with_trades(self):
        trades = [
            _make_trade(
                id=1, probability=0.03, current_price=0.15,
                bet_usd=2.50, potential_payout=83.33,
            ),
            _make_trade(
                id=2, probability=0.02, current_price=0.02,
                bet_usd=1.50, potential_payout=75.0,
            ),
        ]
        msg = format_positions_report(trades)
        assert "Позиции по цене" in msg
        assert "2 шт." in msg
        assert "×5.0" in msg


class TestFormatCloseList:
    def test_empty(self):
        msg = format_close_list([])
        assert "Нет открытых позиций" in msg

    def test_with_trades(self):
        trades = [_make_trade(id=1), _make_trade(id=2)]
        msg = format_close_list(trades)
        assert "/close 1" in msg
        assert "/close 2" in msg


class TestFormatCloseResult:
    def test_profit(self):
        trade = _make_trade(probability=0.03, bet_usd=2.50)
        msg = format_close_result(trade, sell_price=0.15, revenue=12.50, pnl=10.0)
        assert "✅" in msg
        assert "+$10.00" in msg

    def test_loss(self):
        trade = _make_trade(probability=0.03, bet_usd=2.50)
        msg = format_close_result(trade, sell_price=0.01, revenue=0.83, pnl=-1.67)
        assert "❌" in msg
        assert "-$1.67" in msg


class TestFormatPnl:
    def test_mixed_values(self):
        msg = format_pnl(pnl_today=5.0, pnl_week=-10.0, pnl_total=100.0)
        assert "+$5.00" in msg
        assert "-$10.00" in msg
        assert "+$100.00" in msg
