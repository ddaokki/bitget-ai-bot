"""Phase C analysis: same money rules as phase B (conservative funding, hourly
mark-to-market drawdown, post-mortem), reusing analyze_b.py's functions.

  python -P /research/analyze_c.py      (inside the FreqAI image; phase B dir at /research_b)

Writes results/summary_c.json, results/trades_postmortem_c.csv, results/postmortem_c_by_*.csv.
"""
import json
import sys
from pathlib import Path

import pandas as pd
from freqtrade.data.btanalysis import load_backtest_data

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, "/research_b")
sys.path.insert(0, "/freqtrade/user_data/strategies")
import analyze_b as ab  # noqa: E402
from event_impact import load_calendar  # noqa: E402

OUT = ROOT / "results"
RESULTS = Path("/freqtrade/user_data/backtest_results/spike_c")
SETS = ("ind", "ind_ev", "ind_der", "all", "ind_pc", "ind_ev_pc", "ind_der_pc", "all_pc")   # _pc = per-coin control
RUNS = {"baseline_oos": ("SafeTrendV0", ab.OOS, "reference (no model)", None)}
for v in SETS:
    RUNS[f"filter_{v}"] = ("SafeTrendPrecomp", ab.OOS, f"phase C p>=0.5 [{v}]{' per-coin control' if v.endswith('_pc') else ' pooled'}", v)
    RUNS[f"tiered_{v}"] = ("SafeTrendPrecompTiered", ab.OOS, f"phase C tiers+blackout [{v}]{' per-coin control' if v.endswith('_pc') else ' pooled'}", v)
ab.RUNS = RUNS
ab.LOGS = Path("/freqtrade/user_data/logs/phase_c")
KEYS = ("trades", "win_rate", "net_pct", "mdd_mtm_pct", "pf", "net_2024_plus_pct", "net_last6m_pct", "trades_last6m", "max_concurrent_positions", "tiers")


def main():
    calendar = load_calendar(ab.CALENDAR)
    loaded = {}
    for run, (strategy, start, _, _) in RUNS.items():
        path = RESULTS / run
        if not (path / ".last_result.json").is_file():   # run_phase_c.sh creates the folder before the run
            print("missing run", run)
            continue
        trades = load_backtest_data(path, strategy)
        assert trades.empty or trades.open_date.min() >= start, f"{run} trades before {start}"
        assert trades.empty or float(trades.funding_fees.abs().sum()) < 1e-9, f"{run}: engine charged funding"
        loaded[run] = (start, trades)
    pairs = sorted({p for _, t in loaded.values() if not t.empty for p in t.pair.unique()} | {"BTC/USDT:USDT"})
    candles, marks = ab.load_candles(pairs)
    indexed = {p: f.set_index("date") for p, f in candles.items()}
    closes = {p: f.close for p, f in indexed.items()}
    btc = candles["BTC/USDT:USDT"]
    summary = {"funding_model": "both sides pay 0.01%/8h at each 00/08/16 UTC crossing (mark notional)",
               "mdd": "hourly mark-to-market", "wallet_usdt": ab.WALLET, "runs": {},
               "btc_hold": {"oos": ab.bnh(btc, ab.OOS, ab.END), "from_2024": ab.bnh(btc, ab.Y2024, ab.END), "last6m": ab.bnh(btc, ab.LAST6M, ab.END)}}
    pms, equities = [], {}
    for run, (start, trades) in loaded.items():
        if trades.empty:   # the model never let a signal through: no money moved
            summary["runs"][run] = {"kind": RUNS[run][2], "strategy": RUNS[run][0], "trades": 0, "net_pct": 0.0, "mdd_mtm_pct": 0.0,
                                    "blackout_blocked_signals": ab.blackout_counts(run)}
            continue
        trades = ab.enrich(run, trades, marks)
        m, equity = ab.metrics(run, trades, start, closes, btc)
        m["blackout_blocked_signals"] = ab.blackout_counts(run)
        m["by_pair"] = trades.groupby("pair").net.agg(["size", "sum"]).round(2).rename(columns={"size": "trades", "sum": "net_usdt"}).to_dict(orient="index") if len(trades) else {}
        summary["runs"][run] = m
        equities[run] = equity
        if len(trades):
            pms.append(ab.postmortem(run, trades, indexed, calendar))
    pd.DataFrame(equities).to_csv(OUT / "equity_mtm_hourly_c.csv.gz", compression="gzip")
    if pms:
        pm = pd.concat(pms, ignore_index=True)
        pm.drop(columns=["open_utc", "close_utc"]).to_csv(OUT / "trades_postmortem_c.csv", index=False)
        for by in ("cause", "tier", "year", "side", "pair"):
            ab.group_table(pm, by).to_csv(OUT / f"postmortem_c_by_{by}.csv", index=False)
        summary["postmortem_cause_counts"] = pm.groupby(["run", "cause"]).size().unstack(fill_value=0).to_dict(orient="index")
    phase_b = json.loads(Path("/research_b/results/phase_b/summary_b.json").read_text(encoding="utf-8"))["runs"]
    summary["phase_b_reference"] = {r: {k: phase_b[r].get(k) for k in KEYS} for r in ("baseline_oos", "aiv1_cal", "tiered_v1") if r in phase_b}
    (OUT / "summary_c.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps({r: {k: m.get(k) for k in KEYS} for r, m in summary["runs"].items()}, indent=1, default=str))


if __name__ == "__main__":
    main()
