from __future__ import annotations

from datetime import datetime

import pytest

from database.db import Database
from database.models import Trade, TradeStatus
from trading.portfolio import PortfolioManager


@pytest.mark.asyncio
class TestPortfolioManager:
    @pytest.fixture
    def portfolio(self, db: Database) -> PortfolioManager:
        return PortfolioManager(db)

    async def _insert_trade(self, db: Database, market_id: str = "m1", bet: float = 5.0) -> int:
        trade = Trade(
            id=None,
            market_id=market_id,
            question="Test?",
            probability=0.03,
            bet_usd=bet,
            potential_payout=166.67,
            outcome="Yes",
            status=TradeStatus.OPEN,
            created_at=datetime.now(),
            token_id="tok1",
        )
        return await db.insert_trade(trade)

    async def test_open_positions_count(self, db: Database, portfolio: PortfolioManager):
        assert await portfolio.get_open_positions_count() == 0
        await self._insert_trade(db, "m1")
        await self._insert_trade(db, "m2")
        assert await portfolio.get_open_positions_count() == 2

    async def test_open_positions_value(self, db: Database, portfolio: PortfolioManager):
        await self._insert_trade(db, "m1", bet=3.0)
        await self._insert_trade(db, "m2", bet=7.0)
        value = await portfolio.get_open_positions_value()
        assert value == 10.0

    async def test_existing_market_ids(self, db: Database, portfolio: PortfolioManager):
        await self._insert_trade(db, "market_a")
        await self._insert_trade(db, "market_b")
        ids = await portfolio.get_existing_market_ids()
        assert ids == {"market_a", "market_b"}

    async def test_log_balance(self, db: Database, portfolio: PortfolioManager):
        await self._insert_trade(db, "m1", bet=5.0)
        balance = await portfolio.log_balance(free_usdc=95.0)
        assert balance.free_usdc == 95.0
        assert balance.positions_value == 5.0
        assert balance.total_value == 100.0
