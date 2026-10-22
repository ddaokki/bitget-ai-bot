# Post-mortem Archive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every closed Freqtrade trade gets one JSON + one short Korean MD post-mortem (facts, market path, sourced/guessed world events, verdict) within minutes, with a read-only report list in the panel.

**Architecture:** A `bitgetbot/postmortem` package. A watcher thread started by the panel polls Freqtrade read-only every 60 s, writes a facts-first report, then runs `claude -p` (WebSearch only, stream-json) to research events; the report JSON is its own retry state. The panel serves the index and one report read-only.

**Tech Stack:** Python 3.13, httpx (via existing `FreqtradeClient`), ccxt public Bitget endpoints, FastAPI, vanilla JS, pytest.

**Spec:** `docs/superpowers/specs/2026-10-22-postmortem-archive-design.md`

## Global Constraints

- Read-only: Freqtrade `show_config`, `trades`; Bitget public `publicMixGetV2MixMarketCandles`, `publicMixGetV2MixMarketHistoryFundRate`, `publicMixGetV2MixMarketOpenInterest` (no keys).
- Paths relative to the repo root: `reports/postmortem/`, work dir `reports/postmortem/.work`, lock `reports/postmortem/.lock`.
- Key `<mode>-<trade_id>-<open_ts>`, regex `(dry|live)-[0-9]{1,12}-[0-9]{1,16}`.
- Poll 60 s; trades page 50; backfill 30 days; skip `enter_tag == "smoke"`.
- Retry delays after attempt n (1-based): 10 min, 30 min, 2 h, 6 h; quota → at least 1 h; `MAX_ATTEMPTS = 5`; in-progress stale after 30 min; lock stale after 20 min; research timeout 600 s.
- claude args: `-p --safe-mode --no-session-persistence --model sonnet --effort medium --tools WebSearch --allowedTools WebSearch --permission-mode dontAsk --output-format stream-json --verbose --json-schema <schema> --system-prompt <prompt>`; prompt on stdin; no secrets in prompt.
- Tests never touch real network, subprocesses or `claude`.
- Reports are git-ignored (`reports/`).

## Review Focus

- Freqtrade returns a closed trade with `stake_amount` 0/None or missing `close_rate` → report still written, P&L % null, no crash (Task 1 test `test_facts_tolerate_missing_numbers`).
- Panel restarts mid-research → the next cycle after 30 min treats it as a failed attempt, not a duplicate or a stuck file (Task 5 test `test_stale_in_progress_is_retried`).
- LLM returns a URL it never searched, or a timestamp outside the window → downgraded to guess / dropped (Task 3 tests).
- Bot switches dry↔live while a report is pending → pending report still retries from stored facts (Task 5 test `test_pending_retries_without_freqtrade`).
- Corrupt or half-written report JSON on disk → treated as missing only if unparsable, never crashes the loop or the panel list (Task 4/5 tests `test_index_skips_corrupt_files`, `test_corrupt_report_is_rewritten`).

---

### Task 1: Trade facts (`facts.py`)

**Files:** Create `bitgetbot/postmortem/__init__.py`, `bitgetbot/postmortem/facts.py`; Test `tests/postmortem/test_facts.py` (+ `tests/postmortem/__init__.py`).

**Interfaces — Produces:**
- `report_key(mode: str, trade_id: int, open_ts: int) -> str`
- `KEY_RE: re.Pattern`
- `closed_trades(history: dict, now_ms: int, backfill_days: int = 30) -> list[dict]` — closed, non-smoke, valid rows, oldest close first.
- `trade_facts(row: dict, mode: str) -> dict` — keys: `trade_id, key, mode, pair, symbol, side, kind, enter_tag, exit_reason, exit_category, exit_label_ko, open_ts, close_ts, open_utc, open_kst, close_utc, close_kst, hold_minutes, entry_price, exit_price, amount, leverage, margin_usdt, pnl_usdt, pnl_pct_margin, profit_ratio, funding_usdt, max_rate, min_rate, mfe_pct, mae_pct, stop_loss_abs, initial_stop_loss_abs, source`.
- `refine_manual_exit(facts: dict, audit_path: Path) -> dict` — for `force_exit`, sets `exit_detail` to `panel_close`/`emergency`/`unknown`.
- `calendar_events(csv_path: Path, start_ms: int, end_ms: int) -> list[dict]` — `{ts_utc, category, title, region:"US", source:"upcoming_us_macro.csv", source_url}`.
- `iso_utc(ms) -> str`, `iso_kst(ms) -> str`.

- [ ] Tests: `test_key_format_and_regex`, `test_closed_trades_filters_open_smoke_old_and_malformed`, `test_facts_long_profit_and_kst` (open 2026-10-21T12:00Z → KST 21:00; pnl% = profit_abs/stake*100; MFE from max_rate for long, MAE from min_rate), `test_exit_categories` (each mapping in spec §4.3), `test_facts_tolerate_missing_numbers`, `test_refine_manual_exit_from_audit` (trade_close with step=id within 10 min → panel_close; emergency → emergency; none → unknown), `test_calendar_events_in_window`.
- [ ] Run fail → implement → run pass → commit `feat(postmortem): trade facts, exit classes and calendar window`.

### Task 2: Market path (`market.py`)

