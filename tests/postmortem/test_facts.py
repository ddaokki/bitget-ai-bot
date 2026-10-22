import json

import pytest

from bitgetbot.postmortem import facts

OPEN_TS = 1792584000000  # 2026-10-21T12:00:00Z
CLOSE_TS = OPEN_TS + 15 * 3600_000 + 10 * 60_000
NOW = CLOSE_TS + 120_000


def row(**overrides):
    base = {
        "trade_id": 2, "pair": "BTC/USDT:USDT", "is_open": False, "is_short": False,
        "enter_tag": "safe_long", "exit_reason": "chandelier_stop",
        "open_timestamp": OPEN_TS, "close_timestamp": CLOSE_TS,
        "open_rate": 83472.1, "close_rate": 84100.0, "amount": 0.0003, "stake_amount": 12.52,
        "leverage": 2.0, "profit_abs": 0.17, "profit_ratio": 0.0136, "funding_fees": -0.0001,
        "max_rate": 84771.1, "min_rate": 83238.0, "stop_loss_abs": 83900.0, "initial_stop_loss_abs": 81000.0,
    }
    base.update(overrides)
    return base


def test_key_format_and_regex():
    key = facts.report_key("dry", 7, OPEN_TS)
    assert key == f"dry-7-{OPEN_TS}"
    assert facts.KEY_RE.fullmatch(key)
    for bad in ["paper-7-1", "dry-7", "dry-x-1", "../dry-7-1", "dry-7-1.json", "live-1234567890123-1"]:
        assert not facts.KEY_RE.fullmatch(bad)


def test_closed_trades_filters_open_smoke_old_and_malformed():
    old = CLOSE_TS - 31 * 86_400_000
    history = {"trades": [
        row(trade_id=5, close_timestamp=CLOSE_TS + 1000),
        row(trade_id=4),
        row(trade_id=3, is_open=True, close_timestamp=None),
        row(trade_id=1, enter_tag="smoke"),
        row(trade_id=6, close_timestamp=old, open_timestamp=old - 1000),
        row(trade_id="7"),
        row(trade_id=8, close_timestamp="soon"),
        "junk",
    ], "total_trades": 8}
    found = facts.closed_trades(history, NOW)
    assert [r["trade_id"] for r in found] == [4, 5]  # oldest close first
    assert facts.closed_trades({"trades": "x"}, NOW) == []
    assert facts.closed_trades(None, NOW) == []


def test_facts_long_profit_and_kst():
    result = facts.trade_facts(row(), "dry")
    assert result["key"] == f"dry-2-{OPEN_TS}"
    assert result["symbol"] == "BTCUSDT" and result["side"] == "long" and result["kind"] == "strategy"
    assert result["open_utc"] == "2026-10-21T12:00:00Z"
    assert result["open_kst"] == "2026-10-21 21:00 KST"
    assert result["close_utc"] == "2026-10-22T03:10:00Z"
    assert result["hold_minutes"] == 910
    assert result["pnl_usdt"] == pytest.approx(0.17)
    assert result["pnl_pct_margin"] == pytest.approx(0.17 / 12.52 * 100)
    assert result["mfe_pct"] == pytest.approx((84771.1 / 83472.1 - 1) * 100)
    assert result["mae_pct"] == pytest.approx((83238.0 / 83472.1 - 1) * 100)
    assert result["exit_category"] == "strategy"
    assert "추적 손절" in result["exit_label_ko"]
    assert result["source"] == "freqtrade_api"


def test_facts_short_excursions():
    result = facts.trade_facts(row(is_short=True, enter_tag=None), "live")
    assert result["side"] == "short" and result["kind"] == "manual"
    assert result["mfe_pct"] == pytest.approx((1 - 83238.0 / 83472.1) * 100)
    assert result["mae_pct"] == pytest.approx((1 - 84771.1 / 83472.1) * 100)


