from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from trading.clob_account import (
    EPS_SHARES,
    ClobTradesFetch,
    fetch_all_clob_trades,
    net_shares_by_token_id,
    outcome_label_for_token,
    token_to_condition_map,
)
from trading.executor import TradeExecutor
from utils.logger import logger


@dataclass
class ClobPositionsSnapshot:
    """Позиции + ошибка загрузки CLOB (например неверный API secret)."""

    positions: list[ClobOpenPosition]
    clob_error: str | None = None


@dataclass
class ClobOpenPosition:
    token_id: str
    condition_id: str
    shares: float
    outcome: str
    question: str
    current_price: float
    market_closed: bool
    notional_usd: float


def _parse_last_price(raw: Any) -> float:
    if raw is None:
        return 0.0
    if hasattr(raw, "price"):
        try:
            return float(raw.price)
        except (TypeError, ValueError):
            pass
    if isinstance(raw, dict):
        try:
            return float(raw.get("price", 0))
        except (TypeError, ValueError):
            return 0.0
    return 0.0


async def fetch_clob_open_positions(
    executor: TradeExecutor,
) -> ClobPositionsSnapshot:
    """Все ненулевые нетто-позиции по истории CLOB (в т.ч. не из БД бота)."""
    client = executor.client
    fetched: ClobTradesFetch = await fetch_all_clob_trades(client)
    if fetched.error:
        return ClobPositionsSnapshot(positions=[], clob_error=fetched.error)
    raw = fetched.trades

    net = net_shares_by_token_id(raw)
    t2m = token_to_condition_map(raw)
    rows: list[ClobOpenPosition] = []

    for token_id, shares in sorted(
        net.items(), key=lambda x: (-abs(x[1]), x[0])
    ):
        if abs(shares) <= EPS_SHARES:
            continue
        cond = t2m.get(token_id, "")
        outcome = outcome_label_for_token(raw, token_id)
        question = cond[:20] + "…" if len(cond) > 20 else (cond or "—")
        market_closed = False
        market_data: dict[str, Any] | None = None

        if cond:
            try:
                market_data = await asyncio.to_thread(
                    client.get_market, cond
                )
            except Exception as e:
                logger.debug("get_market %s: %s", cond[:16], e)
            if market_data:
                question = (
                    str(market_data.get("question") or market_data.get("description") or question)
                )[:500]
                market_closed = bool(market_data.get("closed"))
                if not outcome and market_data.get("tokens"):
                    for tok in market_data["tokens"]:
                        tid = str(tok.get("token_id") or tok.get("tokenId") or "")
                        if tid == token_id:
                            outcome = str(tok.get("outcome") or "")
                            break

        cur = 0.0
        try:
            lp = await asyncio.to_thread(
                client.get_last_trade_price, token_id
            )
            cur = _parse_last_price(lp)
        except Exception:
            pass

        notional = abs(shares) * cur if cur > 0 else 0.0
        rows.append(
            ClobOpenPosition(
                token_id=token_id,
                condition_id=cond,
                shares=shares,
                outcome=outcome or "?",
                question=question,
                current_price=cur,
                market_closed=market_closed,
                notional_usd=notional,
            )
        )

    return ClobPositionsSnapshot(positions=rows, clob_error=None)
