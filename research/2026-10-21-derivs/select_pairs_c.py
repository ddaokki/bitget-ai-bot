"""Phase C universe: up to 10 coins (user rule: top 5-10 by volume, no new alts).

Public market data only. Run inside the Freqtrade image:
  python -P /research/select_pairs_c.py

Rule (fixed before any phase C result was seen):
  1. Active linear USDT perpetuals on Bitget, stable/fiat/pegged bases excluded.
  2. Pre-rank by 24h quote volume (one tickers call), keep the top 80.
  3. Rank those by summed quote volume over the last 30 complete UTC days.
  4. Walk down that ranking and keep a pair only if
     - Bitget serves a 1h candle at or before 2022-01-01T00:00Z (no new alts; daily
       EMA200 warmup), and
     - Binance USDT-M publishes daily "metrics" (open interest, long/short ratios)
       for it on 2022-01-01 (data.binance.vision). Phase C features need them.
  5. Stop at 10 pairs. BTC and ETH are always kept.

Same survivorship bias as phase B: today's winners applied to the past.
"""
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import ccxt

END = datetime(2026, 10, 20, tzinfo=timezone.utc)
END_MS = int(END.timestamp() * 1000)
START_MS = END_MS - 30 * 86400000
HISTORY_MS = int(datetime(2022, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
PRE_N = 80
MAX_PAIRS = 10
ALWAYS = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
STABLE_BASES = {"USDC", "USDT", "DAI", "TUSD", "FDUSD", "USDE", "USDD", "PYUSD", "USD1", "USDP", "FRAX", "EUR", "EURC", "EURI", "EURCV", "PAXG", "XAUT", "BUSD", "RLUSD"}
VISION = "https://data.binance.vision/data/futures/um/daily/metrics/{s}/{s}-metrics-2022-01-01.zip"
ROOT = Path(__file__).resolve().parent


def bitget_history(exchange, pair):
    candles = exchange.fetch_ohlcv(pair, "1h", HISTORY_MS - 6 * 3600000, 12)
    first = min((int(c[0]) for c in candles), default=None)
    return first is not None and first <= HISTORY_MS, (exchange.iso8601(first) if first else None)


def binance_symbol(base: str) -> str | None:
    """Binance USDT-M symbol with metrics on 2022-01-01, or None. Tries BASE and 1000BASE."""
    for symbol in (f"{base}USDT", f"1000{base}USDT"):
        request = urllib.request.Request(VISION.format(s=symbol), method="HEAD")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                if response.status == 200:
                    return symbol
        except urllib.error.HTTPError:
            continue
    return None


def main():
    exchange = ccxt.bitget({"enableRateLimit": True, "rateLimit": 350, "timeout": 15000, "options": {"uta": False}})
    markets = exchange.load_markets()
    by_id = {m["id"]: m for m in markets.values() if m.get("active") and m.get("swap") and m.get("linear") and m.get("settle") == "USDT" and m["base"].upper() not in STABLE_BASES}
    tickers = exchange.publicMixGetV2MixMarketTickers({"productType": "USDT-FUTURES"})["data"]
    pre = []
    for t in tickers:
        market = by_id.get(t.get("symbol"))
        if market:
            pre.append((float(t.get("usdtVolume") or t.get("quoteVolume") or 0), market))
    pre.sort(key=lambda x: x[0], reverse=True)
    ranked, failures = [], []
    for _, market in pre[:PRE_N]:
        try:
            raw = exchange.publicMixGetV2MixMarketHistoryCandles({"symbol": market["id"], "productType": "USDT-FUTURES", "granularity": "1Dutc", "endTime": END_MS, "limit": 40})["data"]
            rows = [r for r in raw if START_MS <= int(r[0]) < END_MS]
            if len({int(r[0]) for r in rows}) != 30:
                continue
            ranked.append({"pair": market["symbol"], "base": market["base"], "quote_volume_30d": sum(float(r[6]) for r in rows),
                           "minimum_amount": market["limits"]["amount"]["min"], "minimum_cost": market["limits"]["cost"]["min"]})
        except Exception as exc:  # noqa: BLE001 - public data only; record and continue
            failures.append({"pair": market["symbol"], "error": str(exc)[:180]})
    ranked.sort(key=lambda r: r["quote_volume_30d"], reverse=True)
    for rank, row in enumerate(ranked, 1):
        row["volume_rank"] = rank
    order = [r for r in ranked if r["pair"] in ALWAYS] + [r for r in ranked if r["pair"] not in ALWAYS]
    selected, dropped = [], []
    for row in order:
        if len(selected) >= MAX_PAIRS:
            break
        try:
            ok, first = bitget_history(exchange, row["pair"])
        except Exception as exc:  # noqa: BLE001
            ok, first = False, None
            row["history_error"] = str(exc)[:180]
        row["first_candle_probe_utc"] = first
        row["binance_symbol"] = binance_symbol(row["base"]) if ok else None
        reason = None if ok and row["binance_symbol"] else ("no_bitget_2022_history" if not ok else "no_binance_metrics_2022")
        if reason:
            row["dropped_reason"] = reason
            dropped.append(row)
        else:
            selected.append(row)
        print("selected" if not reason else f"dropped ({reason})", row["pair"], row["volume_rank"], first, row.get("binance_symbol"), flush=True)
    missing = [p for p in ALWAYS if p not in {r["pair"] for r in selected}]
    if missing:
        raise SystemExit(f"rule broken: {missing} must always be selected (BTC market features need BTC)")
    selected.sort(key=lambda r: r["volume_rank"])
    result = {
        "selection_utc": datetime.now(timezone.utc).isoformat(),
        "volume_start_utc": exchange.iso8601(START_MS),
        "volume_end_exclusive_utc": END.isoformat(),
        "history_required_utc": exchange.iso8601(HISTORY_MS),
        "method": __doc__.split("Rule (fixed before any phase C result was seen):")[1].split("Same survivorship")[0].strip(),
        "selected": selected,
        "dropped": dropped,
        "failures": failures,
        "caveats": [
            "Survivorship bias: the list is today's winners applied to the past.",
            "Open interest, long/short ratios and funding features come from Binance (largest venue), used as a market-wide proxy; trades are still simulated on Bitget candles.",
        ],
    }
    (ROOT / "pair_selection_c.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
