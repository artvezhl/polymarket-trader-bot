from __future__ import annotations

import asyncio
from datetime import datetime

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

from database.db import Database
from database.models import Trade, TradeStatus
from trading.scanner import MarketOpportunity
from utils.config import AppConfig
from utils.logger import logger


class TradeExecutor:
    def __init__(self, config: AppConfig, db: Database):
        self.config = config
        self.db = db
        self._client: ClobClient | None = None

    @property
    def client(self) -> ClobClient:
        if self._client is None:
            creds = ApiCreds(
                api_key=self.config.secrets.polymarket_api_key,
                api_secret=self.config.secrets.polymarket_api_secret,
                api_passphrase=self.config.secrets.polymarket_api_passphrase,
            )
            self._client = ClobClient(
                host="https://clob.polymarket.com",
                key=self.config.secrets.private_key,
                chain_id=137,
                creds=creds,
            )
        return self._client

    def calculate_bet_size(self, deposit: float) -> float:
        bet = deposit * self.config.trading.bet_size_pct
        bet = max(bet, self.config.trading.min_bet_usd)
        bet = min(bet, self.config.trading.max_bet_usd)
        return round(bet, 2)

    async def execute_trade(
        self, opportunity: MarketOpportunity, deposit: float
    ) -> Trade | None:
        bet_usd = self.calculate_bet_size(deposit)
        if bet_usd < self.config.trading.min_bet_usd:
            logger.info("Bet size $%.2f below minimum, skipping", bet_usd)
            return None

        shares = bet_usd / opportunity.probability
        potential_payout = shares

        try:
            order_args = OrderArgs(
                price=opportunity.probability,
                size=round(shares, 2),
                side=BUY,
                token_id=opportunity.token_id,
            )

            signed_order = await asyncio.to_thread(self.client.create_order, order_args)
            resp = await asyncio.to_thread(
                self.client.post_order, signed_order, OrderType.FOK
            )

            if not resp or resp.get("status") == "error":
                logger.warning(
                    "Order rejected for %s: %s",
                    opportunity.question[:50],
                    resp,
                )
                return None

            trade = Trade(
                id=None,
                market_id=opportunity.market_id,
                question=opportunity.question,
                probability=opportunity.probability,
                bet_usd=bet_usd,
                potential_payout=round(potential_payout, 2),
                outcome=opportunity.outcome,
                status=TradeStatus.OPEN,
                created_at=datetime.now(),
                token_id=opportunity.token_id,
            )

            trade_id = await self.db.insert_trade(trade)
            trade.id = trade_id

            logger.info(
                "Trade executed: %s | prob=%.2f%% | bet=$%.2f | payout=$%.2f",
                opportunity.question[:50],
                opportunity.probability * 100,
                bet_usd,
                potential_payout,
            )
            return trade

        except Exception as e:
            logger.error("Trade execution failed for %s: %s", opportunity.question[:50], e)
            return None

    async def close_position(self, trade: Trade) -> dict | None:
        """Sell a position via CLOB API. Returns result dict or None."""
        try:
            book = await asyncio.to_thread(
                self.client.get_order_book, trade.token_id
            )

            bids = book.get("bids", [])
            if not bids:
                logger.warning("No bids for %s, cannot close", trade.question[:50])
                return None

            best_bid = float(bids[0]["price"])
            shares = round(trade.shares, 2)

            order_args = OrderArgs(
                price=best_bid,
                size=shares,
                side=SELL,
                token_id=trade.token_id,
            )
            signed_order = await asyncio.to_thread(
                self.client.create_order, order_args
            )
            resp = await asyncio.to_thread(
                self.client.post_order, signed_order, OrderType.FOK
            )

            if not resp or resp.get("status") == "error":
                logger.warning("Sell order rejected: %s", resp)
                return None

            revenue = shares * best_bid
            pnl = revenue - trade.bet_usd
            status = TradeStatus.CLOSED

            await self.db.close_trade(trade.id, pnl, status)  # type: ignore[arg-type]

            logger.info(
                "Position closed: %s | sell=%.4f | pnl=$%.2f",
                trade.question[:50],
                best_bid,
                pnl,
            )
            return {
                "trade": trade,
                "sell_price": best_bid,
                "revenue": revenue,
                "pnl": pnl,
            }

        except Exception as e:
            logger.error("Close position failed for %s: %s", trade.question[:50], e)
            return None
