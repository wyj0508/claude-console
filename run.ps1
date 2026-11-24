# Claude Console launcher for Windows (PowerShell 5.1+).
# Keep this file ASCII-only: Windows PowerShell reads .ps1 without BOM as ANSI.
#
# Usage:
#   .\run.ps1              start with a console (errors stay visible)
#   .\run.ps1 --background start with pythonw (no console window)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = 'python' }

if ($args.Count -gt 0 -and $args[0] -eq '--background') {
    $pythonw = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
    if (-not (Test-Path -LiteralPath $pythonw)) { $pythonw = 'pythonw' }
    $rest = if ($args.Count -gt 1) { $args[1..($args.Count - 1)] } else { @() }
    Start-Process -FilePath $pythonw -ArgumentList (@((Join-Path $PSScriptRoot 'main.py')) + $rest)
    exit 0
}

& $python (Join-Path $PSScriptRoot 'main.py') @args
