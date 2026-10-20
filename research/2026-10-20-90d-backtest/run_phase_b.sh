#!/usr/bin/env bash
# Phase B backtests. Run from the worktree root in Git Bash:
#   bash research/2026-10-20-90d-backtest/run_phase_b.sh [group...]
# Groups: base (baseline + independent FreqAI trainings), reuse (runs that reuse
# saved walk-forward predictions), lookahead. Default: base reuse.
# Only `docker compose run --rm`; never `up`; no ports; public data only.
set -u
export MSYS_NO_PATHCONV=1
P="-p bitget-spike -f freqtrade/docker-compose.yml"
F="$P -f research/2026-10-20-90d-backtest/compose.freqai.yml"
U=/freqtrade/user_data
LOGS=freqtrade/user_data/logs/phase_b
mkdir -p "$LOGS"
OUT=$U/backtest_results/spike_b
TR=20231001-20261020          # FreqAI: data loads from 2022-01-01 (indicator_periods 6552h + 365d training)
AI="--config $U/config.backtest-ai.json --config $U/config.phase-b.json"
# Zero-funding engine datadir (review fix 1). Passed on the CLI: the config "datadir" key alone was
# not honoured, and Freqtrade then silently used real funding + the futures_funding_rate fallback.
DD=$U/data/bitget_synth_funding_zero

bt() {  # name, compose-service-args, strategy+config args...
  local name=$1; shift
  mkdir -p "freqtrade/user_data/backtest_results/spike_b/$name"
  sleep "${STAGGER:-0}"   # spread Bitget market-metadata requests (HTTP 429 otherwise)
  echo "[$(date +%H:%M:%S)] start $name"
  docker compose "$@" --datadir $DD --export trades --backtest-directory "$OUT/$name" > "$LOGS/$name.log" 2>&1
  echo "[$(date +%H:%M:%S)] done  $name (exit $?)"
}

base() {
  STAGGER=0 bt baseline_4y  $P run --rm freqtrade backtesting --config $U/config.backtest.json --config $U/config.phase-b.json --strategy SafeTrendV0 --timerange 20220101-20261020 &
  STAGGER=8 bt baseline_oos $P run --rm freqtrade backtesting --config $U/config.backtest.json --config $U/config.phase-b.json --config $U/config.oos-window.json --strategy SafeTrendV0 --timerange 20220101-20261020 &
  STAGGER=16 bt aiv0_raw $F run --rm freqai backtesting $AI --config $U/config.p2-raw.json --strategy SafeTrendAIV0 --freqaimodel LightGBMClassifier --timerange $TR &
  STAGGER=24 bt aiv0_cal $F run --rm freqai backtesting $AI --config $U/config.p2-aiv0cal.json --strategy SafeTrendAIV0 --freqaimodel SpikeCalibratedLGBM --timerange $TR &
  STAGGER=32 bt aiv1_cal $F run --rm freqai backtesting $AI --config $U/config.p2-aiv1cal.json --strategy SafeTrendAIV1 --freqaimodel SpikeCalibratedLGBM --timerange $TR &
  STAGGER=40 bt aiv1_raw $F run --rm freqai backtesting $AI --config $U/config.p2-aiv1raw.json --strategy SafeTrendAIV1 --freqaimodel SpikeCalibratedLGBM --timerange $TR &
  wait
}

reuse() {
  STAGGER=0 bt tiered_raw $F run --rm freqai backtesting $AI --config $U/config.p2-raw.json --strategy SafeTrendAITiered --freqaimodel LightGBMClassifier --timerange $TR &
  STAGGER=8 bt tiered_v1 $F run --rm freqai backtesting $AI --config $U/config.p2-aiv1cal.json --strategy SafeTrendTieredV1 --freqaimodel SpikeCalibratedLGBM --timerange $TR &
  STAGGER=16 bt tiered_v1_plus $F run --rm freqai backtesting $AI --config $U/config.p2-aiv1cal.json --config $U/config.p2-portfolio.json --strategy SafeTrendTieredV1Plus --freqaimodel SpikeCalibratedLGBM --timerange $TR &
  STAGGER=24 bt tiered_v1_tp2 $F run --rm freqai backtesting $AI --config $U/config.p2-aiv1cal.json --strategy SafeTrendTieredV1TP2 --freqaimodel SpikeCalibratedLGBM --timerange $TR &
  STAGGER=32 bt tiered_v1_raw $F run --rm freqai backtesting $AI --config $U/config.p2-aiv1raw.json --strategy SafeTrendTieredV1 --freqaimodel SpikeCalibratedLGBM --timerange $TR &
  STAGGER=40 bt tiered_v1_plus_raw $F run --rm freqai backtesting $AI --config $U/config.p2-aiv1raw.json --config $U/config.p2-portfolio.json --strategy SafeTrendTieredV1Plus --freqaimodel SpikeCalibratedLGBM --timerange $TR &
  wait
}

lookahead() {
  R=/research/results
  RV="-v $(pwd -W)/research/2026-10-20-90d-backtest:/research"
  la() {  # name, args...
    local name=$1; shift
    echo "[$(date +%H:%M:%S)] start lookahead $name"
    docker compose "$@" --datadir $DD --lookahead-analysis-exportfilename $R/lookahead_$name.csv > research/2026-10-20-90d-backtest/results/lookahead_$name.txt 2>&1
    echo "[$(date +%H:%M:%S)] done  lookahead $name (exit $?)"
  }
  la SafeTrendV0 $P run --rm $RV freqtrade lookahead-analysis --config $U/config.backtest.json --config $U/config.phase-b.json --strategy SafeTrendV0 --timerange 20220101-20240101 &
  sleep 8
  # Uncalibrated p (phase A style) gives enough trades for the default 10/20 signal test.
  la SafeTrendAIV0_raw $F run --rm $RV freqai lookahead-analysis $AI --config $U/config.p2-lookahead-aiv0raw.json --strategy SafeTrendAIV0 --freqaimodel LightGBMClassifier --timerange 20231001-20250101 &
  sleep 8
  la SafeTrendAIV1_raw $F run --rm $RV freqai lookahead-analysis $AI --config $U/config.p2-lookahead-aiv1raw.json --strategy SafeTrendAIV1 --freqaimodel SpikeCalibratedLGBM --timerange 20231001-20250101 &
  sleep 8
  # Calibrated p trades rarely (~12 trades in 3 years): whole OOS window, lower minimum.
  la SafeTrendAIV0 $F run --rm $RV freqai lookahead-analysis $AI --config $U/config.p2-lookahead-aiv0.json --strategy SafeTrendAIV0 --freqaimodel SpikeCalibratedLGBM --timerange 20231001-20261020 --minimum-trade-amount 3 --targeted-trade-amount 12 &
  sleep 8
  la SafeTrendAIV1 $F run --rm $RV freqai lookahead-analysis $AI --config $U/config.p2-lookahead-aiv1.json --strategy SafeTrendAIV1 --freqaimodel SpikeCalibratedLGBM --timerange 20231001-20261020 --minimum-trade-amount 3 --targeted-trade-amount 12 &
  wait
}

groups=("$@")
[ ${#groups[@]} -eq 0 ] && groups=(base reuse)
for g in "${groups[@]}"; do "$g"; done
