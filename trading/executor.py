from __future__ import annotations

import asyncio
from datetime import datetime

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    OrderArgs,
    OrderType,
)
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

    async def get_polymarket_balance(self) -> float:
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            resp = await asyncio.to_thread(
                self.client.get_balance_allowance, params
            )
            return float(resp.get("balance", 0)) / 1e6
        except Exception as e:
            logger.error("Failed to get Polymarket balance: %s", e)
            return 0.0

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

            order_id = resp.get("orderID", resp.get("id", ""))
            fee_rate = await self._get_fee_rate(opportunity.token_id)
            fee_usd = round(bet_usd * fee_rate, 4)

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
                order_id=order_id,
                fill_price=opportunity.probability,
                fee_usd=fee_usd,
            )

            trade_id = await self.db.insert_trade(trade)
            trade.id = trade_id

            logger.info(
                "Trade executed: %s | prob=%.2f%% | bet=$%.2f | fee=$%.4f",
                opportunity.question[:50],
                opportunity.probability * 100,
                bet_usd,
                fee_usd,
            )
            return trade

        except Exception as e:
            logger.error("Trade execution failed for %s: %s", opportunity.question[:50], e)
            return None

    async def _get_fee_rate(self, token_id: str) -> float:
        try:
            bps = await asyncio.to_thread(
                self.client.get_fee_rate_bps, token_id
            )
            return int(bps) / 10000
        except Exception:
            return 0.0

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

            sell_order_id = resp.get("orderID", resp.get("id", ""))
            fee_rate = await self._get_fee_rate(trade.token_id)
            revenue = shares * best_bid
            sell_fee = round(revenue * fee_rate, 4)
            pnl = revenue - trade.bet_usd - trade.fee_usd - sell_fee
            status = TradeStatus.CLOSED

            await self.db.close_trade(trade.id, pnl, status)  # type: ignore[arg-type]
            total_fee = trade.fee_usd + sell_fee
            await self.db.update_trade_fill(
                trade.id, sell_order_id, best_bid, total_fee  # type: ignore[arg-type]
            )

            logger.info(
                "Position closed: %s | sell=%.4f | pnl=$%.2f | fee=$%.4f",
                trade.question[:50],
                best_bid,
                pnl,
                total_fee,
            )
            return {
                "trade": trade,
                "sell_price": best_bid,
                "revenue": revenue,
                "pnl": pnl,
                "fee": total_fee,
            }

        except Exception as e:
            logger.error("Close position failed for %s: %s", trade.question[:50], e)
            return None
