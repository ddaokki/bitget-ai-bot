"""Convert read-only Bitget fills into a deduplicated USDT/KRW CSV ledger."""

import csv
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

import httpx


@dataclass(frozen=True)
class LedgerRow:
    ts_utc: str
    symbol: str
    side: str
    trade_side: str
    price: float
    size: float
    quote_usdt: float
    fee_usdt: float
    realized_pnl_usdt: float
    krw_per_usdt: float
    fx_at: str
    order_id: str
    trade_id: str


def _number(value) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError("장부 숫자 필드가 유효하지 않습니다") from None
    if not math.isfinite(result):
        raise ValueError("장부 숫자 필드가 유한수가 아닙니다")
    return result


def fills_to_rows(fills: list[dict], krw_per_usdt: float, fx_at: str) -> list[LedgerRow]:
    rate = _number(krw_per_usdt)
    if rate <= 0:
        raise ValueError("USDT/KRW 환율은 양수여야 합니다")
    rows = []
    for fill in fills:
        if not fill.get("tradeId") or not fill.get("orderId"):
            raise ValueError("체결 번호 또는 주문 번호가 없습니다")
        timestamp = datetime.fromtimestamp(_number(fill["cTime"]) / 1000, timezone.utc)
        rows.append(LedgerRow(
            ts_utc=timestamp.isoformat().replace("+00:00", "Z"),
            symbol=fill["symbol"],
            side=fill["side"],
            trade_side=fill["tradeSide"],
            price=_number(fill["price"]),
            size=_number(fill["baseVolume"]),
            quote_usdt=_number(fill["quoteVolume"]),
            fee_usdt=sum(
                abs(_number(fee["totalFee"])) for fee in fill.get("feeDetail") or []
                if fee.get("feeCoin") == "USDT"
            ),
            realized_pnl_usdt=_number(fill["profit"]),
            krw_per_usdt=rate,
            fx_at=fx_at,
            order_id=str(fill["orderId"]),
            trade_id=str(fill["tradeId"]),
        ))
    return rows


def append_csv(rows: list[LedgerRow], path: Path) -> int:
    if not rows:
        return 0
    names = [field.name for field in fields(LedgerRow)]
    seen = set()
    has_header = path.exists() and path.stat().st_size > 0
    if has_header:
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != names:
                raise ValueError("기존 장부 CSV 헤더가 일치하지 않습니다")
            seen = {row["trade_id"] for row in reader}
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("a", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=names)
        if not has_header:
            writer.writeheader()
        for row in rows:
            if row.trade_id not in seen:
                writer.writerow(asdict(row))
                seen.add(row.trade_id)
                count += 1
    return count


def fetch_krw_per_usdt(get: Callable | None = None) -> tuple[float, str]:
    url = "https://api.upbit.com/v1/ticker?markets=KRW-USDT"
    if get is None:
        response = httpx.get(url, timeout=10.0)
        response.raise_for_status()
        data = response.json()
    else:
        data = get(url)
    try:
        rate = _number(data[0]["trade_price"])
    except (KeyError, IndexError, TypeError):
        raise ValueError("USDT/KRW 환율 응답이 유효하지 않습니다") from None
    if rate <= 0:
        raise ValueError("USDT/KRW 환율은 양수여야 합니다")
    return rate, datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
