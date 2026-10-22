"""Report documents: the machine JSON (also the retry state), a short Korean MD, and the index."""

import json
import os
import tempfile
from pathlib import Path

from bitgetbot.postmortem import market as market_mod
from bitgetbot.postmortem.facts import KEY_RE, iso_kst, iso_utc, parse_utc

REPORTS_DIR = Path("reports/postmortem")  # relative to the repo root (panel cwd); git-ignored
INDEX_NAME = "index.json"
SCHEMA = "postmortem/1"
WINDOW_BEFORE_MS = 24 * 3600_000
NEAR_EXIT_MS = 2 * 3600_000
RULE_EXITS = {"stop", "strategy", "user_take_profit"}
# Re-read the market once the exit candle has closed and events near the exit have 6 h of reaction.
MARKET_REFRESH_AFTER_MS = 7 * 3600_000
MARKET_REFRESH_TRIES = 3
EVIDENCE_NOTE = (
    "trade/calendar: sourced (Freqtrade API, official release calendar). market: Bitget public API. "
    "events[].evidence: 'sourced' only when source_url appeared in this run's WebSearch results, else 'guess'. "
    "verdict.by='llm' is a model judgment (a guess); verdict.by='code' is the fixed rule in report.code_verdict."
)
DRIVER_KO = {"rule": "규칙", "event": "사건", "mixed": "혼합", "manual": "수동", "unclear": "불명"}
CONFIDENCE_KO = {"low": "낮음", "med": "중간", "high": "높음"}


def new_report(facts: dict, calendar: list[dict], market: dict, now_ms: int) -> dict:
    window_from = facts["open_ts"] - WINDOW_BEFORE_MS
    doc = {
        "schema": SCHEMA, "key": facts["key"], "mode": facts["mode"], "status": "pending",
        "created_utc": iso_utc(now_ms), "updated_utc": iso_utc(now_ms),
        "attempts": 0, "next_retry_ms": None, "next_retry_utc": None, "in_progress_since_ms": None,
        "errors": [],
        "window": {"from_ms": window_from, "to_ms": facts["close_ts"],
                   "from_utc": iso_utc(window_from), "to_utc": facts["close_utc"]},
        "trade": facts, "market": market, "calendar": calendar,
        "events": [], "events_dropped": 0,
        "research": {"status": "pending"},
        "market_refresh_due_ms": facts["close_ts"] + MARKET_REFRESH_AFTER_MS, "market_refresh_tries": 0,
        "evidence_note": EVIDENCE_NOTE,
    }
    doc["verdict"] = code_verdict(doc)
    return doc


def _evidence_times(doc: dict) -> list[int]:
    times = [event["ts"] for event in doc.get("calendar") or [] if isinstance(event.get("ts"), int)]
    times += [event["ts"] for event in doc.get("events") or []
              if event.get("evidence") == "sourced" and isinstance(event.get("ts"), int)]
    return times


def rule_basis(doc: dict) -> dict:
    trade, market = doc["trade"], doc.get("market") or {}
    close_ts = trade["close_ts"]
    nearest = min((abs(close_ts - ts) for ts in _evidence_times(doc)), default=None)
    shock = (market.get("summary") or {}).get("shock_during_hold") if market.get("status") == "ok" else None
    return {
        "exit_category": trade["exit_category"],
        "nearest_event_minutes_to_exit": round(nearest / 60_000) if nearest is not None else None,
        "event_within_2h_of_exit": nearest is not None and nearest <= NEAR_EXIT_MS,
        "shock_during_hold": shock if isinstance(shock, bool) else None,
    }


def code_verdict(doc: dict) -> dict:
    """Fixed rule when no model judgment exists: a rule exit with no nearby evidence and no shock is 'rule'."""
    basis = rule_basis(doc)
    quiet = not basis["event_within_2h_of_exit"] and basis["shock_during_hold"] is False
    driver = "unclear"
    if quiet and basis["exit_category"] in RULE_EXITS:
        driver = "rule"
    elif quiet and basis["exit_category"] == "manual":
        driver = "manual"
    return {"by": "code", "driver": driver, "confidence": "low",
            "why_ko": "웹 조사 결과가 없어 코드 규칙(청산 사유·청산 전후 2시간 사건·보유 중 급변)으로만 판정했습니다.",
            "lesson_ko": "", "rule_basis": basis}


