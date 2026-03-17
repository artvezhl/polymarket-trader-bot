from __future__ import annotations

import math
import time

from trading.btc_feed import BtcFeed


class SignalEngine:
    """Calculate trading signals from BTC price feed."""

    def __init__(self, feed: BtcFeed):
        self.feed = feed

    def volatility(self, window_sec: int = 60) -> float:
        """Estimate 5-minute volatility as fraction of price.

        Typical BTC 5-min vol: 0.001-0.005 (0.1%-0.5%).
        Floor at 0.001 to avoid model producing 0%/100% probabilities.
        """
        min_vol = 0.002
        now = time.time()
        prices = [
            t.price
            for t in self.feed.ticks
            if now - t.timestamp <= window_sec
        ]
        if len(prices) < 10:
            return min_vol

        returns = [
            math.log(prices[i] / prices[i - 1])
            for i in range(1, len(prices))
        ]
        if not returns:
            return min_vol

        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / len(returns)
        per_tick_vol = math.sqrt(var)

        elapsed = now - min(
            t.timestamp for t in self.feed.ticks
            if now - t.timestamp <= window_sec
        )
        if elapsed < 1:
            return min_vol

        ticks_per_sec = len(prices) / elapsed
        vol_5min = per_tick_vol * math.sqrt(ticks_per_sec * 300)
        return max(vol_5min, min_vol)

    def momentum(self, window_ticks: int = 20) -> float:
        returns = self.feed.recent_returns(window_ticks)
        if not returns:
            return 0.0
        return sum(returns) / len(returns)

    def drift(self, window_sec: int = 60) -> float:
        """Short-term drift, clamped to avoid dominating the model."""
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
        raw = total_return / elapsed * 300
        max_drift = 0.0005
        return max(-max_drift, min(max_drift, raw))

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
