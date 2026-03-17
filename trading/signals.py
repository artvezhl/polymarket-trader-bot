from __future__ import annotations

import math
import time

from trading.btc_feed import BtcFeed


class SignalEngine:
    """Calculate trading signals from BTC price feed."""

    def __init__(self, feed: BtcFeed):
        self.feed = feed

    def volatility(self, window_sec: int = 60) -> float:
        now = time.time()
        prices = [
            t.price
            for t in self.feed.ticks
            if now - t.timestamp <= window_sec
        ]
        if len(prices) < 2:
            return 0.001

        returns = [
            math.log(prices[i] / prices[i - 1])
            for i in range(1, len(prices))
        ]
        if not returns:
            return 0.001

        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / len(returns)
        per_tick_vol = math.sqrt(var)

        ticks_per_sec = len(prices) / max(window_sec, 1)
        if ticks_per_sec <= 0:
            return 0.001
        annualized = per_tick_vol * math.sqrt(ticks_per_sec * 300)
        return max(annualized, 0.0001)

    def momentum(self, window_ticks: int = 20) -> float:
        returns = self.feed.recent_returns(window_ticks)
        if not returns:
            return 0.0
        return sum(returns) / len(returns)

    def drift(self, window_sec: int = 60) -> float:
        now = time.time()
        prices = [
            t.price
            for t in self.feed.ticks
            if now - t.timestamp <= window_sec
        ]
        if len(prices) < 2:
            return 0.0
        total_return = math.log(prices[-1] / prices[0])
        elapsed = max(
            (
                list(self.feed.ticks)[-1].timestamp
                - list(self.feed.ticks)[0].timestamp
            ),
            0.001,
        )
        return total_return / elapsed * 300

    def all_signals(
        self, vol_window: int = 60, mom_window: int = 20
    ) -> dict:
        return {
            "price": self.feed.price,
            "microprice": self.feed.microprice,
            "mid_price": self.feed.mid_price,
            "imbalance": self.feed.imbalance,
            "bid_volume": self.feed.bid_volume,
            "ask_volume": self.feed.ask_volume,
            "volatility": self.volatility(vol_window),
            "momentum": self.momentum(mom_window),
            "drift": self.drift(vol_window),
        }
