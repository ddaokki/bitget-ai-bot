"""Why no SOL holdout trade: count days in the daily uptrend and 4h breakout candidates."""
import pandas as pd
import talib.abstract as ta

D = "/freqtrade/user_data/data/bitget_synth_funding_zero/futures/"
d = pd.read_feather(D + "SOL_USDT_USDT-1d-futures.feather")
d["ema50"], d["ema200"] = ta.EMA(d, timeperiod=50), ta.EMA(d, timeperiod=200)
up = d.loc[d.ema200.notna() & (d.ema50 > d.ema200)]
print("first ema200", d.loc[d.ema200.notna(), "date"].min(), "uptrend days before 2023-10:", int((up.date < "2023-10-01").sum()),
      "first", up.date.min())
print(up.loc[up.date < "2023-10-01", "date"].dt.strftime("%Y-%m").value_counts().sort_index().to_dict())
