"""Manual entry points. The panel already runs the watcher; these are for checks and the VM.

  python -m bitgetbot.postmortem once    one watcher cycle (new closed trades, then one due research)
  python -m bitgetbot.postmortem index   rebuild reports/postmortem/index.json from the report files
"""

import sys
from pathlib import Path

from bitgetbot.ftclient import FreqtradeClient
from bitgetbot.panel import ops
from bitgetbot.postmortem import report
from bitgetbot.postmortem.watcher import Watcher
from bitgetbot.privcfg import load_settings


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv not in (["once"], ["index"]):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    if Path.cwd().resolve() != ops.REPO_ROOT.resolve():
        print("저장소 루트 폴더에서 실행하세요.", file=sys.stderr)
        return 1
    if argv == ["index"]:
        rows = report.rebuild_index(report.REPORTS_DIR)["reports"]
        print(f"리포트 {len(rows)}건")
        return 0
    touched = Watcher(settings_loader=load_settings, ft_factory=FreqtradeClient).cycle()
    # Keys only: never print trade data, settings or errors with upstream text.
    print("처리한 리포트: " + (", ".join(touched) if touched else "없음"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
