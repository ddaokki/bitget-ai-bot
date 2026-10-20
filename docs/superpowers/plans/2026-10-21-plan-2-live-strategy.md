# Plan 2: Live-Capable Safe Strategy (BTC long only) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the manual-only `SmokeTest` strategy with a rule-based strategy that can run in dry-run and, when the user switches it, small live trading on Bitget.

**Architecture:** `SafeTrendLive` is a Freqtrade strategy. It ports the research baseline (SafeTrendV0: 4h Donchian breakout in the daily EMA50>EMA200 trend, 2xATR stop, 3xATR chandelier, Donchian-10 exit). It reads native Bitget 4h and 1d candles through `@informative`, because Bitget serves only ~990 startup 1h candles, too few for a daily EMA200. Sizing and the spec §7.4 entry caps live in a pure module `live_rules.py`, tested without Freqtrade. Manual and smoke entries keep working: their stake and stop pass through unchanged.

**Tech Stack:** Freqtrade 2026.9 (pinned image), Python 3, TA-Lib, pytest (repo venv), Docker Compose.

**Spec:** `docs/superpowers/specs/2026-10-19-bitget-ai-bot-design.md` (§7.1, §7.4, §13, §18) plus the amendment added in Task 2 (§7.1b). Research basis: branch `spike/90d-backtest`, `research/2026-10-21-derivs/README.md`.

**Execution method:** native (the user asked for autonomous overnight work and said "그냥 해"). Claude never switches to live and never places orders. The user turns live on with `set-mode live` or the panel (typing LIVE).

## Global Constraints

- Freqtrade image digest `sha256:989aff5636d8f0213ed44fe334c3314a194a51c0c755d16866960247aafd2eae`. No new Python dependencies.
- Isolated margin, one-way mode, Classic account (`uta: false`).
- Pair `BTC/USDT:USDT` only, long only (`can_short = False`), `max_open_trades = 1`.
- Leverage 2. Risk per strategy trade 0.5% of the wallet at the initial stop. If the exchange minimum forces more than 1.5x the target risk, skip the trade (spec §7.1).
- Initial stop is entry − 2×ATR(14, 4h). The chandelier stop is the highest 1h high since entry − 3×ATR. The stop only ratchets up. Exit when a 4h close is below the prior 10-bar Donchian low.
- Entry needs all of: 4h close above the prior 20-bar Donchian high, ADX(4h) > 20, ATR% ≤ its prior-180-bar 90th percentile, and daily EMA50 > EMA200. Entries happen only on the 1h row where a 4h bar just closed.
- Entry caps (spec §7.4, entries only):
  - at most 3 strategy entries per UTC day;
  - no entry after realized loss ≥ 3% of the wallet today (UTC);
  - no entry after realized loss ≥ 6% this ISO week.
- Freqtrade protections:
  - CooldownPeriod: 4 candles;
  - StoplossGuard: 2 stops in 24 candles → stop 24 candles;
  - MaxDrawdown: 10% over 720 candles → stop 168 candles.
- `stoploss = -0.50` is only a net. The real stop is set by `custom_stoploss`, and the exchange stop follows it (`stoploss_on_exchange: true`, mark price).
- Market orders for entries and exits (research assumed taker fills at the next open). Pricing `price_side: "other"`.
- Manual or smoke trades (enter_tag not starting with `safe_`) keep the given stake and get a fixed stop of 3% below the open.
- Event blackout: §7.1 needs future release dates, and the research calendar ends 2026-10-16. Read `user_data/events/upcoming_us_macro.csv` (`ts_utc,category`; FOMC/CPI/PPI/NFP) and block entries within ±2h. A missing or empty file means no blackout, and the strategy logs a warning once.

## Review Focus

