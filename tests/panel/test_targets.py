"""User take-profit store shared with the strategy (freqtrade/user_data/manual_targets.json)."""

import importlib.util
import json
from pathlib import Path

import pytest

from bitgetbot.panel import targets

RULES_PATH = Path(__file__).resolve().parents[2] / "freqtrade" / "user_data" / "strategies" / "live_rules.py"


def strategy_rules():
    spec = importlib.util.spec_from_file_location("live_rules_for_targets", RULES_PATH)
    rules = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rules)
    return rules


def test_set_prune_and_clear(tmp_path):
    path = tmp_path / "manual_targets.json"
    path.write_text('{"take_profit": {"1": 70000, "2": 86000}}', encoding="utf-8")
    assert targets.set_target(3, 90000, {2, 3}, path) == {2: 86000.0, 3: 90000.0}
    assert targets.set_target(2, None, {2, 3}, path) == {3: 90000.0}
    assert json.loads(path.read_text(encoding="utf-8")) == {"take_profit": {"3": 90000.0}}


@pytest.mark.parametrize("text", ["", "{", "[]", '{"take_profit": 5}', '{"take_profit": {"a": 1, "2": -5, "3": "9"}}'])
def test_malformed_files_mean_no_targets(tmp_path, text):
    path = tmp_path / "manual_targets.json"
    path.write_text(text, encoding="utf-8")
    assert targets.load(path) == {}
    assert targets.load(tmp_path / "missing.json") == {}


def test_strategy_reads_what_the_panel_writes(tmp_path):
    path = tmp_path / "manual_targets.json"
    targets.set_target(2, 86000.5, {2}, path)
    rules = strategy_rules()
    assert rules.parse_manual_targets(path.read_text(encoding="utf-8")) == {2: 86000.5}
    assert path.name == rules.MANUAL_TARGETS_FILE
    assert targets.PATH == Path("freqtrade/user_data") / rules.MANUAL_TARGETS_FILE
