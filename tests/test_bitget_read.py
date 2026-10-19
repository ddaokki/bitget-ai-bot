import pytest

from bitgetbot.bitget_read import BitgetReader, classify_account, make_exchange, num
from bitgetbot.models import ContractSpec, PositionSnapshot


class FakeEx:
    """Return raw Bitget responses without any network access."""

    def __init__(self, *, positions=None, plans=None, orders=None, accounts=None,
                 contracts=None, v3=None, v2_raises=None, v3_raises=None):
        self.positions = positions
        self.plans = plans
        self.orders = orders
        self.accounts = accounts
        self.contracts = contracts
        self.v3 = v3
        self.v2_raises = v2_raises
        self.v3_raises = v3_raises
        self.calls = []

    def privateMixGetV2MixPositionAllPosition(self, params):
        self.calls.append(("positions", params))
        return self.positions

    def privateMixGetV2MixOrderOrdersPlanPending(self, params):
        self.calls.append(("plans", params))
        return self.plans

    def privateMixGetV2MixOrderOrdersPending(self, params):
        self.calls.append(("orders", params))
        return self.orders

    def privateMixGetV2MixAccountAccounts(self, params):
        self.calls.append(("accounts", params))
        if self.v2_raises is not None:
            raise self.v2_raises
        return self.accounts or {"code": "00000", "data": []}

    def publicMixGetV2MixMarketContracts(self, params):
        self.calls.append(("contracts", params))
        return self.contracts

    def privateUtaGetV3AccountSettings(self):
        self.calls.append(("v3", {}))
        if self.v3_raises is not None:
            raise self.v3_raises
        return self.v3 or {"code": "00000", "data": {}}


class FillPriceEx:
    def __init__(self, fills=None, prices=None, last="112050", price_error=False):
        self.fills = fills
        self.prices = prices
        self.last = last
        self.price_error = price_error
        self.calls = []

    def privateMixGetV2MixOrderFills(self, params):
        self.calls.append(("fills", params))
        return {"data": self.fills}

    def publicMixGetV2MixMarketSymbolPrice(self, params):
        self.calls.append(("price", params))
        if self.price_error:
            raise RuntimeError("secret in upstream error")
        return {"data": self.prices}

    def fetch_ticker(self, pair):
        self.calls.append(("ticker", pair))
        return {"last": self.last}


def test_fetch_fills_parameters_and_rows():
    rows = [{"tradeId": "t1"}]
    ex = FillPriceEx(fills={"fillList": rows})
    assert BitgetReader(ex).fetch_fills("BTCUSDT", 1792396800000) == rows
    assert ex.calls == [("fills", {
        "productType": "USDT-FUTURES", "symbol": "BTCUSDT",
        "startTime": "1792396800000",
    })]


@pytest.mark.parametrize("data", [None, {}, {"fillList": None}, {"fillList": []}])
def test_fetch_fills_empty_when_missing(data):
    assert BitgetReader(FillPriceEx(fills=data)).fetch_fills("BTCUSDT", 1) == []


def test_fetch_mark_price_prefers_public_mark():
    ex = FillPriceEx(prices=[{"markPrice": "112050.5"}])
    assert BitgetReader(ex).fetch_mark_price("BTCUSDT") == 112050.5
    assert ex.calls == [("price", {"productType": "USDT-FUTURES", "symbol": "BTCUSDT"})]


@pytest.mark.parametrize("data", [None, [], [{}], [{"markPrice": "nan"}],
                                  [{"markPrice": "inf"}], [{"markPrice": "0"}],
                                  [{"markPrice": "-2"}], [{"markPrice": "bad"}]])
def test_fetch_mark_price_falls_back_to_last(data):
    ex = FillPriceEx(prices=data)
    assert BitgetReader(ex).fetch_mark_price("BTCUSDT") == 112050.0
    assert ex.calls[-1] == ("ticker", "BTC/USDT:USDT")


def test_fetch_mark_price_falls_back_after_public_error():
    ex = FillPriceEx(price_error=True)
    assert BitgetReader(ex).fetch_mark_price("BTCUSDT") == 112050.0


@pytest.mark.parametrize("last", [None, "nan", "inf", 0, -1, "bad"])
def test_fetch_mark_price_raises_if_both_invalid(last):
    with pytest.raises(ValueError):
        BitgetReader(FillPriceEx(prices=[], last=last)).fetch_mark_price("BTCUSDT")


def test_make_exchange_uses_classic_swap_options():
    exchange = make_exchange("test-key", "test-secret", "test-password")
    assert exchange.apiKey == "test-key"
    assert exchange.secret == "test-secret"
    assert exchange.password == "test-password"
    assert exchange.enableRateLimit is True
    assert exchange.options["defaultType"] == "swap"
    assert exchange.options["uta"] is False


