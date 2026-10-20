"""Static contracts for the launcher script and panel runbook; never runs Docker or the panel."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "launcher" / "start-panel.ps1"
RUNBOOK = ROOT / "docs" / "runbooks" / "panel.md"
FALLBACK = "급하면 비트겟 웹(PC)에서 직접 청산하세요."
EMERGENCY_SCOPE = "거래 봇(Freqtrade)이 연 거래를 시장가로 청산 요청 (현재 모드)"


def launcher_text() -> str:
    return LAUNCHER.read_text(encoding="utf-8-sig")


def test_every_launcher_failure_message_points_to_bitget_web():
    messages = re.findall(r"\$(?:script:)?failureMessage\s*=\s*'([^']*)'", launcher_text())
    assert len(messages) >= 4
    assert all(message.endswith(FALLBACK) for message in messages), messages


def test_launcher_opens_running_panel_before_any_docker_command():
    body = launcher_text().split("\ntry {", 1)[1]
    first_panel = body.index("Test-Panel")
    first_open = body.index("Open-Panel")
    first_docker = body.index("Invoke-Docker")
    assert first_panel < first_open < first_docker


def test_launcher_docker_failures_do_not_skip_panel_start():
    text = launcher_text()
    # Docker problems are recorded inside their own try block, not thrown to the outer error box.
    assert "$dockerProblem" in text
    assert "throw 'docker-not-ready'" not in text and "throw 'compose-failed'" not in text
    body = text.split("\ntry {", 1)[1]
    assert body.index("$dockerProblem") < body.index("Start-Panel")
    # Only a panel that cannot start (or open) reaches the error box.
    assert body.count("MessageBox") == 1


@pytest.mark.skipif(os.name != "nt" or not shutil.which("powershell"), reason="Windows PowerShell parser required")
def test_launcher_parses_without_errors():
    command = (
        "$errors = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{LAUNCHER}', [ref]$null, [ref]$errors); $errors.Count"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "0", result.stdout + result.stderr


def test_runbook_emergency_lockout_lost_phone_and_serve_reset_wording():
    text = RUNBOOK.read_text(encoding="utf-8")
    assert EMERGENCY_SCOPE in text
    assert "모든 포지션" not in text and "전부 청산" not in text
    assert "모의 거래만 청산" in text
    assert "봇이 모르는 포지션" in text
    assert "휴대폰 신원별 5회" in text and "전체 합계 5회" in text
    assert "PC 접속은 잠기지 않습니다" in text
    assert "tailscale serve reset" in text
    assert "휴대폰 분실" in text and "관리 콘솔" in text
    assert FALLBACK in text


def test_spec_security_section_names_scrypt_not_argon2():
    spec = (ROOT / "docs" / "superpowers" / "specs" / "2026-10-19-bitget-ai-bot-design.md").read_text(encoding="utf-8")
    section = spec.split("## 11. 보안", 1)[1].split("## 12.", 1)[0]
    assert "argon2" not in section
    assert "scrypt" in section


def test_launcher_opens_the_panel_in_a_dedicated_edge_profile():
    text = launcher_text()
    assert "'--user-data-dir=' + $profileDir" in text
    assert "Join-Path $script:repoRoot '.panel\edge-profile'" in text
