"""Show zero-volume 1h runs with their price action, and compare 1h vs 4h volume/price there."""
import pandas as pd

D = "/freqtrade/user_data/data/bitget_synth_funding_zero/futures/"
h = pd.read_feather(D + "BTC_USDT_USDT-1h-futures.feather")
f = pd.read_feather(D + "BTC_USDT_USDT-4h-futures.feather")
z = h.loc[h.volume <= 0]
runs = (z.date.diff() != pd.Timedelta(hours=1)).cumsum()
for _, g in z.groupby(runs):
    print(g.date.iloc[0], "..", g.date.iloc[-1], len(g), "close", g.close.iloc[0], "->", g.close.iloc[-1],
          "flat_bars", int((g.high == g.low).sum()))
win = f.loc[(f.date >= "2020-12-01") & (f.date < "2021-01-31")]
print("4h volume zero in that window:", int((win.volume <= 0).sum()), "of", len(win))
