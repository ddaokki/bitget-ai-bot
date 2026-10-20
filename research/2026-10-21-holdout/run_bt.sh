#!/usr/bin/env bash
# Usage: research/2026-10-21-holdout/run_bt.sh <result-dir-name> <timerange> [extra freqtrade args...]
# Runs the UNCHANGED SafeTrendLive from the main checkout (read-only bind mounts) in the
# research compose project. Run from the worktree root. Set DATADIR to override the datadir.
set -euo pipefail
export MSYS_NO_PATHCONV=1
name=$1; range=$2; shift 2
L=$(cd "C:/Users/jeong/dev/bitget-ai-bot" && pwd -W)
W=$(pwd -W)
U=/freqtrade/user_data
M="-v $L/freqtrade/user_data/strategies/SafeTrendLive.py:$U/strategies/SafeTrendLive.py:ro -v $L/freqtrade/user_data/strategies/live_rules.py:$U/strategies/live_rules.py:ro -v $L/freqtrade/user_data/events/upcoming_us_macro.csv:$U/events/upcoming_us_macro.csv:ro -v $W/research/2026-10-21-holdout:/research:ro"
docker compose -p bitget-spike -f freqtrade/docker-compose.yml run --rm $M freqtrade backtesting \
  --config $U/config.backtest.json --config $U/config.plan2-bt.json "$@" \
  --strategy SafeTrendLive --timerange "$range" --datadir "${DATADIR:-$U/data/bitget_synth_funding_zero}" \
  --export trades --backtest-directory $U/backtest_results/$name
