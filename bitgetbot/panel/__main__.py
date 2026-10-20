"""Start the panel only from the repository root, on the fixed loopback socket."""

import os
import secrets
import socket
import sys
import tempfile
from pathlib import Path

import uvicorn

from bitgetbot.panel import ops
from bitgetbot.panel.app import PanelDeps, create_app


def _restrict_to_owner(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o700 if path.is_dir() else 0o600)
        return
    # Windows ignores POSIX mode bits. Use a protected owner-only DACL instead.
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    convert.restype = wintypes.BOOL
    apply = advapi.SetFileSecurityW
    apply.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    apply.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    # OW is the OWNER RIGHTS SID. No inherited/other-user ACEs remain.
    flags = "OICI" if path.is_dir() else ""
    if not convert(f"D:P(A;{flags};FA;;;OW)", 1, ctypes.byref(descriptor), None):
        raise OSError("private permissions failed")
    try:
        if not apply(str(path), 0x80000004, descriptor):
            raise OSError("private permissions failed")
    finally:
        kernel.LocalFree(descriptor)


def _write_token(path: Path, token: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _restrict_to_owner(path.parent)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=".token.", suffix=".tmp", delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            _restrict_to_owner(temporary_path)
            temporary.write(token + "\n")
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _ensure_stdio(directory: Path) -> None:
    """pythonw has no console streams; uvicorn's logging setup fails on None, so give it files."""
    # Access logging is off, so these files never receive the launch token.
    if sys.stdout is None:
        sys.stdout = open(directory / "panel.out.log", "a", encoding="utf-8", buffering=1)
    if sys.stderr is None:
        sys.stderr = open(directory / "panel.err.log", "a", encoding="utf-8", buffering=1)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        print("패널은 인수를 받지 않습니다. 주소는 127.0.0.1:8090으로 고정됩니다.", file=sys.stderr)
        return 2
    try:
        if Path.cwd().resolve() != ops.REPO_ROOT.resolve():
            print("패널은 저장소 루트 폴더에서만 시작할 수 있습니다.", file=sys.stderr)
            return 1
        try:
            with socket.create_connection(("127.0.0.1", 8090), timeout=0.2):
                print("패널이 이미 실행 중입니다.")
                return 0
        except OSError:
            pass
        token = secrets.token_urlsafe(32)
        _write_token(Path(".panel/token"), token)
        app = create_app(PanelDeps(token=token))
        _ensure_stdio(Path(".panel"))
        uvicorn.run(
            app, host="127.0.0.1", port=8090, log_level="warning", access_log=False,
            proxy_headers=False,
        )
    except (Exception, KeyboardInterrupt):
        print("패널 시작 실패: 작업 폴더·권한·포트를 확인하세요. 오류 원문은 표시하지 않습니다.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
