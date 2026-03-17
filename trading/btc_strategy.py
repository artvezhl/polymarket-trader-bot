from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime

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
MARKET_INTERVAL = 300


@dataclass
class BtcMarket:
    condition_id: str
    question: str
    strike: float
    end_timestamp: float
    up_token_id: str
    down_token_id: str
    up_price: float
    down_price: float
    tick_size: str
    neg_risk: bool

    @property
    def time_left(self) -> float:
        return max(0.0, self.end_timestamp - time.time())

    @property
    def time_left_frac(self) -> float:
        return self.time_left / 300.0


class BtcStrategy:
    """5-minute BTC Up/Down market trading strategy."""

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
        self._failed_markets: dict[str, float] = {}
        self._fail_cooldown = 60
        self.auto_close_enabled = False
        self.take_profit_pct = config.strategy.take_profit_pct
        self.stop_loss_pct = config.strategy.stop_loss_pct

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
        logger.info(
            "BTC strategy started (%.0fms interval)", interval * 1000
        )

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
        max_exposure = (
            balance * cfg.max_exposure_pct if balance > 0 else 0
        )

        for market in markets:
            if market.time_left < 10 or market.time_left > 300:
                continue
            if market.condition_id in open_market_ids:
                continue
            cooldown_until = self._failed_markets.get(market.condition_id, 0)
            if time.time() < cooldown_until:
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
                else:
                    self._failed_markets[market.condition_id] = (
                        time.time() + self._fail_cooldown
                    )

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
        if (
            late_p is not None
            and market.time_left < cfg.late_market_sec
        ):
            p_up = late_p
        else:
            p_up = final_probability(
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

        edge_up = compute_edge(p_up, market.up_price)
        edge_down = compute_edge(1 - p_up, market.down_price)

        if edge_up > 0 and edge_up >= edge_down:
            return {
                "side": "Up",
                "edge": edge_up,
                "model_prob": p_up,
                "market_prob": market.up_price,
                "token_id": market.up_token_id,
                "price": market.up_price,
            }
        elif edge_down > 0:
            return {
                "side": "Down",
                "edge": edge_down,
                "model_prob": 1 - p_up,
                "market_prob": market.down_price,
                "token_id": market.down_token_id,
                "price": market.down_price,
            }
        return None

    async def _execute(
        self, market: BtcMarket, result: dict, balance: float, cfg
    ) -> Trade | None:
        edge = result["edge"]
        price = result["price"]
        odds = 1.0 / price - 1.0 if price > 0 else 0
        k = kelly_size(edge, odds, cfg.kelly_fraction)
        bet_usd = round(balance * k, 2)
        bet_usd = min(bet_usd, balance * cfg.trade_size_pct)
        bet_usd = max(bet_usd, 1.0)

        if bet_usd > balance * cfg.max_exposure_pct:
            return None

        rounded_price = self.executor._round_to_tick(
            price, market.tick_size
        )
        if rounded_price <= 0 or rounded_price >= 1:
            return None

        from trading.scanner import MarketOpportunity

        opp = MarketOpportunity(
            market_id=market.condition_id,
            question=market.question,
            probability=rounded_price,
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
                f"Сторона: *{result['side']}* @ ${rounded_price:.2f}\n"
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

    async def _update_live_prices(
        self, open_trades: list[Trade]
    ) -> None:
        for trade in open_trades:
            if not trade.token_id:
                continue
            try:
                price_data = await asyncio.to_thread(
                    self.executor.client.get_last_trade_price,
                    trade.token_id,
                )
                new_price = float(price_data.get("price", 0))
                if new_price > 0:
                    await self.db.update_trade_price(
                        trade.id, new_price  # type: ignore[arg-type]
                    )
                    trade.current_price = new_price
            except Exception:
                pass

    async def _auto_take_profit(
        self, open_trades: list[Trade], sig: dict
    ) -> None:
        await self._update_live_prices(open_trades)

        if not self.auto_close_enabled:
            return

        for trade in open_trades:
            if trade.current_price <= 0:
                continue

            pnl_pct = (
                trade.unrealized_pnl / trade.bet_usd
                if trade.bet_usd > 0
                else 0
            )

            should_close = False
            reason = ""

            if pnl_pct >= self.take_profit_pct:
                should_close = True
                reason = f"take profit ({pnl_pct * 100:.1f}%)"
            elif pnl_pct <= -self.stop_loss_pct:
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

    def _estimate_strike(self, market_up_price: float) -> float:
        """Estimate the strike price from market pricing.

        If Up=50% the strike ≈ current price.
        If Up=60% the price is above strike, so strike < current.
        Uses inverse of the relationship: higher Up price → lower strike.
        """
        price = self.feed.price
        if price <= 0:
            return 0
        if market_up_price <= 0.01 or market_up_price >= 0.99:
            return price
        ratio = (market_up_price - 0.5) * 0.002
        return price * (1 - ratio)

    async def find_active_markets(self) -> list[BtcMarket]:
        results: list[BtcMarket] = []
        now = int(time.time())
        base_ts = (now // MARKET_INTERVAL) * MARKET_INTERVAL
        timeout = aiohttp.ClientTimeout(total=10)

        from trading.scanner import _parse_float, _parse_list_field

        try:
            async with aiohttp.ClientSession() as session:

                async def _fetch_event(ts: int) -> None:
                    slug = f"btc-updown-5m-{ts}"
                    try:
                        async with session.get(
                            f"{GAMMA_API}/events",
                            params={"slug": slug},
                            timeout=timeout,
                        ) as resp:
                            if resp.status != 200:
                                return
                            data = await resp.json()
                            if not data:
                                return

                        ev = (
                            data[0]
                            if isinstance(data, list)
                            else data
                        )
                        for m in ev.get("markets", []):
                            if m.get("closed"):
                                continue

                            end_str = m.get("endDate", "")
                            if not end_str:
                                continue

                            end_dt = datetime.fromisoformat(
                                end_str.replace("Z", "+00:00")
                            )
                            end_ts = end_dt.timestamp()
                            left = end_ts - time.time()
                            if left < 10 or left > 600:
                                continue

                            outcomes = _parse_list_field(
                                m.get("outcomes")
                            )
                            prices = _parse_list_field(
                                m.get("outcomePrices")
                            )
                            tokens = _parse_list_field(
                                m.get("clobTokenIds")
                            )
                            if (
                                len(outcomes) < 2
                                or len(prices) < 2
                                or len(tokens) < 2
                            ):
                                continue

                            up_idx, down_idx = 0, 1
                            for i, o in enumerate(outcomes):
                                if o.lower() == "up":
                                    up_idx = i
                                elif o.lower() == "down":
                                    down_idx = i

                            up_p = _parse_float(prices[up_idx])
                            strike = self._estimate_strike(up_p)
                            tick = (
                                m.get("orderPriceMinTickSize") or "0.01"
                            )

                            results.append(
                                BtcMarket(
                                    condition_id=(
                                        m.get("conditionId")
                                        or m.get("id", "")
                                    ),
                                    question=m.get("question", ""),
                                    strike=strike,
                                    end_timestamp=end_ts,
                                    up_token_id=tokens[up_idx],
                                    down_token_id=tokens[down_idx],
                                    up_price=_parse_float(
                                        prices[up_idx]
                                    ),
                                    down_price=_parse_float(
                                        prices[down_idx]
                                    ),
                                    tick_size=str(tick),
                                    neg_risk=bool(
                                        m.get("negRisk", False)
                                    ),
                                )
                            )
                    except Exception:
                        pass

                await asyncio.gather(
                    *[
                        _fetch_event(base_ts + i * MARKET_INTERVAL)
                        for i in range(-1, 12)
                    ]
                )

        except Exception as e:
            logger.error("Failed to find BTC markets: %s", e)

        results.sort(key=lambda m: m.time_left)
        return results
