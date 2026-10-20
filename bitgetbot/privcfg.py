"""Private Freqtrade settings without exposing API or exchange credentials."""

import json
import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PRIVATE_PATH = Path("freqtrade/user_data/config.private.json")
EXAMPLE_PATH = Path("freqtrade/user_data/config.private.example.json")
DRY_DB_URL = "sqlite:////freqtrade/user_data/tradesv3.dryrun.sqlite"
LIVE_DB_URL = "sqlite:////freqtrade/user_data/tradesv3.sqlite"


@dataclass(frozen=True, repr=False)
class Settings:
    dry_run: bool
    api_url: str
    api_user: str
    api_pass: str
    key: str
    secret: str
    password: str
    db_url: str = DRY_DB_URL

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


def set_mode(mode: str, path: Path = PRIVATE_PATH) -> None:
    """Change only mode and its database; never expose private settings."""
    if mode not in {"dry", "live"}:
        raise ValueError("모드는 dry 또는 live여야 합니다")
    config = json.loads(path.read_text(encoding="utf-8"))
    config["dry_run"] = mode == "dry"
    config["db_url"] = DRY_DB_URL if mode == "dry" else LIVE_DB_URL
    _write_private(config, path)


def _write_private(config: dict[str, Any], path: Path) -> None:
    """Replace private settings only after a complete same-directory write."""
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(config, temporary, indent=2, ensure_ascii=False)
            temporary.write("\n")
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _valid_exchange_key(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and not any(char.isspace() for char in value)


def set_exchange_keys(
    key: str, secret: str, password: str, path: Path = PRIVATE_PATH,
) -> None:
    """Atomically change only exchange credentials, without printing values."""
    values = {"key": key, "secret": secret, "password": password}
    if not all(_valid_exchange_key(value) for value in values.values()):
        raise ValueError("거래소 키는 비어 있거나 공백을 포함할 수 없습니다")
    config = json.loads(path.read_text(encoding="utf-8"))
    config.setdefault("exchange", {}).update(values)
    _write_private(config, path)


def keys_configured(path: Path = PRIVATE_PATH) -> dict[str, bool]:
    """Expose credential presence only; unreadable settings count as unconfigured."""
    exchange = {}
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(config, dict) and isinstance(config.get("exchange"), dict):
            exchange = config["exchange"]
    except (OSError, ValueError, UnicodeError):
        pass
    return {name: _valid_exchange_key(exchange.get(name)) for name in ("key", "secret", "password")}


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
        db_url=config.get("db_url", DRY_DB_URL),
        api_url="http://127.0.0.1:8080",
        api_user=api["username"],
        api_pass=api["password"],
        key=exchange["key"],
        secret=exchange["secret"],
        password=exchange["password"],
    )
