"""Probe the first candle Bitget serves for a USDT-M perpetual (public endpoint, no keys)."""
import sys
from datetime import datetime, timezone

import ccxt

client = ccxt.bitget({"enableRateLimit": True, "options": {"uta": False, "defaultType": "swap"}})
for pair in sys.argv[1:]:
    for tf in ("1d", "4h", "1h"):
        for since in ("2018-01-01", "2019-01-01", "2019-06-01", "2019-09-01", "2020-01-01", "2021-01-01", "2021-06-01"):
            ms = int(datetime.fromisoformat(since).replace(tzinfo=timezone.utc).timestamp() * 1000)
            rows = client.fetch_ohlcv(pair, tf, since=ms, limit=90)
            if rows:
                print(pair, tf, "since", since, "-> first", datetime.fromtimestamp(rows[0][0] / 1000, timezone.utc), "rows", len(rows))
                break
