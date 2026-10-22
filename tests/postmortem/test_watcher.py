import json
import threading

import pytest

from bitgetbot.ftclient import FreqtradeUnreachable
from bitgetbot.postmortem import report, watcher
from bitgetbot.postmortem.research import ResearchError
from bitgetbot.privcfg import Settings
from tests.postmortem.test_market import Exchange, scenario

H = 3600_000
MIN = 60_000
OPEN_TS = 1792584000000
CLOSE_TS = OPEN_TS + 7 * H
SECRETS = ["API-USER-SENTINEL", "API-PASS-SENTINEL", "KEY-SENTINEL", "SECRET-SENTINEL", "PASS-SENTINEL"]


def row(trade_id=2, open_ts=OPEN_TS, close_ts=CLOSE_TS, **overrides):
    base = {"trade_id": trade_id, "pair": "BTC/USDT:USDT", "is_open": False, "is_short": False,
            "enter_tag": "safe_long", "exit_reason": "opposite_donchian_10", "open_timestamp": open_ts,
            "close_timestamp": close_ts, "open_rate": 100.0, "close_rate": 101.0, "amount": 0.001,
            "stake_amount": 10.0, "leverage": 2.0, "profit_abs": 0.1, "profit_ratio": 0.01,
            "funding_fees": 0.0, "max_rate": 102.0, "min_rate": 99.0}
    base.update(overrides)
    return base


class FT:
    def __init__(self, world):
        self.world = world

    def show_config(self):
        if self.world.ft_down:
            raise FreqtradeUnreachable("down")
        return {"dry_run": self.world.dry, "state": "running"}

    def trades(self, limit=500):
        assert limit == 50
        if self.world.ft_down:
            raise FreqtradeUnreachable("down")
        return {"trades": list(self.world.rows), "total_trades": len(self.world.rows)}

    def close(self):
        self.world.closed += 1

    def __getattr__(self, name):
        pytest.fail(f"watcher must only read show_config/trades, not {name}")


VERDICT = {"driver": "rule", "confidence": "med", "why_ko": "규칙.", "lesson_ko": "교훈."}


class World:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.now = CLOSE_TS + 2 * MIN
        self.dry = True
        self.ft_down = False
        self.rows = [row()]
        self.closed = 0
        self.research_calls = []
        self.research_error = None
        self.market_error = False
        start, closes = scenario()
        self.exchange = Exchange(closes + [closes[-1]] * 48, start)
        self.directory = tmp_path / "reports" / "postmortem"
        calendar = tmp_path / "macro.csv"
        calendar.write_text("ts_utc,category,title\n2026-10-21T13:30:00Z,CPI,CPI release\n", encoding="utf-8")
        self.watcher = watcher.Watcher(
            settings_loader=lambda: Settings(self.dry, "http://127.0.0.1:8080", *SECRETS),
            ft_factory=self.ft_factory, exchange_factory=self.exchange_factory, research=self.research,
            clock=lambda: self.now / 1000, directory=self.directory, audit_path=tmp_path / "audit.log",
            calendar_path=calendar,
        )
        self.ft_args = []

    def ft_factory(self, *args, **kwargs):
        self.ft_args.append(args)
        return FT(self)

    def exchange_factory(self):
        if self.market_error:
            class Broken:
                def publicMixGetV2MixMarketCandles(self, params):
                    raise RuntimeError("down")
            return Broken()
        return self.exchange

    def research(self, doc, *, now_ms, cwd):
        self.research_calls.append(doc["key"])
        assert cwd == self.directory / ".work"
        if self.research_error:
            raise self.research_error
        return {"events": [], "dropped": 0, "verdict": VERDICT, "queries": ["q"], "search_links_count": 0,
                "model": "sonnet", "cost_usd": None}

    def doc(self, key=None):
        return json.loads((self.directory / f"{key or self.key()}.json").read_text(encoding="utf-8"))

    def key(self, trade_id=2, open_ts=OPEN_TS, mode="dry"):
        return f"{mode}-{trade_id}-{open_ts}"

    def files(self):
        return sorted(p.name for p in self.directory.iterdir() if p.is_file())


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_closed_trade_produces_both_files(world):
    touched = world.watcher.cycle()
    key = world.key()
    assert touched == [key]
    assert world.files() == [f"{key}.json", f"{key}.md", "index.json"]
    doc = world.doc()
    assert doc["status"] == "complete" and doc["attempts"] == 1 and doc["market"]["status"] == "ok"
    assert doc["calendar"][0]["category"] == "CPI"
    assert world.research_calls == [key]
    assert world.closed == 1
    index = report.read_index(world.directory)
    assert [r["key"] for r in index["reports"]] == [key]
    text = (world.directory / f"{key}.json").read_text(encoding="utf-8") + (world.directory / f"{key}.md").read_text(encoding="utf-8")
    assert not any(secret in text for secret in SECRETS)


def test_facts_are_written_before_research_runs(world):
    seen = []

    def research(doc, *, now_ms, cwd):
        on_disk = world.doc()
        seen.append((on_disk["status"], on_disk["attempts"], on_disk["in_progress_since_ms"]))
        assert (world.directory / f"{world.key()}.md").is_file()
        return world.research.__func__(world, doc, now_ms=now_ms, cwd=cwd)

    world.watcher.research = research
    world.watcher.cycle()
    assert seen == [("pending", 1, world.now)]


def test_second_cycle_is_noop(world):
    world.watcher.cycle()
    before = world.doc()
    world.now += 5 * MIN
    assert world.watcher.cycle() == []
    assert world.doc() == before and world.research_calls == [world.key()]


