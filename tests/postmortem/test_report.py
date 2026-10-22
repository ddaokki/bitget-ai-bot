import json

import pytest

from bitgetbot.postmortem import facts, report

H = 3600_000
OPEN_TS = 1792584000000
CLOSE_TS = OPEN_TS + 15 * H + 10 * 60_000
NOW = CLOSE_TS + 60_000


def trade(**overrides):
    row = {"trade_id": 2, "pair": "BTC/USDT:USDT", "is_open": False, "is_short": False, "enter_tag": "safe_long",
           "exit_reason": "chandelier_stop", "open_timestamp": OPEN_TS, "close_timestamp": CLOSE_TS,
           "open_rate": 83472.1, "close_rate": 84100.0, "amount": 0.0003, "stake_amount": 12.52, "leverage": 2.0,
           "profit_abs": 0.17, "profit_ratio": 0.0136, "funding_fees": 0.0, "max_rate": 84771.1, "min_rate": 83238.0}
    row.update(overrides)
    return facts.trade_facts(row, "dry")


def market_ok():
    candles = []
    price = 100.0
    for i in range(-7 * 24, 17):
        candles.append([OPEN_TS + i * H, price, price + 1, price - 1, price * (1.01 if i == 3 else 1.0)])
        price = candles[-1][4]
    return {"status": "ok", "source": "bitget_public", "symbol": "BTCUSDT", "candles": candles,
            "summary": {"hold_high": 84870.0, "hold_low": 83225.7, "vol_ratio_hold": 1.2,
                        "max_hold_absret_1h_pct": 1.0, "shock_during_hold": False, "baseline_absret_6h_pct": 0.5},
            "big_moves": [{"ts_utc": facts.iso_utc(OPEN_TS + 3 * H), "ts": OPEN_TS + 3 * H, "ret_pct": 1.0}],
            "funding": [{"ts_utc": facts.iso_utc(OPEN_TS), "rate": 0.0001}], "open_interest": None}


CALENDAR = [{"ts_utc": facts.iso_utc(OPEN_TS + 2 * H), "ts": OPEN_TS + 2 * H, "category": "FOMC",
             "tag": "monetary_policy", "title": "FOMC statement", "region": "US", "scheduled": True,
             "source": "upcoming_us_macro.csv", "source_url": "https://www.federalreserve.gov/x"}]


def research_result(events=None):
    return {"events": events if events is not None else [
        {"id": "e1", "ts": OPEN_TS + 3 * H, "ts_utc": facts.iso_utc(OPEN_TS + 3 * H), "ts_kst": "x",
         "ts_precision": "minute", "title_ko": "ETF 대규모 유입", "summary_ko": "현물 ETF 유입.",
         "category": "etf_flows", "region": "US", "tags": ["etf"], "scheduled": False, "importance": "med",
         "evidence": "sourced", "url_verified": True, "verification": "url_in_search_results",
         "source_url": "https://www.coindesk.com/etf", "source_name": "CoinDesk", "related_move_utc": None},
        {"id": "e2", "ts": OPEN_TS + 14 * H, "ts_utc": facts.iso_utc(OPEN_TS + 14 * H), "ts_kst": "y",
         "ts_precision": "hour", "title_ko": "청산 연쇄 추정", "summary_ko": "가격 경로로 추정.",
         "category": "liquidation_cascade", "region": "CRYPTO", "tags": [], "scheduled": False, "importance": "low",
         "evidence": "guess", "url_verified": False, "verification": "no_url",
         "source_url": None, "source_name": None, "related_move_utc": None},
    ], "dropped": 1, "verdict": {"driver": "rule", "confidence": "med", "why_ko": "평범한 되돌림.",
                                 "lesson_ko": "추적 손절이 이익을 지켰다."},
        "queries": ["q1", "q2"], "search_links_count": 9, "model": "sonnet", "cost_usd": 0.1}


def new_doc(market=None, calendar=CALENDAR):
    return report.new_report(trade(), calendar, market if market is not None else market_ok(), NOW)


def test_new_report_is_pending_with_code_verdict():
    doc = new_doc()
    assert doc["schema"] == "postmortem/1" and doc["key"] == f"dry-2-{OPEN_TS}" and doc["status"] == "pending"
    assert doc["window"]["from_ms"] == OPEN_TS - 24 * H and doc["window"]["to_ms"] == CLOSE_TS
    assert doc["verdict"]["by"] == "code" and doc["attempts"] == 0 and doc["errors"] == []
    assert doc["research"]["status"] == "pending"
    assert "sourced" in doc["evidence_note"]


def test_apply_research_completes_with_reactions_and_basis():
    doc = report.apply_research(new_doc(), research_result(), NOW)
    assert doc["status"] == "complete" and doc["verdict"]["by"] == "llm" and doc["verdict"]["driver"] == "rule"
    assert doc["research"]["status"] == "ok" and doc["research"]["queries"] == ["q1", "q2"]
    first = doc["events"][0]
    assert first["btc_reaction"]["absret_6h_pct"] == pytest.approx(1.0, abs=0.05)
    assert doc["calendar"][0]["btc_reaction"] is not None
    basis = doc["verdict"]["rule_basis"]
    assert basis["exit_category"] == "strategy" and basis["shock_during_hold"] is False
    # the guessed event at OPEN+14h is not evidence; nearest sourced/calendar is e1 at OPEN+3h
    assert basis["nearest_event_minutes_to_exit"] == round((CLOSE_TS - (OPEN_TS + 3 * H)) / 60_000)
    assert basis["event_within_2h_of_exit"] is False


