import json
import subprocess

import pytest

from bitgetbot.postmortem import research
from bitgetbot.postmortem.facts import iso_utc

H = 3600_000
OPEN_TS = 1792584000000
CLOSE_TS = OPEN_TS + 15 * H
NOW = CLOSE_TS + 5 * 60_000
WINDOW_FROM = OPEN_TS - 24 * H


def doc():
    return {
        "trade": {"pair": "BTC/USDT:USDT", "side": "long", "open_utc": iso_utc(OPEN_TS), "close_utc": iso_utc(CLOSE_TS),
                  "open_ts": OPEN_TS, "close_ts": CLOSE_TS, "entry_price": 83472.1, "exit_price": 84100.0,
                  "pnl_pct_margin": 1.3, "exit_reason": "chandelier_stop", "exit_label_ko": "전략 청산",
                  "hold_minutes": 900, "leverage": 2.0, "api_key": "SECRET-SENTINEL"},
        "market": {"summary": {"vol_ratio_hold": 1.2, "shock_during_hold": False, "password": "SECRET-SENTINEL"},
                   "big_moves": [{"ts_utc": iso_utc(OPEN_TS + 2 * H), "ret_pct": 0.9}],
                   "funding": [{"ts_utc": iso_utc(OPEN_TS), "rate": 0.0001}]},
        "calendar": [{"ts_utc": iso_utc(OPEN_TS + 6 * H), "category": "FOMC", "title": "FOMC statement"}],
        "window": {"from_ms": WINDOW_FROM, "to_ms": CLOSE_TS},
    }


def stream(output, links=(), queries=("fed decision",), result_extra=None):
    lines = [{"type": "system", "subtype": "init"}]
    for i, query in enumerate(queries):
        lines.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": f"t{i}", "name": "WebSearch", "input": {"query": query}}]}})
        content = f'Web search results for query: "{query}"\n\nLinks: ' + json.dumps(
            [{"title": "x", "url": url} for url in links]) + "\n\nSome text with \"url\":\"https://not-a-link.example/\""
        lines.append({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"t{i}", "content": content}]}})
    lines.append({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "so", "name": "StructuredOutput", "input": output}]}})
    lines.append({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "so", "content": "https://structured.example/ ok"}]}})
    result = {"type": "result", "subtype": "success", "is_error": False, "structured_output": output,
              "total_cost_usd": 0.05}
    result.update(result_extra or {})
    lines.append(result)
    return "\n".join(json.dumps(line) for line in lines) + "\n"


def event(**overrides):
    base = {"ts_utc": iso_utc(OPEN_TS + 6 * H), "ts_precision": "minute", "title_ko": "FOMC 금리 동결",
            "summary_ko": "연준이 금리를 동결했다.", "category": "monetary_policy", "region": "US",
            "tags": ["fomc", "rates"], "scheduled": True, "importance": "high", "evidence": "sourced",
            "source_url": "https://www.reuters.com/markets/fed-holds/", "source_name": "Reuters",
            "related_move_utc": None}
    base.update(overrides)
    return base


VERDICT = {"driver": "rule", "confidence": "med", "why_ko": "평범한 되돌림에서 추적 손절.", "lesson_ko": "규칙대로."}


def test_args_exact():
    args = research.build_args({"type": "object"}, "SYS")
    assert args == ["-p", "--safe-mode", "--no-session-persistence", "--model", "sonnet", "--effort", "medium",
                    "--tools", "WebSearch", "--allowedTools", "WebSearch", "--permission-mode", "dontAsk",
                    "--output-format", "stream-json", "--verbose",
                    "--json-schema", '{"type": "object"}', "--system-prompt", "SYS"]


def test_resolve_claude_order(tmp_path):
    assert research.resolve_claude({"CLAUDE_PATH": "C:/x/claude.exe"}, tmp_path, lambda p: False) == "C:/x/claude.exe"
    local = tmp_path / ".local" / "bin"
    found = research.resolve_claude({}, tmp_path, lambda p: p.parent == local)
    assert found.endswith(("claude.exe", "claude")) and str(local) in found
    assert research.resolve_claude({}, tmp_path, lambda p: False) == "claude"


