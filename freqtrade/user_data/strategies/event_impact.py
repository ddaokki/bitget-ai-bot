"""Event calendar rules, the spec §18 impact study and impact-weighted features.

Research-only helper module (not a strategy). Used by:
- research/.../event_impact_report.py: full-period impact table (REPORT ONLY);
- freqaimodels/SpikeCalibratedLGBM.py: walk-forward weights recomputed inside
  every training window from that window's prices only;
- the phase B strategies (blackout, feature placeholders).

Calendar CSV columns: ts_utc, time_known, region, category, scheduled, importance, ...
`time_known` is a BOOLEAN here: true = ts_utc carries the exact time;
false = only the DATE is reliable.

Availability (when a bot could first use the event):
- exact time:   ts_utc
- date only:    date 00:00Z + 24h (no intraday look-ahead)
Scheduled events are published in advance, so "time until the next scheduled
event" may be used up to SCHEDULED_LEAD before it (nominal time = ts_utc, or
date 00:00Z for date-only rows). Unscheduled events are never visible before
their availability time.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

IMPACT_HOURS = 6          # post window [0, +6h)
PRE_HOURS = 2             # pre window [-2h, 0)
BASELINE_WEEKS = 6        # matched baseline: same weekday-hour, +-6 weeks, non-event hours
RATIO_FLOOR = 1.15        # §18: drop groups with ratio below this
MIN_EVENTS = 8            # §18: drop groups with fewer events
CLUSTER_HOURS = 6         # same-group events closer than this collapse into one
SCHEDULED_LEAD = pd.Timedelta(days=14)
CAP_HOURS = 24.0 * 14
WEIGHTED_FEATURES = ["ev_w_recent6h", "ev_w_last24h", "ev_w_next24h", "ev_hours_to_next_kept"]
BLACKOUT_CATEGORIES = ("FOMC", "CPI", "PPI", "NFP")   # spec §7.1 (FOMC, CPI/PPI, jobs report)
BLACKOUT_HOURS = 2


def _as_bool(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "t"}


def load_calendar(path: str | Path) -> pd.DataFrame:
    """Read the verified calendar and attach availability columns. Missing file -> empty frame."""
    path = Path(path)
    columns = ["ts_utc", "exact", "scheduled", "importance", "region", "category", "group", "available", "nominal", "known_from"]
    if not path.is_file():
        return pd.DataFrame(columns=columns)
    raw = pd.read_csv(path)
    ts = pd.to_datetime(raw.ts_utc, utc=True)
    exact = raw.time_known.map(_as_bool).to_numpy()
    scheduled = raw.scheduled.map(_as_bool).to_numpy()
    day = ts.dt.floor("D")
    available = pd.Series(np.where(exact, ts, day + pd.Timedelta(hours=24)), index=raw.index)
    nominal = pd.Series(np.where(exact, ts, day), index=raw.index)
    known_from = pd.Series(np.where(scheduled, nominal - SCHEDULED_LEAD, available), index=raw.index)
    out = pd.DataFrame({
        "ts_utc": ts,
        "exact": exact,
        "scheduled": scheduled,
        "importance": raw.importance.astype(str).str.lower(),
        "region": raw.region.astype(str).str.upper(),
        "category": raw.category.astype(str).str.upper(),
        "available": pd.to_datetime(available, utc=True),
        "nominal": pd.to_datetime(nominal, utc=True),
        "known_from": pd.to_datetime(known_from, utc=True),
    })
    out["group"] = out.region + ":" + out.category
    for extra in ("title_en", "title_ko", "source_url"):
        if extra in raw:
            out[extra] = raw[extra]
    return out.sort_values("available").reset_index(drop=True)


# ---------------------------------------------------------------- impact study
def abs_returns(close: pd.Series) -> pd.Series:
    """|log return| of each 1h candle, indexed by candle open time (UTC)."""
    close = close.sort_index()
    return np.log(close).diff().abs()


def _event_hours(t0: pd.Series) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(t0).floor("h")


def contaminated_hours(calendar: pd.DataFrame, index: pd.DatetimeIndex) -> np.ndarray:
    """Mask of candles inside any event's [-PRE_HOURS, +IMPACT_HOURS) window (any group)."""
    mask = np.zeros(len(index), dtype=bool)
    if calendar.empty:
        return mask
    # Compare in one unit: parsed calendars may carry s/us resolution, candle indexes ns.
    starts = (_event_hours(calendar.available) - pd.Timedelta(hours=PRE_HOURS)).as_unit("ns")
    values = pd.DatetimeIndex(index).as_unit("ns").asi8
    for start in starts.asi8:
        lo = np.searchsorted(values, start, side="left")
        hi = np.searchsorted(values, start + (PRE_HOURS + IMPACT_HOURS) * 3_600_000_000_000, side="left")
        mask[lo:hi] = True
    return mask