def _with_reaction(event: dict, candles, baseline) -> dict:
    ts = event.get("ts")
    reaction = market_mod.event_reaction(candles, ts, baseline) if candles and isinstance(ts, int) else None
    return {**event, "btc_reaction": reaction}


def _reactions(doc: dict, events: list[dict]) -> list[dict]:
    market = doc.get("market") or {}
    candles = market.get("candles") if market.get("status") == "ok" else None
    baseline = (market.get("summary") or {}).get("baseline_absret_6h_pct")
    return [_with_reaction(event, candles, baseline) for event in events]


def apply_research(doc: dict, research: dict, now_ms: int) -> dict:
    doc = {
        **doc, "status": "complete", "updated_utc": iso_utc(now_ms),
        "next_retry_ms": None, "next_retry_utc": None, "in_progress_since_ms": None,
        "events": _reactions(doc, research["events"]),
        "calendar": _reactions(doc, doc.get("calendar") or []),
        "events_dropped": research.get("dropped", 0),
        "research": {"status": "ok", "finished_utc": iso_utc(now_ms), "model": research.get("model"),
                     "queries": research.get("queries", []), "search_links_count": research.get("search_links_count"),
                     "cost_usd": research.get("cost_usd"), "warning": research.get("warning")},
    }
    doc["verdict"] = {**research["verdict"], "by": "llm", "rule_basis": rule_basis(doc)}
    return doc


def refresh_market(doc: dict, market: dict | None, now_ms: int) -> dict:
    """One later market re-read (no model call): reactions and the code basis use the fuller path."""
    if market is None or market.get("status") != "ok":
        tries = doc.get("market_refresh_tries", 0) + 1
        due = None if tries >= MARKET_REFRESH_TRIES else now_ms + 3600_000
        return {**doc, "market_refresh_tries": tries, "market_refresh_due_ms": due, "updated_utc": iso_utc(now_ms)}
    doc = {**doc, "market": market, "market_refresh_due_ms": None, "market_refreshed_utc": iso_utc(now_ms),
           "updated_utc": iso_utc(now_ms)}
    doc["events"] = _reactions(doc, doc.get("events") or [])
    doc["calendar"] = _reactions(doc, doc.get("calendar") or [])
    verdict = doc.get("verdict") or {}
    doc["verdict"] = code_verdict(doc) if verdict.get("by") != "llm" else {**verdict, "rule_basis": rule_basis(doc)}
    return doc


# --- Korean markdown -------------------------------------------------------------------------

def _price(value) -> str:
    return f"{value:,.1f}" if isinstance(value, (int, float)) else "?"


def _plain(value, digits: int | None = None) -> str:
    if not isinstance(value, (int, float)):
        return "?"
    return f"{value:g}" if digits is None else f"{value:,.{digits}f}"


def _signed(value, digits: int = 2) -> str:
    return f"{value:+.{digits}f}" if isinstance(value, (int, float)) else "?"


def _usdt(value) -> str:
    if not isinstance(value, (int, float)):
        return "?"
    return f"{value:+.2f}" if abs(value) >= 0.01 or value == 0 else f"{value:+.4f}"


def _duration(minutes) -> str:
    if not isinstance(minutes, int):
        return "?"
    if minutes < 1:
        return "1분 미만"
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days}일 {hours}시간"
    return f"{hours}시간 {mins}분" if hours else f"{mins}분"


def _safe(value) -> str:
    """Model or file text as one inert markdown line: no line breaks, no <link> or `code` markup."""
    text = " ".join(str(value or "").split())
    return text.replace("<", "‹").replace(">", "›").replace("`", "'")


def _short_kst(ts_utc: str) -> str:
    at = parse_utc(ts_utc)
    return iso_kst(at)[5:] if at is not None else ts_utc


def _status_line(doc: dict) -> str:
    if doc["status"] == "complete":
        return "상태: 완료"
    if doc["status"] == "incomplete":
        return f"상태: 조사 실패로 사실만 기록({doc['attempts']}회 시도)"
    retry = f", 다음 시도 {iso_kst(doc['next_retry_ms'])}" if doc.get("next_retry_ms") else ""
    return f"상태: 조사 대기({doc['attempts']}회 시도{retry})"