def test_parse_position_long_isolated():
    ex = FakeEx(positions={"code": "00000", "data": [{
        "symbol": "BTCUSDT", "holdSide": "long", "total": "0.0001",
        "openPriceAvg": "111240.5", "markPrice": "112050",
        "liquidationPrice": "56300", "leverage": "2", "marginMode": "isolated",
    }]})
    position = BitgetReader(ex).fetch_position("BTCUSDT")
    assert position == PositionSnapshot(
        "BTCUSDT", "long", 0.0001, 111240.5, 112050.0, 56300.0, 2.0, "isolated",
    )
    assert ex.calls == [("positions", {
        "productType": "USDT-FUTURES", "marginCoin": "USDT",
    })]


def test_position_side_and_margin_mode_normalized():
    ex = FakeEx(positions={"code": "00000", "data": [{
        "symbol": "ETHUSDT", "holdSide": "SHORT", "total": "1",
        "openPriceAvg": "3000", "markPrice": "2990",
        "liquidationPrice": "4500", "leverage": "2", "marginMode": "ISOLATED",
    }]})
    position = BitgetReader(ex).fetch_positions()[0]
    assert (position.side, position.margin_mode) == ("short", "isolated")


@pytest.mark.parametrize("liquidation", ["", "0", "-1", None, "invalid"])
def test_parse_position_blank_liq_is_none(liquidation):
    ex = FakeEx(positions={"code": "00000", "data": [{
        "symbol": "BTCUSDT", "holdSide": "long", "total": "0.0001",
        "openPriceAvg": "111240.5", "markPrice": "",
        "liquidationPrice": liquidation, "leverage": "2", "marginMode": "isolated",
    }]})
    position = BitgetReader(ex).fetch_position("BTCUSDT")
    assert position.liq_price is None and position.mark_price is None


def test_zero_total_position_is_skipped():
    ex = FakeEx(positions={"code": "00000", "data": [{
        "symbol": "BTCUSDT", "holdSide": "long", "total": "0",
    }]})
    assert BitgetReader(ex).fetch_position("BTCUSDT") is None


def test_fetch_position_does_not_return_other_symbol():
    ex = FakeEx(positions={"code": "00000", "data": [{
        "symbol": "ETHUSDT", "holdSide": "long", "total": "1",
        "openPriceAvg": "3000",
    }]})
    assert BitgetReader(ex).fetch_position("BTCUSDT") is None


def test_stop_orders_filtered_to_loss_types():
    ex = FakeEx(plans={"code": "00000", "data": {"entrustedList": [
        {"planType": "pos_loss", "triggerPrice": "108900",
         "triggerType": "mark_price", "size": "", "orderId": "9"},
        {"planType": "pos_profit", "triggerPrice": "130000",
         "triggerType": "mark_price", "size": "", "orderId": "10"},
        {"planType": "loss_plan", "triggerPrice": "108000",
         "triggerType": "fill_price", "size": "0.0001", "orderId": "11"},
    ]}})
    orders = BitgetReader(ex).fetch_stop_orders("BTCUSDT")
    assert [(o.symbol, o.plan_type, o.trigger_price, o.trigger_type, o.size, o.order_id)
            for o in orders] == [
        ("BTCUSDT", "pos_loss", 108900.0, "mark_price", None, "9"),
        ("BTCUSDT", "loss_plan", 108000.0, "fill_price", 0.0001, "11"),
    ]
    assert ex.calls == [("plans", {
        "productType": "USDT-FUTURES", "planType": "profit_loss", "symbol": "BTCUSDT",
    })]


@pytest.mark.parametrize("data", [None, {}, {"entrustedList": []}])
def test_stop_orders_empty_list_when_data_null(data):
    ex = FakeEx(plans={"code": "00000", "data": data})
    assert BitgetReader(ex).fetch_stop_orders("BTCUSDT") == []


@pytest.mark.parametrize("data, expected", [
    (None, 0), ({}, 0), ({"entrustedList": []}, 0),
    ({"entrustedList": [{"orderId": "1"}, {"orderId": "2"}]}, 2),
])
def test_open_order_count(data, expected):
    ex = FakeEx(orders={"code": "00000", "data": data})
    assert BitgetReader(ex).fetch_open_order_count() == expected
    assert ex.calls == [("orders", {"productType": "USDT-FUTURES"})]


@pytest.mark.parametrize("symbol", [None, "BTCUSDT"])
def test_plan_order_count_sums_all_plan_types(monkeypatch, symbol):
    ex = FakeEx()
    responses = {
        "normal_plan": {"data": {"entrustedList": [{"orderId": "1"}, {"orderId": "2"}]}},
        "track_plan": {"data": {"entrustedList": [{"orderId": "3"}]}},
        "profit_loss": {"data": {"entrustedList": [{"orderId": "4"}]}},
    }

    def pending(params):
        ex.calls.append(("plans", params))
        return responses[params["planType"]]

    monkeypatch.setattr(ex, "privateMixGetV2MixOrderOrdersPlanPending", pending)
    assert BitgetReader(ex).fetch_plan_order_count(symbol) == 4
    assert ex.calls == [
        ("plans", {"productType": "USDT-FUTURES", "planType": plan_type,
                   **({"symbol": symbol} if symbol is not None else {})})
        for plan_type in ("normal_plan", "track_plan", "profit_loss")
    ]


