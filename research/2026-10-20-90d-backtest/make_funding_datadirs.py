"""Phase B funding setup (review fix 1). Run inside the Freqtrade image:

  python -P /research/make_funding_datadirs.py

Phase A wrote a synthetic signed funding series over the REAL funding files in
user_data/data/bitget. This script:
1. moves any synthetic phase-A series found under the original names to
   user_data/data/bitget_synth_funding_signed_phaseA/ (kept only as a record);
   the real recent files are then re-downloaded separately (download-data);
2. builds user_data/data/bitget_synth_funding_zero/: copies of the 1h futures and
   mark candles plus a ZERO funding series. Backtests run on this datadir, so the
   engine charges no funding; analyze_b.py then charges funding explicitly at every
   00/08/16 UTC crossing (headline: BOTH sides pay +0.01%/8h; sensitivity: the old
   signed model, longs pay and shorts receive).
"""
import json
import shutil
from pathlib import Path

import pandas as pd
from freqtrade.data.history.datahandlers.featherdatahandler import FeatherDataHandler
from freqtrade.enums import CandleType

ROOT = Path(__file__).resolve().parent
DATA = Path("/freqtrade/user_data/data")
REAL = DATA / "bitget"
ZERO = DATA / "bitget_synth_funding_zero"
PHASE_A = DATA / "bitget_synth_funding_signed_phaseA"
START = pd.Timestamp("2022-01-01", tz="UTC")
END = pd.Timestamp("2026-10-21", tz="UTC")
PHASE_A_RATE = 0.0001


def file_name(pair: str, kind: str) -> str:
    return pair.replace("/", "_").replace(":", "_") + f"-1h-{kind}.feather"


def main():
    pairs = [r["pair"] for r in json.loads((ROOT / "pair_selection.json").read_text())["selected"]]
    report = {"moved_phase_a_synthetic": [], "zero_datadir": str(ZERO), "pairs": pairs}
    real = FeatherDataHandler(REAL)
    for pair in pairs:
        frame = real.ohlcv_load(pair, "1h", candle_type=CandleType.FUNDING_RATE, fill_missing=False, drop_incomplete=False)
        column = "funding_rate" if "funding_rate" in frame else "open"
        synthetic = len(frame) > 1000 and bool((frame[column] == PHASE_A_RATE).all())
        if synthetic:
            target = PHASE_A / "futures"
            target.mkdir(parents=True, exist_ok=True)
            shutil.move(str(REAL / "futures" / file_name(pair, "funding_rate")), str(target / file_name(pair, "funding_rate")))
            report["moved_phase_a_synthetic"].append(pair)
    zero_futures = ZERO / "futures"
    zero_futures.mkdir(parents=True, exist_ok=True)
    for pair in pairs:
        for kind in ("futures", "mark"):
            shutil.copy2(REAL / "futures" / file_name(pair, kind), zero_futures / file_name(pair, kind))
        dates = pd.date_range(START, END, freq="8h", inclusive="left")
        FeatherDataHandler(ZERO).ohlcv_store(pair, "1h", pd.DataFrame({"date": dates, "funding_rate": 0.0}), candle_type=CandleType.FUNDING_RATE)
    tiers = REAL / "futures" / "leverage_tiers_USDT.json"
    if tiers.is_file():
        shutil.copy2(tiers, zero_futures / tiers.name)
    (ZERO / "README.txt").write_text(
        "Synthetic research datadir. Candles are copies of ../bitget. Funding is ZERO on purpose;\n"
        "analyze_b.py charges funding explicitly (both sides pay +0.01%/8h). Not real funding history.\n", encoding="utf-8")
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
