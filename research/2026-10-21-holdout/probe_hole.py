"""Re-fetch BTC 1h futures/mark candles around the Dec 2020 flat run straight from Bitget (public)."""
from datetime import datetime, timezone

import ccxt

client = ccxt.bitget({"enableRateLimit": True, "options": {"uta": False, "defaultType": "swap"}})
ms = int(datetime(2020, 12, 26, tzinfo=timezone.utc).timestamp() * 1000)
for params in ({}, {"price": "mark"}):
    rows = client.fetch_ohlcv("BTC/USDT:USDT", "1h", since=ms, limit=6, params=params)
    print(params, [(datetime.fromtimestamp(r[0] / 1000, timezone.utc).strftime("%m-%d %H"), r[1], r[2], r[3], r[4], r[5]) for r in rows])
rows = client.fetch_ohlcv("BTC/USDT:USDT", "4h", since=ms, limit=4)
print("4h", [(datetime.fromtimestamp(r[0] / 1000, timezone.utc).strftime("%m-%d %H"), r[1], r[2], r[3], r[4], r[5]) for r in rows])
