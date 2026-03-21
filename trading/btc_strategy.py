from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

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
DEBUG_LOG_PATH = str(Path(__file__).resolve().parent.parent / "debug-strategy.log")


def _debug_log(location: str, message: str, data: dict, hypothesis_id: str = "") -> None:
    try:
        payload = {
            "sessionId": "1fd410",
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        if hypothesis_id:
            payload["hypothesisId"] = hypothesis_id
        with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


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
        self._failed_closes: dict[int, float] = {}
        self._fail_cooldown = 60
        self._fail_cooldown_95 = 15
        self.auto_close_enabled = False
        self.take_profit_pct = config.strategy.take_profit_pct
        self.stop_loss_pct = config.strategy.stop_loss_pct
        self.hedge_enabled = False
        self.hedge_trigger_pct = 0.05
        self.hedge_ratio = 0.5
        self.reverse_signal = False
        self._hedged_trades: set[int] = set()
        self._confirmed_cycles: dict[str, int] = {}

    async def load_settings(self) -> None:
        """Load strategy settings from DB."""

        keys = {
            "strategy.auto_close": ("auto_close_enabled", bool),
            "strategy.reverse_signal": ("reverse_signal", bool),
            "strategy.hedge_enabled": ("hedge_enabled", bool),
            "strategy.take_profit_pct": ("take_profit_pct", float),
            "strategy.stop_loss_pct": ("stop_loss_pct", float),
            "strategy.hedge_trigger_pct": ("hedge_trigger_pct", float),
            "strategy.hedge_ratio": ("hedge_ratio", float),
        }
        for db_key, (attr, cast) in keys.items():
            val = await self.db.get_config(db_key)
            if val is not None:
                if cast is bool:
                    setattr(self, attr, val == "1")
                else:
                    setattr(self, attr, cast(val))
        logger.info(
            "Strategy settings loaded: reverse=%s auto_close=%s "
            "tp=%.1f%% sl=%.1f%% hedge=%s",
            self.reverse_signal,
            self.auto_close_enabled,
            self.take_profit_pct * 100,
            self.stop_loss_pct * 100,
            self.hedge_enabled,
        )

    async def save_setting(self, key: str, value) -> None:
        """Save a single strategy setting to DB."""
        if isinstance(value, bool):
            await self.db.set_config(f"strategy.{key}", "1" if value else "0")
        else:
            await self.db.set_config(f"strategy.{key}", str(value))

    def set_notify(self, callback) -> None:
        self._notify_callback = callback

    async def _notify(self, text: str) -> None:
        if self._notify_callback:
            try:
                await self._notify_callback(text)
            except Exception:
                pass

    async def run(self) -> None:
        # #region agent log
        _debug_log(
            "btc_strategy.py:run",
            "strategy run() entered",
            {"mode": self.config.strategy.mode},
            "H0",
        )
        # #endregion
        self._running = True
        interval = self.config.strategy.update_interval_ms / 1000.0
        mode = self.config.strategy.mode
        logger.info(
            "Strategy started: mode=%s (%.0fms interval)",
            mode,
            interval * 1000,
        )

        while self._running:
            try:
                if mode == "btc_eth_95_limit":
                    await self._cycle_95_limit()
                elif self.feed.is_ready:
                    await self._cycle()
            except Exception as e:
                logger.error("Strategy cycle error: %s", e)
            await asyncio.sleep(interval)

    async def stop(self) -> None:
        self._running = False

    async def _cycle_95_limit(self) -> None:
        # #region agent log
        _debug_log("btc_strategy.py:_cycle_95_limit:entry", "cycle_95_limit entered", {}, "H0")
        # #endregion
        markets = await self.find_crypto_5m_markets(
            ["btc-updown-5m", "eth-updown-5m"]
        )
        await self._refresh_market_prices_from_clob(markets)
        # #region agent log
        _debug_log(
            "btc_strategy.py:_cycle_95_limit:start",
            "cycle_95_limit",
            {"markets_count": len(markets)},
            "H2",
        )
        # #endregion
        if not markets:
            return

        cfg = self.config.strategy
        limit_price = cfg.limit_price
        min_fav = cfg.min_favorite_price

        balance = await self.executor.get_polymarket_balance()
        open_trades = await self.db.get_open_trades()
        open_market_ids = {t.market_id for t in open_trades}
        total_exposure = sum(t.bet_usd for t in open_trades)
        max_exposure = balance * cfg.max_exposure_pct if balance > 0 else 0

        # #region agent log
        sample = (
            {
                "up": markets[0].up_price,
                "down": markets[0].down_price,
                "time_left": round(markets[0].time_left, 1),
            }
            if markets
            else {}
        )
        _debug_log(
            "btc_strategy.py:_cycle_95_limit:state",
            "state",
            {
                "balance": balance,
                "min_fav": min_fav,
                "total_exposure": total_exposure,
                "max_exposure": max_exposure,
                "open_market_ids": list(open_market_ids),
                "sample_clob_prices": sample,
            },
            "H2,H4",
        )
        # #endregion

        LAST_MINUTE_SEC = 60
        CONFIRM_CYCLES = 3

        for market in markets:
            if market.time_left < 10 or market.time_left > LAST_MINUTE_SEC:
                self._confirmed_cycles.pop(market.condition_id, None)
                # #region agent log
                if market.up_price >= 0.85 or market.down_price >= 0.85:
                    _debug_log(
                        "btc_strategy.py:_cycle_95_limit:skip_time",
                        "skip: time_left",
                        {
                            "condition_id": market.condition_id[:20],
                            "up_price": market.up_price,
                            "down_price": market.down_price,
                            "time_left": round(market.time_left, 1),
                            "min_fav": min_fav,
                            "up_ge_min": market.up_price >= min_fav,
                            "down_ge_min": market.down_price >= min_fav,
                        },
                        "H2,H3",
                    )
                # #endregion
                continue
            if market.condition_id in open_market_ids:
                # #region agent log
                if market.up_price >= 0.85 or market.down_price >= 0.85:
                    _debug_log(
                        "btc_strategy.py:_cycle_95_limit:skip_open",
                        "skip: already open",
                        {
                            "condition_id": market.condition_id[:20],
                            "up_price": market.up_price,
                            "down_price": market.down_price,
                        },
                        "H4",
                    )
                # #endregion
                continue
            if total_exposure >= max_exposure:
                break

            favorite_side, favorite_price, favorite_token = None, 0.0, ""
            if market.up_price >= min_fav and market.up_price >= market.down_price:
                favorite_side, favorite_price, favorite_token = (
                    "Up",
                    market.up_price,
                    market.up_token_id,
                )
            elif market.down_price >= min_fav:
                favorite_side, favorite_price, favorite_token = (
                    "Down",
                    market.down_price,
                    market.down_token_id,
                )

            # #region agent log
            if market.up_price >= 0.85 or market.down_price >= 0.85:
                _debug_log(
                    "btc_strategy.py:_cycle_95_limit:favorite_check",
                    "favorite_check",
                    {
                        "condition_id": market.condition_id[:20],
                        "up_price": market.up_price,
                        "down_price": market.down_price,
                        "min_fav": min_fav,
                        "favorite_side": favorite_side,
                        "up_ge_min": market.up_price >= min_fav,
                        "down_ge_min": market.down_price >= min_fav,
                        "up_ge_down": market.up_price >= market.down_price,
                    },
                    "H1,H3",
                )
            # #endregion

            if not favorite_side:
                self._confirmed_cycles.pop(market.condition_id, None)
                continue

            confirmed = self._confirmed_cycles.get(market.condition_id, 0) + 1
            self._confirmed_cycles[market.condition_id] = confirmed
            if confirmed < CONFIRM_CYCLES:
                # #region agent log
                _debug_log(
                    "btc_strategy.py:_cycle_95_limit:skip_confirm",
                    "skip: need 3 cycles",
                    {
                        "condition_id": market.condition_id[:20],
                        "confirmed": confirmed,
                        "need": CONFIRM_CYCLES,
                        "favorite_side": favorite_side,
                    },
                    "H4",
                )
                # #endregion
                continue

            cooldown_until = self._failed_markets.get(market.condition_id, 0)
            if time.time() < cooldown_until:
                # #region agent log
                _debug_log(
                    "btc_strategy.py:_cycle_95_limit:skip_cooldown",
                    "skip: cooldown",
                    {"condition_id": market.condition_id[:20]},
                    "H4",
                )
                # #endregion
                continue

            from trading.scanner import MarketOpportunity

            rounded_limit = self.executor._round_to_tick(
                limit_price, market.tick_size
            )
            if rounded_limit <= 0 or rounded_limit >= 1:
                continue

            bet_usd = round(balance * cfg.trade_size_pct, 2)
            bet_usd = max(bet_usd, 1.0)
            if bet_usd > balance * cfg.max_exposure_pct:
                continue

            opp = MarketOpportunity(
                market_id=market.condition_id,
                question=market.question,
                probability=rounded_limit,
                outcome=favorite_side,
                token_id=favorite_token,
                liquidity=0,
                end_date=None,
                category="crypto",
                tick_size=market.tick_size,
                neg_risk=market.neg_risk,
            )

            # #region agent log
            _debug_log(
                "btc_strategy.py:_cycle_95_limit:before_execute",
                "calling execute_trade",
                {
                    "condition_id": market.condition_id[:20],
                    "outcome": favorite_side,
                    "bet_usd": bet_usd,
                },
                "H5",
            )
            # #endregion

            trade = await self.executor.execute_trade(
                opp, balance, bet_usd=bet_usd
            )

            # #region agent log
            _debug_log(
                "btc_strategy.py:_cycle_95_limit:after_execute",
                "execute_trade result",
                {"trade_created": trade is not None},
                "H5",
            )
            # #endregion

            if trade:
                self._confirmed_cycles.pop(market.condition_id, None)
                total_exposure += trade.bet_usd
                open_market_ids.add(market.condition_id)
                msg = (
                    f"📊 *Limit 0.95:*\n"
                    f"Рынок: _{market.question[:50]}_\n"
                    f"Фаворит: *{favorite_side}* (mkt {favorite_price:.2f}) "
                    f"→ лимит {rounded_limit:.2f}\n"
                    f"Ставка: ${trade.bet_usd:.2f}"
                )
                await self._notify(msg)
                logger.info(
                    "Limit 0.95: %s %s mkt=%.2f limit=%.2f bet=$%.2f",
                    favorite_side,
                    market.question[:40],
                    favorite_price,
                    rounded_limit,
                    trade.bet_usd,
                )
            else:
                self._confirmed_cycles.pop(market.condition_id, None)
                self._failed_markets[market.condition_id] = (
                    time.time() + self._fail_cooldown_95
                )

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
        await self._hedge_positions(open_trades, markets)

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

        if self.reverse_signal:
            p_up = 1.0 - p_up

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
                raw = price_data
                if hasattr(raw, "price"):
                    new_price = float(raw.price)
                elif isinstance(raw, dict):
                    new_price = float(raw.get("price", 0))
                else:
                    continue
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

        now = time.time()
        for trade in open_trades:
            if trade.current_price <= 0:
                continue
            if trade.current_price >= 0.99 or trade.current_price <= 0.01:
                continue
            if now < self._failed_closes.get(trade.id or 0, 0):
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
                else:
                    self._failed_closes[trade.id or 0] = (
                        now + self._fail_cooldown
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

    async def _hedge_positions(
        self, open_trades: list[Trade], markets: list[BtcMarket]
    ) -> None:
        if not self.hedge_enabled:
            return

        market_map = {m.condition_id: m for m in markets}

        for trade in open_trades:
            if trade.id in self._hedged_trades:
                continue
            if trade.current_price <= 0 or trade.bet_usd <= 0:
                continue

            pnl_pct = trade.unrealized_pnl / trade.bet_usd
            if pnl_pct < self.hedge_trigger_pct:
                continue

            market = market_map.get(trade.market_id)
            if not market or market.time_left < 30:
                continue

            is_up = trade.outcome.lower() == "up"
            hedge_token = market.down_token_id if is_up else market.up_token_id
            hedge_price = market.down_price if is_up else market.up_price
            hedge_side = "Down" if is_up else "Up"

            hedge_amount = round(trade.unrealized_pnl * self.hedge_ratio, 2)
            if hedge_amount < 1.0:
                continue

            rounded_price = self.executor._round_to_tick(
                hedge_price, market.tick_size
            )
            if rounded_price <= 0 or rounded_price >= 1:
                continue

            from trading.scanner import MarketOpportunity

            opp = MarketOpportunity(
                market_id=market.condition_id + "_hedge",
                question=f"HEDGE: {market.question}",
                probability=rounded_price,
                outcome=hedge_side,
                token_id=hedge_token,
                liquidity=0,
                end_date=None,
                category="crypto",
                tick_size=market.tick_size,
                neg_risk=market.neg_risk,
            )

            balance = await self.executor.get_polymarket_balance()
            hedge_trade = await self.executor.execute_trade(opp, balance)
            if hedge_trade:
                self._hedged_trades.add(trade.id)  # type: ignore[arg-type]
                msg = (
                    f"🛡 *Хедж-позиция:*\n"
                    f"Основная: {trade.outcome} "
                    f"(P&L: +{pnl_pct * 100:.1f}%)\n"
                    f"Хедж: *{hedge_side}* @ ${rounded_price:.2f}\n"
                    f"Сумма хеджа: ${hedge_trade.bet_usd:.2f}"
                )
                await self._notify(msg)
                logger.info(
                    "Hedge: %s -> %s $%.2f",
                    trade.outcome,
                    hedge_side,
                    hedge_trade.bet_usd,
                )

    def _extract_price(self, level) -> float:
        """Извлечь цену из уровня order book (объект/dict/список)."""
        if hasattr(level, "price"):
            return float(level.price)
        if isinstance(level, (list, tuple)) and len(level) >= 1:
            return float(level[0])
        if isinstance(level, dict):
            return float(level.get("price", 0))
        return 0.0

    async def _refresh_market_prices_from_clob(
        self, markets: list[BtcMarket]
    ) -> None:
        """Обновить up_price/down_price из CLOB (midpoint = как в UI Polymarket)."""
        for market in markets:
            try:
                for token_id, attr in [
                    (market.up_token_id, "up_price"),
                    (market.down_token_id, "down_price"),
                ]:
                    price = 0.0
                    try:
                        mid = await asyncio.to_thread(
                            self.executor.client.get_midpoint, token_id
                        )
                        if mid:
                            p = (
                                mid.get("mid", mid.mid)
                                if isinstance(mid, dict)
                                else getattr(mid, "mid", None)
                            )
                            if p is not None:
                                price = float(p)
                    except Exception:
                        pass
                    if not (0 < price < 1):
                        try:
                            book = await asyncio.to_thread(
                                self.executor.client.get_order_book, token_id
                            )
                            asks = getattr(book, "asks", []) or (
                                book.get("asks", []) if isinstance(book, dict) else []
                            )
                            if asks:
                                prices = [self._extract_price(a) for a in asks]
                                valid = [p for p in prices if 0 < p < 1]
                                best_ask = min(valid) if valid else 0.0
                                if best_ask > 0:
                                    price = best_ask
                        except Exception:
                            pass
                    if not (0 < price < 1):
                        try:
                            lp = await asyncio.to_thread(
                                self.executor.client.get_last_trade_price, token_id
                            )
                            if lp:
                                p = lp.get("price", getattr(lp, "price", None))
                                if p is not None:
                                    price = float(p)
                        except Exception:
                            pass
                    if 0 < price < 1:
                        setattr(market, attr, price)
            except Exception:
                pass

    async def find_crypto_5m_markets(
        self, base_slugs: list[str]
    ) -> list[BtcMarket]:
        results: list[BtcMarket] = []
        now = int(time.time())
        base_ts = (now // MARKET_INTERVAL) * MARKET_INTERVAL
        timeout = aiohttp.ClientTimeout(total=10)

        from trading.scanner import _parse_float, _parse_list_field

        try:
            async with aiohttp.ClientSession() as session:

                async def _fetch_event(base_slug: str, ts: int) -> None:
                    slug = f"{base_slug}-{ts}"
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

                tasks = []
                for base_slug in base_slugs:
                    for i in range(-1, 12):
                        tasks.append(
                            _fetch_event(
                                base_slug, base_ts + i * MARKET_INTERVAL
                            )
                        )
                await asyncio.gather(*tasks)

        except Exception as e:
            logger.error("Failed to find crypto 5m markets: %s", e)

        results.sort(key=lambda m: m.time_left)
        return results

    async def find_active_markets(self) -> list[BtcMarket]:
        """Crypto 5m markets for current mode. btc_5min: BTC only; btc_eth_95_limit: BTC+ETH."""
        slugs = (
            ["btc-updown-5m", "eth-updown-5m"]
            if self.config.strategy.mode == "btc_eth_95_limit"
            else ["btc-updown-5m"]
        )
        return await self.find_crypto_5m_markets(slugs)
