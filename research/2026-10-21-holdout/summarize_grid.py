"""Summarize the Task B grid backtests into research/2026-10-21-holdout/grid_results.json.

Usage (inside the freqtrade container, or any Python with only the stdlib):
  python summarize_grid.py <result_zip_p1> <result_zip_p2> <out_json>

Each zip is one Freqtrade --strategy-list result. Pulls trades / net % / PF / max DD per
strategy and labels each grid point.
"""
import json
import re
import sys
import zipfile
from pathlib import Path

NAME = re.compile(r"Grid_d(\d+)_s(\d+)p(\d)_c(\d+)p(\d)_a(\d+)")


def params(name: str) -> dict | None:
    m = NAME.fullmatch(name)
    if not m:
        return None
    d, s1, s2, c1, c2, a = m.groups()
    return {"dc_len": int(d), "stop_atr": float(f"{s1}.{s2}"), "trail_atr": float(f"{c1}.{c2}"), "adx_min": int(a)}


def load(path: Path) -> dict:
    with zipfile.ZipFile(path) as zf:
        member = next(n for n in zf.namelist() if n.endswith(".json") and "_config" not in n and "meta" not in n)
        data = json.loads(zf.read(member))
    rows = {}
    for name, st in data["strategy"].items():
        trades = st.get("trades", [])
        gross_win = sum(t["profit_abs"] for t in trades if t["profit_abs"] > 0)
        gross_loss = -sum(t["profit_abs"] for t in trades if t["profit_abs"] < 0)
        exits = {}
        for t in trades:
            exits[t["exit_reason"]] = exits.get(t["exit_reason"], 0) + 1
        rows[name] = {
            "params": params(name),
            "trades": st["total_trades"],
            "wins": st.get("wins"),
            "profit_abs": round(st["profit_total_abs"], 3),
            "net_pct": round(100 * st["profit_total"], 2),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
            "max_dd_pct": round(100 * st.get("max_drawdown_account", 0.0), 2),
            "max_rel_dd_pct": round(100 * st.get("max_relative_drawdown", 0.0), 2),
            "exit_reasons": exits,
            "start": st.get("backtest_start"),
            "end": st.get("backtest_end"),
            "market_change_pct": round(100 * st.get("market_change", 0.0), 1),
        }
    return {"result_file": path.name, "rows": rows}


def main() -> None:
    p1, p2, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    result = {"p1_2023-10-01_2026-10-20": load(p1), "p2_2021-06-01_2023-09-30": load(p2)}
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    for period, block in result.items():
        print(period, block["result_file"])
        for name, r in block["rows"].items():
            print(f"  {name:28s} n={r['trades']:3d} net={r['net_pct']:6.2f}% pf={r['profit_factor']} "
                  f"dd={r['max_dd_pct']:5.2f}% rdd={r['max_rel_dd_pct']:5.2f}% abs={r['profit_abs']:7.3f} {r['exit_reasons']}")


if __name__ == "__main__":
    main()
