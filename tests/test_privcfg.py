import io
import json
import re
from pathlib import Path

import pytest

from bitgetbot import privcfg
from bitgetbot.privcfg import EXAMPLE_PATH, PRIVATE_PATH, Settings, init_private, load_settings


def test_init_private_fills_random_api_creds_once(tmp_path, capsys):
    assert PRIVATE_PATH == Path("freqtrade/user_data/config.private.json")
    assert EXAMPLE_PATH == Path("freqtrade/user_data/config.private.example.json")
    example = tmp_path / "example.json"
    template = Path("freqtrade/user_data/config.private.example.json").read_bytes()
    example.write_bytes(template)
    private = tmp_path / "config.private.json"

    assert init_private(private, example) is True
    original = private.read_bytes()
    config = json.loads(original)
    credentials = config["api_server"]
    lengths = {name: len(value) for name, value in credentials.items()}
    assert lengths == {"username": 16, "password": 32, "jwt_secret_key": 64}
    urlsafe = all(re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in credentials.values())
    assert urlsafe
    assert config["dry_run"] is True
    assert config["exchange"] == {"key": "", "secret": "", "password": ""}
    assert example.read_bytes() == template

    another = tmp_path / "another.private.json"
    assert init_private(another, example) is True
    independent = all(
        credentials[name] != value
        for name, value in json.loads(another.read_bytes())["api_server"].items()
    )
    assert independent

    # Existing files must survive even when the example is unavailable.
    example.unlink()
    assert init_private(private, example) is False
    unchanged = private.read_bytes() == original
    assert unchanged
    captured = capsys.readouterr()
    emitted_output = bool(captured.out or captured.err)
    assert not emitted_output


def test_load_settings_merges_private_over_base(tmp_path):
    base = tmp_path / "base.json"
    private = tmp_path / "config.private.json"
    base.write_text(json.dumps({
        "dry_run": True,
        "exchange": {
            "key": "base-key", "secret": "base-secret", "password": "base-passphrase",
        },
        "api_server": {
            "username": "base-user", "password": "base-api-password",
            "listen_ip_address": "0.0.0.0", "listen_port": 9999,
        },
    }), encoding="utf-8")
    private.write_text(json.dumps({
        "dry_run": False,
        "exchange": {"key": "private-key"},
        "api_server": {"password": "private-api-password"},
    }), encoding="utf-8")
    settings = load_settings(private, base)
    assert isinstance(settings, Settings)
    assert settings.dry_run is False
    assert settings.api_url == "http://127.0.0.1:8080"
    assert settings.api_user == "base-user"
    assert settings.api_pass == "private-api-password"
    assert settings.key == "private-key"
    assert settings.secret == "base-secret"
    assert settings.password == "base-passphrase"

    # Empty overrides preserve all nested base fields and its dry-run default.
    private.write_text("{}", encoding="utf-8")
    settings = load_settings(private, base)
    assert settings.dry_run is True
    assert settings.key == "base-key"
    assert settings.api_pass == "base-api-password"


def test_settings_repr_hides_secrets():
    sensitive = {
        "api_user": "sensitive-api-username", "api_pass": "sensitive-api-password",
        "key": "sensitive-exchange-key", "secret": "sensitive-exchange-secret",
        "password": "sensitive-exchange-passphrase",
    }
    settings = Settings(dry_run=True, api_url="http://127.0.0.1:8080", **sensitive)
    rendered = repr(settings)
    for name, value in sensitive.items():
        assert value not in rendered
        assert f"{name}='***'" in rendered
        assert getattr(settings, name) == value
    assert "dry_run=True" in rendered
    assert "http://127.0.0.1:8080" in rendered
    assert str(settings) == rendered