- Bitget returns fewer daily candles than requested at startup, leaving EMA200 NaN. Expected: no entries while daily EMA is NaN (test: NaN EMA → no signal).
- A wallet too small for the exchange minimum (0.0001 BTC, 5 USDT). Expected: the trade is skipped, never rounded up (test in `live_rules`).
- Restart with an open position and no stored `entry_atr`. Expected: the stop is recomputed from the last ATR before the open date (test: stop from history only).
- A trade closed today at a loss below the daily cap, then another signal. Expected: allowed; at the cap, blocked (test on cap boundaries).
- Smoke or forced entry while the strategy is loaded. Expected: the given stake and a 3% stop are kept (test on the tag rule).

---

### Task 1: Pure live rules (sizing, caps, stop)

**Files:**
- Create: `freqtrade/user_data/strategies/live_rules.py`
- Test: `tests/test_live_rules.py` (loads the module by path; no Freqtrade import)

**Interfaces:**
- Produces:
  - `exchange_min_margin(min_amount: float|None, min_cost: float|None, contract_size: float, rate: float, leverage: float) -> float`
  - `risk_stake(wallet: float, rate: float, atr: float, leverage: float, minimum: float, maximum: float, risk: float = 0.005, stop_atr: float = 2.0) -> float` (0.0 means skip)
  - `chandelier_stop(entry: float, entry_atr: float, highest_since_entry: float|None, current_atr: float, previous: float|None, stop_atr: float = 2.0, trail_atr: float = 3.0) -> float`
  - `entry_allowed(today_entries: int, today_realized: float, week_realized: float, wallet: float) -> tuple[bool, str]` with constants `MAX_ENTRIES_PER_DAY = 3`, `DAILY_LOSS_CAP = 0.03`, `WEEKLY_LOSS_CAP = 0.06`
  - `is_strategy_tag(tag: str|None) -> bool` (True when the tag starts with `safe_`)
  - `blackout(entry_time, release_times: list, hours: float = 2.0) -> bool`

- [ ] **Step 1: Write the failing tests**
  - `risk_stake(114, 80000, 1000, 2, 5, 100)` equals `114*0.005/((2*1000/80000)*2)`.
  - The minimum is above 1.5x target → 0.0. The minimum is between 1x and 1.5x → returns the minimum.
  - NaN ATR → 0.0.
  - `chandelier_stop` cases:
    - no high yet → entry − 2·atr;
    - a high well above → high − 3·atr when that is higher;
    - never below `previous`.
  - `entry_allowed` cases:
    - (3, 0, 0, 100) → False;
    - (2, -2.99, 0, 100) → True;
    - (0, -3.0, 0, 100) → False;
    - (0, 0, -6.0, 100) → False.
  - Tags: `safe_long` → True; `smoke`, `force_entry`, `None` → False.
  - `blackout` cases: ±2h inclusive edges → True; 2h01m away → False; empty list → False.
