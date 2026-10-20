"""Download Binance USDT-M positioning data for the phase C coins (public data only).

  python -P /research/fetch_binance.py          (inside the FreqAI image)

Sources (no API key, no account):
  metrics  data.binance.vision daily zips, 5-minute rows: open interest (coins and USDT),
           top-trader long/short account and position ratios, global long/short account
           ratio, taker buy/sell volume ratio.
  funding  fapi.binance.com /fapi/v1/fundingRate (settled rates); falls back to the
           data.binance.vision monthly zips if the REST endpoint is unreachable.

Writes user_data/data/binance_vision/{metrics,funding}_<SYMBOL>.feather and
results/binance_coverage.json. Zips are parsed in memory as CSV data only.
Long/short columns are partly empty in 2022 on Binance's side; they stay NaN.
"""
import io
import json
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
OUT = Path("/freqtrade/user_data/data/binance_vision")
START = pd.Timestamp("2022-01-01")
END = pd.Timestamp("2026-10-19")          # last complete daily file before the 2026-10-20 cut
METRICS = "https://data.binance.vision/data/futures/um/daily/metrics/{s}/{s}-metrics-{d}.zip"
FUND_ZIP = "https://data.binance.vision/data/futures/um/monthly/fundingRate/{s}/{s}-fundingRate-{m}.zip"
FUND_REST = "https://fapi.binance.com/fapi/v1/fundingRate?symbol={s}&startTime={t}&limit=1000"
METRIC_COLUMNS = ["sum_open_interest", "sum_open_interest_value", "count_toptrader_long_short_ratio",
                  "sum_toptrader_long_short_ratio", "count_long_short_ratio", "sum_taker_long_short_vol_ratio"]


def get(url: str, tries: int = 4) -> bytes | None:
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
        except Exception:  # noqa: BLE001 - network hiccup, retry
            pass
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"download failed: {url}")


def read_zip_csv(blob: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        name = archive.namelist()[0]
        return pd.read_csv(archive.open(name))


def metrics_day(symbol: str, day: pd.Timestamp) -> pd.DataFrame | None:
    blob = get(METRICS.format(s=symbol, d=day.strftime("%Y-%m-%d")))
    if blob is None:
        return None
    frame = read_zip_csv(blob)
    frame["date"] = pd.to_datetime(frame.create_time, utc=True)
    for column in METRIC_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame[["date", *METRIC_COLUMNS]]


def fetch_metrics(symbol: str) -> tuple[pd.DataFrame, list[str]]:
    days = pd.date_range(START, END, freq="D")
    with ThreadPoolExecutor(max_workers=16) as pool:
        parts = list(pool.map(lambda d: metrics_day(symbol, d), days))
    missing = [d.strftime("%Y-%m-%d") for d, p in zip(days, parts, strict=True) if p is None]
    frame = pd.concat([p for p in parts if p is not None], ignore_index=True)
    frame = frame.drop_duplicates("date").sort_values("date").reset_index(drop=True)
    return frame, missing


def fetch_funding(symbol: str) -> tuple[pd.DataFrame, str]:
    try:
        rows, start = [], int(START.tz_localize("UTC").timestamp() * 1000)
        end_ms = int((END + pd.Timedelta(days=1)).tz_localize("UTC").timestamp() * 1000)
        while start < end_ms:
            batch = json.loads(get(FUND_REST.format(s=symbol, t=start)) or b"[]")
            if not batch:
                break
            rows += batch
            start = int(batch[-1]["fundingTime"]) + 1
            if len(batch) < 1000:
                break
        frame = pd.DataFrame(rows)
        frame["date"] = pd.to_datetime(frame.fundingTime.astype("int64"), unit="ms", utc=True)
        frame["funding_rate"] = pd.to_numeric(frame.fundingRate, errors="coerce")
        source = "fapi.binance.com"
    except Exception:  # noqa: BLE001 - REST unreachable: use the monthly archive
        parts = []
        for month in pd.period_range(START, END, freq="M"):
            blob = get(FUND_ZIP.format(s=symbol, m=str(month)))
            if blob is not None:
                parts.append(read_zip_csv(blob))
        frame = pd.concat(parts, ignore_index=True)
        frame["date"] = pd.to_datetime(frame.calc_time.astype("int64"), unit="ms", utc=True)
        frame["funding_rate"] = pd.to_numeric(frame.last_funding_rate, errors="coerce")
        source = "data.binance.vision monthly"
    frame = frame[["date", "funding_rate"]].drop_duplicates("date").sort_values("date").reset_index(drop=True)
    cut = (END + pd.Timedelta(days=1)).tz_localize("UTC")
    return frame.loc[frame.date < cut].reset_index(drop=True), source


def main():
    selection = json.loads((ROOT / "pair_selection_c.json").read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    coverage = {}
    for row in selection["selected"]:
        symbol = row["binance_symbol"]
        metrics, missing = fetch_metrics(symbol)
        metrics.to_feather(OUT / f"metrics_{symbol}.feather")
        funding, source = fetch_funding(symbol)
        funding.to_feather(OUT / f"funding_{symbol}.feather")
        coverage[symbol] = {
            "pair": row["pair"], "metrics_rows": len(metrics), "metrics_first": str(metrics.date.min()), "metrics_last": str(metrics.date.max()),
            "metrics_missing_days": missing,
            "non_null_share": {c: round(float(metrics[c].notna().mean()), 4) for c in METRIC_COLUMNS},
            "first_full_ls_day": str(metrics.loc[metrics[METRIC_COLUMNS].notna().all(axis=1), "date"].min()),
            "funding_rows": len(funding), "funding_first": str(funding.date.min()), "funding_last": str(funding.date.max()), "funding_source": source,
        }
        print(symbol, len(metrics), "metrics rows,", len(missing), "missing days,", len(funding), "funding rows from", source, flush=True)
    (ROOT / "results" / "binance_coverage.json").write_text(json.dumps(coverage, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
