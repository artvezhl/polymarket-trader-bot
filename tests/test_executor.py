from __future__ import annotations

import pytest

from trading.executor import TradeExecutor


class TestBetSizeCalculation:
    @pytest.fixture
    def executor(self, db, app_config) -> TradeExecutor:
        return TradeExecutor(app_config, db)

    def test_normal_bet(self, executor: TradeExecutor):
        bet = executor.calculate_bet_size(deposit=1000.0)
        assert bet == 10.0

    def test_min_bet(self, executor: TradeExecutor):
        bet = executor.calculate_bet_size(deposit=50.0)
        assert bet == 1.0

    def test_max_bet(self, executor: TradeExecutor):
        executor.config.trading.bet_size_pct = 0.10
        bet = executor.calculate_bet_size(deposit=1000.0)
        assert bet == 10.0

    def test_custom_limits(self, executor: TradeExecutor):
        executor.config.trading.min_bet_usd = 5.0
        executor.config.trading.max_bet_usd = 20.0
        executor.config.trading.bet_size_pct = 0.05
        bet = executor.calculate_bet_size(deposit=1000.0)
        assert bet == 20.0
