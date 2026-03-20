from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from py_clob_client.client import ClobClient

from utils.logger import logger

# Минимальная доля, ниже которой считаем позицию нулевой (плавающая арифметика + пыль)
EPS_SHARES = 1e-8


def _asset_id(t: dict[str, Any]) -> str:
    return str(
        t.get("asset_id")
        or t.get("assetId")
        or t.get("token_id")
        or t.get("tokenId")
        or ""
    ).strip()


def _market_condition_id(t: dict[str, Any]) -> str:
    """CLOB trade: market = condition id (0x…)."""
    return str(
        t.get("market")
        or t.get("condition_id")
        or t.get("conditionId")
        or ""
    ).strip()


def _trade_size(t: dict[str, Any]) -> float:
    raw = t.get("size") or t.get("matched_amount") or t.get("matchedAmount") or 0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _trade_side(t: dict[str, Any]) -> str:
    return str(t.get("side") or "").upper()


def net_shares_by_token_id(trades: list[dict[str, Any]]) -> dict[str, float]:
    """Агрегация нетто-позиции по token_id из истории CLOB (BUY +, SELL −)."""
    net: dict[str, float] = {}
    for t in trades:
        aid = _asset_id(t)
        if not aid:
            continue
        sz = _trade_size(t)
        if sz <= 0:
            continue
        side = _trade_side(t)
        if side == "BUY":
            net[aid] = net.get(aid, 0.0) + sz
        elif side == "SELL":
            net[aid] = net.get(aid, 0.0) - sz
    return net


def token_to_condition_map(trades: list[dict[str, Any]]) -> dict[str, str]:
    """Последнее известное соответствие token_id → condition_id (market)."""
    m: dict[str, str] = {}
    for t in trades:
        aid = _asset_id(t)
        mid = _market_condition_id(t)
        if aid and mid:
            m[aid] = mid
    return m


@dataclass
class ClobTradesFetch:
    """Результат загрузки истории CLOB; при ошибке подписи/API trades пустой."""

    trades: list[dict[str, Any]]
    error: str | None = None


async def fetch_all_clob_trades(client: ClobClient) -> ClobTradesFetch:
    """Полная история сделок с пагинацией (источник истины Polymarket CLOB)."""
    try:
        out = await asyncio.to_thread(client.get_trades)
        data = out if isinstance(out, list) else []
        return ClobTradesFetch(trades=data, error=None)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        logger.error("fetch_all_clob_trades failed: %s", err)
        return ClobTradesFetch(trades=[], error=err[:500])


def outcome_label_for_token(
    trades: list[dict[str, Any]], token_id: str
) -> str:
    """Берём outcome из последней сделки по этому asset_id."""
    for t in reversed(trades):
        if _asset_id(t) != token_id:
            continue
        o = t.get("outcome")
        if o is not None:
            return str(o).strip()
    return ""


def winning_token_id_from_market(market_data: dict[str, Any]) -> str | None:
    """Для закрытого рынка — token_id исхода с ценой ~1."""
    if not market_data.get("closed"):
        return None
    best: tuple[float, str] = (-1.0, "")
    for tok in market_data.get("tokens") or []:
        tid = str(tok.get("token_id") or tok.get("tokenId") or "")
        if not tid:
            continue
        try:
            p = float(tok.get("price", 0))
        except (TypeError, ValueError):
            p = 0.0
        if p > best[0]:
            best = (p, tid)
    if best[0] >= 0.95:
        return best[1]
    return None