@pytest.mark.parametrize("mode,dry_run,db_url", [
    ("dry", True, "sqlite:////freqtrade/user_data/tradesv3.dryrun.sqlite"),
    ("live", False, "sqlite:////freqtrade/user_data/tradesv3.sqlite"),
])
def test_set_mode_preserves_every_other_private_key(tmp_path, capsys, mode, dry_run, db_url):
    path = tmp_path / "config.private.json"
    original = {
        "dry_run": not dry_run, "db_url": "old",
        "exchange": {"key": "SECRET-SENTINEL", "options": {"uta": False}},
        "api_server": {"password": "SECRET-SENTINEL"},
        "custom": [1, {"keep": "한글"}],
    }
    path.write_text(json.dumps(original), encoding="utf-8")
    privcfg.set_mode(mode, path)
    updated = json.loads(path.read_text(encoding="utf-8"))
    assert updated == {**original, "dry_run": dry_run, "db_url": db_url}
    assert capsys.readouterr().out == ""


def test_set_mode_rejects_invalid_mode_without_changing_file(tmp_path):
    path = tmp_path / "config.private.json"
    path.write_text('{"dry_run":true}', encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(ValueError):
        privcfg.set_mode("invalid", path)
    assert path.read_bytes() == original


@pytest.mark.parametrize("failure", ["write", "replace"])
def test_set_mode_failure_preserves_original_and_removes_temp(tmp_path, monkeypatch, capsys, failure):
    path = tmp_path / "config.private.json"
    path.write_text('{"dry_run":true,"custom":"SECRET-SENTINEL"}\n', encoding="utf-8")
    original = path.read_bytes()
    open_file = io.open

    class BrokenWriter:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def write(self, text):
            self.stream.write(text[:8])
            self.stream.flush()
            raise OSError("simulated partial write")

    def partial_write(file, mode="r", *args, **kwargs):
        stream = open_file(file, mode, *args, **kwargs)
        return BrokenWriter(stream) if "w" in mode else stream

    def failed_replace(*_):
        raise OSError("simulated replace failure")

    if failure == "write":
        monkeypatch.setattr(io, "open", partial_write)
    else:
        monkeypatch.setattr(privcfg.os, "replace", failed_replace)
    with pytest.raises(OSError):
        privcfg.set_mode("live", path)
    assert path.read_bytes() == original
    assert set(tmp_path.iterdir()) == {path}
    assert capsys.readouterr().out == ""


def test_set_mode_replaces_from_same_directory_after_complete_write(tmp_path, monkeypatch):
    path = tmp_path / "config.private.json"
    path.write_text('{"dry_run":true,"custom":"keep"}\n', encoding="utf-8")
    original = path.read_bytes()
    replace_file = privcfg.os.replace
    replacements = []

    def checked_replace(source, destination):
        source, destination = Path(source), Path(destination)
        assert source.parent == destination.parent == path.parent
        assert path.read_bytes() == original
        assert json.loads(source.read_text(encoding="utf-8")) == {
            "dry_run": False, "custom": "keep",
            "db_url": "sqlite:////freqtrade/user_data/tradesv3.sqlite",
        }
        replacements.append((source, destination))
        return replace_file(source, destination)

    monkeypatch.setattr(privcfg.os, "replace", checked_replace)
    privcfg.set_mode("live", path)
    assert len(replacements) == 1
    assert json.loads(path.read_text(encoding="utf-8"))["dry_run"] is False
    assert set(tmp_path.iterdir()) == {path}


def test_load_settings_exposes_merged_db_url(tmp_path):
    base = tmp_path / "base.json"
    private = tmp_path / "private.json"
    base.write_text(json.dumps({
        "dry_run": True, "db_url": "sqlite:////freqtrade/user_data/tradesv3.dryrun.sqlite",
        "api_server": {"username": "u", "password": "p"},
        "exchange": {"key": "", "secret": "", "password": ""},
    }), encoding="utf-8")
    private.write_text(json.dumps({
        "dry_run": False, "db_url": "sqlite:////freqtrade/user_data/tradesv3.sqlite",
    }), encoding="utf-8")
    assert load_settings(private, base).db_url == "sqlite:////freqtrade/user_data/tradesv3.sqlite"
    private.write_text("{}", encoding="utf-8")
    assert load_settings(private, base).db_url == "sqlite:////freqtrade/user_data/tradesv3.dryrun.sqlite"
