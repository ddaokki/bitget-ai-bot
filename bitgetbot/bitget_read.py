"""Read-only access to classic Bitget USDT futures account snapshots."""

import math
import re
from typing import Literal, cast

import ccxt

from bitgetbot.models import ContractSpec, PlanOrder, PositionSnapshot


def make_exchange(key: str, secret: str, password: str) -> ccxt.bitget:
    return ccxt.bitget({
        "apiKey": key,
        "secret": secret,
        "password": password,
        "enableRateLimit": True,
        "options": {"defaultType": "swap", "uta": False},
    })


def num(x) -> float | None:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class BitgetReader:
    def __init__(self, exchange):
        self.exchange = exchange

    def fetch_positions(self) -> list[PositionSnapshot]:
        response = self.exchange.privateMixGetV2MixPositionAllPosition({
            "productType": "USDT-FUTURES", "marginCoin": "USDT",
        })
        positions = []
        for row in response.get("data") or []:
            size = float(row["total"])
            if size == 0:
                continue
            liq_price = num(row.get("liquidationPrice"))
            if liq_price is not None and liq_price <= 0:
                liq_price = None
            margin_mode = row.get("marginMode")
            positions.append(PositionSnapshot(
                symbol=row["symbol"],
                side=cast(Literal["long", "short"], row["holdSide"].lower()),
                size=size,
                entry_price=float(row["openPriceAvg"]),
                mark_price=num(row.get("markPrice")),
                liq_price=liq_price,
                leverage=num(row.get("leverage")),
                margin_mode=margin_mode.lower() if margin_mode is not None else None,
            ))
        return positions

    def fetch_position(self, symbol: str) -> PositionSnapshot | None:
        return next((pos for pos in self.fetch_positions() if pos.symbol == symbol), None)

    def fetch_stop_orders(self, symbol: str) -> list[PlanOrder]:
        response = self.exchange.privateMixGetV2MixOrderOrdersPlanPending({
            "productType": "USDT-FUTURES", "planType": "profit_loss", "symbol": symbol,
        })
        data = response.get("data") or {}
        return [
            PlanOrder(
                symbol=symbol,
                plan_type=row["planType"],
                trigger_price=float(row["triggerPrice"]),
                trigger_type=row["triggerType"],
                size=num(row.get("size")),
                order_id=row["orderId"],
            )
            for row in data.get("entrustedList") or []
            if row["planType"] in {"pos_loss", "loss_plan"}
        ]

    def fetch_open_order_count(self) -> int:
        response = self.exchange.privateMixGetV2MixOrderOrdersPending({
            "productType": "USDT-FUTURES",
        })
        data = response.get("data") or {}
        return len(data.get("entrustedList") or [])

    def fetch_plan_order_count(self, symbol: str | None = None) -> int:
        count = 0
        for plan_type in ("normal_plan", "track_plan", "profit_loss"):
            params = {"productType": "USDT-FUTURES", "planType": plan_type}
            if symbol is not None:
                params["symbol"] = symbol
            response = self.exchange.privateMixGetV2MixOrderOrdersPlanPending(params)
            data = response.get("data") or {}
            count += len(data.get("entrustedList") or [])
        return count

    def fetch_usdt_available(self) -> float:
        response = self.exchange.privateMixGetV2MixAccountAccounts({
            "productType": "USDT-FUTURES",
        })
        for row in response.get("data") or []:
            if row["marginCoin"] == "USDT":
                return float(row["available"])
        raise ValueError("USDT 계정 잔액을 찾을 수 없습니다")

    def fetch_contract(self, symbol: str) -> ContractSpec:
        response = self.exchange.publicMixGetV2MixMarketContracts({
            "productType": "USDT-FUTURES", "symbol": symbol,
        })
        for row in response.get("data") or []:
            if row["symbol"] == symbol:
                return ContractSpec(
                    symbol=symbol,
                    min_qty=float(row["minTradeNum"]),
                    size_step=float(row["sizeMultiplier"]),
                    min_notional_usdt=float(row["minTradeUSDT"]),
                )
        raise ValueError("요청한 심볼의 계약 정보를 찾을 수 없습니다")

    def fetch_fills(self, symbol: str, start_ms: int) -> list[dict]:
        response = self.exchange.privateMixGetV2MixOrderFills({
            "productType": "USDT-FUTURES", "symbol": symbol, "startTime": str(start_ms),
        })
        return (response.get("data") or {}).get("fillList") or []

    def fetch_mark_price(self, symbol: str) -> float:
        try:
            response = self.exchange.publicMixGetV2MixMarketSymbolPrice({
                "productType": "USDT-FUTURES", "symbol": symbol,
            })
            price = num((response.get("data") or [{}])[0].get("markPrice"))
            if price is not None and math.isfinite(price) and price > 0:
                return price
        except Exception:
            pass
        try:
            base = symbol.removesuffix("USDT")
            price = num(self.exchange.fetch_ticker(f"{base}/USDT:USDT").get("last"))
            if price is not None and math.isfinite(price) and price > 0:
                return price
        except Exception:
            pass
        raise ValueError("유효한 표시가격 또는 최근 체결가를 조회할 수 없습니다")

    def account_mode_report(self) -> dict:
        report = {"v2_ok": False, "v3_ok": False, "v3_data": None, "v3_error": None}
        try:
            self.exchange.privateMixGetV2MixAccountAccounts({
                "productType": "USDT-FUTURES",
            })
            report["v2_ok"] = True
        except Exception:
            pass
        try:
            response = self.exchange.privateUtaGetV3AccountSettings()
            report["v3_ok"] = True
            report["v3_data"] = response.get("data")
        except Exception as error:
            code = re.search(r'"code":"(\d+)"', str(error))
            report["v3_error"] = type(error).__name__
            if code is not None:
                report["v3_error"] += f": {code.group(1)}"
        return report


def classify_account(report: dict) -> Literal["classic", "uta", "unknown"]:
    if report["v2_ok"] and not report["v3_ok"]:
        return "classic"
    if not report["v2_ok"] and report["v3_ok"]:
        return "uta"
    return "unknown"