@pytest.mark.parametrize("response", [{}, {"data": None}, {"data": {}},
                                       {"data": {"entrustedList": None}},
                                       {"data": {"entrustedList": []}}])
def test_plan_order_count_empty_when_data_missing_or_null(response):
    ex = FakeEx(plans=response)
    assert BitgetReader(ex).fetch_plan_order_count() == 0
    assert len(ex.calls) == 3


def test_usdt_available_selects_margin_coin():
    ex = FakeEx(accounts={"code": "00000", "data": [
        {"marginCoin": "USDC", "available": "999"},
        {"marginCoin": "USDT", "available": "12.34"},
    ]})
    assert BitgetReader(ex).fetch_usdt_available() == 12.34
    assert ex.calls == [("accounts", {"productType": "USDT-FUTURES"})]


def test_usdt_available_missing_coin_raises():
    with pytest.raises(ValueError):
        BitgetReader(FakeEx()).fetch_usdt_available()


@pytest.mark.parametrize("v2_ok, v3_ok, expected", [
    (True, False, "classic"), (False, True, "uta"),
    (True, True, "unknown"), (False, False, "unknown"),
])
def test_classify_account(v2_ok, v3_ok, expected):
    assert classify_account({
        "v2_ok": v2_ok, "v3_ok": v3_ok, "v3_data": None, "v3_error": "Exception: 40084",
    }) == expected


@pytest.mark.parametrize("error", [None, "RequestTimeout", "NetworkError", "Exception"])
def test_classify_account_v3_failure_without_numeric_code_is_unknown(error):
    assert classify_account({"v2_ok": True, "v3_ok": False, "v3_error": error}) == "unknown"


@pytest.mark.parametrize("error", [TimeoutError("timeout"), ConnectionError("network")])
def test_account_probe_network_error_is_not_classic(error):
    report = BitgetReader(FakeEx(v3_raises=error)).account_mode_report()
    assert classify_account(report) == "unknown"


def test_account_report_success():
    ex = FakeEx(v3={"code": "00000", "data": {"accountMode": "advanced"}})
    assert BitgetReader(ex).account_mode_report() == {
        "v2_ok": True, "v3_ok": True,
        "v3_data": {"accountMode": "advanced"}, "v3_error": None,
    }
    assert ex.calls == [
        ("accounts", {"productType": "USDT-FUTURES"}), ("v3", {}),
    ]


@pytest.mark.parametrize("message, expected", [
    ('bitget {"code":"40084","msg":"x"} apiKey=SECRETKEY', "Exception: 40084"),
    ("apiKey=SECRETKEY", "Exception"),
])
def test_account_report_error_has_no_secret(message, expected):
    ex = FakeEx(v3_raises=Exception(message))
    report = BitgetReader(ex).account_mode_report()
    assert report == {
        "v2_ok": True, "v3_ok": False, "v3_data": None, "v3_error": expected,
    }
    assert "SECRETKEY" not in repr(report)


def test_account_report_probes_v3_after_v2_failure():
    ex = FakeEx(v2_raises=RuntimeError("SECRETKEY"))
    assert BitgetReader(ex).account_mode_report() == {
        "v2_ok": False, "v3_ok": True, "v3_data": {}, "v3_error": None,
    }


def test_contract_spec_selects_requested_symbol():
    ex = FakeEx(contracts={"code": "00000", "data": [
        {"symbol": "ETHUSDT", "minTradeNum": "0.01",
         "sizeMultiplier": "0.01", "minTradeUSDT": "5"},
        {"symbol": "BTCUSDT", "minTradeNum": "0.0001",
         "sizeMultiplier": "0.0001", "minTradeUSDT": "5"},
    ]})
    assert BitgetReader(ex).fetch_contract("BTCUSDT") == ContractSpec(
        "BTCUSDT", 0.0001, 0.0001, 5.0,
    )
    assert ex.calls == [("contracts", {
        "productType": "USDT-FUTURES", "symbol": "BTCUSDT",
    })]


def test_contract_missing_symbol_raises():
    ex = FakeEx(contracts={"code": "00000", "data": []})
    with pytest.raises(ValueError):
        BitgetReader(ex).fetch_contract("BTCUSDT")


@pytest.mark.parametrize("value, expected", [
    (None, None), ("", None), ("invalid", None), ({}, None),
    ("0", 0.0), ("-1", -1.0), ("0.0001", 0.0001), (2, 2.0),
])
def test_num(value, expected):
    assert num(value) == expected
