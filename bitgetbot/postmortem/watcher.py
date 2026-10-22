"""Poll the running bot read-only for closed trades and keep exactly one post-mortem per trade.

Each report JSON is its own state record (status, attempts, next retry, in-progress mark), so a crash
or restart resumes from disk. Only Freqtrade GET show_config / GET trades are used.
"""

import json
import os
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path

from bitgetbot.postmortem import facts as facts_mod
from bitgetbot.postmortem import market as market_mod
from bitgetbot.postmortem import report as report_mod
from bitgetbot.postmortem import research as research_mod
from bitgetbot.postmortem.facts import iso_utc

MIN = 60_000
POLL_S = 60
TRADES_PAGE = 50
BACKFILL_DAYS = 30
MAX_ATTEMPTS = 5
RETRY_DELAYS_MS = [10 * MIN, 30 * MIN, 120 * MIN, 360 * MIN]  # after attempts 1..4
# Failures that never reached the model cost nothing, so they do not use up attempts (up to a cap):
# no claude binary yet (e.g. a fresh VM) and subscription usage limits.
DEFERRED_DELAYS_MS = {"unavailable": 360 * MIN, "quota": 60 * MIN}
MAX_DEFERRALS = 48
STALE_ATTEMPT_MS = 30 * MIN  # longer than the 10-minute research timeout plus the market fetch
STALE_LOCK_MS = 20 * MIN
SAVE_TRIES = 3
REFRESHES_PER_CYCLE = 5
AUDIT_PATH = Path(".panel/audit.log")
CALENDAR_PATH = Path("freqtrade/user_data/events/upcoming_us_macro.csv")


def _close(client) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


