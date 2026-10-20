import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest


def finished(jobs):
    deadline = time.monotonic() + 5
    while jobs.snapshot()["running"] and time.monotonic() < deadline:
        Event().wait(0.001)
    result = jobs.snapshot()
    assert result["running"] is False
    return result


def test_job_runs_with_answers_and_restart_and_detached_snapshot():
    from bitgetbot.panel.jobs import JobRunner

    restarted = []
    answers = {"known": "answer"}

    def execute(name, io):
        assert name == "restart-check"
        assert io.ask("known") == "answer"
        assert io.ask("unknown") == ""
        io.restart()
        io.out("첫 줄\n둘째 줄")
        return 3

    jobs = JobRunner(execute)
    assert jobs.snapshot() == {
        "name": None, "running": False, "exit_code": None, "lines": [],
        "started_at": None, "finished_at": None,
    }
    before = time.time()
    assert jobs.start("restart-check", answers, lambda: restarted.append(True))
    result = finished(jobs)
    assert result["exit_code"] == 3
    assert result["lines"] == ["첫 줄", "둘째 줄"]
    assert before <= result["started_at"] <= result["finished_at"] <= time.time()
    assert restarted == [True]
    result["lines"].append("외부 변경")
    assert "외부 변경" not in jobs.snapshot()["lines"]


def test_concurrent_starts_admit_exactly_one_and_clear_previous_result():
    from bitgetbot.panel.jobs import JobRunner

    release = Event()
    barrier = Barrier(12)

    def execute(name, io):
        assert release.wait(5)
        io.out(name)
        return 0

    jobs = JobRunner(execute)

    def start(_):
        barrier.wait(timeout=5)
        return jobs.start("check", {}, None)

    try:
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(start, range(12)))
        assert results.count(True) == 1
        assert results.count(False) == 11
        assert jobs.snapshot()["running"] is True
    finally:
        release.set()
        finished(jobs)
    assert jobs.start("ledger", {}, None)
    result = finished(jobs)
    assert result["name"] == "ledger"
    assert result["lines"] == ["ledger"]


@pytest.mark.parametrize("error", [RuntimeError("SECRET-SENTINEL"), KeyboardInterrupt("SECRET-SENTINEL")])
def test_job_failure_hides_exception_and_does_not_log(error, capsys, caplog):
    from bitgetbot.panel.jobs import JobRunner

    def execute(name, io):
        raise error

    jobs = JobRunner(execute)
    assert jobs.start("check", {}, None)
    result = finished(jobs)
    assert result["exit_code"] == 1
    assert result["lines"] == ["작업 실패: 오류 원문은 표시하지 않습니다"]
    assert "SECRET-SENTINEL" not in repr(result) + caplog.text
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_job_keeps_only_last_500_lines():
    from bitgetbot.panel.jobs import JobRunner

    def execute(name, io):
        for number in range(750):
            io.out(f"줄 {number}")
        return 0

    jobs = JobRunner(execute)
    assert jobs.start("ledger", {}, None)
    result = finished(jobs)
    assert result["lines"] == [f"줄 {number}" for number in range(250, 750)]
    assert result["exit_code"] == 0
