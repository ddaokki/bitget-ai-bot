"""Holdout data prep (run inside the Freqtrade image):

  python -P /research/prep_data.py BTC/USDT:USDT ETH/USDT:USDT SOL/USDT:USDT

1. Copies the 1h futures/mark and 4h/1d futures candles of each pair from
   data/bitget into data/bitget_synth_funding_zero (candles only; the downloads
   only prepended older history, newer rows are unchanged).
2. Rewrites the ZERO funding series of each pair so it covers FUNDING_START..END
   (all zeros, as before; funding is charged explicitly in analyze.py).
3. Writes data_check.json: first/last date, rows and gap runs per file.
"""
import json
import shutil
import sys
from pathlib import Path

import pandas as pd
from freqtrade.data.history.datahandlers.featherdatahandler import FeatherDataHandler
from freqtrade.enums import CandleType

ROOT = Path(__file__).resolve().parent
DATA = Path("/freqtrade/user_data/data")
REAL = DATA / "bitget"
ZERO = DATA / "bitget_synth_funding_zero"
FUNDING_START = pd.Timestamp("2019-01-01", tz="UTC")
FUNDING_END = pd.Timestamp("2026-10-21", tz="UTC")
FILES = [("1h", "futures"), ("1h", "mark"), ("4h", "futures"), ("1d", "futures")]
STEP = {"1h": pd.Timedelta(hours=1), "4h": pd.Timedelta(hours=4), "1d": pd.Timedelta(days=1)}


def file_name(pair: str, timeframe: str, kind: str) -> str:
    return pair.replace("/", "_").replace(":", "_") + f"-{timeframe}-{kind}.feather"


def gap_runs(dates: pd.Series, step: pd.Timedelta) -> list[dict]:
    dates = pd.to_datetime(dates, utc=True).sort_values().reset_index(drop=True)
    diffs = dates.diff()
    runs = []
    for idx in diffs.index[diffs > step]:
        runs.append({"after_utc": str(dates[idx - 1]), "before_utc": str(dates[idx]),
                     "missing_bars": int(diffs[idx] / step) - 1})
    return runs


def main(pairs: list[str]) -> None:
    zero_futures = ZERO / "futures"
    report = {}
    for pair in pairs:
        entry = {}
        for timeframe, kind in FILES:
            source = REAL / "futures" / file_name(pair, timeframe, kind)
            if not source.is_file():
                entry[f"{timeframe}-{kind}"] = "missing"
                continue
            shutil.copy2(source, zero_futures / source.name)
            frame = pd.read_feather(source)
            entry[f"{timeframe}-{kind}"] = {
                "rows": len(frame), "first_utc": str(frame.date.min()), "last_utc": str(frame.date.max()),
                "duplicates": int(frame.date.duplicated().sum()),
                "zero_volume_rows": int((frame.volume <= 0).sum()) if kind == "futures" else None,
                "gaps": gap_runs(frame.date, STEP[timeframe]),
            }
        dates = pd.date_range(FUNDING_START, FUNDING_END, freq="8h", inclusive="left")
        FeatherDataHandler(ZERO).ohlcv_store(pair, "1h", pd.DataFrame({"date": dates, "funding_rate": 0.0}),
                                             candle_type=CandleType.FUNDING_RATE)
        entry["zero_funding"] = {"first_utc": str(dates.min()), "last_utc": str(dates.max()), "rows": len(dates)}
        report[pair] = entry
    out = ROOT / "data_check.json"
    old = json.loads(out.read_text()) if out.is_file() else {}
    old.update(report)
    out.write_text(json.dumps(old, indent=1), encoding="utf-8")
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
