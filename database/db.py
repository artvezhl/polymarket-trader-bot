from __future__ import annotations

from datetime import datetime

import aiosqlite

from database.models import BalanceLog, Trade, TradeStatus
from utils.logger import logger

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT,
    question TEXT,
    probability REAL,
    bet_usd REAL,
    potential_payout REAL,
    outcome TEXT,
    status TEXT DEFAULT 'open',
    created_at TIMESTAMP,
    resolved_at TIMESTAMP,
    pnl REAL DEFAULT 0,
    token_id TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS balance_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    free_usdc REAL,
    positions_value REAL,
    total_value REAL,
    timestamp TIMESTAMP
);
"""


class Database:
    def __init__(self, db_path: str = "bot.db"):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        logger.info("Database connected: %s", self._db_path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._conn

    async def insert_trade(self, trade: Trade) -> int:
        cursor = await self.conn.execute(
            """INSERT INTO trades
               (market_id, question, probability, bet_usd, potential_payout,
                outcome, status, created_at, resolved_at, pnl, token_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trade.market_id,
                trade.question,
                trade.probability,
                trade.bet_usd,
                trade.potential_payout,
                trade.outcome,
                trade.status.value,
                trade.created_at.isoformat(),
                trade.resolved_at.isoformat() if trade.resolved_at else None,
                trade.pnl,
                trade.token_id,
            ),
        )
        await self.conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def update_trade_status(
        self, trade_id: int, status: TradeStatus, pnl: float = 0.0
    ) -> None:
        await self.conn.execute(
            """UPDATE trades SET status = ?, pnl = ?, resolved_at = ?
               WHERE id = ?""",
            (status.value, pnl, datetime.now().isoformat(), trade_id),
        )
        await self.conn.commit()

    async def get_open_trades(self) -> list[Trade]:
        cursor = await self.conn.execute(
            "SELECT * FROM trades WHERE status = ?",
            (TradeStatus.OPEN.value,),
        )
        rows = await cursor.fetchall()
        return [Trade.from_row(row) for row in rows]

    async def get_trade_by_market(self, market_id: str) -> Trade | None:
        cursor = await self.conn.execute(
            "SELECT * FROM trades WHERE market_id = ? AND status = ?",
            (market_id, TradeStatus.OPEN.value),
        )
        row = await cursor.fetchone()
        return Trade.from_row(row) if row else None

    async def get_recent_trades(self, limit: int = 20) -> list[Trade]:
        cursor = await self.conn.execute(
            "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [Trade.from_row(row) for row in rows]

    async def get_pnl_since(self, since: datetime) -> float:
        cursor = await self.conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE resolved_at >= ?",
            (since.isoformat(),),
        )
        row = await cursor.fetchone()
        return float(row[0]) if row else 0.0

    async def get_total_pnl(self) -> float:
        cursor = await self.conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE status != ?",
            (TradeStatus.OPEN.value,),
        )
        row = await cursor.fetchone()
        return float(row[0]) if row else 0.0

    async def get_trades_count_today(self) -> int:
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        cursor = await self.conn.execute(
            "SELECT COUNT(*) FROM trades WHERE created_at >= ?",
            (today.isoformat(),),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def insert_balance_log(self, log: BalanceLog) -> None:
        await self.conn.execute(
            """INSERT INTO balance_log (free_usdc, positions_value, total_value, timestamp)
               VALUES (?, ?, ?, ?)""",
            (log.free_usdc, log.positions_value, log.total_value, log.timestamp.isoformat()),
        )
        await self.conn.commit()

    async def get_config(self, key: str) -> str | None:
        cursor = await self.conn.execute(
            "SELECT value FROM config WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def set_config(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            (key, value),
        )
        await self.conn.commit()
