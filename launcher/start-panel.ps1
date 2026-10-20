#requires -Version 5.1
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
$repoRoot = Split-Path -Parent $PSScriptRoot
$failureMessage = '패널을 시작하지 못했습니다. 작업 폴더와 설치 상태를 확인하세요.'

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

try {
    Set-Location -LiteralPath $repoRoot
    $failureMessage = 'Docker를 찾을 수 없습니다. Docker Desktop을 설치한 뒤 다시 실행하세요.'
    $dockerPath = (Get-Command docker.exe -CommandType Application -ErrorAction Stop).Source
    if (-not (Invoke-Docker -Arguments 'info' -TimeoutSeconds 5)) {
        $failureMessage = 'Docker Desktop을 시작하지 못했습니다. Docker Desktop 오류를 확인한 뒤 다시 실행하세요.'
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
        if (-not $dockerReady) { throw 'docker-not-ready' }
    }

    $failureMessage = '거래 봇을 시작하지 못했습니다. Docker Desktop과 freqtrade 설정을 확인하세요. 잠시 후 바로가기를 다시 실행하세요.'
    if (-not (Invoke-Docker -Arguments 'compose -f freqtrade/docker-compose.yml up -d' -TimeoutSeconds 120)) {
        throw 'compose-failed'
    }

    $failureMessage = '패널을 열지 못했습니다. 가상환경 설치와 8090 포트 사용 여부를 확인한 뒤 다시 실행하세요.'
    if (-not (Test-Panel)) {
        $python = Join-Path $repoRoot '.venv\Scripts\pythonw.exe'
        # pythonw has no console handles and uvicorn's logging setup fails on them, so give it files.
        # Access log is off in the server, so these files hold no token.
        $panelDir = Join-Path $repoRoot '.panel'
        New-Item -ItemType Directory -Force -Path $panelDir | Out-Null
        Start-Process -FilePath $python -ArgumentList '-m bitgetbot.panel' -WorkingDirectory $repoRoot -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $panelDir 'panel.out.log') -RedirectStandardError (Join-Path $panelDir 'panel.err.log') `
            -ErrorAction Stop | Out-Null
        $deadline = [DateTime]::UtcNow.AddSeconds(20)
        $panelReady = $false
        while ([DateTime]::UtcNow -lt $deadline) {
            if (Test-Panel) { $panelReady = $true; break }
            Start-Sleep -Milliseconds 250
        }
        if (-not $panelReady) { throw 'panel-not-ready' }
    }

    $failureMessage = '패널 로그인 정보를 읽지 못했습니다. 패널을 다시 시작한 뒤 바로가기를 실행하세요.'
    $token = [IO.File]::ReadAllText((Join-Path $repoRoot '.panel\token')).Trim()
    if ($token -notmatch '^[A-Za-z0-9_-]{43}$') { throw 'invalid-token' }
    $url = 'http://127.0.0.1:8090/?t=' + $token
    $failureMessage = '브라우저를 열지 못했습니다. Edge 또는 기본 브라우저 설치 상태를 확인하세요.'
    $edge = Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'
    if (Test-Path -LiteralPath $edge -PathType Leaf) {
        Start-Process -FilePath $edge -ArgumentList ('--app=' + $url) -ErrorAction Stop | Out-Null
    }
    else {
        Start-Process -FilePath $url -ErrorAction Stop | Out-Null
    }
}
catch {
    [void][System.Windows.Forms.MessageBox]::Show($failureMessage, '비트겟 봇', 'OK', 'Error')
    exit 1
}
finally {
    $token = $null
    $url = $null
}