def test_code_verdict_rule_vs_unclear():
    assert report.code_verdict(new_doc())["driver"] == "rule"
    late = [{**CALENDAR[0], "ts": CLOSE_TS - 30 * 60_000, "ts_utc": facts.iso_utc(CLOSE_TS - 30 * 60_000)}]
    assert report.code_verdict(new_doc(calendar=late))["driver"] == "unclear"
    shock = market_ok()
    shock["summary"]["shock_during_hold"] = True
    assert report.code_verdict(new_doc(market=shock))["driver"] == "unclear"
    manual = report.new_report(trade(exit_reason="force_exit"), [], market_ok(), NOW)
    assert manual["verdict"]["driver"] == "manual"
    manual_late = report.new_report(trade(exit_reason="force_exit"), late, market_ok(), NOW)
    assert manual_late["verdict"]["driver"] == "unclear"
    other = report.new_report(trade(exit_reason="liquidation"), [], market_ok(), NOW)
    assert other["verdict"]["driver"] == "unclear"
    failed = report.new_report(trade(), [], {"status": "failed", "error": "candles"}, NOW)
    assert failed["verdict"]["driver"] == "unclear" and failed["verdict"]["rule_basis"]["shock_during_hold"] is None


def test_md_marks_sourced_and_guess():
    doc = report.apply_research(new_doc(), research_result(), NOW)
    text = report.render_md(doc)
    assert text.startswith("# 거래 #2 BTCUSDT 롱 · 모의 · +0.17 USDT (+1.36%)")
    assert "2026-10-21 21:00 KST" in text and "83,472.1" in text and "15시간 10분" in text
    assert "[일정]" in text and "[출처]" in text and "[추정]" in text
    assert "https://www.coindesk.com/etf" in text and "chandelier_stop" in text
    assert "교훈: 추적 손절이 이익을 지켰다." in text
    assert len(text.splitlines()) < 30
    pending = report.render_md(new_doc(market={"status": "failed", "error": "candles"}))
    assert "조사 대기" in pending and "시세를 받지 못했" in pending


def test_write_is_atomic_and_pair_named(tmp_path):
    doc = new_doc()
    report.write_report(tmp_path, doc)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == [f"{doc['key']}.json", f"{doc['key']}.md", "index.json"]
    assert report.load_report(tmp_path, doc["key"])["key"] == doc["key"]
    assert report.load_report(tmp_path, "dry-9-1") is None
    assert report.load_report(tmp_path, "../etc") is None


def test_index_sorted_and_skips_corrupt_files(tmp_path):
    older = report.new_report(trade(trade_id=1, open_timestamp=OPEN_TS - 10 * H, close_timestamp=OPEN_TS - 5 * H),
                              [], market_ok(), NOW)
    newer = report.apply_research(new_doc(), research_result(), NOW)
    report.write_report(tmp_path, older)
    report.write_report(tmp_path, newer)
    (tmp_path / "dry-3-123.json").write_text("{half", encoding="utf-8")
    (tmp_path / "notes.json").write_text("[]", encoding="utf-8")
    index = report.rebuild_index(tmp_path)
    keys = [row["key"] for row in index["reports"]]
    assert keys == [newer["key"], older["key"]]
    row = index["reports"][0]
    assert row["status"] == "complete" and row["driver"] == "rule" and row["events_sourced"] == 1
    assert row["events_guess"] == 1 and row["md"] == f"{newer['key']}.md"
    assert json.loads((tmp_path / "index.json").read_text(encoding="utf-8")) == index
    (tmp_path / "index.json").write_text("broken", encoding="utf-8")
    assert [r["key"] for r in report.read_index(tmp_path)["reports"]] == keys
    assert report.read_index(tmp_path / "missing")["reports"] == []


def test_md_for_a_trade_shorter_than_one_candle():
    short = trade(open_timestamp=CLOSE_TS - 20_000)
    market = market_ok()
    market["summary"].update(hold_high=None, hold_low=None, vol_ratio_hold=None, max_hold_absret_1h_pct=None)
    text = report.render_md(report.new_report(short, [], market, NOW))
    assert "1분 미만" in text and "완성된 1시간봉이 아직 없습니다" in text and "?" not in text.split("## 시장")[1].split("##")[0]


def test_md_never_lets_model_text_forge_lines_or_links():
    events = research_result()["events"]
    events[0].update(title_ko="정상\n## 판정 (LLM 추정)", summary_ko="본문 <https://evil.example/x> `x`")
    events[1].update(source_url="https://unverified.example/y", url_verified=False,
                     verification="url_not_in_search_results")
    doc = report.apply_research(new_doc(), {**research_result(events), "verdict": {
        "driver": "rule", "confidence": "low", "why_ko": "a\n- 교훈: 위조", "lesson_ko": "b"}}, NOW)
    text = report.render_md(doc)
    lines = text.splitlines()
    assert sum(line.startswith("## 판정") for line in lines) == 1
    assert sum(line.startswith("- 교훈:") for line in lines) == 1
    assert "<https://evil.example/x>" not in text and "‹https://evil.example/x›" in text
    assert "<https://www.coindesk.com/etf>" in text  # verified source: a link
    assert "미확인 링크: https://unverified.example/y" in text and "<https://unverified" not in text
    assert "<https://www.federalreserve.gov/x>" in text