def test_same_id_new_open_ts_is_separate(world):
    world.watcher.cycle()
    world.dry = False  # live DB also numbers from 1
    world.rows = [row(open_ts=OPEN_TS + H, close_ts=CLOSE_TS + H)]
    world.now = CLOSE_TS + H + MIN
    world.watcher.cycle()
    names = world.files()
    assert f"{world.key()}.json" in names and f"{world.key(open_ts=OPEN_TS + H, mode='live')}.json" in names


def test_smoke_open_and_old_trades_are_ignored(world):
    world.rows = [row(trade_id=1, enter_tag="smoke"), row(trade_id=3, is_open=True, close_ts=None),
                  row(trade_id=4, open_ts=OPEN_TS - 40 * 24 * H, close_ts=OPEN_TS - 31 * 24 * H)]
    assert world.watcher.cycle() == []
    assert not world.directory.exists() or world.files() == []


def test_research_failure_keeps_facts_and_schedules_retry(world):
    world.research_error = ResearchError("timeout")
    world.watcher.cycle()
    doc = world.doc()
    assert doc["status"] == "pending" and doc["attempts"] == 1 and doc["in_progress_since_ms"] is None
    assert doc["next_retry_ms"] == world.now + 10 * MIN
    assert doc["errors"] == [{"at_utc": doc["errors"][0]["at_utc"], "stage": "research", "kind": "timeout"}]
    assert (world.directory / f"{world.key()}.md").is_file()
    world.now += 9 * MIN
    world.watcher.cycle()
    assert len(world.research_calls) == 1  # not due yet
    world.now += MIN
    world.research_error = None
    world.watcher.cycle()
    assert world.doc()["status"] == "complete" and world.doc()["attempts"] == 2


def test_quota_waits_an_hour(world):
    world.research_error = ResearchError("quota")
    world.watcher.cycle()
    assert world.doc()["next_retry_ms"] == world.now + H


def test_unexpected_research_exception_is_an_error_kind(world):
    world.research_error = ValueError("boom " + SECRETS[1])
    world.watcher.cycle()
    doc = world.doc()
    assert doc["errors"][0]["kind"] == "error" and SECRETS[1] not in json.dumps(doc)


def test_gives_up_after_max_attempts(world):
    world.research_error = ResearchError("error")
    delays = []
    for _ in range(watcher.MAX_ATTEMPTS):
        world.watcher.cycle()
        doc = world.doc()
        if doc["status"] == "pending":
            delays.append(doc["next_retry_ms"] - world.now)
            world.now = doc["next_retry_ms"]
    doc = world.doc()
    assert delays == [10 * MIN, 30 * MIN, 2 * H, 6 * H]
    assert doc["status"] == "incomplete" and doc["attempts"] == 5 and doc["verdict"]["by"] == "code"
    assert doc["research"]["status"] == "failed"
    world.now += 48 * H
    world.watcher.cycle()
    assert len(world.research_calls) == 5
    assert "조사 실패" in (world.directory / f"{world.key()}.md").read_text(encoding="utf-8")


def test_stale_in_progress_is_retried(world):
    world.research_error = ResearchError("timeout")
    world.watcher.cycle()
    doc = world.doc()
    doc.update(in_progress_since_ms=world.now, next_retry_ms=None, attempts=2)  # crashed mid-attempt
    (world.directory / f"{world.key()}.json").write_text(json.dumps(doc), encoding="utf-8")
    world.research_error = None
    world.now += 29 * MIN
    world.watcher.cycle()
    assert len(world.research_calls) == 1  # still looks alive
    world.now += MIN
    world.watcher.cycle()
    doc = world.doc()
    assert doc["status"] == "complete" and doc["attempts"] == 3
    assert [e["kind"] for e in doc["errors"]] == ["timeout", "interrupted"]


def test_market_failure_still_writes_files(world):
    world.market_error = True
    world.watcher.cycle()
    doc = world.doc()
    assert doc["market"]["status"] == "failed" and doc["status"] == "pending"
    assert doc["errors"][0]["stage"] == "market" and world.research_calls == []
    assert (world.directory / f"{world.key()}.md").is_file()
    world.market_error = False
    world.now = doc["next_retry_ms"]
    world.watcher.cycle()
    doc = world.doc()
    assert doc["market"]["status"] == "ok" and doc["status"] == "complete"


def test_freqtrade_unreachable_writes_nothing(world):
    world.ft_down = True
    assert world.watcher.cycle() == []
    assert not world.directory.exists() or world.files() == []


def test_unknown_bot_mode_writes_nothing(world):
    world.dry = None
    assert world.watcher.cycle() == []


def test_pending_retries_without_freqtrade(world):
    world.research_error = ResearchError("timeout")
    world.watcher.cycle()
    world.ft_down = True
    world.research_error = None
    world.now += 10 * MIN
    world.watcher.cycle()
    assert world.doc()["status"] == "complete"


def test_lock_blocks_second_runner(world, tmp_path):
    world.directory.mkdir(parents=True)
    lock = watcher.DirLock(world.directory / ".lock", lambda: world.now / 1000)
    assert lock.acquire()
    assert world.watcher.cycle() == [] and world.research_calls == []
    world.now += 21 * MIN  # the holder died long ago
    assert world.watcher.cycle() == [world.key()]
    assert not (world.directory / ".lock").exists()


def test_corrupt_report_is_rewritten(world):
    world.directory.mkdir(parents=True)
    (world.directory / f"{world.key()}.json").write_text("{half-written", encoding="utf-8")
    world.watcher.cycle()
    assert world.doc()["status"] == "complete"


def test_run_forever_survives_errors_and_stops(world):
    stop = threading.Event()
    calls = []

    def cycle():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        stop.set()
        return []

    world.watcher.cycle = cycle
    world.watcher.run_forever(stop, interval_s=0)
    assert len(calls) == 2
