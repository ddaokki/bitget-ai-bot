"""Persistent read-only token for the robo-briefing trading widget (GET /api/widget/summary only).

Unlike the per-launch panel token, this token survives restarts: it is created once at
.panel/widget_token and replaced only by `python -m bitgetbot.panel.widget_token rotate`.
The value is never printed, logged, returned by an API or written to the audit log.
"""

import base64
import os
import re
import secrets
import sys
import threading
from collections.abc import Callable
from pathlib import Path

PATH = Path(".panel/widget_token")
TOKEN_BYTES = 32
# 32 random bytes in unpadded url-safe base64 are 43 characters; accept a hand-made longer one too.
VALID = re.compile(r"[A-Za-z0-9_-]{43,128}")
ROTATED = "새 위젯 토큰을 만들었습니다"
USAGE = "사용법: python -m bitgetbot.panel.widget_token rotate"

Writer = Callable[[Path, str], None]


def generate() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(TOKEN_BYTES)).rstrip(b"=").decode("ascii")


def read(path: Path) -> str | None:
    """The stored token, or None when the file is missing, unreadable or malformed."""
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return value if VALID.fullmatch(value) else None


def ensure(path: Path, write: Writer) -> bool:
    """Create the token on first start (or replace a malformed one); keep a valid existing one.

    Returns whether a new token was written.
    """
    if read(path) is not None:
        return False
    write(path, generate())
    return True


def rotate(path: Path, write: Writer) -> None:
    write(path, generate())


class FileToken:
    """Reads the token file again only when it changes, so a rotation applies without a restart."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._stamp: tuple[int, int, int] | None = None
        self._value: str | None = None

    def __call__(self) -> str | None:
        try:
            info = os.stat(self.path)
        except OSError:
            return None
        # os.replace gives a rotated token a new file id even within one mtime tick.
        stamp = (info.st_ino, info.st_mtime_ns, info.st_size)
        with self._lock:
            if stamp != self._stamp:
                self._value, self._stamp = read(self.path), stamp
            return self._value


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv != ["rotate"]:
        print(USAGE, file=sys.stderr)
        return 2
    # Lazy import: the entry module owns the owner-only permission handling for .panel files.
    from bitgetbot.panel import ops
    from bitgetbot.panel.__main__ import _write_token

    try:
        if Path.cwd().resolve() != ops.REPO_ROOT.resolve():
            print("저장소 루트 폴더에서 실행하세요.", file=sys.stderr)
            return 1
        rotate(PATH, _write_token)
    except Exception:
        print("위젯 토큰을 만들지 못했습니다: 작업 폴더·권한을 확인하세요. 오류 원문은 표시하지 않습니다.", file=sys.stderr)
        return 1
    print(ROTATED)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