@pytest.mark.parametrize("reason,category", [
    ("user_take_profit", "user_take_profit"),
    ("stoploss_on_exchange", "stop"), ("stop_loss", "stop"), ("trailing_stop_loss", "stop"),
    ("liquidation", "liquidation"),
    ("opposite_donchian_10", "strategy"), ("chandelier_stop", "strategy"), ("exit_signal", "strategy"),
    ("roi", "strategy"), ("custom_exit", "strategy"),
    ("force_exit", "manual"), ("emergency_exit", "bot_emergency"),
    ("something_new", "other"), (None, "other"),
])
def test_exit_categories(reason, category):
    result = facts.trade_facts(row(exit_reason=reason), "dry")
    assert result["exit_category"] == category
    assert result["exit_label_ko"]


def test_facts_tolerate_missing_numbers():
    result = facts.trade_facts(row(stake_amount=0, close_rate=None, profit_abs=None, max_rate=None,
                                   min_rate="x", leverage=None, funding_fees=None), "dry")
    assert result["pnl_pct_margin"] is None and result["exit_price"] is None
    assert result["pnl_usdt"] is None and result["mfe_pct"] is None and result["mae_pct"] is None
    assert result["leverage"] is None and result["funding_usdt"] is None


def write_audit(path, entries):
    path.write_text("".join(json.dumps(e) + "\n" for e in entries) + "not json\n", encoding="utf-8")


def iso(ms):
    return facts.iso_utc(ms).replace("Z", ".000000Z")


def test_refine_manual_exit_from_audit(tmp_path):
    audit = tmp_path / "audit.log"
    base = facts.trade_facts(row(exit_reason="force_exit"), "dry")
    write_audit(audit, [{"ts": iso(CLOSE_TS - 30_000), "action": "trade_close", "step": "2", "result": 200}])
    assert facts.refine_manual_exit(base, audit)["exit_detail"] == "panel_close"
    write_audit(audit, [{"ts": iso(CLOSE_TS - 5_000), "action": "emergency", "result": 200}])
    refined = facts.refine_manual_exit(base, audit)
    assert refined["exit_detail"] == "emergency" and "긴급" in refined["exit_label_ko"]
    write_audit(audit, [
        {"ts": iso(CLOSE_TS - 3600_000), "action": "emergency", "result": 200},  # too early
        {"ts": iso(CLOSE_TS), "action": "trade_close", "step": "9", "result": 200},  # other trade
        {"ts": iso(CLOSE_TS), "action": "trade_close", "step": "2", "result": 503},  # failed request
    ])
    assert facts.refine_manual_exit(base, audit)["exit_detail"] == "unknown"
    assert facts.refine_manual_exit(base, tmp_path / "missing.log")["exit_detail"] == "unknown"
    strategy = facts.trade_facts(row(), "dry")
    assert "exit_detail" not in facts.refine_manual_exit(strategy, audit)


def test_calendar_events_in_window(tmp_path):
    csv = tmp_path / "macro.csv"
    csv.write_text(
        "ts_utc,category,title\n"
        "2026-10-21T18:00:00Z,FOMC,FOMC statement\n"
        "2026-10-21T13:30:00Z,CPI,CPI release\n"
        "2026-11-06T13:30:00Z,NFP,Employment Situation\n"
        "garbage,line\n", encoding="utf-8")
    found = facts.calendar_events(csv, OPEN_TS - 86_400_000, CLOSE_TS)
    assert [e["category"] for e in found] == ["CPI", "FOMC"]
    fomc = found[1]
    assert fomc["ts_utc"] == "2026-10-21T18:00:00Z" and fomc["region"] == "US"
    assert fomc["tag"] == "monetary_policy" and fomc["source_url"].startswith("https://www.federalreserve.gov/")
    assert found[0]["tag"] == "macro_data" and "bls.gov" in found[0]["source_url"]
    assert facts.calendar_events(tmp_path / "missing.csv", 0, CLOSE_TS) == []
