"""Private Freqtrade settings without exposing API or exchange credentials."""

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PRIVATE_PATH = Path("freqtrade/user_data/config.private.json")
EXAMPLE_PATH = Path("freqtrade/user_data/config.private.example.json")


@dataclass(frozen=True, repr=False)
class Settings:
    dry_run: bool
    api_url: str
    api_user: str
    api_pass: str
    key: str
    secret: str
    password: str

    def __repr__(self) -> str:
        return (
            f"Settings(dry_run={self.dry_run!r}, api_url={self.api_url!r}, "
            "api_user='***', api_pass='***', key='***', secret='***', password='***')"
        )


def init_private(path: Path = PRIVATE_PATH, example: Path = EXAMPLE_PATH) -> bool:
    """Create private settings once; never overwrite an existing file or print secrets."""
    if path.exists():
        return False

    config = json.loads(example.read_text(encoding="utf-8"))
    config["api_server"].update({
        "username": secrets.token_urlsafe(12),
        "password": secrets.token_urlsafe(24),
        "jwt_secret_key": secrets.token_urlsafe(48),
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as private:
        json.dump(config, private, indent=2)
        private.write("\n")
    return True


def _merge_config(base: dict[str, Any], private: dict[str, Any]) -> dict[str, Any]:
    """Merge nested dictionaries with private values taking precedence."""
    for name, value in private.items():
        if isinstance(value, dict) and isinstance(base.get(name), dict):
            _merge_config(base[name], value)
        else:
            base[name] = value
    return base


def load_settings(
    path: Path = PRIVATE_PATH,
    base: Path = Path("freqtrade/user_data/config.json"),
) -> Settings:
    config = _merge_config(
        json.loads(base.read_text(encoding="utf-8")),
        json.loads(path.read_text(encoding="utf-8")),
    )
    api = config["api_server"]
    exchange = config["exchange"]
    return Settings(
        dry_run=config["dry_run"],
        api_url="http://127.0.0.1:8080",
        api_user=api["username"],
        api_pass=api["password"],
        key=exchange["key"],
        secret=exchange["secret"],
        password=exchange["password"],
    )
