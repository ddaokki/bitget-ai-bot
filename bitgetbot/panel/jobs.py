"""One in-memory smoke job at a time, with bounded, thread-safe output."""

import threading
import time
from collections import deque
from collections.abc import Callable

from bitgetbot.smoke import SmokeIO


class JobRunner:
    def __init__(
        self, execute: Callable[[str, SmokeIO], int], *, clock: Callable[[], float] = time.time,
    ):
        self._execute = execute
        self._clock = clock
        self._lock = threading.Lock()
        self._lines: deque[str] = deque(maxlen=500)
        self._name: str | None = None
        self._running = False
        self._exit_code: int | None = None
        self._started_at: float | None = None
        self._finished_at: float | None = None

    def start(
        self, name: str, io_answers: dict[str, str], restart: Callable[[], None] | None,
    ) -> bool:
        with self._lock:
            if self._running:
                return False
            self._name = name
            self._running = True
            self._exit_code = None
            self._lines.clear()
            self._started_at = self._clock()
            self._finished_at = None
            answers = dict(io_answers)
            try:
                threading.Thread(
                    target=self._run, args=(name, answers, restart), daemon=True,
                ).start()
            except Exception:
                self._lines.append("작업 실패: 오류 원문은 표시하지 않습니다")
                self._exit_code = 1
                self._finished_at = self._clock()
                self._running = False
            return True

    def _out(self, line: str) -> None:
        with self._lock:
            self._lines.extend(str(line).splitlines())

    def _run(self, name: str, answers: dict[str, str], restart: Callable[[], None] | None) -> None:
        try:
            io = SmokeIO(self._out, lambda question: answers.get(question, ""), restart)
            code = self._execute(name, io)
        except (Exception, KeyboardInterrupt):
            self._out("작업 실패: 오류 원문은 표시하지 않습니다")
            code = 1
        with self._lock:
            self._exit_code = code
            self._finished_at = self._clock()
            self._running = False

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "name": self._name, "running": self._running, "exit_code": self._exit_code,
                "lines": list(self._lines), "started_at": self._started_at,
                "finished_at": self._finished_at,
            }
