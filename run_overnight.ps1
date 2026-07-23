<#
.SYNOPSIS
    Supervised launcher for unattended (overnight) operation.

.DESCRIPTION
    Starts the dashboard and keeps it up. If the Streamlit process exits for any
    reason it is restarted after a short backoff, so an overnight run survives a
    crash, an OOM kill, or a transient failure that takes the server down.

    The app itself already tolerates upstream API outages: a failed refresh falls
    back to the last good snapshot (held in memory and mirrored to .cache/) and
    the page shows a STALE banner instead of blanking. This script covers the
    remaining case, the process itself dying.

    Logs are appended, never truncated, so an overnight run leaves a full record.
    Press Ctrl+C to stop supervising and shut the server down.

.EXAMPLE
    .\run_overnight.ps1
    .\run_overnight.ps1 -Port 8600 -LogDir "C:\logs\mehi"
#>
[CmdletBinding()]
param(
    [int]$Port = 8501,
    [string]$LogDir = "",
    [int]$RestartDelaySeconds = 10,
    [int]$MaxRestarts = 0   # 0 = unlimited
)

$ErrorActionPreference = "Stop"

# $PSScriptRoot is not reliably populated during param binding under
# Windows PowerShell 5.1, so resolve paths here instead of in the defaults.
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $projectRoot) { $projectRoot = (Get-Location).Path }
if (-not $LogDir) { $LogDir = Join-Path $projectRoot "logs" }

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

$supervisorLog = Join-Path $LogDir "supervisor.log"

function Write-Log {
    param([string]$Message)
    $line = "{0:yyyy-MM-dd HH:mm:ss}Z  {1}" -f (Get-Date).ToUniversalTime(), $Message
    Write-Host $line
    Add-Content -Path $supervisorLog -Value $line -Encoding utf8
}

Write-Log "supervisor starting; project=$projectRoot port=$Port"
Write-Log "dashboard will be at http://localhost:$Port  (Ctrl+C to stop)"

$restarts = 0
$stopping = $false

# Make Ctrl+C shut the child down rather than orphaning it.
try {
    [Console]::TreatControlCAsInput = $false
} catch { }

while (-not $stopping) {
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
    $outLog = Join-Path $LogDir "streamlit-$stamp.out.log"
    $errLog = Join-Path $LogDir "streamlit-$stamp.err.log"

    Write-Log "launching streamlit (attempt $($restarts + 1)); stdout=$outLog"

    $proc = Start-Process -FilePath "python" `
        -ArgumentList "-m", "streamlit", "run", "app.py",
                      "--server.port", $Port,
                      "--server.headless", "true",
                      "--server.fileWatcherType", "none" `
        -WorkingDirectory $projectRoot `
        -RedirectStandardOutput $outLog `
        -RedirectStandardError $errLog `
        -NoNewWindow -PassThru

    Write-Log "streamlit running with PID $($proc.Id)"

    try {
        Wait-Process -Id $proc.Id
        $exitCode = $proc.ExitCode
    } catch {
        Write-Log "supervisor interrupted: $_"
        $stopping = $true
        try { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } catch { }
        break
    }

    Write-Log "streamlit PID $($proc.Id) exited with code $exitCode"

    $tail = ""
    if (Test-Path $errLog) {
        $tail = (Get-Content $errLog -Tail 5 -ErrorAction SilentlyContinue) -join " | "
    }
    if ($tail) { Write-Log "last stderr: $tail" }

    $restarts++
    if ($MaxRestarts -gt 0 -and $restarts -ge $MaxRestarts) {
        Write-Log "reached MaxRestarts=$MaxRestarts; supervisor stopping"
        break
    }

    Write-Log "restarting in $RestartDelaySeconds s"
    Start-Sleep -Seconds $RestartDelaySeconds
}

Write-Log "supervisor stopped after $restarts launch(es)"
