"""Build a repaired BTC datadir for the holdout (run inside the Freqtrade image):

  python -P /research/repair_btc_1h.py

Bitget's 1h BTC futures (last price) history has a frozen run: 2020-12-24 20:00 ..
2021-01-02 02:00 UTC (199 bars, OHLC all 23301, volume 0) plus one bar on 2021-06-01.
The 1h MARK candles and the 4h futures candles are normal there. This copies the
zero-funding datadir files for BTC into /research/data_repaired/futures and replaces
every frozen bar (volume 0 and high == low) with the mark OHLC of the same hour;
volume is set to a quarter of the containing 4h bar's volume so the volume > 0 entry
filter behaves as on normal bars. Mark vs last price differ by a few basis points.
"""
import json
import shutil
from pathlib import Path

import pandas as pd

SRC = Path("/freqtrade/user_data/data/bitget_synth_funding_zero/futures")
DST = Path("/research/data_repaired/futures")
BASE = "BTC_USDT_USDT"


def main() -> None:
    DST.mkdir(parents=True, exist_ok=True)
    for name in (f"{BASE}-1h-mark.feather", f"{BASE}-4h-futures.feather", f"{BASE}-1d-futures.feather",
                 f"{BASE}-1h-funding_rate.feather", "leverage_tiers_USDT.json"):
        shutil.copy2(SRC / name, DST / name)
    last = pd.read_feather(SRC / f"{BASE}-1h-futures.feather")
    mark = pd.read_feather(SRC / f"{BASE}-1h-mark.feather").set_index("date")
    four = pd.read_feather(SRC / f"{BASE}-4h-futures.feather").set_index("date")
    frozen = (last.volume <= 0) & (last.high == last.low)
    fixed = []
    for idx in last.index[frozen]:
        when = last.at[idx, "date"]
        if when not in mark.index:
            continue
        for col in ("open", "high", "low", "close"):
            last.at[idx, col] = float(mark.at[when, col])
        bucket = when.floor("4h")
        last.at[idx, "volume"] = float(four.at[bucket, "volume"]) / 4 if bucket in four.index else 1.0
        fixed.append(str(when))
    last.to_feather(DST / f"{BASE}-1h-futures.feather")
    report = {"frozen_bars": int(frozen.sum()), "repaired": len(fixed), "first": fixed[0] if fixed else None,
              "last": fixed[-1] if fixed else None}
    (Path("/research") / "repair_report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
