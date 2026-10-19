import json
import shlex
import subprocess
from pathlib import Path

import yaml


USER_DATA = Path("freqtrade/user_data")


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_safety_invariants():
    config = load_json(USER_DATA / "config.json")
    assert config["trading_mode"] == "futures"
    assert config["margin_mode"] == "isolated"
    assert config["initial_state"] == "stopped"
    assert config["dry_run"] is True
    assert config["db_url"] == "sqlite:////freqtrade/user_data/tradesv3.dryrun.sqlite"
    assert config["dry_run_wallet"] == 100
    assert config["max_open_trades"] == 1
    assert config["stake_currency"] == "USDT"
    assert config["stake_amount"] == 7
    assert config["tradable_balance_ratio"] == 0.99
    assert config["force_entry_enable"] is True
    assert config["timeframe"] == "1h"
    assert config["telegram"] == {"enabled": False, "token": "", "chat_id": ""}
    assert config["order_types"] == {
        "entry": "limit", "exit": "limit", "emergency_exit": "market",
        "force_entry": "market", "force_exit": "market", "stoploss": "market",
        "stoploss_on_exchange": True, "stoploss_on_exchange_interval": 60,
        "stoploss_price_type": "mark",
    }
    for pricing in ("entry_pricing", "exit_pricing"):
        assert config[pricing] == {
            "price_side": "same", "use_order_book": True, "order_book_top": 1,
        }
    exchange = config["exchange"]
    assert exchange["name"] == "bitget"
    assert exchange["ccxt_config"]["options"]["uta"] is False
    assert exchange["ccxt_async_config"]["options"]["uta"] is False
    assert exchange["pair_whitelist"] == ["BTC/USDT:USDT"]
    assert exchange["pair_blacklist"] == []
    assert config["pairlists"] == [{"method": "StaticPairList"}]
    api = config["api_server"]
    assert api["enabled"] is True
    assert api["listen_ip_address"] == "0.0.0.0"
    assert api["listen_port"] == 8080
    assert api["verbosity"] == "error"
    assert api["enable_openapi"] is False
    assert api["CORS_origins"] == []
    assert config["bot_name"] == "bitget-ai-bot"
    assert config["internals"] == {"process_throttle_secs": 5}


def test_committed_config_has_no_secrets():
    config = load_json(USER_DATA / "config.json")
    exchange = config["exchange"]
    assert exchange["key"] == exchange["secret"] == exchange["password"] == ""
    api = config["api_server"]
    assert api["username"] == api["password"] == api["jwt_secret_key"] == ""


def test_compose_publishes_only_localhost_and_pins_digest():
    compose = yaml.safe_load(Path("freqtrade/docker-compose.yml").read_text(encoding="utf-8"))
    assert set(compose["services"]) == {"freqtrade"}
    service = compose["services"]["freqtrade"]
    assert service["ports"] == ["127.0.0.1:8080:8080"]
    assert service["image"] == (
        "freqtradeorg/freqtrade:2026.9@sha256:"
        "989aff5636d8f0213ed44fe334c3314a194a51c0c755d16866960247aafd2eae"
    )
    assert service["restart"] == "unless-stopped"
    assert service["volumes"] == ["./user_data:/freqtrade/user_data"]
    assert shlex.split(service["command"]) == [
        "trade", "--logfile", "/freqtrade/user_data/logs/freqtrade.log",
        "--config", "/freqtrade/user_data/config.json",
        "--config", "/freqtrade/user_data/config.private.json",
        "--strategy", "SmokeTest",
    ]


def test_private_example_defaults_to_dry_run():
    assert load_json(USER_DATA / "config.private.example.json") == {
        "dry_run": True,
        "db_url": "sqlite:////freqtrade/user_data/tradesv3.dryrun.sqlite",
        "exchange": {"key": "", "secret": "", "password": ""},
        "api_server": {"username": "", "password": "", "jwt_secret_key": ""},
    }


def test_private_file_is_gitignored():
    result = subprocess.run(
        ["git", "check-ignore", "freqtrade/user_data/config.private.json"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
