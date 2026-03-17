from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass

import aiohttp

from utils.logger import logger

EXCHANGES = {
    "binance": {
        "url": "wss://stream.binance.com:9443/stream?streams=btcusdt@aggTrade/btcusdt@depth5@100ms",
        "subscribe": None,
    },
    "bybit": {
        "url": "wss://stream.bybit.com/v5/public/spot",
        "subscribe": {
            "op": "subscribe",
            "args": ["tickers.BTCUSDT", "orderbook.1.BTCUSDT"],
        },
    },
}


@dataclass
class PriceTick:
    price: float
    timestamp: float
    volume: float = 0.0


@dataclass
class OrderBookLevel:
    price: float
    volume: float


class BtcFeed:
    """Real-time BTC/USDT price and orderbook via WebSocket."""

    def __init__(self, exchange: str = "binance", max_ticks: int = 600):
        self.exchange = exchange
        self.price: float = 0.0
        self.bids: list[OrderBookLevel] = []
        self.asks: list[OrderBookLevel] = []
        self.ticks: deque[PriceTick] = deque(maxlen=max_ticks)
        self._running = False
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._last_update: float = 0.0

    @property
    def is_ready(self) -> bool:
        return self.price > 0 and len(self.ticks) >= 5

    @property
    def mid_price(self) -> float:
        if self.bids and self.asks:
            return (self.bids[0].price + self.asks[0].price) / 2
        return self.price

    @property
    def microprice(self) -> float:
        if not self.bids or not self.asks:
            return self.price
        best_bid = self.bids[0]
        best_ask = self.asks[0]
        total_vol = best_bid.volume + best_ask.volume
        if total_vol == 0:
            return self.mid_price
        return (
            best_ask.price * best_bid.volume
            + best_bid.price * best_ask.volume
        ) / total_vol

    @property
    def imbalance(self) -> float:
        if not self.bids or not self.asks:
            return 0.0
        bid_vol = sum(b.volume for b in self.bids)
        ask_vol = sum(a.volume for a in self.asks)
        total = bid_vol + ask_vol
        if total == 0:
            return 0.0
        return (bid_vol - ask_vol) / total

    @property
    def bid_volume(self) -> float:
        return sum(b.volume for b in self.bids)

    @property
    def ask_volume(self) -> float:
        return sum(a.volume for a in self.asks)

    def recent_returns(self, n: int = 20) -> list[float]:
        if len(self.ticks) < 2:
            return []
        prices = [t.price for t in list(self.ticks)[-n - 1:]]
        return [
            (prices[i] - prices[i - 1]) / prices[i - 1]
            for i in range(1, len(prices))
        ]

    async def start(self) -> None:
        self._running = True
        while self._running:
            try:
                await self._connect_and_listen()
            except Exception as e:
                logger.warning("BTC feed error: %s, reconnecting...", e)
                await asyncio.sleep(2)

    async def stop(self) -> None:
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()

    async def _connect_and_listen(self) -> None:
        ex = EXCHANGES.get(self.exchange)
        if not ex:
            logger.error("Unknown exchange: %s", self.exchange)
            return

        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(
                ex["url"],
                heartbeat=20,
                timeout=aiohttp.ClientTimeout(total=30),
            )
            logger.info("BTC feed connected to %s", self.exchange)

            if ex["subscribe"]:
                import json

                await self._ws.send_str(json.dumps(ex["subscribe"]))

            async for msg in self._ws:
                if not self._running:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = msg.json()
                    if self.exchange == "binance":
                        self._process_binance(data)
                    elif self.exchange == "bybit":
                        self._process_bybit(data)
                elif msg.type in (
                    aiohttp.WSMsgType.ERROR,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    break
        finally:
            if self._session and not self._session.closed:
                await self._session.close()

    def _process_binance(self, data: dict) -> None:
        stream = data.get("stream", "")
        payload = data.get("data", {})

        if "aggTrade" in stream:
            price = float(payload.get("p", 0))
            volume = float(payload.get("q", 0))
            if price > 0:
                self._update_price(price, volume)

        elif "depth" in stream:
            self._update_book(
                payload.get("bids", []), payload.get("asks", [])
            )

    def _process_bybit(self, data: dict) -> None:
        topic = data.get("topic", "")

        if "tickers" in topic:
            d = data.get("data", {})
            price = float(d.get("lastPrice", 0))
            volume = float(d.get("volume24h", 0))
            if price > 0:
                self._update_price(price, volume)

        elif "orderbook" in topic:
            d = data.get("data", {})
            bids = d.get("b", [])
            asks = d.get("a", [])
            if bids or asks:
                self._update_book(bids, asks)

    def _update_price(self, price: float, volume: float) -> None:
        self.price = price
        self.ticks.append(
            PriceTick(price=price, timestamp=time.time(), volume=volume)
        )
        self._last_update = time.time()

    def _update_book(self, raw_bids: list, raw_asks: list) -> None:
        self.bids = [
            OrderBookLevel(float(b[0]), float(b[1])) for b in raw_bids
        ]
        self.asks = [
            OrderBookLevel(float(a[0]), float(a[1])) for a in raw_asks
        ]
