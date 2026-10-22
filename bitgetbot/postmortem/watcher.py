"""Poll the running bot read-only for closed trades and keep exactly one post-mortem per trade.

Each report JSON is its own state record (status, attempts, next retry, in-progress mark), so a crash
or restart resumes from disk. Only Freqtrade GET show_config / GET trades are used.
"""

import json
import os
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
QUOTA_DELAY_MS = 60 * MIN
STALE_ATTEMPT_MS = 30 * MIN  # longer than the 10-minute research timeout plus the market fetch
STALE_LOCK_MS = 20 * MIN
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
    """One runner per reports directory (panel thread or the CLI). A lock older than 20 min is stale."""

    def __init__(self, path: Path, clock: Callable[[], float]):
        self.path, self.clock = path, clock

    def _now_ms(self) -> int:
        return int(self.clock() * 1000)

    def _create(self) -> bool:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "ts": self._now_ms()}, handle)
        return True

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._create():
            return True
        try:
            held = json.loads(self.path.read_text(encoding="utf-8")).get("ts")
        except (OSError, ValueError, AttributeError):
            held = None
        if isinstance(held, int) and self._now_ms() - held < STALE_LOCK_MS:
            return False
        try:
            self.path.unlink()
        except OSError:
            return False
        return self._create()

    def release(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


class Watcher:
    def __init__(self, *, settings_loader: Callable, ft_factory: Callable,
                 exchange_factory: Callable = market_mod.public_exchange,
                 research: Callable = research_mod.run_research, clock: Callable[[], float] = time.time,
                 directory: Path = report_mod.REPORTS_DIR, audit_path: Path = AUDIT_PATH,
                 calendar_path: Path = CALENDAR_PATH):
        self.settings_loader, self.ft_factory = settings_loader, ft_factory
        self.exchange_factory, self.research, self.clock = exchange_factory, research, clock
        self.directory, self.audit_path, self.calendar_path = directory, audit_path, calendar_path
        self._exchange = None

    def _now(self) -> int:
        return int(self.clock() * 1000)

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

    def _discover(self, now: int) -> list[str]:
        found = self._closed_rows(now)
        if found is None:
            return []
        mode, rows = found
        created = []
        for row in rows:
            try:
                key = facts_mod.report_key(mode, row["trade_id"], row["open_timestamp"])
                if report_mod.load_report(self.directory, key) is not None:
                    continue
                facts = facts_mod.refine_manual_exit(facts_mod.trade_facts(row, mode), self.audit_path)
                calendar = facts_mod.calendar_events(
                    self.calendar_path, facts["open_ts"] - report_mod.WINDOW_BEFORE_MS, facts["close_ts"])
                doc = report_mod.new_report(facts, calendar, self._market(facts, now), now)
                report_mod.write_report(self.directory, doc)
                created.append(key)
            except Exception:
                continue  # one odd row never blocks the others
        return created

    # -- research attempts ------------------------------------------------------------------------

    def _due(self, doc: dict, now: int) -> bool:
        if doc.get("status") != "pending":
            return False
        started = doc.get("in_progress_since_ms")
        if isinstance(started, int) and now - started < STALE_ATTEMPT_MS:
            return False
        retry = doc.get("next_retry_ms")
        return not (isinstance(retry, int) and retry > now and started is None)

    def _error(self, doc: dict, now: int, stage: str, kind: str) -> None:
        doc["errors"] = [*doc.get("errors", []), {"at_utc": iso_utc(now), "stage": stage, "kind": kind}]

    def _give_up_or_retry(self, doc: dict, now: int, kind: str) -> dict:
        doc = {**doc, "in_progress_since_ms": None, "updated_utc": iso_utc(now)}
        if doc["attempts"] >= MAX_ATTEMPTS:
            doc.update(status="incomplete", next_retry_ms=None, next_retry_utc=None,
                       research={"status": "failed", "last_error": kind})
            doc["verdict"] = report_mod.code_verdict(doc)
            return doc
        delay = RETRY_DELAYS_MS[doc["attempts"] - 1]
        if kind == "quota":
            delay = max(delay, QUOTA_DELAY_MS)
        doc.update(next_retry_ms=now + delay, next_retry_utc=iso_utc(now + delay),
                   research={"status": "pending", "last_error": kind})
        doc["verdict"] = report_mod.code_verdict(doc)
        return doc

    def _attempt(self, doc: dict, now: int) -> None:
        if isinstance(doc.get("in_progress_since_ms"), int):
            # The previous attempt never finished (panel restart or crash): count it as failed.
            self._error(doc, now, "research", "interrupted")
            if doc["attempts"] >= MAX_ATTEMPTS:
                report_mod.write_report(self.directory, self._give_up_or_retry(doc, now, "interrupted"))
                return
        doc = {**doc, "attempts": doc.get("attempts", 0) + 1, "in_progress_since_ms": now,
               "updated_utc": iso_utc(now)}
        report_mod.write_report(self.directory, doc)  # persist the attempt before the slow part
        if (doc.get("market") or {}).get("status") != "ok":
            doc["market"] = self._market(doc["trade"], now)
            if doc["market"]["status"] != "ok":
                self._error(doc, now, "market", doc["market"].get("error", "error"))
                report_mod.write_report(self.directory, self._give_up_or_retry(doc, now, "market"))
                return
        try:
            result = self.research(doc, now_ms=now, cwd=self.directory / ".work")
        except research_mod.ResearchError as error:
            kind = error.kind
        except Exception:
            kind = "error"  # never store exception text: it may echo upstream data
        else:
            report_mod.write_report(self.directory, report_mod.apply_research(doc, result, self._now()))
            return
        self._error(doc, self._now(), "research", kind)
        report_mod.write_report(self.directory, self._give_up_or_retry(doc, self._now(), kind))

    def _next_due(self, now: int) -> dict | None:
        due = [doc for doc in report_mod.report_docs(self.directory) if self._due(doc, now)]
        return min(due, key=lambda doc: doc["trade"]["close_ts"]) if due else None

    def cycle(self) -> list[str]:
        """Write facts for newly closed trades, then run at most one due research attempt."""
        lock = DirLock(self.directory / ".lock", self.clock)
        if not lock.acquire():
            return []
        try:
            now = self._now()
            touched = self._discover(now)
            doc = self._next_due(now)
            if doc is not None:
                self._attempt(doc, now)
                if doc["key"] not in touched:
                    touched.append(doc["key"])
            return touched
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