**Interfaces — Produces:**
- `fetch_market(exchange, symbol: str, open_ts: int, close_ts: int, now_ms: int) -> dict` — `{status:"ok", source:"bitget_public", symbol, timeframe:"1h", window:{from,to}, candles:[[ts,o,h,l,c]], summary:{high, low, hold_change_pct, window_change_pct, baseline_absret_1h_pct, hold_absret_1h_pct, vol_ratio_hold, max_hold_absret_1h_pct}, big_moves:[{ts_utc, ret_pct}] (top 3 during window), funding:[{ts_utc, rate}], open_interest:{at_utc, size, note}}`; raises on candle failure; funding/OI failures become `null`.
- `event_reaction(candles_all: list, ts_ms: int) -> dict | None` — `{absret_6h_pct, vol_ratio_6h, pre_2h_absret_pct}`.
- `public_exchange()` — `ccxt.bitget({"enableRateLimit": True})` without keys.
- Only completed candles (`ts + 3600000 <= now_ms`); chunked by 999 h.

- [ ] Tests with a fake exchange: `test_fetch_market_summary_and_big_moves`, `test_incomplete_candle_dropped`, `test_funding_and_oi_failures_are_null`, `test_event_reaction_needs_six_hours_after`.
- [ ] Commit `feat(postmortem): public market path and event reaction`.

### Task 3: Research (`research.py`)

**Interfaces — Produces:**
- `resolve_claude(env, home, exists) -> str`
- `build_args(schema: dict, system_prompt: str) -> list[str]`
- `parse_stream(stdout: str) -> tuple[dict | None, set[str], list[str], dict]` — (structured_output, search link URLs normalized, queries, result meta incl. `is_error`, `api_error_status`, `result`).
- `ResearchError(kind: str)` with kinds `timeout|quota|unavailable|error|parse`.
- `run_research(context: dict, *, runner=None, timeout_s=600, cwd: Path) -> dict` — returns `{events, verdict, queries, search_links}` raw-validated.
- `validate(raw: dict, links: set[str], window_from_ms: int, now_ms: int) -> dict` — `events` normalized (`evidence`, `url_verified`, `verification`), `dropped` count, `verdict`.
- `build_prompt(context: dict) -> str`, `SCHEMA`, `SYSTEM_PROMPT`.

- [ ] Tests: `test_args_exact`, `test_parse_stream_collects_links_and_output`, `test_unsearched_url_downgraded_to_guess`, `test_out_of_window_or_bad_time_dropped`, `test_enums_coerced_and_capped`, `test_runner_timeout_quota_and_missing_binary_raise_kinds`, `test_prompt_has_no_secret_fields`.
- [ ] Commit `feat(postmortem): headless claude research with URL verification`.

### Task 4: Report files (`report.py`)

**Interfaces — Produces:**
- `REPORTS_DIR = Path("reports/postmortem")`
- `new_report(facts, calendar, market, now_ms) -> dict` (schema `postmortem/1`, `status:"pending"`)
- `apply_research(doc, research, candles_all) -> dict` (adds reactions, verdict `by:"llm"`, `status:"complete"`)
- `code_verdict(doc) -> dict` (`by:"code"`), `rule_basis(doc) -> dict`
- `render_md(doc) -> str` (Korean, short; `[출처]`/`[추정]`)
- `write_report(directory, doc)` (atomic json+md), `load_report(directory, key) -> dict | None`, `rebuild_index(directory) -> dict`, `read_index(directory) -> dict`.

- [ ] Tests: `test_write_is_atomic_and_pair_named`, `test_md_marks_sourced_and_guess`, `test_code_verdict_rule_vs_unclear`, `test_index_sorted_and_skips_corrupt_files`.
- [ ] Commit `feat(postmortem): report json, korean md and index`.

### Task 5: Watcher (`watcher.py`)

**Interfaces — Produces:**
- `class Watcher(settings_loader, ft_factory, exchange_factory, research, clock, directory, audit_path, calendar_path)` with `cycle() -> list[str]` (keys touched) and `run_forever(stop: threading.Event, interval_s=60)`.
- `DirLock(path, clock)` context manager; `acquire() -> bool`.
- `start_watcher(background) -> None` used by the panel.

- [ ] Tests: `test_closed_trade_produces_both_files`, `test_second_cycle_is_noop`, `test_complete_report_never_researched_again`, `test_same_id_new_open_ts_is_separate`, `test_research_failure_keeps_facts_and_schedules_retry`, `test_quota_waits_an_hour`, `test_gives_up_after_max_attempts`, `test_stale_in_progress_is_retried`, `test_market_failure_still_writes_files`, `test_freqtrade_unreachable_writes_nothing`, `test_pending_retries_without_freqtrade`, `test_lock_blocks_second_runner`, `test_corrupt_report_is_rewritten`.
- [ ] Commit `feat(postmortem): polling watcher with retries and lock`.

### Task 6: Panel, CLI, ignore, runbook

**Files:** Modify `bitgetbot/panel/app.py` (PanelDeps `postmortem`, `reports_dir`; lifespan start; `GET /api/reports`, `GET /api/reports/{key}`, `GET /reports`), `bitgetbot/panel/__main__.py`, `bitgetbot/panel/static/index.html` + `style.css`; Create `static/reports.html`, `static/reports.js`, `bitgetbot/postmortem/__main__.py`, `docs/runbooks/postmortem.md`; Modify `.gitignore`.

- [ ] Tests (`tests/panel/test_reports.py`, `tests/panel/test_static.py`): `test_reports_need_session`, `test_reports_list_and_view`, `test_bad_or_missing_key_404`, `test_no_report_write_routes`, `test_lifespan_starts_postmortem_only_when_given`, `test_index_has_corner_report_link`, `test_reports_page_is_csp_safe`.
- [ ] Commit `feat(panel): read-only post-mortem report list`.

### Task 7: End-to-end check

- [ ] Throwaway dry Freqtrade on port 8081 (separate project, DB and config without keys; live container untouched), force-enter and force-exit one dry trade, run the watcher against it, confirm both files appear within minutes and the panel lists it.
