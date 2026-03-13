from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

import aiohttp

from utils.config import TradingConfig
from utils.logger import logger

GAMMA_API_URL = "https://gamma-api.polymarket.com"


@dataclass
class MarketOpportunity:
    market_id: str
    question: str
    probability: float
    outcome: str
    token_id: str
    liquidity: float
    end_date: datetime | None
    category: str
    min_order_size: float = 0.0


def _parse_list_field(value: str | list | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    s = str(value).strip()
    if s.startswith("["):
        try:
            parsed = json.loads(s)
            return [str(v) for v in parsed]
        except (json.JSONDecodeError, TypeError):
            pass
    return [v.strip() for v in s.split(",") if v.strip()]


def _parse_float(value: str | float | int | None, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


class MarketScanner:
    def __init__(self, config: TradingConfig):
        self.config = config

    async def fetch_markets(self) -> list[dict]:
        all_markets: list[dict] = []
        offset = 0
        limit = 100

        async with aiohttp.ClientSession() as session:
            while True:
                params = {
                    "active": "true",
                    "closed": "false",
                    "limit": str(limit),
                    "offset": str(offset),
                }
                try:
                    timeout = aiohttp.ClientTimeout(total=30)
                    async with session.get(
                        f"{GAMMA_API_URL}/markets", params=params, timeout=timeout
                    ) as resp:
                        if resp.status != 200:
                            logger.error("Gamma API returned status %d", resp.status)
                            break
                        markets = await resp.json()
                        if not markets:
                            break
                        all_markets.extend(markets)
                        if len(markets) < limit:
                            break
                        offset += limit
                except Exception as e:
                    logger.error("Failed to fetch markets: %s", e)
                    break

        logger.info("Fetched %d active markets", len(all_markets))
        return all_markets

    def filter_markets(
        self, markets: list[dict], existing_market_ids: set[str]
    ) -> list[MarketOpportunity]:
        opportunities: list[MarketOpportunity] = []

        for market in markets:
            market_id = market.get("conditionId") or market.get("id", "")
            if market_id in existing_market_ids:
                continue

            category = market.get("category", "")
            if category in self.config.skip_categories:
                continue

            liquidity = _parse_float(market.get("liquidity"))
            if liquidity < self.config.min_liquidity:
                continue

            end_date_str = market.get("endDate") or market.get("end_date_iso")
            end_date: datetime | None = None
            if end_date_str:
                try:
                    end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    delta = end_date - datetime.now(timezone.utc)
                    hours_until_close = delta.total_seconds() / 3600
                    if hours_until_close < 24:
                        continue
                except (ValueError, TypeError):
                    pass

            outcomes = _parse_list_field(market.get("outcomes"))
            prices = _parse_list_field(market.get("outcomePrices"))
            token_ids = _parse_list_field(market.get("clobTokenIds"))

            if len(outcomes) != len(prices) or len(outcomes) != len(token_ids):
                continue

            min_order = _parse_float(market.get("orderMinSize"))

            for i, (outcome, price_str, token_id) in enumerate(zip(outcomes, prices, token_ids)):
                price = _parse_float(price_str)
                if 0 < price <= self.config.max_probability:
                    opportunities.append(
                        MarketOpportunity(
                            market_id=market_id,
                            question=market.get("question", "Unknown"),
                            probability=price,
                            outcome=outcome,
                            token_id=token_id,
                            liquidity=liquidity,
                            end_date=end_date,
                            category=category,
                            min_order_size=min_order,
                        )
                    )

        opportunities.sort(key=lambda o: o.probability)
        logger.info("Found %d opportunities after filtering", len(opportunities))
        return opportunities

    async def scan(self, existing_market_ids: set[str]) -> list[MarketOpportunity]:
        markets = await self.fetch_markets()
        return self.filter_markets(markets, existing_market_ids)

    async def fetch_market_prices(
        self, trades: list
    ) -> dict[str, float]:
        """Fetch current prices for trades from Gamma API.

        Returns {market_id: {outcome: price}} for each trade.
        """
        if not trades:
            return {}

        unique_ids = {t.market_id for t in trades}
        market_data: dict[str, dict] = {}
        timeout = aiohttp.ClientTimeout(total=15)

        async with aiohttp.ClientSession() as session:

            async def _fetch_one(cid: str) -> None:
                try:
                    async with session.get(
                        f"{GAMMA_API_URL}/markets",
                        params={"conditionId": cid},
                        timeout=timeout,
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data:
                                m = data[0] if isinstance(data, list) else data
                                market_data[cid] = m
                except Exception as e:
                    logger.debug("Price fetch failed for %s: %s", cid[:16], e)

            import asyncio

            await asyncio.gather(
                *[_fetch_one(cid) for cid in unique_ids]
            )

        result: dict[str, float] = {}
        for trade in trades:
            m = market_data.get(trade.market_id)
            if not m:
                continue
            outcomes = _parse_list_field(m.get("outcomes"))
            prices = _parse_list_field(m.get("outcomePrices"))
            for outcome, price_str in zip(outcomes, prices):
                if outcome.lower() == trade.outcome.lower():
                    result[str(trade.id)] = _parse_float(price_str)
                    break

        return result
