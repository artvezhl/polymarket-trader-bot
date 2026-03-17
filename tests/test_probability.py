from __future__ import annotations

from trading.probability import (
    base_probability,
    compute_edge,
    distance_adjustment,
    final_probability,
    imbalance_adjustment,
    kelly_size,
    late_market_probability,
    microprice_adjustment,
    momentum_adjustment,
)


class TestBaseProbability:
    def test_price_above_strike(self):
        p = base_probability(85000, 84000, 0.01, 0, 1.0)
        assert p > 0.5

    def test_price_below_strike(self):
        p = base_probability(83000, 84000, 0.01, 0, 1.0)
        assert p < 0.5

    def test_price_equals_strike(self):
        p = base_probability(84000, 84000, 0.01, 0, 1.0)
        assert 0.45 < p < 0.55

    def test_zero_time(self):
        assert base_probability(85000, 84000, 0.01, 0, 0) == 1.0
        assert base_probability(83000, 84000, 0.01, 0, 0) == 0.0


class TestAdjustments:
    def test_distance_positive(self):
        adj = distance_adjustment(85000, 84000, 0.01, 1.0)
        assert adj > 0

    def test_distance_negative(self):
        adj = distance_adjustment(83000, 84000, 0.01, 1.0)
        assert adj < 0

    def test_imbalance_bullish(self):
        adj = imbalance_adjustment(100, 50)
        assert adj > 0

    def test_imbalance_bearish(self):
        adj = imbalance_adjustment(50, 100)
        assert adj < 0

    def test_microprice_above(self):
        adj = microprice_adjustment(85001, 85000)
        assert adj > 0

    def test_momentum_positive(self):
        adj = momentum_adjustment([0.001, 0.002, 0.001])
        assert adj > 0


class TestFinalProbability:
    def test_clamped_0_1(self):
        p = final_probability(
            100000, 50000, 0.5, 0.1, 1.0,
            1000, 100, 100001, 100000, [0.01] * 20
        )
        assert 0 <= p <= 1

    def test_clamped_low(self):
        p = final_probability(
            50000, 100000, 0.5, -0.1, 1.0,
            100, 1000, 49999, 50000, [-0.01] * 20
        )
        assert 0 <= p <= 1


class TestLateMarket:
    def test_far_above(self):
        p = late_market_probability(85000, 84000, 0.001, 0.01)
        assert p is not None
        assert p > 0.95

    def test_far_below(self):
        p = late_market_probability(83000, 84000, 0.001, 0.01)
        assert p is not None
        assert p < 0.05

    def test_close_to_strike(self):
        p = late_market_probability(84001, 84000, 0.01, 0.1)
        assert p is None


class TestEdgeAndKelly:
    def test_positive_edge(self):
        assert compute_edge(0.63, 0.57) > 0

    def test_kelly_positive(self):
        size = kelly_size(0.06, 1.0, 0.25)
        assert size > 0

    def test_kelly_zero_edge(self):
        assert kelly_size(0, 1.0, 0.25) == 0.0
