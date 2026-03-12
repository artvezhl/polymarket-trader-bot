from __future__ import annotations

import yaml

from utils.config import AppConfig, TradingConfig, load_config


class TestLoadConfig:
    def test_load_defaults_when_no_files(self, tmp_path):
        config = load_config(
            config_path=str(tmp_path / "nonexistent.yaml"),
            env_path=str(tmp_path / "nonexistent.env"),
        )
        assert isinstance(config, AppConfig)
        assert config.trading.max_probability == 0.05
        assert config.trading.bet_size_pct == 0.01
        assert config.trading.min_bet_usd == 1.0
        assert config.trading.max_bet_usd == 10.0

    def test_load_from_yaml(self, tmp_path):
        yaml_file = tmp_path / "config.yaml"
        yaml_data = {
            "trading": {
                "max_probability": 0.03,
                "bet_size_pct": 0.02,
                "min_liquidity": 10000,
                "skip_categories": ["Sports", "Entertainment"],
            },
            "reporting": {"status_interval_min": 30},
            "telegram": {"admin_ids": [111, 222]},
        }
        with open(yaml_file, "w") as f:
            yaml.dump(yaml_data, f)

        config = load_config(
            config_path=str(yaml_file),
            env_path=str(tmp_path / "nonexistent.env"),
        )
        assert config.trading.max_probability == 0.03
        assert config.trading.bet_size_pct == 0.02
        assert config.trading.min_liquidity == 10000
        assert config.trading.skip_categories == ["Sports", "Entertainment"]
        assert config.reporting.status_interval_min == 30
        assert config.telegram.admin_ids == [111, 222]

    def test_load_secrets_from_env(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "TELEGRAM_BOT_TOKEN=test_bot_token\n"
            "PRIVATE_KEY=0xdeadbeef\n"
            "POLYMARKET_API_KEY=ak_test\n"
        )

        config = load_config(
            config_path=str(tmp_path / "nonexistent.yaml"),
            env_path=str(env_file),
        )
        assert config.telegram.bot_token == "test_bot_token"
        assert config.secrets.private_key == "0xdeadbeef"
        assert config.secrets.polymarket_api_key == "ak_test"


class TestTradingConfig:
    def test_defaults(self):
        cfg = TradingConfig()
        assert cfg.max_probability == 0.05
        assert cfg.max_open_positions == 50
        assert cfg.skip_categories == []
