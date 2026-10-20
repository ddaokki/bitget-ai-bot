"""Probe the first daily candle Bitget serves for ETH and SOL perpetuals (public endpoint)."""
import time
from datetime import datetime, timezone

import ccxt

client = ccxt.bitget({"enableRateLimit": True, "options": {"uta": False, "defaultType": "swap"}})
for pair, since in (("ETH/USDT:USDT", "2019-06-01"), ("ETH/USDT:USDT", "2019-08-30"), ("SOL/USDT:USDT", "2021-05-01")):
    ms = int(datetime.fromisoformat(since).replace(tzinfo=timezone.utc).timestamp() * 1000)
    rows = client.fetch_ohlcv(pair, "1d", since=ms, limit=90)
    print(pair, "since", since, "->", datetime.fromtimestamp(rows[0][0] / 1000, timezone.utc) if rows else None, len(rows))
    time.sleep(1)
