from __future__ import annotations

import re
from typing import Any

import aiohttp

from utils.logger import logger

DATA_API_TRADES = "https://data-api.polymarket.com/trades"
ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def is_valid_wallet_address(addr: str) -> bool:
    return bool(addr and ADDR_RE.match(addr.strip()))


def normalize_wallet_address(addr: str) -> str:
    return addr.strip().lower()


def trade_timestamp(trade: dict[str, Any]) -> int:
    v = trade.get("timestamp")
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


async def fetch_recent_trades(
    session: aiohttp.ClientSession,
    user_address: str,
    *,
    limit: int = 100,
) -> tuple[list[dict[str, Any]], bool]:
    """Returns (trades, ok). ok is False on HTTP/parse failure (do not advance watch cursor)."""
    params = {
        "user": user_address,
        "limit": str(limit),
        "takerOnly": "false",
    }
    try:
        async with session.get(
            DATA_API_TRADES,
            params=params,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning(
                    "Data API /trades status=%s body=%s", resp.status, body[:200]
                )
                return [], False
            data = await resp.json()
    except Exception as e:
        logger.error("Data API /trades request failed: %s", e)
        return [], False
    trades = data if isinstance(data, list) else []
    return trades, True


def format_watch_trade_message(
    wallet_address: str,
    label: str,
    trade: dict[str, Any],
) -> str:
    side = trade.get("side", "?")
    title = trade.get("title") or trade.get("slug") or "—"
    outcome = trade.get("outcome") or ""
    size = trade.get("size")
    price = trade.get("price")
    slug = trade.get("slug") or ""
    event_slug = trade.get("eventSlug") or ""
    tx = trade.get("transactionHash") or ""
    pseudo = trade.get("pseudonym") or trade.get("name") or ""

    who = f"`{wallet_address[:10]}…`"
    if label:
        who = f"*{label}* ({who})"
    if pseudo:
        who = f"{who} — _{pseudo}_"

    path = event_slug or slug
    url = (
        f"https://polymarket.com/event/{path}"
        if path
        else "https://polymarket.com"
    )
    lines = [
        "👁 *Сделка отслеживаемого кошелька*",
        f"Кошелёк: {who}",
        f"Сторона: *{side}*",
        f"Рынок: _{title[:200]}_",
    ]
    if outcome:
        lines.append(f"Исход: `{outcome}`")
    if size is not None and price is not None:
        lines.append(f"Размер: `{size}` @ цена `{price}`")
    if tx:
        lines.append(f"[Polygonscan tx](https://polygonscan.com/tx/{tx})")
    lines.append(f"[Polymarket]({url})")
    return "\n".join(lines)
