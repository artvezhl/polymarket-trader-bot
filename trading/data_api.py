"""Polymarket Data API — позиции, сделки и т.д."""

from __future__ import annotations

import asyncio
import json
import urllib.parse
import urllib.request


def fetch_positions(addr: str, size_threshold: float = 0) -> list[dict]:
    """Позиции кошелька из Data API (user = proxy или EOA)."""
    params = urllib.parse.urlencode({
        "user": addr,
        "sizeThreshold": size_threshold,
    })
    url = f"https://data-api.polymarket.com/positions?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "PolymarketBot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


async def fetch_positions_async(addr: str, size_threshold: float = 0) -> list[dict]:
    """Async обёртка для fetch_positions."""
    return await asyncio.to_thread(fetch_positions, addr, size_threshold)
