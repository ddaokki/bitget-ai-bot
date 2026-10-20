"""Synchronous Freqtrade REST API client."""

from typing import Literal

import httpx


class FreqtradeError(Exception):
    """An unsuccessful HTTP response from Freqtrade."""

    status_code: int
    body: str

    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Freqtrade HTTP {status_code} 오류")


class FreqtradeUnreachable(Exception):
    """Freqtrade could not be reached or did not respond in time."""


class FreqtradeClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ):
        self._password = password
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            auth=(username, password),
            transport=transport,
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = self._client.request(method, f"/api/v1/{path}", **kwargs)
        except httpx.RequestError:
            raise FreqtradeUnreachable("Freqtrade에 연결할 수 없습니다") from None
        if not response.is_success:
            body = response.text
            if self._password:
                body = body.replace(self._password, "[REDACTED]")
            raise FreqtradeError(response.status_code, body)
        return response

    def ping(self) -> bool:
        try:
            self._request("GET", "ping", auth=None)
        except FreqtradeUnreachable:
            return False
        return True

    def health(self) -> dict:
        return self._request("GET", "health").json()

    def show_config(self) -> dict:
        return self._request("GET", "show_config").json()

    def status(self) -> list[dict]:
        return self._request("GET", "status").json()

    def profit(self) -> dict:
        return self._request("GET", "profit").json()

    def trades(self, limit: int = 500) -> dict:
        # order_by_id=false makes Freqtrade return the most recently closed trades first.
        return self._request(
            "GET", "trades", params={"limit": limit, "order_by_id": "false"},
        ).json()

    def balance(self) -> dict:
        return self._request("GET", "balance").json()

    def pair_candles(self, pair: str, timeframe: str, limit: int) -> dict:
        # Read-only: the analyzed candles the running strategy already holds in memory.
        return self._request(
            "GET", "pair_candles", params={"pair": pair, "timeframe": timeframe, "limit": limit},
        ).json()

    def start(self) -> dict:
        return self._request("POST", "start").json()

    def stop(self) -> dict:
        return self._request("POST", "stop").json()

    def pause(self) -> dict:
        return self._request("POST", "pause").json()

    def force_enter(
        self,
        pair: str,
        side: Literal["long", "short"],
        stake_amount: float,
        leverage: float,
        ordertype: str = "market",
        entry_tag: str | None = None,
    ) -> dict:
        return self._request("POST", "forceenter", json={
            "pair": pair,
            "side": side,
            "ordertype": ordertype,
            "stakeamount": stake_amount,
            "leverage": leverage,
            "entry_tag": entry_tag,
        }).json()

    def force_exit(self, trade_id: int | str, ordertype: str = "market") -> dict:
        return self._request("POST", "forceexit", json={
            "tradeid": trade_id,
            "ordertype": ordertype,
        }).json()