- [ ] **Step 2:** Run `.venv/Scripts/python -m pytest tests/test_live_rules.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3:** Implement `live_rules.py` with only `math`, `datetime` and the standard library.
- [ ] **Step 4:** Run the same command. Expected: all pass.
- [ ] **Step 5:** Commit `feat(strategy): pure live sizing, stop and entry-cap rules`.

### Task 2: SafeTrendLive strategy, config, compose, spec amendment

**Files:**
- Create: `freqtrade/user_data/strategies/SafeTrendLive.py`, `freqtrade/user_data/events/upcoming_us_macro.csv`
- Modify:
  - `freqtrade/user_data/config.json`: entry/exit market, `price_side` other, `use_order_book` false, `dry_run_wallet` 114, `stake_amount` "unlimited";
  - `freqtrade/docker-compose.yml`: `--strategy SafeTrendLive`;
  - `tests/test_freqtrade_config.py`: the expected strategy and order types;
  - the spec: add §7.1b.
- Test: `tests/test_freqtrade_config.py`, plus the Task 3 docker checks.

**Interfaces:**
- Consumes: Task 1 functions.
- Produces: the strategy class `SafeTrendLive`. Columns `enter_long`, `exit_long`, `enter_tag` (`safe_long`) and `exit_tag` (`opposite_donchian_10`). Trade custom data keys `entry_atr`, `entry_open`, `stop_ratchet`.

- [ ] **Step 1:** Update `tests/test_freqtrade_config.py`:
  - the compose command ends with `--strategy SafeTrendLive`;
  - `order_types.entry == "market"` and `order_types.exit == "market"`;
  - `entry_pricing.price_side == exit_pricing.price_side == "other"`;
  - `stoploss_on_exchange` is true and `stoploss_price_type == "mark"`.

  Run pytest. Expected: FAIL.
- [ ] **Step 2:** Change the config and compose. Run pytest. Expected: PASS.
- [ ] **Step 3:** Write `SafeTrendLive.py`:
  - `@informative("4h")`: ATR, ADX, RSI, `dc_high`/`dc_low` (20, shift 1), `exit_low`/`exit_high` (10, shift 1), `atr_pct`, `atr_q90` (180, min_periods 180, shift 1).
  - `@informative("1d")`: `ema50` and `ema200`.
  - `four_close = date == date_4h + 3h`.
  - Entry and exit as in Global Constraints.
  - `custom_stake_amount`, `custom_stoploss`, `leverage` and `confirm_trade_entry` all go through `live_rules`. Realized P&L comes from `Trade.get_trades_proxy(is_open=False)` closed since 00:00 UTC and since Monday 00:00 UTC.
  - `protections` property as in Global Constraints.
  - `startup_candle_count = 990`.
  - The blackout file is read once in `bot_start`.
- [ ] **Step 4:** Add spec §7.1b "실전 1차 (계획 2)" in Korean. Record:
  - the user's decision on 2026-10-20/21 (small live with known risk, the user switches live);
  - the BTC long-only choice and its in-sample caveat;
  - the 0.5% risk;
  - that the §7.4 watchdog stays plan 4 and plan 2 only blocks entries.
- [ ] **Step 5:** Commit `feat(strategy): SafeTrendLive BTC long-only with exchange stop and entry caps`.

### Task 3: Verification (docker, research data)

**Files:**
- Create: `docs/runbooks/live.md` (Korean)
- Modify: `docs/runbooks/panel.md`, adding a short "실전 전환" section linking `live.md`.

- [ ] **Step 1:** Run `list-strategies` inside the image. Expected: `SafeTrendLive OK`.
- [ ] **Step 2:** Backtest `BTC/USDT:USDT`, timerange 20231001-20261020, on the spike project's zero-funding datadir with 4h/1d BTC candles downloaded.

  Expected:
  - trade count close to the research baseline BTC longs (51; exact match is not required because of protections and caps);
  - net within reason of half the research P&L at half the risk.

  Record the numbers in `live.md`.
- [ ] **Step 3:** Run `lookahead-analysis` on the same data. Expected: no biased entries or exits.
- [ ] **Step 4:** Write `live.md`:
  - what runs;
  - the risks in plain words (no proven edge; chosen on the same data; at most about 0.57 USDT per trade, 3 trades a day);
  - exact steps for the user to go live (panel mode switch + LIVE, then the `check` step after the first trade);
  - how to stop it (emergency close, `set-mode dry`).
- [ ] **Step 5:** Commit `docs: live runbook and plan 2 verification results`.

### Task 4: Deploy to the local bot in dry-run

- [ ] **Step 1:** Run the full test suite (`.venv/Scripts/python -m pytest -q`). Expected: all pass.
- [ ] **Step 2:** Merge `feat/plan-2-live` into `master` locally. The user asked for it to be running by morning. Do not push.
- [ ] **Step 3:** Restart the `freqtrade` container from the main checkout. Confirm `dry_run` is true in the effective config, then start the bot through the API.
- [ ] **Step 4:** Verify:
  - `/api/v1/show_config` shows strategy `SafeTrendLive` and `dry_run: true`;
  - the logs show the BTC 1h/4h/1d pairs analyzed with no errors;
  - the panel `/api/status` is healthy.
- [ ] **Step 5:** Leave it running. Write the morning report.
