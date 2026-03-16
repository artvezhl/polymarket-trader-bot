from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trading.scanner import MarketScanner, _parse_float, _parse_list_field
from utils.config import TradingConfig


def _make_market(
    condition_id: str = "cond_001",
    question: str = "Test question?",
    outcomes: list[str] | str = "Yes,No",
    prices: list[str] | str = "0.95,0.05",
    token_ids: list[str] | str = "tok_yes,tok_no",
    liquidity: float = 10000.0,
    end_date: str | None = None,
    category: str = "Politics",
) -> dict:
    if end_date is None:
        future = datetime.now(timezone.utc) + timedelta(days=7)
        end_date = future.isoformat()

    return {
        "conditionId": condition_id,
        "question": question,
        "outcomes": outcomes,
        "outcomePrices": prices,
        "clobTokenIds": token_ids,
        "liquidity": liquidity,
        "endDate": end_date,
        "category": category,
        "active": True,
        "closed": False,
    }


class TestParseListField:
    def test_list_input(self):
        assert _parse_list_field(["A", "B"]) == ["A", "B"]

    def test_csv_input(self):
        assert _parse_list_field("A,B") == ["A", "B"]

    def test_none_input(self):
        assert _parse_list_field(None) == []

    def test_json_string(self):
        assert _parse_list_field('["Yes", "No"]') == ["Yes", "No"]

    def test_json_string_numbers(self):
        assert _parse_list_field('["0.95", "0.05"]') == ["0.95", "0.05"]


class TestParseFloat:
    def test_valid(self):
        assert _parse_float("0.05") == 0.05

    def test_none(self):
        assert _parse_float(None) == 0.0

    def test_invalid(self):
        assert _parse_float("abc") == 0.0


class TestMarketScanner:
    @pytest.fixture
    def scanner(self) -> MarketScanner:
        config = TradingConfig(
            max_probability=0.05,
            min_liquidity=5000,
            skip_keywords=["sports"],
        )
        return MarketScanner(config)

    def test_filter_eligible_market(self, scanner: MarketScanner):
        markets = [_make_market(prices="0.95,0.03")]
        result = scanner.filter_markets(markets, set())
        assert len(result) == 1
        assert result[0].probability == 0.03
        assert result[0].outcome == "No"

    def test_filter_skips_high_probability(self, scanner: MarketScanner):
        markets = [_make_market(prices="0.90,0.10")]
        result = scanner.filter_markets(markets, set())
        assert len(result) == 0

    def test_filter_skips_low_liquidity(self, scanner: MarketScanner):
        markets = [_make_market(prices="0.97,0.03", liquidity=1000)]
        result = scanner.filter_markets(markets, set())
        assert len(result) == 0

    def test_filter_skips_existing(self, scanner: MarketScanner):
        markets = [_make_market(prices="0.97,0.03")]
        result = scanner.filter_markets(markets, {"cond_001"})
        assert len(result) == 0

    def test_filter_skips_keyword(self, scanner: MarketScanner):
        markets = [_make_market(
            prices="0.97,0.03",
            question="Will Sports Team win?",
        )]
        result = scanner.filter_markets(markets, set())
        assert len(result) == 0

    def test_filter_skips_expiring_soon(self, scanner: MarketScanner):
        soon = datetime.now(timezone.utc) + timedelta(hours=12)
        markets = [_make_market(prices="0.97,0.03", end_date=soon.isoformat())]
        result = scanner.filter_markets(markets, set())
        assert len(result) == 0

    def test_filter_with_list_fields(self, scanner: MarketScanner):
        markets = [
            _make_market(
                outcomes=["Yes", "No"],
                prices=["0.96", "0.04"],
                token_ids=["tok_y", "tok_n"],
            )
        ]
        result = scanner.filter_markets(markets, set())
        assert len(result) == 1
        assert result[0].token_id == "tok_n"

    def test_filter_both_outcomes_low(self, scanner: MarketScanner):
        markets = [_make_market(prices="0.02,0.03")]
        result = scanner.filter_markets(markets, set())
        assert len(result) == 2

    def test_sorts_by_probability(self, scanner: MarketScanner):
        markets = [
            _make_market(condition_id="a", prices="0.97,0.03"),
            _make_market(condition_id="b", prices="0.99,0.01"),
        ]
        result = scanner.filter_markets(markets, set())
        assert len(result) == 2
        assert result[0].probability == 0.01
        assert result[1].probability == 0.03
