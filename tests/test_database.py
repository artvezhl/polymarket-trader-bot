from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from database.db import Database
from database.models import BalanceLog, Trade, TradeStatus


@pytest.mark.asyncio
class TestDatabase:
    async def test_insert_and_get_trade(self, db: Database):
        trade = Trade(
            id=None,
            market_id="market_001",
            question="Will it rain tomorrow?",
            probability=0.03,
            bet_usd=2.50,
            potential_payout=83.33,
            outcome="Yes",
            status=TradeStatus.OPEN,
            created_at=datetime.now(),
            token_id="token_001",
        )
        trade_id = await db.insert_trade(trade)
        assert trade_id is not None
        assert trade_id > 0

        open_trades = await db.get_open_trades()
        assert len(open_trades) == 1
        assert open_trades[0].market_id == "market_001"
        assert open_trades[0].question == "Will it rain tomorrow?"

    async def test_get_trade_by_market(self, db: Database):
        trade = Trade(
            id=None,
            market_id="market_002",
            question="Test market",
            probability=0.02,
            bet_usd=1.0,
            potential_payout=50.0,
            outcome="No",
            status=TradeStatus.OPEN,
            created_at=datetime.now(),
            token_id="token_002",
        )
        await db.insert_trade(trade)

        found = await db.get_trade_by_market("market_002")
        assert found is not None
        assert found.outcome == "No"

        not_found = await db.get_trade_by_market("nonexistent")
        assert not_found is None

    async def test_update_trade_status(self, db: Database):
        trade = Trade(
            id=None,
            market_id="market_003",
            question="Test update",
            probability=0.04,
            bet_usd=3.0,
            potential_payout=75.0,
            outcome="Yes",
            status=TradeStatus.OPEN,
            created_at=datetime.now(),
            token_id="token_003",
        )
        trade_id = await db.insert_trade(trade)
        await db.update_trade_status(trade_id, TradeStatus.WON, pnl=72.0)

        open_trades = await db.get_open_trades()
        assert len(open_trades) == 0

    async def test_recent_trades(self, db: Database):
        for i in range(5):
            trade = Trade(
                id=None,
                market_id=f"market_{i:03d}",
                question=f"Trade {i}",
                probability=0.03,
                bet_usd=1.0,
                potential_payout=33.33,
                outcome="Yes",
                status=TradeStatus.OPEN,
                created_at=datetime.now(),
                token_id=f"token_{i:03d}",
            )
            await db.insert_trade(trade)

        recent = await db.get_recent_trades(3)
        assert len(recent) == 3

    async def test_pnl_since(self, db: Database):
        now = datetime.now()
        trade = Trade(
            id=None,
            market_id="market_pnl",
            question="PnL test",
            probability=0.02,
            bet_usd=2.0,
            potential_payout=100.0,
            outcome="Yes",
            status=TradeStatus.OPEN,
            created_at=now - timedelta(hours=1),
            token_id="token_pnl",
        )
        trade_id = await db.insert_trade(trade)
        await db.update_trade_status(trade_id, TradeStatus.WON, pnl=98.0)

        pnl = await db.get_pnl_since(now - timedelta(hours=2))
        assert pnl == 98.0

    async def test_balance_log(self, db: Database):
        log = BalanceLog(
            id=None,
            free_usdc=100.0,
            positions_value=50.0,
            total_value=150.0,
            timestamp=datetime.now(),
        )
        await db.insert_balance_log(log)

    async def test_update_trade_price(self, db: Database):
        trade = Trade(
            id=None, market_id="m_price", question="Price test",
            probability=0.03, bet_usd=2.0, potential_payout=66.67,
            outcome="Yes", status=TradeStatus.OPEN,
            created_at=datetime.now(), token_id="tok_p",
        )
        trade_id = await db.insert_trade(trade)
        await db.update_trade_price(trade_id, 0.15)

        trades = await db.get_open_trades()
        assert trades[0].current_price == 0.15

    async def test_mark_price_alert_and_close(self, db: Database):
        trade = Trade(
            id=None, market_id="m_alert", question="Alert test",
            probability=0.02, bet_usd=1.0, potential_payout=50.0,
            outcome="Yes", status=TradeStatus.OPEN,
            created_at=datetime.now(), token_id="tok_a",
        )
        trade_id = await db.insert_trade(trade)
        await db.mark_price_alert_sent(trade_id)

        trades = await db.get_open_trades()
        assert trades[0].price_alert_sent is True

        await db.close_trade(trade_id, pnl=5.0, status=TradeStatus.CLOSED)
        open_trades = await db.get_open_trades()
        assert len(open_trades) == 0

    async def test_get_open_trades_by_price(self, db: Database):
        for price, mid in [(0.05, "m1"), (0.20, "m2"), (0.01, "m3")]:
            t = Trade(
                id=None, market_id=mid, question=f"Q {mid}",
                probability=0.03, bet_usd=1.0, potential_payout=33.33,
                outcome="Yes", status=TradeStatus.OPEN,
                created_at=datetime.now(), token_id=f"tok_{mid}",
                current_price=price,
            )
            await db.insert_trade(t)

        trades = await db.get_open_trades_by_price()
        assert len(trades) == 3
        assert trades[0].current_price == 0.20
        assert trades[-1].current_price == 0.01

    async def test_config_store(self, db: Database):
        await db.set_config("test_key", "test_value")
        value = await db.get_config("test_key")
        assert value == "test_value"

        await db.set_config("test_key", "updated_value")
        value = await db.get_config("test_key")
        assert value == "updated_value"

        missing = await db.get_config("nonexistent")
        assert missing is None
