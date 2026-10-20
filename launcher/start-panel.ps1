#requires -Version 5.1
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
$repoRoot = Split-Path -Parent $PSScriptRoot
$failureMessage = '패널을 시작하지 못했습니다. 작업 폴더와 설치 상태를 확인하세요. 급하면 비트겟 웹(PC)에서 직접 청산하세요.'
$dockerProblem = $null

# Drain both streams without displaying Docker output or exception details.
function Invoke-Docker {
    param([string]$Arguments, [int]$TimeoutSeconds)
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = New-Object System.Diagnostics.ProcessStartInfo
    $process.StartInfo.FileName = $script:dockerPath
    $process.StartInfo.Arguments = $Arguments
    $process.StartInfo.WorkingDirectory = $script:repoRoot
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.CreateNoWindow = $true
    $process.StartInfo.RedirectStandardOutput = $true
    $process.StartInfo.RedirectStandardError = $true
    try {
        if (-not $process.Start()) { return $false }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            $process.Kill()
            return $false
        }
        return ($process.ExitCode -eq 0)
    }
    catch { return $false }
    finally { $process.Dispose() }
}

function Test-Panel {
    try {
        $response = Invoke-WebRequest -Uri 'http://127.0.0.1:8090/api/ping' -UseBasicParsing -TimeoutSec 1
        return ($response.StatusCode -eq 200 -and ($response.Content | ConvertFrom-Json).ok -eq $true)
    }
    catch { return $false }
}

function Start-Panel {
    $python = Join-Path $script:repoRoot '.venv\Scripts\pythonw.exe'
    # pythonw has no console handles and uvicorn's logging setup fails on them, so give it files.
    # Access log is off in the server, so these files hold no token.
    $panelDir = Join-Path $script:repoRoot '.panel'
    New-Item -ItemType Directory -Force -Path $panelDir | Out-Null
    Start-Process -FilePath $python -ArgumentList '-m bitgetbot.panel' -WorkingDirectory $script:repoRoot -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $panelDir 'panel.out.log') -RedirectStandardError (Join-Path $panelDir 'panel.err.log') `
        -ErrorAction Stop | Out-Null
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-Panel) { return $true }
        Start-Sleep -Milliseconds 250
    }
    return $false
}

function Open-PanelWindow {
    $script:failureMessage = '패널 로그인 정보를 읽지 못했습니다. 패널을 다시 시작한 뒤 바로가기를 실행하세요. 급하면 비트겟 웹(PC)에서 직접 청산하세요.'
    $token = $null
    $url = $null
    try {
        $token = [IO.File]::ReadAllText((Join-Path $script:repoRoot '.panel\token')).Trim()
        if ($token -notmatch '^[A-Za-z0-9_-]{43}$') { throw 'invalid-token' }
        $url = 'http://127.0.0.1:8090/?t=' + $token
        $script:failureMessage = '브라우저를 열지 못했습니다. Edge 또는 기본 브라우저 설치 상태를 확인하세요. 급하면 비트겟 웹(PC)에서 직접 청산하세요.'
        $edge = Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'
        if (Test-Path -LiteralPath $edge -PathType Leaf) {
            Start-Process -FilePath $edge -ArgumentList ('--app=' + $url) -ErrorAction Stop | Out-Null
        }
        else {
            Start-Process -FilePath $url -ErrorAction Stop | Out-Null
        }
    }
    finally {
        $token = $null
        $url = $null
    }
}

try {
    Set-Location -LiteralPath $repoRoot
    # A running panel (emergency button and Bitget link) opens first and never waits for Docker.
    $panelWasRunning = Test-Panel
    if ($panelWasRunning) { Open-PanelWindow }

    # Docker problems are recorded, not fatal: the panel shows Docker state and still offers emergency close.
    try {
        $dockerProblem = 'Docker를 찾을 수 없습니다. Docker Desktop 설치를 확인하세요.'
        $dockerPath = (Get-Command docker.exe -CommandType Application -ErrorAction Stop).Source
        if (-not (Invoke-Docker -Arguments 'info' -TimeoutSeconds 5)) {
            $dockerProblem = 'Docker Desktop을 시작하지 못했습니다. Docker Desktop 오류를 확인하세요.'
            $dockerDesktop = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
            if (-not (Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue)) {
                # Delete only this known stale file, never a directory or other socket.
                $staleSocket = Join-Path $env:LOCALAPPDATA 'Docker\run\dockerInference'
                if (Test-Path -LiteralPath $staleSocket -PathType Leaf) {
                    Remove-Item -LiteralPath $staleSocket -Force -Confirm:$false
                }
                Start-Process -FilePath $dockerDesktop -ErrorAction Stop | Out-Null
            }
            $deadline = [DateTime]::UtcNow.AddSeconds(120)
            $dockerReady = $false
            while ([DateTime]::UtcNow -lt $deadline) {
                $remaining = [int][Math]::Floor(($deadline - [DateTime]::UtcNow).TotalSeconds)
                if ($remaining -lt 1) { break }
                if (Invoke-Docker -Arguments 'info' -TimeoutSeconds ([Math]::Min(5, $remaining))) {
                    $dockerReady = $true
                    break
                }
                Start-Sleep -Milliseconds 500
            }
            if (-not $dockerReady) { throw 'docker-unavailable' }
        }
        $dockerProblem = '거래 봇을 시작하지 못했습니다. Docker Desktop과 freqtrade 설정을 확인하세요.'
        if (-not (Invoke-Docker -Arguments 'compose -f freqtrade/docker-compose.yml up -d' -TimeoutSeconds 120)) {
            throw 'docker-unavailable'
        }
        $dockerProblem = $null
    }
    catch {
        # Keep $dockerProblem for the error box in case the panel cannot start either.
    }

    if (-not $panelWasRunning) {
        $failureMessage = '패널을 열지 못했습니다. 가상환경 설치와 8090 포트 사용 여부를 확인한 뒤 다시 실행하세요. 급하면 비트겟 웹(PC)에서 직접 청산하세요.'
        if (-not (Start-Panel)) { throw 'panel-not-ready' }
        Open-PanelWindow
    }
}
catch {
    $message = if ($dockerProblem) { $dockerProblem + "`n" + $failureMessage } else { $failureMessage }
    [void][System.Windows.Forms.MessageBox]::Show($message, '비트겟 봇', 'OK', 'Error')
    exit 1
}