def matched_baseline(absret: pd.Series, contaminated: np.ndarray) -> pd.Series:
    """Mean |return| at the same weekday-hour within +-BASELINE_WEEKS, excluding event hours.

    `absret` must be on a regular hourly index; values outside the series (for a
    training window: outside the window) simply do not contribute.
    """
    clean = absret.where(~contaminated)
    full = clean.asfreq("h")
    total = pd.Series(0.0, index=full.index)
    count = pd.Series(0.0, index=full.index)
    for weeks in range(1, BASELINE_WEEKS + 1):
        for sign in (-1, 1):
            shifted = full.shift(sign * weeks * 168)
            total = total.add(shifted.fillna(0.0))
            count = count.add(shifted.notna().astype(float))
    return (total / count.replace(0, np.nan)).reindex(absret.index)


def _cluster(events: pd.DataFrame) -> pd.DataFrame:
    """Within a group, drop events less than CLUSTER_HOURS after the previous kept one."""
    keep, last = [], {}
    for idx, row in events.sort_values("available").iterrows():
        prev = last.get(row.group)
        if prev is not None and row.available - prev < pd.Timedelta(hours=CLUSTER_HOURS):
            continue
        last[row.group] = row.available
        keep.append(idx)
    return events.loc[keep]


def event_windows(calendar: pd.DataFrame, prices: dict[str, pd.Series], start=None, end=None) -> pd.DataFrame:
    """Per event and asset: mean |ret| in post/pre windows and the matched baseline.

    Only prices in [start, end) are used (baseline included). Events whose post
    window does not fit completely inside [start, end) are skipped.
    """
    rows = []
    events = calendar.copy()
    if start is not None:
        events = events.loc[events.available - pd.Timedelta(hours=PRE_HOURS) >= pd.Timestamp(start)]
    if end is not None:
        events = events.loc[events.available.dt.floor("h") + pd.Timedelta(hours=IMPACT_HOURS) <= pd.Timestamp(end)]
    events = _cluster(events)
    for asset, close in prices.items():
        close = close.sort_index()
        if start is not None:
            close = close.loc[close.index >= pd.Timestamp(start)]
        if end is not None:
            close = close.loc[close.index < pd.Timestamp(end)]
        ar = abs_returns(close).asfreq("h")
        base = matched_baseline(ar, contaminated_hours(calendar, ar.index))
        idx = ar.index
        for ev in events.itertuples():
            h0 = ev.available.floor("h")
            lo = idx.searchsorted(h0)
            if lo >= len(idx) or idx[lo] != h0:
                continue
            post = slice(lo, lo + IMPACT_HOURS)
            pre = slice(max(lo - PRE_HOURS, 0), lo)
            post_ret, post_base = ar.iloc[post], base.iloc[post]
            if len(post_ret) < IMPACT_HOURS or post_ret.isna().any() or post_base.isna().any():
                continue
            pre_ret, pre_base = ar.iloc[pre], base.iloc[pre]
            rows.append({
                "group": ev.group, "region": ev.region, "category": ev.category, "available": ev.available,
                "exact": bool(ev.exact), "scheduled": bool(ev.scheduled), "asset": asset,
                "post": float(post_ret.mean()), "post_base": float(post_base.mean()),
                "pre": float(pre_ret.mean()) if len(pre_ret) == PRE_HOURS and not pre_ret.isna().any() else np.nan,
                "pre_base": float(pre_base.mean()) if len(pre_base) == PRE_HOURS and not pre_base.isna().any() else np.nan,
            })
    return pd.DataFrame(rows)