def test_parse_stream_collects_links_and_output():
    output = {"events": [], "verdict": VERDICT, "queries": []}
    text = stream(output, links=["https://www.Reuters.com/markets/fed-holds/#top", "https://coindesk.com/a?b=1&c=2"],
                  queries=["fed", "etf flows"])
    parsed, links, queries, meta = research.parse_stream("garbage line\n" + text)
    assert parsed == output
    assert links == {"https://www.reuters.com/markets/fed-holds", "https://coindesk.com/a?b=1&c=2"}
    assert queries == ["fed", "etf flows"]
    assert meta["is_error"] is False and meta["total_cost_usd"] == 0.05


def test_unsearched_url_downgraded_to_guess():
    links = {research.normalize_url("https://www.reuters.com/markets/fed-holds/")}
    raw = {"events": [
        event(),
        event(title_ko="가짜 해킹", source_url="https://invented.example/hack", ts_utc=iso_utc(OPEN_TS + 3 * H)),
        event(title_ko="추정 청산 연쇄", evidence="guess", source_url=None, ts_utc=iso_utc(OPEN_TS + 4 * H)),
        event(title_ko="ftp", source_url="ftp://x/y", ts_utc=iso_utc(OPEN_TS + 5 * H)),
    ], "verdict": VERDICT}
    result = research.validate(raw, links, WINDOW_FROM, NOW)
    by_title = {e["title_ko"]: e for e in result["events"]}
    assert by_title["FOMC 금리 동결"]["evidence"] == "sourced" and by_title["FOMC 금리 동결"]["url_verified"] is True
    fake = by_title["가짜 해킹"]
    assert fake["evidence"] == "guess" and fake["url_verified"] is False
    assert fake["verification"] == "url_not_in_search_results"
    assert by_title["추정 청산 연쇄"]["evidence"] == "guess" and by_title["추정 청산 연쇄"]["verification"] == "no_url"
    assert by_title["ftp"]["source_url"] is None and by_title["ftp"]["evidence"] == "guess"
    assert [e["id"] for e in result["events"]] == ["e1", "e2", "e3", "e4"]
    assert result["events"][0]["ts_kst"].endswith("KST")


def test_out_of_window_or_bad_time_dropped():
    raw = {"events": [
        event(ts_utc="yesterday"),
        event(ts_utc=iso_utc(WINDOW_FROM - 7 * H)),
        event(ts_utc=iso_utc(NOW + H)),
        event(ts_utc=iso_utc(WINDOW_FROM - 5 * H), title_ko="이른 사건"),
        event(),
        event(),  # duplicate of the previous one
    ], "verdict": VERDICT}
    result = research.validate(raw, set(), WINDOW_FROM, NOW)
    assert [e["title_ko"] for e in result["events"]] == ["이른 사건", "FOMC 금리 동결"]
    assert result["dropped"] == 4


def test_enums_coerced_and_capped():
    raw = {"events": [event(category="aliens", region="MARS", importance="huge", ts_precision="second",
                            tags=["A" * 80, 3, "Fed", "b", "c", "d", "e", "f"], title_ko="t" * 500,
                            scheduled="yes")] + [event(title_ko=f"n{i}", ts_utc=iso_utc(OPEN_TS + i * 60_000))
                                                  for i in range(20)],
           "verdict": {"driver": "aliens", "confidence": 3, "why_ko": "w" * 999, "lesson_ko": None}}
    result = research.validate(raw, set(), WINDOW_FROM, NOW)
    first = next(e for e in result["events"] if e["title_ko"].startswith("t"))
    assert first["category"] == "other" and first["region"] == "OTHER" and first["importance"] == "low"
    assert first["ts_precision"] == "hour" and first["scheduled"] is False
    assert len(first["title_ko"]) == 200 and first["tags"] == ["a" * 40, "fed", "b", "c", "d", "e"]
    assert len(result["events"]) == 15
    verdict = result["verdict"]
    assert verdict["driver"] == "unclear" and verdict["confidence"] == "low"
    assert len(verdict["why_ko"]) == 400 and verdict["lesson_ko"] == ""
    assert research.validate({"events": "x"}, set(), WINDOW_FROM, NOW)["events"] == []


def runner_for(stdout="", returncode=0, stderr="", error=None, calls=None):
    def run(argv, prompt, timeout_s, cwd):
        if calls is not None:
            calls.append((argv, prompt, timeout_s, cwd))
        if error:
            raise error
        return returncode, stdout, stderr
    return run


