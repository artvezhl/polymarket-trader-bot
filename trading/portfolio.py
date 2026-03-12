from __future__ import annotations

from datetime import datetime, timezone

import aiohttp

from database.db import Database
from database.models import BalanceLog, TradeStatus
from trading.scanner import GAMMA_API_URL, _parse_float
from utils.logger import logger


class PortfolioManager:
    def __init__(self, db: Database):
        self.db = db

    async def get_open_positions_count(self) -> int:
        trades = await self.db.get_open_trades()
        return len(trades)

    async def get_open_positions_value(self) -> float:
        trades = await self.db.get_open_trades()
        return sum(t.bet_usd for t in trades)

    async def get_existing_market_ids(self) -> set[str]:
        trades = await self.db.get_open_trades()
        return {t.market_id for t in trades}

    async def check_resolved_markets(self) -> list[dict]:
        resolved: list[dict] = []
        open_trades = await self.db.get_open_trades()

        if not open_trades:
            return resolved

        async with aiohttp.ClientSession() as session:
            for trade in open_trades:
                try:
                    params = {"id": trade.market_id}
                    async with session.get(
                        f"{GAMMA_API_URL}/markets",
                        params=params,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        if resp.status != 200:
                            continue
                        markets = await resp.json()
                        if not markets:
                            continue

                        market = markets[0] if isinstance(markets, list) else markets
                        if not market.get("closed", False):
                            continue

                        winning_outcome = market.get("winningOutcome")
                        if winning_outcome is None:
                            resolution_prices = market.get("resolutionPrices")
                            if resolution_prices:
                                outcomes = market.get("outcomes", [])
                                if isinstance(outcomes, str):
                                    outcomes = [o.strip() for o in outcomes.split(",")]
                                if isinstance(resolution_prices, str):
                                    resolution_prices = [
                                        _parse_float(p) for p in resolution_prices.split(",")
                                    ]
                                for o, p in zip(outcomes, resolution_prices):
                                    if _parse_float(p) >= 0.99:
                                        winning_outcome = o
                                        break

                        if winning_outcome is None:
                            continue

                        won = trade.outcome.lower() == str(winning_outcome).lower()
                        pnl = trade.potential_payout - trade.bet_usd if won else -trade.bet_usd
                        status = TradeStatus.WON if won else TradeStatus.LOST

                        await self.db.update_trade_status(trade.id, status, pnl)  # type: ignore[arg-type]

                        resolved.append({
                            "trade": trade,
                            "won": won,
                            "pnl": pnl,
                            "winning_outcome": winning_outcome,
                        })

                        logger.info(
                            "Position resolved: %s | %s | PnL=$%.2f",
                            trade.question[:50],
                            "WON" if won else "LOST",
                            pnl,
                        )

                except Exception as e:
                    logger.error("Error checking market %s: %s", trade.market_id, e)

        return resolved

    async def log_balance(self, free_usdc: float) -> BalanceLog:
        positions_value = await self.get_open_positions_value()
        total_value = free_usdc + positions_value

        log = BalanceLog(
            id=None,
            free_usdc=free_usdc,
            positions_value=positions_value,
            total_value=total_value,
            timestamp=datetime.now(timezone.utc),
        )
        await self.db.insert_balance_log(log)
        return log