def _ratio(frame: pd.DataFrame, num: str, den: str) -> float:
    valid = frame[[num, den]].dropna()
    return float(valid[num].sum() / valid[den].sum()) if len(valid) and valid[den].sum() > 0 else np.nan


def impact_table(calendar: pd.DataFrame, prices: dict[str, pd.Series], start=None, end=None, bootstrap: int = 0, seed: int = 81) -> pd.DataFrame:
    """Group volatility ratios (post/baseline pooled over assets) with optional bootstrap 90% CI."""
    windows = event_windows(calendar, prices, start, end)
    columns = ["group", "region", "category", "n", "n_exact", "n_dateonly", "ratio", "ci_lo", "ci_hi", "ratio_btc", "ratio_eth", "pre_ratio", "n_exact_only", "ratio_exact_only", "kept"]
    if windows.empty:
        return pd.DataFrame(columns=columns)
    rng = np.random.default_rng(seed)
    rows = []
    for group, frame in windows.groupby("group"):
        events = frame.drop_duplicates("available")
        n = len(events)
        by_event = frame.groupby("available")[["post", "post_base"]].sum()
        lo = hi = np.nan
        if bootstrap and n >= 2:
            draws = rng.integers(0, n, size=(bootstrap, n))
            post, base = by_event.post.to_numpy(), by_event.post_base.to_numpy()
            ratios = post[draws].sum(axis=1) / base[draws].sum(axis=1)
            lo, hi = np.percentile(ratios, [5, 95])
        exact = frame.loc[frame.exact]
        ratio = _ratio(frame, "post", "post_base")
        rows.append({
            "group": group, "region": frame.region.iloc[0], "category": frame.category.iloc[0],
            "n": n, "n_exact": int(events.exact.sum()), "n_dateonly": int((~events.exact).sum()),
            "ratio": ratio, "ci_lo": lo, "ci_hi": hi,
            "ratio_btc": _ratio(frame.loc[frame.asset.str.startswith("BTC")], "post", "post_base"),
            "ratio_eth": _ratio(frame.loc[frame.asset.str.startswith("ETH")], "post", "post_base"),
            "pre_ratio": _ratio(frame, "pre", "pre_base"),
            "n_exact_only": int(exact.drop_duplicates("available").shape[0]),
            "ratio_exact_only": _ratio(exact, "post", "post_base"),
            "kept": bool(n >= MIN_EVENTS and ratio >= RATIO_FLOOR),
        })
    return pd.DataFrame(rows, columns=columns).sort_values(["kept", "ratio"], ascending=[False, False]).reset_index(drop=True)


def weights_from_table(table: pd.DataFrame) -> dict[str, float]:
    """§18: kept groups use their ratio as weight; dropped groups get no weight."""
    if table is None or table.empty:
        return {}
    kept = table.loc[table.kept]
    return {str(g): float(r) for g, r in zip(kept.group, kept.ratio, strict=True)}


