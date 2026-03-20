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
    token_id TEXT DEFAULT '',
    current_price REAL DEFAULT 0,
    price_alert_sent INTEGER DEFAULT 0,
    order_id TEXT DEFAULT '',
    fill_price REAL DEFAULT 0,
    fee_usd REAL DEFAULT 0,
    redeemed INTEGER DEFAULT 0,
    redeem_tx_hash TEXT DEFAULT '',
    redeem_attempts INTEGER DEFAULT 0,
    last_redeem_at TIMESTAMP,
    redeem_last_error TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS balance_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    free_usdc REAL,
    positions_value REAL,
    total_value REAL,
    timestamp TIMESTAMP
);

CREATE TABLE IF NOT EXISTS watched_wallets (
    address TEXT PRIMARY KEY,
    label TEXT DEFAULT '',
    created_at TIMESTAMP,
    watch_initialized INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS watch_notified_trades (
    wallet_address TEXT NOT NULL,
    transaction_hash TEXT NOT NULL,
    asset TEXT NOT NULL,
    notified_at TIMESTAMP,
    PRIMARY KEY (wallet_address, transaction_hash, asset)
);

CREATE TABLE IF NOT EXISTS clob_redeem_log (
    condition_id TEXT PRIMARY KEY,
    redeemed INTEGER DEFAULT 0,
    redeem_tx_hash TEXT DEFAULT '',
    redeem_attempts INTEGER DEFAULT 0,
    last_redeem_at TIMESTAMP,
    redeem_last_error TEXT DEFAULT ''
);
"""

MIGRATIONS = [
    "ALTER TABLE trades ADD COLUMN current_price REAL DEFAULT 0",
    "ALTER TABLE trades ADD COLUMN price_alert_sent INTEGER DEFAULT 0",
    "ALTER TABLE trades ADD COLUMN order_id TEXT DEFAULT ''",
    "ALTER TABLE trades ADD COLUMN fill_price REAL DEFAULT 0",
    "ALTER TABLE trades ADD COLUMN fee_usd REAL DEFAULT 0",
    "ALTER TABLE trades ADD COLUMN redeemed INTEGER DEFAULT 0",
    "ALTER TABLE trades ADD COLUMN redeem_tx_hash TEXT DEFAULT ''",
    "ALTER TABLE trades ADD COLUMN redeem_attempts INTEGER DEFAULT 0",
    "ALTER TABLE trades ADD COLUMN last_redeem_at TIMESTAMP",
    "ALTER TABLE trades ADD COLUMN redeem_last_error TEXT DEFAULT ''",
    "ALTER TABLE watched_wallets ADD COLUMN watch_initialized INTEGER DEFAULT 0",
    """CREATE TABLE IF NOT EXISTS clob_redeem_log (
        condition_id TEXT PRIMARY KEY,
        redeemed INTEGER DEFAULT 0,
        redeem_tx_hash TEXT DEFAULT '',
        redeem_attempts INTEGER DEFAULT 0,
        last_redeem_at TIMESTAMP,
        redeem_last_error TEXT DEFAULT ''
    )""",
]


class Database:
    def __init__(self, db_path: str = "data/bot.db"):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.executescript(SCHEMA)
        for sql in MIGRATIONS:
            try:
                await self._conn.execute(sql)
            except Exception:
                pass
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
                outcome, status, created_at, resolved_at, pnl, token_id,
                current_price, price_alert_sent,
                order_id, fill_price, fee_usd,
                redeemed, redeem_tx_hash, redeem_attempts, last_redeem_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                trade.current_price,
                int(trade.price_alert_sent),
                trade.order_id,
                trade.fill_price,
                trade.fee_usd,
                int(trade.redeemed),
                trade.redeem_tx_hash,
                trade.redeem_attempts,
                trade.last_redeem_at.isoformat() if trade.last_redeem_at else None,
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
            """SELECT COALESCE(SUM(pnl), 0) FROM trades
               WHERE status != ? AND
               (resolved_at >= ? OR created_at >= ?)""",
            (TradeStatus.OPEN.value, since.isoformat(), since.isoformat()),
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

    async def update_trade_price(
        self, trade_id: int, current_price: float
    ) -> None:
        await self.conn.execute(
            "UPDATE trades SET current_price = ? WHERE id = ?",
            (current_price, trade_id),
        )
        await self.conn.commit()

    async def mark_price_alert_sent(self, trade_id: int) -> None:
        await self.conn.execute(
            "UPDATE trades SET price_alert_sent = 1 WHERE id = ?",
            (trade_id,),
        )
        await self.conn.commit()

    async def close_trade(
        self, trade_id: int, pnl: float, status: TradeStatus
    ) -> None:
        await self.conn.execute(
            """UPDATE trades SET status = ?, pnl = ?, resolved_at = ?
               WHERE id = ?""",
            (status.value, pnl, datetime.now().isoformat(), trade_id),
        )
        await self.conn.commit()

    async def get_open_trades_by_price(self) -> list[Trade]:
        cursor = await self.conn.execute(
            """SELECT * FROM trades WHERE status = ?
               ORDER BY current_price DESC""",
            (TradeStatus.OPEN.value,),
        )
        rows = await cursor.fetchall()
        return [Trade.from_row(row) for row in rows]

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

    async def update_trade_fill(
        self, trade_id: int, order_id: str, fill_price: float, fee_usd: float
    ) -> None:
        await self.conn.execute(
            """UPDATE trades SET order_id = ?, fill_price = ?, fee_usd = ?
               WHERE id = ?""",
            (order_id, fill_price, fee_usd, trade_id),
        )
        await self.conn.commit()

    async def get_total_fees(self) -> float:
        cursor = await self.conn.execute(
            "SELECT COALESCE(SUM(fee_usd), 0) FROM trades"
        )
        row = await cursor.fetchone()
        return float(row[0]) if row else 0.0

    async def get_fees_since(self, since: datetime) -> float:
        cursor = await self.conn.execute(
            "SELECT COALESCE(SUM(fee_usd), 0) FROM trades WHERE created_at >= ?",
            (since.isoformat(),),
        )
        row = await cursor.fetchone()
        return float(row[0]) if row else 0.0

    async def get_all_config(self) -> dict[str, str]:
        cursor = await self.conn.execute("SELECT key, value FROM config")
        rows = await cursor.fetchall()
        return {row[0]: row[1] for row in rows}

    async def get_unredeemed_won_trades(self, limit: int = 100) -> list[Trade]:
        cursor = await self.conn.execute(
            """SELECT * FROM trades
               WHERE status = ? AND COALESCE(redeemed, 0) = 0
               ORDER BY resolved_at DESC LIMIT ?""",
            (TradeStatus.WON.value, limit),
        )
        rows = await cursor.fetchall()
        return [Trade.from_row(row) for row in rows]

    async def mark_redeem_result(
        self,
        trade_id: int,
        tx_hash: str,
        success: bool,
        last_error: str | None = None,
    ) -> None:
        now = datetime.now().isoformat()
        err = ((last_error or "")[:2000]) if not success else ""
        if success:
            await self.conn.execute(
                """
                UPDATE trades SET
                    redeemed = 1,
                    redeem_tx_hash = ?,
                    redeem_attempts = COALESCE(redeem_attempts, 0) + 1,
                    last_redeem_at = ?,
                    redeem_last_error = ''
                WHERE id = ?
                """,
                (tx_hash, now, trade_id),
            )
        else:
            await self.conn.execute(
                """
                UPDATE trades SET
                    redeem_attempts = COALESCE(redeem_attempts, 0) + 1,
                    last_redeem_at = ?,
                    redeem_last_error = ?
                WHERE id = ?
                """,
                (now, err, trade_id),
            )
        await self.conn.commit()

    async def add_watched_wallet(self, address: str, label: str = "") -> None:
        addr = address.lower()
        await self.conn.execute(
            """INSERT INTO watched_wallets (address, label, created_at)
               VALUES (?, ?, ?)
               ON CONFLICT(address) DO UPDATE SET label = excluded.label""",
            (addr, label.strip(), datetime.now().isoformat()),
        )
        await self.conn.commit()

    async def remove_watched_wallet(self, address: str) -> bool:
        addr = address.lower()
        cur = await self.conn.execute(
            "DELETE FROM watched_wallets WHERE address = ?", (addr,)
        )
        await self.conn.commit()
        return cur.rowcount > 0  # type: ignore[no-any-return]

    async def list_watched_wallets(
        self,
    ) -> list[tuple[str, str, bool]]:
        cursor = await self.conn.execute(
            """SELECT address, label, COALESCE(watch_initialized, 0)
               FROM watched_wallets ORDER BY created_at ASC"""
        )
        rows = await cursor.fetchall()
        return [
            (str(r[0]), str(r[1] or ""), bool(r[2])) for r in rows
        ]

    async def mark_watch_initialized(self, address: str) -> None:
        addr = address.lower()
        await self.conn.execute(
            "UPDATE watched_wallets SET watch_initialized = 1 WHERE address = ?",
            (addr,),
        )
        await self.conn.commit()

    async def try_insert_watch_notification(
        self, wallet_address: str, transaction_hash: str, asset: str
    ) -> bool:
        """Return True if this trade was not notified before (row inserted)."""
        w = wallet_address.lower()
        cur = await self.conn.execute(
            """INSERT OR IGNORE INTO watch_notified_trades
               (wallet_address, transaction_hash, asset, notified_at)
               VALUES (?, ?, ?, ?)""",
            (w, transaction_hash, asset or "", datetime.now().isoformat()),
        )
        await self.conn.commit()
        return cur.rowcount > 0  # type: ignore[no-any-return]

    async def clob_redeem_already_done(self, condition_id: str) -> bool:
        cursor = await self.conn.execute(
            """SELECT COALESCE(redeemed, 0) FROM clob_redeem_log
               WHERE condition_id = ?""",
            (condition_id,),
        )
        row = await cursor.fetchone()
        return bool(row and row[0])

    async def sync_trades_redeem_from_clob_log(self, condition_id: str) -> None:
        """Выставить redeemed у WON-сделок, если по CLOB уже зачислили."""
        cursor = await self.conn.execute(
            """SELECT redeem_tx_hash FROM clob_redeem_log
               WHERE condition_id = ? AND COALESCE(redeemed, 0) = 1""",
            (condition_id,),
        )
        row = await cursor.fetchone()
        if not row or not row[0]:
            return
        txh = str(row[0])
        now = datetime.now().isoformat()
        await self.conn.execute(
            """
            UPDATE trades SET
                redeemed = 1,
                redeem_tx_hash = ?,
                last_redeem_at = ?
            WHERE market_id = ? AND status = ? AND COALESCE(redeemed, 0) = 0
            """,
            (txh, now, condition_id, TradeStatus.WON.value),
        )
        await self.conn.commit()

    async def mark_clob_redeem_result(
        self,
        condition_id: str,
        tx_hash: str,
        success: bool,
        last_error: str | None = None,
    ) -> None:
        now = datetime.now().isoformat()
        err = ((last_error or "")[:2000]) if not success else ""
        cur = await self.conn.execute(
            "SELECT redeem_attempts FROM clob_redeem_log WHERE condition_id = ?",
            (condition_id,),
        )
        row = await cur.fetchone()
        attempts = (int(row[0]) if row and row[0] is not None else 0) + 1
        if success:
            await self.conn.execute(
                """
                INSERT INTO clob_redeem_log
                    (condition_id, redeemed, redeem_tx_hash, redeem_attempts,
                     last_redeem_at, redeem_last_error)
                VALUES (?, 1, ?, ?, ?, '')
                ON CONFLICT(condition_id) DO UPDATE SET
                    redeemed = 1,
                    redeem_tx_hash = excluded.redeem_tx_hash,
                    redeem_attempts = excluded.redeem_attempts,
                    last_redeem_at = excluded.last_redeem_at,
                    redeem_last_error = ''
                """,
                (condition_id, tx_hash, attempts, now),
            )
        else:
            await self.conn.execute(
                """
                INSERT INTO clob_redeem_log
                    (condition_id, redeemed, redeem_tx_hash, redeem_attempts,
                     last_redeem_at, redeem_last_error)
                VALUES (?, 0, '', ?, ?, ?)
                ON CONFLICT(condition_id) DO UPDATE SET
                    redeem_attempts = excluded.redeem_attempts,
                    last_redeem_at = excluded.last_redeem_at,
                    redeem_last_error = excluded.redeem_last_error
                """,
                (condition_id, attempts, now, err),
            )
        await self.conn.commit()

    async def mark_trades_redeemed_by_condition(
        self, condition_id: str, tx_hash: str
    ) -> None:
        """Пометить все WON-сделки с этим market_id (condition) как redeemed."""
        now = datetime.now().isoformat()
        await self.conn.execute(
            """
            UPDATE trades SET
                redeemed = 1,
                redeem_tx_hash = ?,
                redeem_attempts = COALESCE(redeem_attempts, 0) + 1,
                last_redeem_at = ?,
                redeem_last_error = ''
            WHERE market_id = ? AND status = ?
            """,
            (tx_hash, now, condition_id, TradeStatus.WON.value),
        )
        await self.conn.commit()
