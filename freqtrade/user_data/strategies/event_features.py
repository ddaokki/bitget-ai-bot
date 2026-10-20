"""Point-in-time event features (hook for the event-aware stage, spec §18).

Input CSV columns: ts_utc, time_known, region, category, scheduled, importance

- ts_utc:     when the event happens (or happened).
- time_known: when the information became available. Scheduled events
              (FOMC/CPI/NFP) are usually known weeks ahead; unscheduled events
              (hacks, bans) are known at or after ts_utc. Missing values are
              treated as ts_utc, i.e. never known in advance.
- importance: "high" / "medium" / "low" or a number (>= 3 counts as high).

A feature value at a 1h candle is computed "as of" that candle's CLOSE
(date + 1h), the moment the strategy decides. An event contributes only when
time_known <= as_of. Past-event features additionally need ts_utc <= as_of.
Nothing here looks at prices, so the features carry no price look-ahead.

This module is not a strategy; Freqtrade ignores it when searching strategies.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

GROUPS = {"US": "us", "KR": "kr", "CRYPTO": "crypto_global", "GLOBAL": "crypto_global"}
GROUP_NAMES = ("us", "kr", "crypto_global")
CAP_HOURS = 24.0 * 14
FEATURE_COLUMNS = ["ev_hours_to_next_sched_high"] + [f"ev_hours_since_high_{g}" for g in GROUP_NAMES] + [f"ev_in_last_24h_{g}" for g in GROUP_NAMES]


def _is_high(value) -> bool:
    text = str(value).strip().lower()
    if text in {"high", "h"}:
        return True
    try:
        return float(text) >= 3
    except ValueError:
        return False


def _as_bool(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "t"}


def load_events(path: str | Path) -> pd.DataFrame:
    """Read and normalise the events CSV. Missing file -> empty frame."""
    path = Path(path)
    columns = ["ts_utc", "time_known", "group", "category", "scheduled", "high"]
    if not path.is_file():
        return pd.DataFrame(columns=columns)
    raw = pd.read_csv(path)
    missing = {"ts_utc", "time_known", "region", "category", "scheduled", "importance"} - set(raw.columns)
    if missing:
        raise ValueError(f"events CSV missing columns: {sorted(missing)}")
    ts = pd.to_datetime(raw.ts_utc, utc=True)
    flags = raw.time_known.astype(str).str.strip().str.lower()
    if len(raw) and flags.isin({"true", "false", "1", "0", "nan", ""}).all():
        # Verified calendar format (phase B): time_known is a boolean "exact time known" flag.
        # Date-only rows are treated as happening at date 00:00Z + 24h (no intraday
        # look-ahead); scheduled rows are known 14 days before that time.
        exact = flags.isin({"true", "1"})
        scheduled = raw.scheduled.map(_as_bool)
        ts = ts.where(exact, ts.dt.floor("D") + pd.Timedelta(hours=24))
        known = ts.where(~scheduled, ts - pd.Timedelta(days=14))
    else:
        known = pd.to_datetime(raw.time_known, utc=True, errors="coerce")
    events = pd.DataFrame({
        "ts_utc": ts,
        "time_known": known,
        "group": raw.region.astype(str).str.strip().str.upper().map(GROUPS),
        "category": raw.category.astype(str),
        "scheduled": raw.scheduled.map(_as_bool),
        "high": raw.importance.map(_is_high),
    })
    events["time_known"] = events.time_known.fillna(events.ts_utc)
    # An unscheduled event cannot be known before it happens.
    events.loc[~events.scheduled, "time_known"] = events.loc[~events.scheduled, ["time_known", "ts_utc"]].max(axis=1)
    return events.dropna(subset=["ts_utc"]).reset_index(drop=True)


def compute_event_features(dates: pd.Series, events: pd.DataFrame) -> pd.DataFrame:
    """Return FEATURE_COLUMNS aligned to `dates` (1h candle open times, UTC)."""
    as_of = (pd.to_datetime(dates, utc=True) + pd.Timedelta(hours=1)).to_numpy(dtype="datetime64[ns]")
    n = len(as_of)
    out = pd.DataFrame(index=getattr(dates, "index", None))
    hour = np.timedelta64(1, "h")
    high = events.loc[events.high] if len(events) else events

    # Hours until the next known, high-importance scheduled event.
    nxt = np.full(n, np.datetime64("NaT"), dtype="datetime64[ns]")
    order = np.argsort(as_of)
    sorted_asof = as_of[order]
    for _, ev in high.loc[high.scheduled].iterrows() if len(high) else []:
        known = np.datetime64(ev.time_known.tz_convert(None))
        when = np.datetime64(ev.ts_utc.tz_convert(None))
        lo = np.searchsorted(sorted_asof, known, side="left")
        hi = np.searchsorted(sorted_asof, when, side="left")
        if hi <= lo:
            continue
        idx = order[lo:hi]
        current = nxt[idx]
        nxt[idx] = np.where(np.isnat(current) | (current > when), when, current)
    hours = (nxt - as_of) / hour
    out["ev_hours_to_next_sched_high"] = np.where(np.isnat(nxt), CAP_HOURS, np.minimum(hours, CAP_HOURS))

    # Hours since the last high-importance event per region group (scheduled or not).
    for group in GROUP_NAMES:
        sub = high.loc[high.group == group] if len(high) else high
        since = np.full(n, CAP_HOURS)
        if len(sub):
            available = np.maximum(sub.ts_utc.dt.tz_convert(None).to_numpy(dtype="datetime64[ns]"), sub.time_known.dt.tz_convert(None).to_numpy(dtype="datetime64[ns]"))
            happened = sub.ts_utc.dt.tz_convert(None).to_numpy(dtype="datetime64[ns]")
            ordr = np.argsort(available)
            available, happened = available[ordr], happened[ordr]
            latest = np.maximum.accumulate(happened.astype("int64")).astype("datetime64[ns]")
            pos = np.searchsorted(available, as_of, side="right") - 1
            valid = pos >= 0
            since[valid] = np.minimum((as_of[valid] - latest[pos[valid]]) / hour, CAP_HOURS)
        out[f"ev_hours_since_high_{group}"] = since
        out[f"ev_in_last_24h_{group}"] = (since <= 24).astype(float)
    return out[FEATURE_COLUMNS]
