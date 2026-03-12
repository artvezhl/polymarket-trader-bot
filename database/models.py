from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class TradeStatus(str, Enum):
    OPEN = "open"
    WON = "won"
    LOST = "lost"
    RESOLVED = "resolved"


@dataclass
class Trade:
    id: int | None
    market_id: str
    question: str
    probability: float
    bet_usd: float
    potential_payout: float
    outcome: str
    status: TradeStatus
    created_at: datetime
    resolved_at: datetime | None = None
    pnl: float = 0.0
    token_id: str = ""

    @classmethod
    def from_row(cls, row: tuple) -> Trade:
        return cls(
            id=row[0],
            market_id=row[1],
            question=row[2],
            probability=row[3],
            bet_usd=row[4],
            potential_payout=row[5],
            outcome=row[6],
            status=TradeStatus(row[7]),
            created_at=datetime.fromisoformat(row[8]) if row[8] else datetime.now(),
            resolved_at=datetime.fromisoformat(row[9]) if row[9] else None,
            pnl=row[10] or 0.0,
            token_id=row[11] or "",
        )


@dataclass
class BalanceLog:
    id: int | None
    free_usdc: float
    positions_value: float
    total_value: float
    timestamp: datetime

    @classmethod
    def from_row(cls, row: tuple) -> BalanceLog:
        return cls(
            id=row[0],
            free_usdc=row[1],
            positions_value=row[2],
            total_value=row[3],
            timestamp=datetime.fromisoformat(row[4]) if row[4] else datetime.now(),
        )
