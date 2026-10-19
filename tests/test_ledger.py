import csv
from dataclasses import replace

import pytest

from bitgetbot.ledger import append_csv, fetch_krw_per_usdt, fills_to_rows


FILL = {"tradeId": "t1", "orderId": "o1", "symbol": "BTCUSDT", "side": "buy",
        "tradeSide": "open", "price": "111240.5", "baseVolume": "0.0001",
        "quoteVolume": "11.12405", "profit": "0", "cTime": "1792396800000",
        "feeDetail": [{"feeCoin": "USDT", "totalFee": "-0.00667443"}]}


def test_fill_to_row_values():
    [r] = fills_to_rows([FILL], 1390.1, "2026-10-19T10:00:00Z")
    assert (r.trade_id, r.price, r.size, r.quote_usdt, r.fee_usdt, r.krw_per_usdt) == (
        "t1", 111240.5, 0.0001, 11.12405, 0.00667443, 1390.1,
    )
    assert r.ts_utc == "2026-10-19T08:00:00Z"
    assert (r.symbol, r.side, r.trade_side, r.order_id, r.realized_pnl_usdt) == (
        "BTCUSDT", "buy", "open", "o1", 0.0,
    )
    assert r.fx_at == "2026-10-19T10:00:00Z"


def test_fees_only_usdt_and_realized_loss_preserved():
    [row] = fills_to_rows([{**FILL, "profit": "-0.5", "feeDetail": [
        {"feeCoin": "BGB", "totalFee": "-10"},
        {"feeCoin": "USDT", "totalFee": "-0.01"},
        {"feeCoin": "USDT", "totalFee": "0.02"},
    ]}], 1390.1, "x")
    assert row.fee_usdt == pytest.approx(0.03)
    assert row.realized_pnl_usdt == -0.5


def test_append_csv_skips_duplicate_trade_ids(tmp_path):
    rows = fills_to_rows([FILL], 1390.1, "x")
    path = tmp_path / "nested" / "l.csv"
    assert append_csv(rows + rows, path) == 1
    assert append_csv(rows, path) == 0
    assert append_csv([replace(rows[0], trade_id="t2")], path) == 1
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        saved = list(reader)
    assert len(saved) == 2
    assert saved[0]["fee_usdt"] == "0.00667443"
    assert saved[1]["trade_id"] == "t2"
    assert reader.fieldnames == [
        "ts_utc", "symbol", "side", "trade_side", "price", "size", "quote_usdt",
        "fee_usdt", "realized_pnl_usdt", "krw_per_usdt", "fx_at", "order_id", "trade_id",
    ]


def test_append_csv_empty_does_not_create_file(tmp_path):
    path = tmp_path / "empty.csv"
    assert append_csv([], path) == 0
    assert not path.exists()


def test_fetch_krw_per_usdt_uses_trade_price():
    urls = []

    def get(url):
        urls.append(url)
        return [{"market": "KRW-USDT", "trade_price": 1390.1}]

    rate, at = fetch_krw_per_usdt(get=get)
    assert rate == 1390.1 and at.endswith("Z")
    assert urls == ["https://api.upbit.com/v1/ticker?markets=KRW-USDT"]


@pytest.mark.parametrize("price", [None, 0, -1, "nan", "inf", "invalid"])
def test_invalid_fx_rejected(price):
    with pytest.raises(ValueError):
        fetch_krw_per_usdt(get=lambda _: [{"trade_price": price}])


@pytest.mark.parametrize("field,value", [("price", "nan"), ("baseVolume", "inf"),
                                          ("tradeId", ""), ("cTime", "bad")])
def test_bad_fill_rejected(field, value):
    with pytest.raises(ValueError):
        fills_to_rows([{**FILL, field: value}], 1390.1, "x")
