"""Trade facts from a closed Freqtrade trade row, plus the scheduled US releases in its window.

Everything here is read from the bot's own API or the repository calendar, so it is "sourced".
"""

import csv
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

KEY_RE = re.compile(r"(dry|live)-[0-9]{1,12}-[0-9]{1,16}")
SMOKE_TAG = "smoke"  # bitgetbot.smoke test entries: not trading decisions, never reported
STRATEGY_TAG_PREFIX = "safe_"
KST = timezone(timedelta(hours=9))
DAY_MS = 86_400_000

# Freqtrade exit_reason -> (category, Korean label). Custom exits and exit tags arrive verbatim.
EXITS = {
    "user_take_profit": ("user_take_profit", "사용자 익절가 도달"),
    "stoploss_on_exchange": ("stop", "거래소 손절"),
    "stop_loss": ("stop", "봇 손절"),
    "trailing_stop_loss": ("stop", "추적 손절"),
    "liquidation": ("liquidation", "강제 청산(청산가 도달)"),
    "opposite_donchian_10": ("strategy", "전략 청산(4시간봉이 직전 10봉 저점 아래 마감)"),
    "chandelier_stop": ("strategy", "전략 청산(추적 손절선이 현재가 위로 올라감)"),
    "exit_signal": ("strategy", "전략 청산 신호"),
    "roi": ("strategy", "목표 수익(ROI) 도달"),
    "custom_exit": ("strategy", "전략 사용자 청산"),
    "force_exit": ("manual", "수동 종료(패널 시장가 종료 또는 긴급 청산)"),
    "emergency_exit": ("bot_emergency", "거래 봇 비상 청산"),
}
OTHER_EXIT = ("other", "기타 청산")
MANUAL_LABELS = {
    "panel_close": "패널 시장가 종료",
    "emergency": "패널 긴급 청산",
    "unknown": "수동 종료(패널 시장가 종료인지 긴급 청산인지 확인 못 함)",
}
# A force exit is requested shortly before the close is recorded.
AUDIT_BEFORE_MS = 10 * 60_000
AUDIT_AFTER_MS = 2 * 60_000

CALENDAR_SOURCES = {
    "FOMC": ("monetary_policy", "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"),
    "CPI": ("macro_data", "https://www.bls.gov/schedule/news_release/cpi.htm"),
    "PPI": ("macro_data", "https://www.bls.gov/schedule/news_release/ppi.htm"),
    "NFP": ("macro_data", "https://www.bls.gov/schedule/news_release/empsit.htm"),
}


def report_key(mode: str, trade_id: int, open_ts: int) -> str:
    """Dry and live databases both number trades from 1, so the key carries mode and open time."""
    return f"{mode}-{trade_id}-{open_ts}"


def iso_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_kst(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, KST).strftime("%Y-%m-%d %H:%M KST")


