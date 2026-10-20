"""Gap check for downloaded hourly futures/mark candles + real funding summary.

Bitget's public funding history only reaches back ~100 records (about 1 month
for 8h pairs), so a 4-year real funding series cannot be built from Bitget.
The real recent records are only summarised here; they are never overwritten.
(Phase A overwrote them with a synthetic series; review fix 1 moved that series
to data/bitget_synth_funding_signed_phaseA. Backtests now use the separate
data/bitget_synth_funding_zero datadir built by make_funding_datadirs.py and
charge funding in analyze_b.py.)
"""
import hashlib
import json
from pathlib import Path

import pandas as pd
from freqtrade.data.history.datahandlers.featherdatahandler import FeatherDataHandler
from freqtrade.enums import CandleType

ROOT = Path(__file__).resolve().parent
DATA = Path("/freqtrade/user_data/data/bitget")
START = pd.Timestamp("2022-01-01", tz="UTC")
END = pd.Timestamp("2026-10-20", tz="UTC")
FUNDING_RATE = 0.0001


def gap_runs(missing: pd.DatetimeIndex) -> list[dict]:
    runs, current = [], None
    for ts in missing:
        if current and ts - current["last"] == pd.Timedelta(hours=1):
            current["last"] = ts
            current["hours"] += 1
        else:
            current = {"first": ts, "last": ts, "hours": 1}
            runs.append(current)
    return [{"first_utc": str(r["first"]), "last_utc": str(r["last"]), "hours": r["hours"]} for r in runs]


def main():
    pairs = [r["pair"] for r in json.loads((ROOT / "pair_selection.json").read_text())["selected"]]
    handler = FeatherDataHandler(DATA)
    records, funding_real = [], {}
    expected = pd.date_range(START, END, freq="h", inclusive="left")
    for pair in pairs:
        for kind in (CandleType.FUTURES, CandleType.MARK):
            frame = handler.ohlcv_load(pair, "1h", candle_type=kind, fill_missing=False, drop_incomplete=False)
            frame = frame.loc[(frame.date >= START) & (frame.date < END)]
            missing = expected.difference(pd.DatetimeIndex(frame.date))
            zero_volume = int((frame.volume <= 0).sum()) if kind == CandleType.FUTURES else None
            records.append({
                "pair": pair, "kind": str(kind), "rows": len(frame), "expected_rows": len(expected),
                "missing_count": len(missing), "gap_runs": gap_runs(missing),
                "duplicates": int(frame.date.duplicated().sum()), "zero_volume_rows": zero_volume,
                "first_utc": str(frame.date.min()), "last_utc": str(frame.date.max()),
                "sha256_values": hashlib.sha256(pd.util.hash_pandas_object(frame, index=False).values.tobytes()).hexdigest(),
            })
        real = handler.ohlcv_load(pair, "1h", candle_type=CandleType.FUNDING_RATE, fill_missing=False, drop_incomplete=False)
        if len(real) and "funding_rate" not in real.columns:
            real = real.rename(columns={"open": "funding_rate"})
        if len(real) and not (real.funding_rate == FUNDING_RATE).all():
            gaps = real.date.diff().dropna().dt.total_seconds().div(3600)
            funding_real[pair] = {
                "records": len(real), "first_utc": str(real.date.min()), "last_utc": str(real.date.max()),
                "interval_hours_mode": float(gaps.mode().iloc[0]) if len(gaps) else None,
                "mean_rate": float(real.funding_rate.mean()), "median_rate": float(real.funding_rate.median()),
                "share_positive": float((real.funding_rate > 0).mean()),
            }
    result = {
        "start_utc": str(START), "end_exclusive_utc": str(END),
        "funding_model": f"Phase B headline: BOTH sides pay {FUNDING_RATE:+.4%} per 8h at each 00/08/16 UTC crossing, charged in analyze_b.py (engine datadir bitget_synth_funding_zero has zero funding). Sensitivity: phase A signed model (longs pay, shorts receive). NOT actual history: Bitget only serves ~100 recent funding records per pair.",
        "funding_real_recent_summary": funding_real,
        "records": records,
    }
    (ROOT / "data_quality.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = [(r["pair"], r["kind"], r["rows"], r["missing_count"], r["duplicates"], r["first_utc"][:16], r["last_utc"][:16]) for r in records]
    for row in summary:
        print(*row)
    print(json.dumps(funding_real, indent=1))


if __name__ == "__main__":
    main()
