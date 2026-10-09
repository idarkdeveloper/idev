# Run the trading agent's watch mode on this Windows laptop whenever you log in.
# Usage (from the repo folder, in PowerShell):  .\deploy\windows\install-autostart.ps1
# Remove again:                                 .\deploy\windows\install-autostart.ps1 -Remove
#
# Watch mode keeps the laptop awake only during market hours on NSE trading days and
# sleeps it normally otherwise. The laptop still has to be on, plugged in and online.
param([switch]$Remove)

$TaskName = "Trading Agent watch"
if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed the '$TaskName' task."
    return
}

$Repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$Python = Join-Path $Repo ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $Python)) { throw "No virtual environment at $Repo\.venv - create it first (python -m venv .venv; .venv\Scripts\pip install -r requirements.txt)." }
if (-not (Test-Path (Join-Path $Repo ".env"))) { throw "No .env in $Repo - copy .env.example to .env and fill it in first." }

$Action = New-ScheduledTaskAction -Execute $Python -Argument "-m trading_agent watch" -WorkingDirectory $Repo
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 2) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "Trading Agent watch mode: NSE deals, announcements and stops on trading days." -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Installed '$TaskName': it starts at every login and is running now."
Write-Host "Check your public IP is fixed with:  .venv\Scripts\python -m trading_agent groww-check --ip"
