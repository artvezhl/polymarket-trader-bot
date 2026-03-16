from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv


@dataclass
class TradingConfig:
    max_probability: float = 0.05
    bet_size_pct: float = 0.01
    min_bet_usd: float = 1.0
    max_bet_usd: float = 10.0
    min_liquidity: float = 5000.0
    max_open_positions: int = 50
    scan_interval_sec: int = 60
    skip_keywords: list[str] = field(default_factory=list)
    min_end_date_days: int = 1
    price_check_interval_sec: int = 120
    price_spike_multiplier: float = 10.0


@dataclass
class ReportingConfig:
    status_interval_min: int = 60
    positions_report_interval_hours: int = 4


@dataclass
class TelegramConfig:
    admin_ids: list[int] = field(default_factory=list)
    bot_token: str = ""


@dataclass
class SecretsConfig:
    private_key: str = ""
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_api_passphrase: str = ""
    polygon_rpc_url: str = "https://polygon-rpc.com"
    signature_type: int = 0


@dataclass
class AppConfig:
    trading: TradingConfig = field(default_factory=TradingConfig)
    reporting: ReportingConfig = field(default_factory=ReportingConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    secrets: SecretsConfig = field(default_factory=SecretsConfig)


def load_config(config_path: str = "config.yaml", env_path: str = ".env") -> AppConfig:
    load_dotenv(env_path)

    yaml_data: dict = {}
    config_file = Path(config_path)
    if config_file.exists():
        with open(config_file, encoding="utf-8") as f:
            yaml_data = yaml.safe_load(f) or {}

    trading_data = yaml_data.get("trading", {})
    reporting_data = yaml_data.get("reporting", {})
    telegram_data = yaml_data.get("telegram", {})

    trading_kwargs = {
        k: v for k, v in trading_data.items()
        if k in TradingConfig.__dataclass_fields__
    }
    trading = TradingConfig(**trading_kwargs)
    reporting_kwargs = {
        k: v for k, v in reporting_data.items()
        if k in ReportingConfig.__dataclass_fields__
    }
    reporting = ReportingConfig(**reporting_kwargs)
    telegram = TelegramConfig(
        admin_ids=telegram_data.get("admin_ids", []),
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
    )
    secrets = SecretsConfig(
        private_key=os.getenv("PRIVATE_KEY", ""),
        polymarket_api_key=os.getenv("POLYMARKET_API_KEY", ""),
        polymarket_api_secret=os.getenv("POLYMARKET_API_SECRET", ""),
        polymarket_api_passphrase=os.getenv("POLYMARKET_API_PASSPHRASE", ""),
        polygon_rpc_url=os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com"),
        signature_type=int(os.getenv("POLYMARKET_SIG_TYPE", "0")),
    )

    return AppConfig(
        trading=trading,
        reporting=reporting,
        telegram=telegram,
        secrets=secrets,
    )


TRADING_CONFIG_KEYS = {
    "max_probability": float,
    "bet_size_pct": float,
    "min_bet_usd": float,
    "max_bet_usd": float,
    "min_liquidity": float,
    "max_open_positions": int,
    "scan_interval_sec": int,
    "min_end_date_days": int,
    "price_check_interval_sec": int,
    "price_spike_multiplier": float,
}

TRADING_LIST_KEYS = {"skip_keywords"}

REPORTING_CONFIG_KEYS = {
    "positions_report_interval_hours": int,
    "status_interval_min": int,
}


def apply_db_overrides(config: AppConfig, db_values: dict[str, str]) -> None:
    """Apply config overrides stored in the database."""
    import json as _json

    for key, cast in TRADING_CONFIG_KEYS.items():
        db_key = f"trading.{key}"
        if db_key in db_values:
            try:
                setattr(config.trading, key, cast(db_values[db_key]))
            except (ValueError, TypeError):
                pass

    for key in TRADING_LIST_KEYS:
        db_key = f"trading.{key}"
        if db_key in db_values:
            try:
                setattr(config.trading, key, _json.loads(db_values[db_key]))
            except (ValueError, TypeError):
                pass

    for key, cast in REPORTING_CONFIG_KEYS.items():
        db_key = f"reporting.{key}"
        if db_key in db_values:
            try:
                setattr(config.reporting, key, cast(db_values[db_key]))
            except (ValueError, TypeError):
                pass
