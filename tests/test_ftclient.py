import base64
import json
import traceback

import httpx
import pytest

from bitgetbot.ftclient import FreqtradeClient, FreqtradeError, FreqtradeUnreachable


def test_force_enter_sends_payload_and_basic_auth():
    seen = {}

    def handler(req):
        assert req.method == "POST"
        seen["url"] = str(req.url)
        seen["auth"] = req.headers["authorization"]
        seen["json"] = json.loads(req.content)
        return httpx.Response(200, json={"trade_id": 1})

    client = FreqtradeClient(
        "http://127.0.0.1:8080", "u", "p", transport=httpx.MockTransport(handler),
    )
    assert client.force_enter(
        "BTC/USDT:USDT", "long", 7.0, 2, entry_tag="smoke",
    ) == {"trade_id": 1}
    assert seen["url"] == "http://127.0.0.1:8080/api/v1/forceenter"
    assert seen["auth"] == "Basic " + base64.b64encode(b"u:p").decode()
    assert seen["json"] == {
        "pair": "BTC/USDT:USDT", "side": "long", "ordertype": "market",
        "stakeamount": 7.0, "leverage": 2, "entry_tag": "smoke",
    }


def test_force_enter_short_with_optional_defaults():
    def handler(req):
        assert json.loads(req.content) == {
            "pair": "BTC/USDT:USDT", "side": "short", "ordertype": "limit",
            "stakeamount": 7.0, "leverage": 2, "entry_tag": None,
        }
        return httpx.Response(200, json={"trade_id": 2})

    client = FreqtradeClient(
        "http://127.0.0.1:8080", "u", "p", transport=httpx.MockTransport(handler),
    )
    assert client.force_enter(
        "BTC/USDT:USDT", "short", 7.0, 2, ordertype="limit",
    ) == {"trade_id": 2}


@pytest.mark.parametrize("trade_id, ordertype", [(5, "market"), ("5", "limit")])
def test_force_exit_payload(trade_id, ordertype):
    def handler(req):
        assert req.method == "POST"
        assert req.url.path == "/api/v1/forceexit"
        assert json.loads(req.content) == {"tradeid": trade_id, "ordertype": ordertype}
        return httpx.Response(200, json={"result": "closed"})

    client = FreqtradeClient(
        "http://127.0.0.1:8080", "u", "p", transport=httpx.MockTransport(handler),
    )
    if ordertype == "market":
        assert client.force_exit(trade_id) == {"result": "closed"}
    else:
        assert client.force_exit(trade_id, ordertype) == {"result": "closed"}


@pytest.mark.parametrize("method, verb, payload", [
    ("show_config", "GET", {"state": "stopped"}),
    ("health", "GET", {"bot_startup_ts": 1792396800}),
    ("health", "GET", {"bot_startup_ts": None}),
    ("status", "GET", [{"trade_id": 5}]),
    ("profit", "GET", {"closed_trade_count": 3, "winning_trades": 2, "winrate": 0.66}),
    ("start", "POST", {"status": "starting trader ..."}),
    ("stop", "POST", {"status": "stopping trader ..."}),
    ("pause", "POST", {"status": "pausing trader ..."}),
    ("balance", "GET", {"total": 105.5, "stake": "USDT"}),
])
@pytest.mark.parametrize("base_url", ["http://127.0.0.1:8080", "http://127.0.0.1:8080/"])
def test_authenticated_routes(method, verb, payload, base_url):
    def handler(req):
        assert req.method == verb
        assert str(req.url) == f"http://127.0.0.1:8080/api/v1/{method}"
        assert req.headers["authorization"] == "Basic dTpw"
        return httpx.Response(200, json=payload)

    client = FreqtradeClient(base_url, "u", "p", transport=httpx.MockTransport(handler))
    assert getattr(client, method)() == payload


@pytest.mark.parametrize("limit", [None, 50])
def test_trades_requests_newest_closed_trades_with_auth(limit):
    payload = {"trades": [{"trade_id": 1}], "trades_count": 1, "offset": 0, "total_trades": 1}

    def handler(req):
        assert req.method == "GET"
        assert req.url.path == "/api/v1/trades"
        assert dict(req.url.params) == {
            "limit": str(500 if limit is None else limit), "order_by_id": "false",
        }
        assert req.headers["authorization"] == "Basic dTpw"
        return httpx.Response(200, json=payload)

    client = FreqtradeClient(
        "http://127.0.0.1:8080", "u", "p", transport=httpx.MockTransport(handler),
    )
    assert (client.trades() if limit is None else client.trades(limit)) == payload


