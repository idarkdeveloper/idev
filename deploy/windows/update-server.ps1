# Update the Oracle server to the latest code, in one click.
# Pulls the latest code, installs any new requirements into the server's Python environment, restarts the watch and
# dashboard services, then shows whether both are running. Reads and changes nothing on this laptop.
#
# Run:   powershell -ExecutionPolicy Bypass -File deploy\windows\update-server.ps1
# Tip:   heavy work belongs after 15:30 IST; this update itself takes seconds and is safe in market hours.

param(
    [string]$Server = "130.210.18.7",
    [string]$User = "ubuntu",
    [string]$Key = "$HOME\.ssh\oracle_agent"
)

$Host.UI.RawUI.WindowTitle = "Trading Agent server update"
if (-not (Test-Path $Key)) { Write-Error "SSH key not found: $Key"; Read-Host "Press Enter to close"; exit 1 }

# python -m pip works in every server environment (the Python 3.15 one has no bin/pip script).
$remote = "cd /opt/trading-agent && sudo -u agent git pull && sudo -u agent .venv/bin/python -m pip install -q -r requirements.txt && sudo systemctl restart trading-agent-watch trading-agent-dashboard && echo && echo 'Services:' && systemctl is-active trading-agent-watch trading-agent-dashboard && sudo -u agent git log --oneline -1"

Write-Host "Updating $Server ..." -ForegroundColor Cyan
ssh -i "$Key" "$User@$Server" $remote
if ($LASTEXITCODE -eq 0) {
    Write-Host "`nDone: the server runs the latest code." -ForegroundColor Green
} else {
    Write-Host "`nThe update stopped with an error (exit $LASTEXITCODE). Nothing after the failing step ran; read the lines above." -ForegroundColor Red
}
Read-Host "Press Enter to close"
