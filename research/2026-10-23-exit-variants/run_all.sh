#!/usr/bin/env bash
# Usage (from the spike worktree root): research/2026-10-23-exit-variants/run_all.sh
# Backtests the live SafeTrendLive and three exit variants on the same two spans as the
# 2026-10-21 holdout study, in the separate research compose project (never the live bot).
# Live strategy files are mounted read-only from the main checkout.
set -euo pipefail
export MSYS_NO_PATHCONV=1
L=$(cd "C:/Users/jeong/dev/bitget-ai-bot" && pwd -W)
W=$(pwd -W)
U=/freqtrade/user_data
M="-v $L/freqtrade/user_data/strategies/SafeTrendLive.py:$U/strategies/SafeTrendLive.py:ro"
M="$M -v $L/freqtrade/user_data/strategies/live_rules.py:$U/strategies/live_rules.py:ro"
M="$M -v $L/freqtrade/user_data/events/upcoming_us_macro.csv:$U/events/upcoming_us_macro.csv:ro"
M="$M -v $W/research/2026-10-23-exit-variants/SafeTrendExitVariants.py:$U/strategies/SafeTrendExitVariants.py:ro"
M="$M -v $W/research/2026-10-21-holdout:/research"  # rw: freqtrade rewrites leverage_tiers_USDT.json in the datadir

run() {  # name strategy timerange datadir
  docker compose -p bitget-spike -f freqtrade/docker-compose.yml run --rm $M freqtrade backtesting \
    --config $U/config.backtest.json --config $U/config.plan2-bt.json \
    --strategy "$2" --timerange "$3" --datadir "$4" \
    --export trades --backtest-directory $U/backtest_results/exitvar_$1 > "research/2026-10-23-exit-variants/logs/$1.log" 2>&1     && echo "$1 ok" || echo "$1 FAILED (see logs/$1.log)"
}

mkdir -p research/2026-10-23-exit-variants/logs
IS="20231001-20261020"; HO="20200101-20231001"
ISD=$U/data/bitget_synth_funding_zero; HOD=/research/data_repaired
for s in SafeTrendLive SafeTrendBaselineCopy SafeTrendTrail25 SafeTrendBreakeven SafeTrendPartial; do
  run "is_$s" "$s" "$IS" "$ISD"
  run "ho_$s" "$s" "$HO" "$HOD"
done
echo ALL_DONE