def render_md(doc: dict) -> str:
    t, m = doc["trade"], doc.get("market") or {}
    side = "롱" if t["side"] == "long" else "숏"
    mode = "모의" if doc["mode"] == "dry" else "실전"
    pct = f" ({_signed(t['pnl_pct_margin'])}%)" if t.get("pnl_pct_margin") is not None else ""
    lines = [
        f"# 거래 #{t['trade_id']} {t.get('symbol') or t['pair']} {side} · {mode} · {_usdt(t['pnl_usdt'])} USDT{pct}",
        f"- 진입 {t['open_kst']} ({t['open_utc'][11:16]}Z) {_price(t['entry_price'])} → "
        f"청산 {t['close_kst']} ({t['close_utc'][11:16]}Z) {_price(t['exit_price'])}",
        f"- 보유 {_duration(t['hold_minutes'])} · {_plain(t['leverage'])}배 · 증거금 {_plain(t['margin_usdt'], 2)} USDT"
        f" · 청산: {t['exit_label_ko']} (`{t['exit_reason']}`)",
        f"- 보유 중 최대 유리 {_signed(t['mfe_pct'])}% / 최대 불리 {_signed(t['mae_pct'])}% (가격 기준)",
        "",
        "## 시장 (비트겟 공개 1시간봉)",
    ]
    if m.get("status") == "ok":
        s = m["summary"]
        if isinstance(s.get("hold_high"), (int, float)) and isinstance(s.get("vol_ratio_hold"), (int, float)):
            lines.append(f"- 보유 중 고가 {_price(s['hold_high'])} / 저가 {_price(s['hold_low'])}"
                         f" · 변동성 평소의 {s['vol_ratio_hold']:.1f}배"
                         f" · {'급변 있음' if s.get('shock_during_hold') else '급변 없음'}")
        else:
            lines.append("- 보유 구간에 완성된 1시간봉이 아직 없습니다(보유가 짧음).")
        if s.get("covers_close") is False and doc.get("market_refresh_due_ms"):
            lines.append("- 청산 시간대 1시간봉은 아직 미완성입니다(청산 7시간 뒤 다시 받음).")
        moves = ", ".join(f"{_short_kst(x['ts_utc'])} {_signed(x['ret_pct'])}%" for x in m.get("big_moves") or [])
        if moves:
            lines.append(f"- 큰 1시간 움직임: {moves}")
        funding = m.get("funding")
        if funding:
            lines.append(f"- 펀딩 {len(funding)}회, 최근 {_signed(funding[-1]['rate'] * 100, 4)}%")
    else:
        lines.append("- 시세를 받지 못했습니다(다음 시도에서 다시 받음).")
    events = doc.get("events") or []
    sourced = sum(1 for e in events if e.get("evidence") == "sourced")
    lines += ["", f"## 사건 (출처 확인 {sourced} · 추정 {len(events) - sourced} · 일정 {len(doc.get('calendar') or [])})"]
    for e in doc.get("calendar") or []:
        lines.append(f"- [일정] {_short_kst(e['ts_utc'])} {_safe(e['title'])} — <{e['source_url']}>")
    for e in events:
        mark = "[출처]" if e.get("evidence") == "sourced" else "[추정]"
        where = ""
        if e.get("source_url") and e.get("url_verified"):
            where = f" ({_safe(e['source_name']) or '출처'}) <{e['source_url']}>"
        elif e.get("source_url"):
            # Not in this run's search results: shown as text, never as a link.
            where = f" (미확인 링크: {_safe(e['source_url'])})"
        lines.append(f"- {mark} {_short_kst(e['ts_utc'])} {_safe(e['title_ko'])} — {_safe(e['summary_ko'])}{where}")
    if not events and not doc.get("calendar"):
        lines.append("- 없음" if doc["status"] == "complete" else "- 아직 조사하지 않았습니다.")
    v = doc["verdict"]
    by = "LLM 추정" if v["by"] == "llm" else "코드 규칙"
    lines += ["", f"## 판정 ({by})",
              f"- 원인: {DRIVER_KO.get(v['driver'], v['driver'])} (확신 {CONFIDENCE_KO.get(v['confidence'], '?')})"
              f" — {_safe(v['why_ko'])}"]
    if v.get("lesson_ko"):
        lines.append(f"- 교훈: {_safe(v['lesson_ko'])}")
    lines += ["", _status_line(doc)]
    return "\n".join(lines) + "\n"