def parse_utc(text) -> int | None:
    """ISO-8601 text -> epoch ms (naive text is UTC); None when it cannot be read."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        value = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _positive(value) -> float | None:
    result = number(value)
    return result if result is not None and result > 0 else None


def _ms(value) -> int | None:
    return value if type(value) is int and value > 0 else None


def closed_trades(history, now_ms: int, backfill_days: int = 30) -> list[dict]:
    """Closed, non-smoke trades closed within the backfill window, oldest close first."""
    rows = history.get("trades") if isinstance(history, dict) else None
    if not isinstance(rows, list):
        return []
    cutoff = now_ms - backfill_days * DAY_MS
    found = []
    for row in rows:
        if not isinstance(row, dict) or row.get("is_open") is True:
            continue
        trade_id, open_ts, close_ts = row.get("trade_id"), _ms(row.get("open_timestamp")), _ms(row.get("close_timestamp"))
        if type(trade_id) is not int or trade_id <= 0 or open_ts is None or close_ts is None:
            continue
        if row.get("enter_tag") == SMOKE_TAG or close_ts < cutoff:
            continue
        found.append(row)
    return sorted(found, key=lambda r: (r["close_timestamp"], r["trade_id"]))


def _excursions(short: bool, entry, high, low) -> tuple[float | None, float | None]:
    """Best and worst price move against entry while held, in price % (not leveraged)."""
    if entry is None:
        return None, None
    if short:
        mfe = (1 - low / entry) * 100 if low is not None else None
        mae = (1 - high / entry) * 100 if high is not None else None
    else:
        mfe = (high / entry - 1) * 100 if high is not None else None
        mae = (low / entry - 1) * 100 if low is not None else None
    return mfe, mae


def trade_facts(row: dict, mode: str) -> dict:
    trade_id, open_ts, close_ts = row["trade_id"], row["open_timestamp"], row["close_timestamp"]
    pair = row.get("pair") if isinstance(row.get("pair"), str) else ""
    symbol = pair.split(":")[0].replace("/", "")
    short = row.get("is_short") is True
    tag = row.get("enter_tag")
    reason = row.get("exit_reason") if isinstance(row.get("exit_reason"), str) else None
    category, label = EXITS.get(reason, OTHER_EXIT)
    entry, exit_price = _positive(row.get("open_rate")), _positive(row.get("close_rate"))
    margin, pnl = _positive(row.get("stake_amount")), number(row.get("profit_abs"))
    mfe, mae = _excursions(short, entry, _positive(row.get("max_rate")), _positive(row.get("min_rate")))
    return {
        "trade_id": trade_id, "key": report_key(mode, trade_id, open_ts), "mode": mode,
        "pair": pair, "symbol": symbol if re.fullmatch(r"[A-Z0-9]{1,20}USDT", symbol) else None,
        "side": "short" if short else "long",
        "kind": "strategy" if isinstance(tag, str) and tag.startswith(STRATEGY_TAG_PREFIX) else "manual",
        "enter_tag": tag if isinstance(tag, str) else None,
        "exit_reason": reason, "exit_category": category, "exit_label_ko": label,
        "open_ts": open_ts, "close_ts": close_ts,
        "open_utc": iso_utc(open_ts), "open_kst": iso_kst(open_ts),
        "close_utc": iso_utc(close_ts), "close_kst": iso_kst(close_ts),
        "hold_minutes": round((close_ts - open_ts) / 60_000),
        "entry_price": entry, "exit_price": exit_price,
        "amount": _positive(row.get("amount")), "leverage": _positive(row.get("leverage")),
        "margin_usdt": margin, "pnl_usdt": pnl,
        "pnl_pct_margin": pnl / margin * 100 if pnl is not None and margin is not None else None,
        "profit_ratio": number(row.get("profit_ratio")),
        "funding_usdt": number(row.get("funding_fees")),
        "max_rate": _positive(row.get("max_rate")), "min_rate": _positive(row.get("min_rate")),
        "mfe_pct": mfe, "mae_pct": mae,
        "stop_loss_abs": _positive(row.get("stop_loss_abs")),
        "initial_stop_loss_abs": _positive(row.get("initial_stop_loss_abs")),
        "source": "freqtrade_api",
    }


def _audit_entries(path: Path):
    try:
        with open(path, encoding="utf-8") as log:
            for line in log:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    yield entry
    except OSError:
        return


def refine_manual_exit(facts: dict, audit_path: Path) -> dict:
    """Tell a panel market close from an emergency close using the panel's own audit log."""
    if facts.get("exit_category") != "manual":
        return facts
    detail = "unknown"
    close_ts = facts["close_ts"]
    for entry in _audit_entries(audit_path):
        at = parse_utc(entry.get("ts"))
        if at is None or entry.get("result") != 200:
            continue
        if not close_ts - AUDIT_BEFORE_MS <= at <= close_ts + AUDIT_AFTER_MS:
            continue
        if entry.get("action") == "trade_close" and entry.get("step") == str(facts["trade_id"]):
            detail = "panel_close"
        elif entry.get("action") == "emergency" and detail == "unknown":
            detail = "emergency"
    return {**facts, "exit_detail": detail, "exit_label_ko": MANUAL_LABELS[detail]}


def calendar_events(csv_path: Path, start_ms: int, end_ms: int) -> list[dict]:
    """Scheduled US releases from the repository calendar that fall inside [start, end]."""
    try:
        with open(csv_path, encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except OSError:
        return []
    events = []
    for row in rows:
        at = parse_utc(row.get("ts_utc"))
        category = row.get("category")
        if at is None or category not in CALENDAR_SOURCES or not start_ms <= at <= end_ms:
            continue
        tag, url = CALENDAR_SOURCES[category]
        events.append({
            "ts_utc": iso_utc(at), "ts": at, "category": category, "tag": tag,
            "title": (row.get("title") or category)[:200], "region": "US", "scheduled": True,
            "source": "upcoming_us_macro.csv", "source_url": url,
        })
    return sorted(events, key=lambda e: e["ts"])
