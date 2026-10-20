"""Public market selection only. No account configuration or private endpoints.

Universe rule (4-year spike):
  1. All active linear USDT perpetuals on Bitget, stable/fiat/pegged bases excluded.
  2. Rank by summed quote volume over the last 30 complete UTC days.
  3. Keep today's top 15.
  4. Keep only pairs where Bitget itself serves a 1h candle at or before
     2022-01-01T00:00Z (needed for the daily-EMA200 warmup).
  5. BTC and ETH are always included.

This is a present-day universe applied to the past, so it has survivorship bias.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import ccxt

END = datetime(2026, 10, 20, tzinfo=timezone.utc)
END_MS = int(END.timestamp() * 1000)
START_MS = END_MS - 30 * 86400000
HISTORY_MS = int(datetime(2022, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
TOP_N = 15
ALWAYS = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
STABLE_BASES = {"USDC", "USDT", "DAI", "TUSD", "FDUSD", "USDE", "USDD", "PYUSD", "USD1", "USDP", "FRAX", "EUR", "EURC", "EURI", "EURCV", "PAXG", "XAUT", "BUSD", "RLUSD"}
ROOT = Path(__file__).resolve().parent


def has_history(exchange, pair):
    """True when Bitget returns a real 1h candle at or before 2022-01-01T00:00Z."""
    candles = exchange.fetch_ohlcv(pair, "1h", HISTORY_MS - 6 * 3600000, 12)
    first = min((int(c[0]) for c in candles), default=None)
    return first is not None and first <= HISTORY_MS, (exchange.iso8601(first) if first else None)


def main():
    exchange = ccxt.bitget({"enableRateLimit": True, "rateLimit": 350, "timeout": 15000, "options": {"uta": False}})
    markets = exchange.load_markets()
    candidates = [m for m in markets.values() if m.get("active") and m.get("swap") and m.get("linear") and m.get("settle") == "USDT" and m["base"].upper() not in STABLE_BASES]
    ranked, failures = [], []
    for index, market in enumerate(candidates):
        try:
            raw = exchange.publicMixGetV2MixMarketHistoryCandles({"symbol": market["id"], "productType": "USDT-FUTURES", "granularity": "1Dutc", "endTime": END_MS, "limit": 40})["data"]
            rows = [r for r in raw if START_MS <= int(r[0]) < END_MS]
            if len({int(r[0]) for r in rows}) != 30:
                continue
            ranked.append({"pair": market["symbol"], "quote_volume_30d": sum(float(r[6]) for r in rows), "minimum_amount": market["limits"]["amount"]["min"], "minimum_cost": market["limits"]["cost"]["min"]})
        except Exception as exc:  # noqa: BLE001 - record and continue, public data only
            failures.append({"pair": market["symbol"], "error": str(exc)[:180]})
        if (index + 1) % 100 == 0:
            print(f"volume_scan {index + 1}/{len(candidates)}", flush=True)
    ranked.sort(key=lambda r: r["quote_volume_30d"], reverse=True)
    for rank, row in enumerate(ranked, 1):
        row["volume_rank"] = rank
    top = ranked[:TOP_N]
    for pair in ALWAYS:
        if pair not in {r["pair"] for r in top}:
            top.append(next(r for r in ranked if r["pair"] == pair))
    selected, dropped = [], []
    for row in top:
        try:
            ok, first = has_history(exchange, row["pair"])
        except Exception as exc:  # noqa: BLE001
            ok, first = False, None
            row["history_error"] = str(exc)[:180]
        row["first_candle_probe_utc"] = first
        (selected if ok else dropped).append(row)
        print("selected" if ok else "dropped", row["pair"], first, flush=True)
    result = {
        "selection_utc": datetime.now(timezone.utc).isoformat(),
        "volume_start_utc": exchange.iso8601(START_MS),
        "volume_end_exclusive_utc": END.isoformat(),
        "history_required_utc": exchange.iso8601(HISTORY_MS),
        "method": "Sum Bitget UTC daily candle quote volume (field 6) over 30 complete days across active linear USDT perpetuals (stable/fiat/pegged bases excluded). Take today's top 15, always add BTC/ETH, then keep only pairs for which Bitget returns a 1h candle at or before 2022-01-01T00:00Z.",
        "universe_count": len(candidates),
        "ranked_count": len(ranked),
        "top15": [r["pair"] for r in ranked[:TOP_N]],
        "selected": selected,
        "dropped_no_history": dropped,
        "failures": failures,
        "caveats": [
            "Survivorship bias: the list is today's winners. Coins that were big in 2022-2023 and later collapsed or were delisted are not in it.",
            "History is probed directly from Bitget; no other exchange is used to fill missing early data.",
        ],
    }
    (ROOT / "pair_selection.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