class DirLock:
    """One runner per reports directory (panel thread or the CLI). A lock older than 20 min is stale.

    The lock names its owner with a random token, so a runner only refreshes or removes its own lock.
    """

    def __init__(self, path: Path, clock: Callable[[], float]):
        self.path, self.clock = path, clock
        self.token = secrets.token_hex(8)

    def _payload(self) -> str:
        return json.dumps({"pid": os.getpid(), "ts": int(self.clock() * 1000), "token": self.token})

    def _create(self) -> bool:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(self._payload())
        return True

    def _holder(self) -> dict | None:
        try:
            held = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return None
        return held if isinstance(held, dict) else None

    def _stale(self) -> bool:
        held = self._holder()
        ts = held.get("ts") if held else None
        if isinstance(ts, int):
            return int(self.clock() * 1000) - ts >= STALE_LOCK_MS
        try:
            # Unreadable or half-written (just created): judge by the file's age, not as stale at once.
            return time.time() - self.path.stat().st_mtime >= STALE_LOCK_MS / 1000
        except OSError:
            return True

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._create():
            return True
        if not self._stale():
            return False
        try:
            self.path.unlink()
        except OSError:
            return False
        return self._create()

    def _mine(self) -> bool:
        held = self._holder()
        return bool(held) and held.get("token") == self.token

    def refresh(self) -> None:
        if not self._mine():
            return
        temporary = self.path.with_name(f"{self.path.name}.{self.token}.tmp")
        try:
            temporary.write_text(self._payload(), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            temporary.unlink(missing_ok=True)

    def release(self) -> None:
        if not self._mine():
            return  # someone took over a stale lock: never delete their lock
        try:
            self.path.unlink()
        except OSError:
            pass


class Watcher:
    def __init__(self, *, settings_loader: Callable, ft_factory: Callable,
                 exchange_factory: Callable = market_mod.public_exchange,
                 research: Callable = research_mod.run_research, clock: Callable[[], float] = time.time,
                 directory: Path = report_mod.REPORTS_DIR, audit_path: Path = AUDIT_PATH,
                 calendar_path: Path = CALENDAR_PATH, sleep: Callable[[float], None] = time.sleep):
        self.settings_loader, self.ft_factory = settings_loader, ft_factory
        self.exchange_factory, self.research, self.clock = exchange_factory, research, clock
        self.directory, self.audit_path, self.calendar_path = directory, audit_path, calendar_path
        self.sleep = sleep
        self._exchange = None

    def _now(self) -> int:
        return int(self.clock() * 1000)

    def _save(self, doc: dict) -> None:
        """Write the report; a file briefly held open by a reader (Windows) gets two more tries."""
        for attempt in range(SAVE_TRIES):
            try:
                report_mod.write_report(self.directory, doc)
                return
            except OSError:
                if attempt == SAVE_TRIES - 1:
                    raise
                self.sleep(1)

    def _market(self, facts: dict, now: int) -> dict:
        try:
            if self._exchange is None:
                self._exchange = self.exchange_factory()
            if not facts.get("symbol"):
                raise market_mod.MarketError("symbol")
            return market_mod.fetch_market(self._exchange, facts["symbol"], facts["open_ts"], facts["close_ts"], now)
        except Exception as error:
            self._exchange = None  # rebuild the client next time
            kind = str(error) if isinstance(error, market_mod.MarketError) else "error"
            return {"status": "failed", "error": kind}

    # -- discovery --------------------------------------------------------------------------------

    def _closed_rows(self, now: int) -> tuple[str, list[dict]] | None:
        ft = None
        try:
            settings = self.settings_loader()
            ft = self.ft_factory(settings.api_url, settings.api_user, settings.api_pass)
            config = ft.show_config()
            dry = config.get("dry_run") if isinstance(config, dict) else None
            if type(dry) is not bool:
                return None  # the running bot's mode decides which database these trades come from
            history = ft.trades(TRADES_PAGE)
        except Exception:
            return None
        finally:
            _close(ft)
        return ("dry" if dry else "live"), facts_mod.closed_trades(history, now, BACKFILL_DAYS)

    def _set_aside(self, key: str, now: int) -> bool:
        """Keep the bytes of a corrupt report for inspection instead of overwriting them."""
        path = self.directory / f"{key}.json"
        try:
            os.replace(path, path.with_name(f"{key}.json.corrupt-{now}"))
            return True
        except OSError:
            return False

    def _discover(self, now: int) -> list[str]:
        found = self._closed_rows(now)
        if found is None:
            return []
        mode, rows = found
        created = []
        for row in rows:
            try:
                key = facts_mod.report_key(mode, row["trade_id"], row["open_timestamp"])
                state, _ = report_mod.read_state(self.directory, key)
                if state in ("ok", "unreadable"):
                    continue  # exists (or may exist): never overwrite it
                if state == "corrupt" and not self._set_aside(key, now):
                    continue
                facts = facts_mod.refine_manual_exit(facts_mod.trade_facts(row, mode), self.audit_path)
                calendar = facts_mod.calendar_events(
                    self.calendar_path, facts["open_ts"] - report_mod.WINDOW_BEFORE_MS, facts["close_ts"])
                self._save(report_mod.new_report(facts, calendar, self._market(facts, now), now))
                created.append(key)
            except Exception:
                continue  # one odd row never blocks the others
        return created

    # -- research attempts ------------------------------------------------------------------------

    def _due(self, doc: dict, now: int) -> bool:
        if doc.get("status") != "pending":
            return False
        started = doc.get("in_progress_since_ms")
        if isinstance(started, int):
            return now - started >= STALE_ATTEMPT_MS
        retry = doc.get("next_retry_ms")
        return not (isinstance(retry, int) and retry > now)

    def _error(self, doc: dict, now: int, stage: str, kind: str) -> None:
        doc["errors"] = [*doc.get("errors", []), {"at_utc": iso_utc(now), "stage": stage, "kind": kind}]

    def _give_up_or_retry(self, doc: dict, now: int, kind: str) -> dict:
        doc = {**doc, "in_progress_since_ms": None, "updated_utc": iso_utc(now)}
        deferrals = doc.get("deferrals", 0)
        if kind in DEFERRED_DELAYS_MS and deferrals < MAX_DEFERRALS:
            # The model was never reached: give the attempt back and wait longer.
            delay = DEFERRED_DELAYS_MS[kind]
            doc.update(attempts=max(doc["attempts"] - 1, 0), deferrals=deferrals + 1,
                       next_retry_ms=now + delay, next_retry_utc=iso_utc(now + delay),
                       research={"status": "pending", "last_error": kind})
        elif doc["attempts"] >= MAX_ATTEMPTS:
            doc.update(status="incomplete", next_retry_ms=None, next_retry_utc=None,
                       research={"status": "failed", "last_error": kind})
        else:
            delay = RETRY_DELAYS_MS[doc["attempts"] - 1]
            doc.update(next_retry_ms=now + delay, next_retry_utc=iso_utc(now + delay),
                       research={"status": "pending", "last_error": kind})
        doc["verdict"] = report_mod.code_verdict(doc)
        return doc

    def _attempt(self, doc: dict, now: int) -> None:
        if isinstance(doc.get("in_progress_since_ms"), int):
            # The previous attempt never finished (panel restart or crash): count it as failed.
            self._error(doc, now, "research", "interrupted")
            if doc["attempts"] >= MAX_ATTEMPTS:
                self._save(self._give_up_or_retry(doc, now, "interrupted"))
                return
        doc = {**doc, "attempts": doc.get("attempts", 0) + 1, "in_progress_since_ms": now,
               "updated_utc": iso_utc(now)}
        self._save(doc)  # persist the attempt before the slow part
        if (doc.get("market") or {}).get("status") != "ok":
            doc["market"] = self._market(doc["trade"], now)
            if doc["market"]["status"] != "ok":
                self._error(doc, now, "market", doc["market"].get("error", "error"))
                self._save(self._give_up_or_retry(doc, now, "market"))
                return
        try:
            result = self.research(doc, now_ms=now, cwd=self.directory / ".work")
        except research_mod.ResearchError as error:
            kind = error.kind
        except Exception:
            kind = "error"  # never store exception text: it may echo upstream data
        else:
            try:
                self._save(report_mod.apply_research(doc, result, self._now()))
                return
            except Exception:
                # The paid result could not be stored. Record it and clear the in-progress mark so the
                # retry follows the normal schedule (bounded by MAX_ATTEMPTS).
                kind = "save"
        self._error(doc, self._now(), "research", kind)
        self._save(self._give_up_or_retry(doc, self._now(), kind))

    def _next_due(self, now: int) -> dict | None:
        due = [doc for doc in report_mod.report_docs(self.directory) if self._due(doc, now)]
        return min(due, key=lambda doc: doc["trade"]["close_ts"]) if due else None

    def _refresh_markets(self, now: int) -> list[str]:
        """Finished reports get one later market re-read (no model call) once the exit candle closed."""
        refreshed = []
        for doc in report_mod.report_docs(self.directory):
            if len(refreshed) >= REFRESHES_PER_CYCLE:
                break
            due = doc.get("market_refresh_due_ms")
            if doc.get("status") not in ("complete", "incomplete") or not isinstance(due, int) or due > now:
                continue
            try:
                self._save(report_mod.refresh_market(doc, self._market(doc["trade"], now), now))
                refreshed.append(doc["key"])
            except Exception:
                continue
        return refreshed

    def cycle(self) -> list[str]:
        """New closed trades first, then at most one due research attempt, then due market re-reads."""
        lock = DirLock(self.directory / ".lock", self.clock)
        if not lock.acquire():
            return []
        try:
            now = self._now()
            touched = self._discover(now)
            doc = self._next_due(now)
            if doc is not None:
                lock.refresh()
                self._attempt(doc, now)
                touched.append(doc["key"])
            lock.refresh()
            touched += self._refresh_markets(self._now())
            return list(dict.fromkeys(touched))
        finally:
            lock.release()

    def run_forever(self, stop: threading.Event | None = None, interval_s: float = POLL_S) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                self.cycle()
            except Exception:
                pass  # a bad cycle never ends the watcher; the next one retries
            stop.wait(interval_s)