def test_ping_succeeds_without_authentication():
    def handler(req):
        assert req.method == "GET"
        assert req.url.path == "/api/v1/ping"
        assert "authorization" not in req.headers
        return httpx.Response(200, json={"status": "pong"})

    client = FreqtradeClient(
        "http://127.0.0.1:8080", "u", "p", transport=httpx.MockTransport(handler),
    )
    assert client.ping() is True


@pytest.mark.parametrize("status_code", [301, 401, 500])
def test_non_2xx_raises_ft_error_with_status(status_code):
    def handler(req):
        return httpx.Response(status_code, text="Unauthorized")

    client = FreqtradeClient(
        "http://127.0.0.1:8080", "u", "p", transport=httpx.MockTransport(handler),
    )
    with pytest.raises(FreqtradeError) as caught:
        client.status()
    assert caught.value.status_code == status_code
    assert caught.value.body == "Unauthorized"
    assert "p" not in str(caught.value)
    assert "p" not in repr(caught.value)


def test_error_body_does_not_expose_echoed_password():
    password = "test-password-do-not-disclose"

    def handler(req):
        return httpx.Response(400, text=f"Invalid password: {password}")

    client = FreqtradeClient(
        "http://127.0.0.1:8080", "u", password, transport=httpx.MockTransport(handler),
    )
    with pytest.raises(FreqtradeError) as caught:
        client.start()
    assert password not in caught.value.body
    assert password not in str(caught.value)
    assert password not in repr(caught.value)


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout])
def test_unreachable_raises_ft_unreachable(error_type):
    password = "test-password-do-not-disclose"

    def handler(req):
        raise error_type(password, request=req)

    client = FreqtradeClient(
        "http://127.0.0.1:8080", "u", password, transport=httpx.MockTransport(handler),
    )
    with pytest.raises(FreqtradeUnreachable) as caught:
        client.status()
    assert password not in str(caught.value)
    assert password not in repr(caught.value)
    assert password not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout])
def test_ping_false_when_unreachable(error_type):
    def handler(req):
        raise error_type("connection failed", request=req)

    client = FreqtradeClient(
        "http://127.0.0.1:8080", "u", "p", transport=httpx.MockTransport(handler),
    )
    assert client.ping() is False


def test_ping_non_2xx_raises_ft_error():
    client = FreqtradeClient(
        "http://127.0.0.1:8080", "u", "p",
        transport=httpx.MockTransport(lambda req: httpx.Response(503, text="Unavailable")),
    )
    with pytest.raises(FreqtradeError) as caught:
        client.ping()
    assert caught.value.status_code == 503


@pytest.mark.parametrize("timeout", [None, 3.5])
def test_timeout_is_applied_to_requests(timeout):
    def handler(req):
        expected = 10.0 if timeout is None else timeout
        assert req.extensions["timeout"] == {
            "connect": expected, "read": expected, "write": expected, "pool": expected,
        }
        return httpx.Response(200, json=[])

    kwargs = {} if timeout is None else {"timeout": timeout}
    client = FreqtradeClient(
        "http://127.0.0.1:8080", "u", "p", transport=httpx.MockTransport(handler), **kwargs,
    )
    assert client.status() == []


def test_pair_candles_is_an_authenticated_read_only_get():
    payload = {"columns": ["__date_ts", "close"], "data": [[1, 2.0]]}

    def handler(req):
        assert req.method == "GET"
        assert req.url.path == "/api/v1/pair_candles"
        assert dict(req.url.params) == {"pair": "BTC/USDT:USDT", "timeframe": "1h", "limit": "72"}
        assert req.headers["authorization"] == "Basic dTpw"
        return httpx.Response(200, json=payload)

    client = FreqtradeClient(
        "http://127.0.0.1:8080", "u", "p", transport=httpx.MockTransport(handler),
    )
    assert client.pair_candles("BTC/USDT:USDT", "1h", 72) == payload


@pytest.mark.parametrize("timescale", [None, 7])
def test_daily_is_an_authenticated_read_only_get(timescale):
    payload = {"data": [{"date": "2026-10-21", "abs_profit": 1.5}], "stake_currency": "USDT"}

    def handler(req):
        assert req.method == "GET"
        assert req.url.path == "/api/v1/daily"
        assert dict(req.url.params) == {"timescale": str(1 if timescale is None else timescale)}
        assert req.headers["authorization"] == "Basic dTpw"
        return httpx.Response(200, json=payload)

    client = FreqtradeClient(
        "http://127.0.0.1:8080", "u", "p", transport=httpx.MockTransport(handler),
    )
    assert (client.daily() if timescale is None else client.daily(timescale)) == payload
