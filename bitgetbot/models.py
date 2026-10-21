"""Immutable snapshots used by exchange protection checks."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    side: Literal["long", "short"]
    size: float
    entry_price: float
    mark_price: float | None
    liq_price: float | None
    leverage: float | None
    margin_mode: str | None
    # Exchange-reported position margin and unrealized PnL (USDT); None when not reported.
    margin: float | None = None
    unrealized_pnl: float | None = None


@dataclass(frozen=True)
class PlanOrder:
    symbol: str
    plan_type: str
    trigger_price: float
    trigger_type: str
    size: float | None
    order_id: str


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool | None
    detail: str


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    min_qty: float
    size_step: float
    min_notional_usdt: float
