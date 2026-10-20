"""Plot HTML files with Freqtrade's own plot-dataframe / plot-profit builders.

Indicators are computed on the full history (so the daily EMA200 is warm), then
the candle chart is cut to the last 90 days. Plotly JS is loaded from its CDN
to keep the files small.
Run inside the Freqtrade image with this directory mounted at /research.
"""
import json
import sys
from pathlib import Path

import pandas as pd
from freqtrade.data.btanalysis import load_backtest_data
from freqtrade.data.history.datahandlers.featherdatahandler import FeatherDataHandler
from freqtrade.enums import CandleType
from freqtrade.plot.plotting import generate_candlestick_graph, generate_profit_graph

sys.path.insert(0, "/freqtrade/user_data/strategies")
from SafeTrendV0 import SafeTrendV0  # noqa: E402

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "plots"
DATA = Path("/freqtrade/user_data/data/bitget")
RESULTS = Path("/freqtrade/user_data/backtest_results/spike")
END = pd.Timestamp("2026-10-20", tz="UTC")
LAST90 = END - pd.Timedelta(days=90)
OOS_START = pd.Timestamp("2023-10-01", tz="UTC")
RUNS = {"baseline_oos": "SafeTrendV0", "ai_v0": "SafeTrendAIV0", "tiered": "SafeTrendAITiered"}


def main():
    OUT.mkdir(exist_ok=True)
    summary = json.loads((ROOT / "results" / "summary.json").read_text())
    pairs = [r["pair"] for r in json.loads((ROOT / "pair_selection.json").read_text())["selected"]]
    handler = FeatherDataHandler(DATA)
    strategy = SafeTrendV0({})
    frames = {p: handler.ohlcv_load(p, "1h", candle_type=CandleType.FUTURES, fill_missing=False, drop_incomplete=False) for p in pairs}
    trades = {run: load_backtest_data(RESULTS / run, strat) for run, strat in RUNS.items() if (RESULTS / run).exists()}
    chart_pairs = ["BTC/USDT:USDT"]
    busiest = summary.get("most_traded_non_btc_pair_last90")
    if busiest and busiest not in chart_pairs:
        chart_pairs.append(busiest)
    for pair in chart_pairs:
        full = strategy.populate_indicators(frames[pair].copy(), {"pair": pair})
        recent = full.loc[(full.date >= LAST90) & (full.date < END)].copy()
        for run in ("baseline_oos", "tiered"):
            if run not in trades:
                continue
            t = trades[run]
            t = t.loc[(t.pair == pair) & (t.close_date >= LAST90)]
            fig = generate_candlestick_graph(pair, recent, t, indicators1=["dc_high_4h", "dc_low_4h", "ema50_1d", "ema200_1d"], indicators2=["adx_4h"])
            fig.update_layout(title=f"{run} {pair} last 90 days (UTC)")
            fig.write_html(OUT / f"last90_{run}_{pair.split('/')[0]}.html", include_plotlyjs="cdn")
    # 4h closes keep the profit plots small; trades keep their exact timestamps.
    oos = {p: SafeTrendV0.closed_resample(f.loc[(f.date >= OOS_START) & (f.date < END)], 4) for p, f in frames.items()}
    for run, t in trades.items():
        fig = generate_profit_graph(pairs, oos, t, "4h", "USDT", 105.0)
        fig.update_layout(title=f"{run} out-of-sample profit 2023-10-01 to 2026-10-20")
        fig.write_html(OUT / f"profit_oos_{run}.html", include_plotlyjs="cdn")
    print(sorted(p.name for p in OUT.iterdir()))


if __name__ == "__main__":
    main()
