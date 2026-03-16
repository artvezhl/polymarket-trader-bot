from __future__ import annotations

import pytest
import pytest_asyncio

from database.db import Database
from utils.config import AppConfig, ReportingConfig, SecretsConfig, TelegramConfig, TradingConfig


@pytest.fixture
def app_config(tmp_path) -> AppConfig:
    return AppConfig(
        trading=TradingConfig(
            max_probability=0.05,
            bet_size_pct=0.01,
            min_bet_usd=1.0,
            max_bet_usd=10.0,
            min_liquidity=5000,
            max_open_positions=50,
            scan_interval_sec=60,
            skip_keywords=["sports"],
        ),
        reporting=ReportingConfig(status_interval_min=60),
        telegram=TelegramConfig(admin_ids=[123456789], bot_token="test_token"),
        secrets=SecretsConfig(),
    )


@pytest_asyncio.fixture
async def db(tmp_path) -> Database:
    db_path = str(tmp_path / "test.db")
    database = Database(db_path)
    await database.connect()
    yield database
    await database.close()