# --- files ------------------------------------------------------------------------------------

def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.",
                                         suffix=".tmp", delete=False, newline="\n") as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_report(directory: Path, doc: dict) -> None:
    """MD first, then the JSON (the state record), then the index rebuilt from the JSON files."""
    _atomic_write(directory / f"{doc['key']}.md", render_md(doc))
    _atomic_write(directory / f"{doc['key']}.json", json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
    rebuild_index(directory)


def _parse_doc(text: str, key: str) -> dict | None:
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or doc.get("key") != key:
        return None
    return doc if isinstance(doc.get("trade"), dict) else None


def read_state(directory: Path, key: str) -> tuple[str, dict | None]:
    """("ok", doc) | ("missing", None) | ("corrupt", None) | ("unreadable", None).

    "unreadable" is a read error (a Windows sharing violation, antivirus): the file may be fine, so the
    watcher must not overwrite it. "corrupt" means the bytes were read and are not a report.
    """
    path = directory / f"{key}.json"
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "unreadable", None
    try:
        doc = _parse_doc(raw.decode("utf-8"), key)
    except UnicodeError:
        doc = None
    return ("ok", doc) if doc is not None else ("corrupt", None)


def _read_doc(path: Path) -> dict | None:
    state, doc = read_state(path.parent, path.stem)
    return doc if state == "ok" else None


def load_report(directory: Path, key: str) -> dict | None:
    """The stored document, or None when it is missing, unreadable or not a report."""
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        return None
    return _read_doc(directory / f"{key}.json")


def report_docs(directory: Path) -> list[dict]:
    if not directory.is_dir():
        return []
    docs = []
    for path in directory.glob("*.json"):
        if path.name != INDEX_NAME and KEY_RE.fullmatch(path.stem):
            doc = _read_doc(path)
            if doc is not None:
                docs.append(doc)
    return docs


def _row(doc: dict) -> dict:
    t, v = doc["trade"], doc.get("verdict") or {}
    events = doc.get("events") or []
    return {
        "key": doc["key"], "mode": doc["mode"], "trade_id": t.get("trade_id"), "pair": t.get("pair"),
        "symbol": t.get("symbol"), "side": t.get("side"), "open_utc": t.get("open_utc"),
        "close_utc": t.get("close_utc"), "close_kst": t.get("close_kst"), "close_ts": t.get("close_ts"),
        "pnl_usdt": t.get("pnl_usdt"), "pnl_pct_margin": t.get("pnl_pct_margin"),
        "exit_reason": t.get("exit_reason"), "exit_category": t.get("exit_category"),
        "exit_label_ko": t.get("exit_label_ko"), "driver": v.get("driver"), "verdict_by": v.get("by"),
        "status": doc.get("status"),
        "events_sourced": sum(1 for e in events if e.get("evidence") == "sourced"),
        "events_guess": sum(1 for e in events if e.get("evidence") != "sourced"),
        "json": f"{doc['key']}.json", "md": f"{doc['key']}.md",
    }


def _index(directory: Path) -> dict:
    rows = sorted((_row(doc) for doc in report_docs(directory)),
                  key=lambda row: (row["close_ts"] or 0, row["key"]), reverse=True)
    return {"schema": "postmortem-index/1", "reports": rows}


def rebuild_index(directory: Path) -> dict:
    index = _index(directory)
    if directory.is_dir():
        _atomic_write(directory / INDEX_NAME, json.dumps(index, ensure_ascii=False, indent=1) + "\n")
    return index


def read_index(directory: Path, persist: bool = True) -> dict:
    """The stored index; rebuilt from the report files when missing or broken (saved only if persist)."""
    try:
        index = json.loads((directory / INDEX_NAME).read_text(encoding="utf-8"))
        if isinstance(index, dict) and isinstance(index.get("reports"), list):
            return index
    except (OSError, ValueError, UnicodeError):
        pass
    return rebuild_index(directory) if persist else _index(directory)