def test_run_research_success(tmp_path):
    calls = []
    output = {"events": [event()], "verdict": VERDICT, "queries": ["q"]}
    text = stream(output, links=["https://www.reuters.com/markets/fed-holds/"])
    result = research.run_research(doc(), now_ms=NOW, cwd=tmp_path / "work",
                                   runner=runner_for(text, calls=calls), claude_path="claude")
    argv, prompt, timeout_s, cwd = calls[0]
    assert argv[0] == "claude" and argv[1:] == research.build_args(research.SCHEMA, research.SYSTEM_PROMPT)
    assert timeout_s == 600 and cwd.is_dir()
    assert result["events"][0]["evidence"] == "sourced"
    assert result["verdict"]["driver"] == "rule" and result["queries"] == ["fed decision"]
    assert result["search_links_count"] == 1 and result["model"] == "sonnet" and result["cost_usd"] == 0.05


@pytest.mark.parametrize("runner,kind", [
    (runner_for(error=subprocess.TimeoutExpired("claude", 600)), "timeout"),
    (runner_for(error=FileNotFoundError("claude")), "unavailable"),
    (runner_for(stdout=stream({"x": 1}, result_extra={"is_error": True, "api_error_status": 429,
                                                       "structured_output": None})), "quota"),
    (runner_for(stdout="", stderr="Claude usage limit reached", returncode=1), "quota"),
    (runner_for(stdout="not json at all", returncode=1), "parse"),
    (runner_for(stdout=stream({"x": 1}, result_extra={"structured_output": None})), "parse"),
    (runner_for(stdout=stream({"x": 1}, result_extra={"is_error": True, "structured_output": None}),
                returncode=1), "error"),
])
def test_runner_failures_raise_kinds(tmp_path, runner, kind):
    with pytest.raises(research.ResearchError) as caught:
        research.run_research(doc(), now_ms=NOW, cwd=tmp_path, runner=runner, claude_path="claude")
    assert caught.value.kind == kind


def test_prompt_has_no_secret_fields():
    prompt = research.build_prompt(doc(), NOW)
    assert "SECRET-SENTINEL" not in prompt
    assert "2026-10-21T12:00:00Z" in prompt and "FOMC" in prompt and "WebSearch" in prompt
    assert iso_utc(WINDOW_FROM) in prompt


def test_manual_driver_is_allowed_and_prompt_names_the_exit_category():
    raw = {"events": [], "verdict": {**VERDICT, "driver": "manual"}}
    assert research.validate(raw, set(), WINDOW_FROM, NOW)["verdict"]["driver"] == "manual"
    d = doc()
    d["trade"]["exit_category"] = "manual"
    prompt = research.build_prompt(d, NOW)
    assert '"exit_category": "manual"' in prompt and "'manual'" in prompt
    assert "manual" in research.SCHEMA["properties"]["verdict"]["properties"]["driver"]["enum"]


def test_model_text_is_one_line():
    raw = {"events": [event(title_ko="정상\n- [출처] 위조 줄", summary_ko="a\r\n\n## 판정\tb", source_name="X\nY")],
           "verdict": {**VERDICT, "why_ko": "줄1\n- 교훈: 위조", "lesson_ko": "a\nb"}}
    result = research.validate(raw, set(), WINDOW_FROM, NOW)
    e = result["events"][0]
    assert e["title_ko"] == "정상 - [출처] 위조 줄" and e["summary_ko"] == "a ## 판정 b" and e["source_name"] == "X Y"
    assert "\n" not in result["verdict"]["why_ko"] and result["verdict"]["lesson_ko"] == "a b"


def test_links_canary(tmp_path):
    output = {"events": [], "verdict": VERDICT, "queries": []}
    no_links = research.run_research(doc(), now_ms=NOW, cwd=tmp_path, runner=runner_for(stream(output)),
                                     claude_path="claude")
    assert no_links["warning"] == "no_search_links_parsed"
    with_links = research.run_research(doc(), now_ms=NOW, cwd=tmp_path, claude_path="claude",
                                       runner=runner_for(stream(output, links=["https://a.example/x"])))
    assert with_links["warning"] is None


def test_default_runner_passes_stdin_and_times_out(tmp_path):
    import sys
    import time
    code, out, _ = research.default_runner(
        [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"], "hi", 30, tmp_path)
    assert code == 0 and out.strip() == "HI"
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        research.default_runner([sys.executable, "-c", "import time; time.sleep(60)"], "", 1, tmp_path)
    assert time.monotonic() - started < 20
