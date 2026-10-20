"""Add the phase C coins to the zero-funding engine datadir (same rule as phase B).

  python -P /research/make_zero_funding_c.py      (inside the Freqtrade image)

Copies real 1h futures + mark candles from user_data/data/bitget and writes a ZERO
funding series, so the engine charges nothing and analyze_c.py charges funding
explicitly (both sides pay 0.01%/8h). Existing phase B files are overwritten with the
same copies, so phase B results are unaffected.
"""
import json
import shutil
from pathlib import Path

import pandas as pd
from freqtrade.data.history.datahandlers.featherdatahandler import FeatherDataHandler
from freqtrade.enums import CandleType

ROOT = Path(__file__).resolve().parent
DATA = Path("/freqtrade/user_data/data")
REAL = DATA / "bitget" / "futures"
ZERO = DATA / "bitget_synth_funding_zero"
START = pd.Timestamp("2022-01-01", tz="UTC")
END = pd.Timestamp("2026-10-21", tz="UTC")


def file_name(pair: str, kind: str) -> str:
    return pair.replace("/", "_").replace(":", "_") + f"-1h-{kind}.feather"


def main():
    pairs = [r["pair"] for r in json.loads((ROOT / "pair_selection_c.json").read_text(encoding="utf-8"))["selected"]]
    target = ZERO / "futures"
    target.mkdir(parents=True, exist_ok=True)
    report = {}
    for pair in pairs:
        for kind in ("futures", "mark"):
            shutil.copy2(REAL / file_name(pair, kind), target / file_name(pair, kind))
        dates = pd.date_range(START, END, freq="8h", inclusive="left")
        FeatherDataHandler(ZERO).ohlcv_store(pair, "1h", pd.DataFrame({"date": dates, "funding_rate": 0.0}), candle_type=CandleType.FUNDING_RATE)
        candles = FeatherDataHandler(ZERO).ohlcv_load(pair, "1h", candle_type=CandleType.FUTURES, fill_missing=False, drop_incomplete=False)
        report[pair] = {"rows": len(candles), "first": str(candles.date.min()), "last": str(candles.date.max())}
    tiers = REAL / "leverage_tiers_USDT.json"
    if tiers.is_file():
        shutil.copy2(tiers, target / tiers.name)
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
