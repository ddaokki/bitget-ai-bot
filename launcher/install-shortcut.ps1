#requires -Version 5.1
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms

try {
    $repoRoot = Split-Path -Parent $PSScriptRoot
    $desktop = [Environment]::GetFolderPath('Desktop')
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut((Join-Path $desktop '비트겟 봇.lnk'))
    $shortcut.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $shortcut.Arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + (Join-Path $repoRoot 'launcher\start-panel.ps1') + '"'
    $shortcut.WorkingDirectory = $repoRoot
    $shortcut.WindowStyle = 7
    $shortcut.Description = '비트겟 봇 제어판 열기'
    # Use the optional converted icon when present; otherwise keep the default.
    $icon = Join-Path $repoRoot 'bitgetbot\panel\static\icon-192.ico'
    if (Test-Path -LiteralPath $icon -PathType Leaf) {
        $shortcut.IconLocation = $icon + ',0'
    }
    $shortcut.Save()
}
catch {
    [void][System.Windows.Forms.MessageBox]::Show('바탕화면 바로가기를 만들지 못했습니다. 바탕화면 폴더의 권한을 확인하세요.', '비트겟 봇', 'OK', 'Error')
    exit 1
}
