"""Spec §18 event impact study over the WHOLE period (REPORT ONLY, never used for training).

  python -P /research/event_impact_report.py

For each region x category group: BTC/ETH mean |1h log return| in [0, +6h) after
the event (from its availability time) vs a matched baseline (same weekday-hour,
+-6 weeks, non-event hours) -> volatility ratio, n, bootstrap 90% CI; also the
pre-event [-2h, 0) ratio. Groups with ratio < 1.15 or n < 8 are 'dropped'.
"""
import json
import sys
from pathlib import Path

import pandas as pd
from freqtrade.data.history.datahandlers.featherdatahandler import FeatherDataHandler
from freqtrade.enums import CandleType

sys.path.insert(0, "/freqtrade/user_data/strategies")
from event_impact import MIN_EVENTS, RATIO_FLOOR, impact_table, load_calendar  # noqa: E402

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "results"
CALENDAR = Path("/freqtrade/user_data/events/events.csv")
DATA = Path("/freqtrade/user_data/data/bitget")
END = pd.Timestamp("2026-10-20", tz="UTC")
REGION_KO = {"US": "미국", "KR": "한국", "CRYPTO": "크립토", "GLOBAL": "세계", "EU": "유럽", "JP": "일본", "CN": "중국", "UK": "영국", "MIDEAST": "중동", "RU_UA": "러·우", "OTHER": "기타"}
CATEGORY_KO = {
    "FOMC": "연준 금리결정", "CPI": "소비자물가", "PPI": "생산자물가", "NFP": "고용지표", "PCE": "개인소비지출 물가", "GDP": "GDP",
    "REG_US": "규제·당국 조치", "REG_KR": "규제·당국 조치", "EXCHANGE_INCIDENT": "거래소·해킹 사고", "CENTRAL_BANK": "중앙은행 결정",
    "GEOPOLITICAL": "지정학(전쟁·분쟁)", "TARIFF": "관세", "ETF": "ETF", "STABLECOIN": "스테이블코인", "REGULATION": "규제",
    "SANCTIONS": "제재", "OTHER": "기타",
}


def main():
    calendar = load_calendar(CALENDAR)
    handler = FeatherDataHandler(DATA)
    prices = {}
    for pair in ("BTC/USDT:USDT", "ETH/USDT:USDT"):
        frame = handler.ohlcv_load(pair, "1h", candle_type=CandleType.FUTURES, fill_missing=False, drop_incomplete=False)
        prices[pair] = frame.set_index("date").close
    table = impact_table(calendar, prices, end=END, bootstrap=2000)
    table.insert(1, "name_ko", [f"{REGION_KO.get(r, r)} {CATEGORY_KO.get(c, c)}" for r, c in zip(table.region, table.category, strict=True)])
    table = table.round({"ratio": 3, "ci_lo": 3, "ci_hi": 3, "ratio_btc": 3, "ratio_eth": 3, "pre_ratio": 3, "ratio_exact_only": 3})
    OUT.mkdir(exist_ok=True)
    table.to_csv(OUT / "event_impact.csv", index=False)
    summary = {
        "rule": f"kept if ratio >= {RATIO_FLOOR} and n >= {MIN_EVENTS}; ratio = sum post |ret| / sum matched baseline, BTC+ETH pooled",
        "calendar_rows": int(len(calendar)), "groups": int(len(table)),
        "kept": int(table.kept.sum()), "dropped": int((~table.kept).sum()),
        "events_measured": int(table.n.sum()),
    }
    (OUT / "event_impact_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(table.to_string())


if __name__ == "__main__":
    main()