# ---------------------------------------------------------- weighted features
def weighted_event_features(dates: pd.Series, calendar: pd.DataFrame, weights: dict[str, float]) -> pd.DataFrame:
    """Point-in-time impact-weighted features as of each 1h candle CLOSE (date + 1h).

    ev_w_recent6h:   sum of weights of kept events that became available in (as_of-6h, as_of]
    ev_w_last24h:    same over (as_of-24h, as_of]
    ev_w_next24h:    sum of weights of kept SCHEDULED events with nominal time in (as_of, as_of+24h]
    ev_hours_to_next_kept: hours to the next kept scheduled event (capped at 14 days)
    """
    as_of = (pd.to_datetime(pd.Series(dates), utc=True) + pd.Timedelta(hours=1)).to_numpy(dtype="datetime64[ns]").astype("int64")
    n = len(as_of)
    out = {name: np.zeros(n) for name in WEIGHTED_FEATURES}
    out["ev_hours_to_next_kept"] = np.full(n, CAP_HOURS)
    hour = 3_600_000_000_000
    if calendar is None or calendar.empty or not weights:
        return pd.DataFrame(out, index=getattr(dates, "index", None))
    kept = calendar.loc[calendar.group.isin(list(weights))]
    if not kept.empty:
        avail = kept.available.dt.tz_convert(None).to_numpy(dtype="datetime64[ns]").astype("int64")
        w = kept.group.map(weights).to_numpy(dtype=float)
        order = np.argsort(avail)
        avail, cum = avail[order], np.concatenate([[0.0], np.cumsum(w[order])])

        def window_sum(lag_hours: float) -> np.ndarray:
            hi = np.searchsorted(avail, as_of, side="right")
            lo = np.searchsorted(avail, as_of - int(lag_hours * hour), side="right")
            return cum[hi] - cum[lo]

        out["ev_w_recent6h"] = window_sum(6)
        out["ev_w_last24h"] = window_sum(24)
        sched = kept.loc[kept.scheduled]
        if not sched.empty:
            nominal = sched.nominal.dt.tz_convert(None).to_numpy(dtype="datetime64[ns]").astype("int64")
            ws = sched.group.map(weights).to_numpy(dtype=float)
            order = np.argsort(nominal)
            nominal, cums = nominal[order], np.concatenate([[0.0], np.cumsum(ws[order])])
            lo = np.searchsorted(nominal, as_of, side="right")
            hi = np.searchsorted(nominal, as_of + 24 * hour, side="right")
            out["ev_w_next24h"] = cums[hi] - cums[lo]
            nxt = np.where(lo < len(nominal), nominal[np.minimum(lo, len(nominal) - 1)], np.iinfo("int64").max)
            hours = (nxt.astype(float) - as_of.astype(float)) / hour
            # SCHEDULED_LEAD (14d) == CAP_HOURS, so anything beyond the cap is also not yet "known".
            out["ev_hours_to_next_kept"] = np.where(lo < len(nominal), np.minimum(hours, CAP_HOURS), CAP_HOURS)
    return pd.DataFrame(out, index=getattr(dates, "index", None))[WEIGHTED_FEATURES]


# ------------------------------------------------------------------- blackout
def blackout_mask(entry_times: pd.Series, calendar: pd.DataFrame, categories=BLACKOUT_CATEGORIES, hours: float = BLACKOUT_HOURS) -> np.ndarray:
    """True where an entry at `entry_times` falls within +-hours of a scheduled US macro release.

    Only exact-time scheduled events count; these are published in advance, so
    using them at decision time is not look-ahead.
    """
    times = pd.to_datetime(pd.Series(entry_times), utc=True).to_numpy(dtype="datetime64[ns]").astype("int64")
    mask = np.zeros(len(times), dtype=bool)
    if calendar is None or calendar.empty:
        return mask
    ev = calendar.loc[calendar.scheduled & calendar.exact & (calendar.region == "US") & calendar.category.isin(categories)]
    if ev.empty:
        return mask
    stamps = np.sort(ev.ts_utc.dt.tz_convert(None).to_numpy(dtype="datetime64[ns]").astype("int64"))
    span = int(hours * 3_600_000_000_000)
    lo = np.searchsorted(stamps, times - span, side="left")
    hi = np.searchsorted(stamps, times + span, side="right")
    return hi > lo
