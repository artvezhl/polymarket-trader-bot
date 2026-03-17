from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass

import aiohttp

from database.db import Database
from database.models import Trade
from trading.btc_feed import BtcFeed
from trading.executor import TradeExecutor
from trading.probability import (
    compute_edge,
    final_probability,
    kelly_size,
    late_market_probability,
)
from trading.signals import SignalEngine
from utils.config import AppConfig
from utils.logger import logger

GAMMA_API = "https://gamma-api.polymarket.com"
STRIKE_RE = re.compile(r"\$?([\d,]+(?:\.\d+)?)")


@dataclass
class BtcMarket:
    condition_id: str
    question: str
    strike: float
    end_timestamp: float
    yes_token_id: str
    no_token_id: str
    yes_price: float
    no_price: float
    tick_size: str
    neg_risk: bool

    @property
    def time_left(self) -> float:
        return max(0.0, self.end_timestamp - time.time())

    @property
    def time_left_frac(self) -> float:
        return self.time_left / 300.0


def _parse_strike(question: str) -> float | None:
    matches = STRIKE_RE.findall(question)
    for m in matches:
        try:
            val = float(m.replace(",", ""))
            if 1000 < val < 1_000_000:
                return val
        except ValueError:
            continue
    return None


class BtcStrategy:
    """5-minute BTC market trading strategy."""

    def __init__(
        self,
        config: AppConfig,
        db: Database,
        executor: TradeExecutor,
        feed: BtcFeed,
    ):
        self.config = config
        self.db = db
        self.executor = executor
        self.feed = feed
        self.signals = SignalEngine(feed)
        self._running = False
        self._notify_callback = None

    def set_notify(self, callback) -> None:
        self._notify_callback = callback

    async def _notify(self, text: str) -> None:
        if self._notify_callback:
            try:
                await self._notify_callback(text)
            except Exception:
                pass

    async def run(self) -> None:
        self._running = True
        interval = self.config.strategy.update_interval_ms / 1000.0
        logger.info("BTC strategy started (%.0fms interval)", interval * 1000)

        while self._running:
            try:
                if self.feed.is_ready:
                    await self._cycle()
            except Exception as e:
                logger.error("Strategy cycle error: %s", e)
            await asyncio.sleep(interval)

    async def stop(self) -> None:
        self._running = False

    async def _cycle(self) -> None:
        markets = await self.find_active_markets()
        if not markets:
            return

        cfg = self.config.strategy
        sig = self.signals.all_signals(
            cfg.volatility_window_sec, cfg.momentum_window_ticks
        )

        balance = await self.executor.get_polymarket_balance()
        open_trades = await self.db.get_open_trades()
        open_market_ids = {t.market_id for t in open_trades}
        total_exposure = sum(t.bet_usd for t in open_trades)
        max_exposure = balance * cfg.max_exposure_pct if balance > 0 else 0

        for market in markets:
            if market.time_left < 5:
                continue
            if market.condition_id in open_market_ids:
                continue
            if total_exposure >= max_exposure:
                break

            result = self._evaluate(market, sig, cfg)
            if result and abs(result["edge"]) >= cfg.edge_threshold:
                trade = await self._execute(
                    market, result, balance, cfg
                )
                if trade:
                    total_exposure += trade.bet_usd
                    open_market_ids.add(market.condition_id)

        await self._auto_take_profit(open_trades, sig)

    def _evaluate(
        self, market: BtcMarket, sig: dict, cfg
    ) -> dict | None:
        t = market.time_left_frac
        if t <= 0:
            return None

        sigma = sig["volatility"]
        mu = sig["drift"]
        returns = self.feed.recent_returns(cfg.momentum_window_ticks)

        late_p = late_market_probability(
            sig["price"], market.strike, sigma, t
        )
        if late_p is not None and market.time_left < cfg.late_market_sec:
            model_prob = late_p
        else:
            model_prob = final_probability(
                price=sig["price"],
                strike=market.strike,
                sigma=sigma,
                mu=mu,
                t=t,
                bid_vol=sig["bid_volume"],
                ask_vol=sig["ask_volume"],
                microprice=sig["microprice"],
                midprice=sig["mid_price"],
                returns=returns,
            )

        edge_yes = compute_edge(model_prob, market.yes_price)
        edge_no = compute_edge(1 - model_prob, market.no_price)

        if abs(edge_yes) >= abs(edge_no) and edge_yes > 0:
            return {
                "side": "YES",
                "edge": edge_yes,
                "model_prob": model_prob,
                "market_prob": market.yes_price,
                "token_id": market.yes_token_id,
                "price": market.yes_price,
            }
        elif edge_no > 0:
            return {
                "side": "NO",
                "edge": edge_no,
                "model_prob": 1 - model_prob,
                "market_prob": market.no_price,
                "token_id": market.no_token_id,
                "price": market.no_price,
            }
        return None

    async def _execute(
        self, market: BtcMarket, result: dict, balance: float, cfg
    ) -> Trade | None:
        edge = result["edge"]
        odds = 1.0 / result["price"] - 1.0 if result["price"] > 0 else 0
        k = kelly_size(edge, odds, cfg.kelly_fraction)
        bet_usd = round(balance * k, 2)
        bet_usd = min(bet_usd, balance * cfg.trade_size_pct)
        bet_usd = max(bet_usd, 1.0)

        if bet_usd > balance * cfg.max_exposure_pct:
            return None

        price = self.executor._round_to_tick(
            result["price"], market.tick_size
        )
        if price <= 0 or price >= 1:
            return None

        from trading.scanner import MarketOpportunity

        opp = MarketOpportunity(
            market_id=market.condition_id,
            question=market.question,
            probability=price,
            outcome=result["side"],
            token_id=result["token_id"],
            liquidity=0,
            end_date=None,
            category="crypto",
            tick_size=market.tick_size,
            neg_risk=market.neg_risk,
        )

        trade = await self.executor.execute_trade(opp, balance)
        if trade:
            msg = (
                f"⚡ *BTC 5min trade:*\n"
                f"Рынок: _{market.question[:50]}_\n"
                f"Сторона: {result['side']} @ ${price:.2f}\n"
                f"Edge: {edge * 100:.1f}% | "
                f"Model: {result['model_prob'] * 100:.1f}% vs "
                f"Market: {result['market_prob'] * 100:.1f}%\n"
                f"Ставка: ${trade.bet_usd:.2f} | "
                f"BTC: ${self.feed.price:,.0f}"
            )
            await self._notify(msg)
            logger.info(
                "BTC trade: %s %s edge=%.1f%% bet=$%.2f",
                result["side"],
                market.question[:40],
                edge * 100,
                trade.bet_usd,
            )
        return trade

    async def _auto_take_profit(
        self, open_trades: list[Trade], sig: dict
    ) -> None:
        cfg = self.config.strategy
        for trade in open_trades:
            if trade.current_price <= 0:
                continue

            pnl_pct = trade.unrealized_pnl / trade.bet_usd if trade.bet_usd > 0 else 0

            should_close = False
            reason = ""

            if pnl_pct >= cfg.take_profit_pct:
                should_close = True
                reason = f"take profit ({pnl_pct * 100:.1f}%)"
            elif pnl_pct <= -cfg.stop_loss_pct:
                should_close = True
                reason = f"stop loss ({pnl_pct * 100:.1f}%)"

            if should_close:
                result = await self.executor.close_position(trade)
                if result:
                    msg = (
                        f"💰 *Авто-закрытие ({reason}):*\n"
                        f"_{trade.question[:50]}_\n"
                        f"P&L: ${result['pnl']:.2f}"
                    )
                    await self._notify(msg)
                    logger.info(
                        "Auto-close: %s reason=%s pnl=$%.2f",
                        trade.question[:30],
                        reason,
                        result["pnl"],
                    )

    async def find_active_markets(self) -> list[BtcMarket]:
        markets: list[BtcMarket] = []
        try:
            async with aiohttp.ClientSession() as session:
                params = {
                    "active": "true",
                    "closed": "false",
                    "limit": "50",
                }
                timeout = aiohttp.ClientTimeout(total=10)
                async with session.get(
                    f"{GAMMA_API}/markets",
                    params=params,
                    timeout=timeout,
                ) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json()

            from trading.scanner import _parse_float, _parse_list_field

            for m in data:
                q = m.get("question", "")
                q_lower = q.lower()
                if "btc" not in q_lower and "bitcoin" not in q_lower:
                    continue

                end_str = m.get("endDate") or ""
                if not end_str:
                    continue
                from datetime import datetime

                try:
                    end_dt = datetime.fromisoformat(
                        end_str.replace("Z", "+00:00")
                    )
                    end_ts = end_dt.timestamp()
                except (ValueError, TypeError):
                    continue

                time_left = end_ts - time.time()
                if time_left < 5 or time_left > 600:
                    continue

                strike = _parse_strike(q)
                if strike is None:
                    continue

                outcomes = _parse_list_field(m.get("outcomes"))
                prices = _parse_list_field(m.get("outcomePrices"))
                tokens = _parse_list_field(m.get("clobTokenIds"))

                if len(outcomes) < 2 or len(prices) < 2 or len(tokens) < 2:
                    continue

                yes_idx = 0
                no_idx = 1
                for i, o in enumerate(outcomes):
                    if o.lower() == "yes":
                        yes_idx = i
                    elif o.lower() == "no":
                        no_idx = i

                tick = m.get("orderPriceMinTickSize") or "0.01"

                markets.append(
                    BtcMarket(
                        condition_id=(
                            m.get("conditionId") or m.get("id", "")
                        ),
                        question=q,
                        strike=strike,
                        end_timestamp=end_ts,
                        yes_token_id=tokens[yes_idx],
                        no_token_id=tokens[no_idx],
                        yes_price=_parse_float(prices[yes_idx]),
                        no_price=_parse_float(prices[no_idx]),
                        tick_size=str(tick),
                        neg_risk=bool(m.get("negRisk", False)),
                    )
                )

        except Exception as e:
            logger.error("Failed to find BTC markets: %s", e)

        markets.sort(key=lambda m: m.time_left)
        return markets
