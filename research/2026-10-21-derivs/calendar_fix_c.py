"""Phase C calendar correction (review fix): remove hindsight "scheduled" flags.

  python /research/calendar_fix_c.py      (any Python with pandas)

event_impact.load_calendar treats a scheduled event as known 14 days before its nominal
time. The lookahead review found rows flagged scheduled whose date or outcome was NOT
public 14 days ahead (ceasefires, committee outcomes, votes, court rulings, summits, a
delisting announced 7 days ahead, and US releases re-dated after the 2025 federal
shutdown). Those rows are set to scheduled=false here, so they become visible only when
they happen. The verified source calendar is not changed; the corrected copy is written
to user_data/events/events_c.csv and used by phase C only, for the event FEATURES. The
§7.1 blackout keeps reading the original events.csv: a re-dated US release was public days
ahead, so blocking entries around it is not look-ahead.

Known remaining limitation: the originally scheduled 2025-10-03 NFP and 2025-10-15 CPI
releases (cancelled by the shutdown) are not in the calendar, so rows just before
2025-10-01 do not see them as upcoming.
"""
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parent / "events_2022-10_2026-10.csv"
TARGET = Path("/freqtrade/user_data/events/events_c.csv")

# (ts_utc, category): reason. Matched exactly; the script fails if a row is missing.
UNSCHEDULE = {
    ("2023-11-24T05:00:00Z", "GEOPOLITICAL"): "Israel-Hamas truce: deal announced 2023-11-22",
    ("2024-11-27T02:00:00Z", "GEOPOLITICAL"): "Israel-Hezbollah ceasefire: deal announced 2024-11-26",
    ("2025-01-19T09:15:00Z", "GEOPOLITICAL"): "Gaza ceasefire: deal announced 2025-01-15",
    ("2024-12-14T08:00:00Z", "GEOPOLITICAL"): "impeachment vote: scheduled days ahead, outcome unknown",
    ("2025-04-04T02:22:00Z", "GEOPOLITICAL"): "Constitutional Court ruling: date announced 2025-04-01",
    ("2025-08-15T19:00:00Z", "GEOPOLITICAL"): "Trump-Putin summit: announced 2025-08-08",
    ("2025-10-30T02:00:00Z", "TARIFF"): "Trump-Xi summit: confirmed less than 14 days ahead",
    ("2024-05-16T05:00:00Z", "EXCHANGE_INCIDENT"): "Hanbitco closure: announced 2024-05-09",
    ("2022-12-23T00:00:00Z", "REG_KR"): "plenary passage: outcome not known ahead",
    ("2023-06-30T00:00:00Z", "REG_KR"): "plenary passage: outcome not known ahead",
    ("2023-07-18T00:00:00Z", "REG_KR"): "promulgation date not published 14 days ahead",
    ("2024-07-25T00:00:00Z", "REG_KR"): "tax plan announcement: exact date not published 14 days ahead",
    ("2024-11-06T00:00:00Z", "REG_KR"): "FSC committee meeting outcome",
    ("2024-12-10T00:00:00Z", "REG_KR"): "plenary passage: outcome not known ahead",
    ("2025-01-15T00:00:00Z", "REG_KR"): "FSC committee meeting outcome",
    ("2025-02-13T00:00:00Z", "REG_KR"): "FSC committee meeting outcome",
    ("2025-05-02T00:00:00Z", "REG_KR"): "FSC committee meeting outcome",
    ("2026-08-03T09:00:00Z", "REG_KR"): "tax reform plan announcement: exact date not published 14 days ahead",
    ("2025-10-24T12:30:00Z", "CPI"): "re-dated after the 2025 federal shutdown",
    ("2025-11-20T13:30:00Z", "NFP"): "re-dated after the 2025 federal shutdown (date published mid-November)",
    ("2025-11-25T13:30:00Z", "PPI"): "re-dated after the 2025 federal shutdown",
    ("2025-12-05T15:00:00Z", "PCE"): "re-dated after the 2025 federal shutdown",
    ("2025-12-16T13:30:00Z", "NFP"): "re-dated after the 2025 federal shutdown",
    ("2025-12-18T13:30:00Z", "CPI"): "re-dated after the 2025 federal shutdown",
}


def main():
    calendar = pd.read_csv(SOURCE, dtype=str, keep_default_na=False)
    keys = list(zip(calendar.ts_utc, calendar.category, strict=True))
    missing = [k for k in UNSCHEDULE if k not in keys]
    if missing:
        raise SystemExit(f"calendar rows not found: {missing}")
    hit = [k in UNSCHEDULE for k in keys]
    already = calendar.loc[hit, "scheduled"].str.lower().isin(["false", "0"]).sum()
    calendar.loc[hit, "scheduled"] = "false"
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    calendar.to_csv(TARGET, index=False)
    print(f"{sum(hit)} rows set to scheduled=false ({already} already false) -> {TARGET}")


if __name__ == "__main__":
    main()
