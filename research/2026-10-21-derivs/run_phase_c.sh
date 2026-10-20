#!/usr/bin/env bash
# Phase C backtests on 10 coins with offline walk-forward p. Run from the worktree root (Git Bash):
#   bash research/2026-10-21-derivs/run_phase_c.sh
# Only `docker compose run --rm`; never `up`; no ports; public data only.
set -u
export MSYS_NO_PATHCONV=1
P="-p bitget-spike -f freqtrade/docker-compose.yml"
U=/freqtrade/user_data
LOGS=freqtrade/user_data/logs/phase_c
mkdir -p "$LOGS"
OUT=$U/backtest_results/spike_c
DD=$U/data/bitget_synth_funding_zero
C="--config $U/config.backtest.json --config $U/config.phase-c.json"
TR=20220101-20261020   # daily EMA200 warmup from 2022-01-01; entries only from 2023-10-01 (config.phase-c.json)
FAILS=$(mktemp)   # names of failed runs (background jobs cannot set a shared variable)

bt() {  # name, args...
  local name=$1; shift
  mkdir -p "freqtrade/user_data/backtest_results/spike_c/$name"
  sleep "${STAGGER:-0}"   # spread Bitget market-metadata requests (HTTP 429 otherwise)
  echo "[$(date +%H:%M:%S)] start $name"
  docker compose $P run --rm freqtrade backtesting $C "$@" --timerange $TR --datadir $DD --export trades --backtest-directory "$OUT/$name" > "$LOGS/$name.log" 2>&1
  local rc=$?   # capture before $(date) below resets $?
  if [ "$rc" -eq 0 ] && grep -q "Traceback" "$LOGS/$name.log"; then rc=1; fi   # a strategy crash can still exit 0
  echo "[$(date +%H:%M:%S)] done  $name (exit $rc)"
  if [ "$rc" -ne 0 ]; then echo "$name" >> "$FAILS"; fi
}

# Pooled model (pre-registered) and per-coin control (_pc, added after review): p>=0.5 filter and tiers.
SETS="ind ind_ev ind_der all ind_pc ind_ev_pc ind_der_pc all_pc"
jobs=("baseline_oos --strategy SafeTrendV0")
for v in $SETS; do jobs+=("filter_$v --config $U/config.pc-$v.json --strategy SafeTrendPrecomp"); done
for v in $SETS; do jobs+=("tiered_$v --config $U/config.pc-$v.json --strategy SafeTrendPrecompTiered"); done
i=0
for job in "${jobs[@]}"; do
  # shellcheck disable=SC2086
  STAGGER=$(( (i % 5) * 8 )) bt $job &
  i=$((i + 1))
  if [ $((i % 5)) -eq 0 ]; then wait; fi   # at most 5 backtests at once
done
wait
if [ -s "$FAILS" ]; then echo "FAILED: $(tr '\n' ' ' < "$FAILS")"; rm -f "$FAILS"; exit 1; fi
rm -f "$FAILS"
