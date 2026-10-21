"""User take-profit store shared with the strategy (freqtrade/user_data/manual_targets.json)."""

import importlib.util
import json
import os
from pathlib import Path

import pytest

from bitgetbot.panel import targets

RULES_PATH = Path(__file__).resolve().parents[2] / "freqtrade" / "user_data" / "strategies" / "live_rules.py"


def strategy_rules():
    spec = importlib.util.spec_from_file_location("live_rules_for_targets", RULES_PATH)
    rules = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rules)
    return rules


def test_set_binds_to_the_trade_and_prunes_other_modes_and_closed_trades(tmp_path):
    path = tmp_path / "manual_targets.json"
    path.write_text(json.dumps({"take_profit": {
        "1": {"price": 70000, "open_ts": 10, "dry_run": False},     # closed trade
        "2": {"price": 86000, "open_ts": 20, "dry_run": False},     # still open, kept
        "3": {"price": 90000, "open_ts": 30, "dry_run": True}}}), encoding="utf-8")   # other mode
    result = targets.set_target(4, 95000, 40, False, {2: 20, 4: 40}, path)
    assert result == {2: {"price": 86000.0, "open_ts": 20, "dry_run": False},
                      4: {"price": 95000.0, "open_ts": 40, "dry_run": False}}
    assert targets.set_target(2, None, 20, False, {2: 20, 4: 40}, path) == {4: result[4]}


def test_price_for_matches_id_open_time_and_mode(tmp_path):
    saved = {2: {"price": 86000.0, "open_ts": 20, "dry_run": False}}
    assert targets.price_for(saved, 2, 20, False) == 86000.0
    assert targets.price_for(saved, 2, 21, False) is None
    assert targets.price_for(saved, 2, 20, True) is None
    assert targets.price_for(saved, 3, 20, False) is None


@pytest.mark.parametrize("text", ["", "{", "[]", '{"take_profit": 5}', '{"take_profit": {"a": {}, "2": 86000}}'])
def test_malformed_files_mean_no_targets(tmp_path, text):
    path = tmp_path / "manual_targets.json"
    path.write_text(text, encoding="utf-8")
    assert targets.load(path) == {}
    assert targets.load(tmp_path / "missing.json") == {}


def test_strategy_reads_exactly_what_the_panel_writes(tmp_path):
    path = tmp_path / "manual_targets.json"
    targets.set_target(2, 86000.5, 1792584003000, False, {2: 1792584003000}, path)
    rules = strategy_rules()
    parsed = rules.parse_manual_targets(path.read_text(encoding="utf-8"))
    assert parsed == targets.load(path)
    assert rules.target_for(parsed, 2, 1792584003000, False) == 86000.5
    assert path.name == rules.MANUAL_TARGETS_FILE
    assert targets.PATH == Path("freqtrade/user_data") / rules.MANUAL_TARGETS_FILE


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_file_is_readable_by_the_container_user(tmp_path):
    path = tmp_path / "manual_targets.json"
    old = os.umask(0o077)
    try:
        targets.set_target(2, 86000, 1, False, {2: 1}, path)
    finally:
        os.umask(old)
    assert path.stat().st_mode & 0o777 == 0o644
