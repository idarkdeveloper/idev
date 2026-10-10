# Open the Oracle server's dashboard on this laptop through an SSH tunnel.
# The dashboard on the server listens only on the server itself (127.0.0.1:8787); the tunnel forwards
# http://127.0.0.1:8788 on this laptop to it. Nothing is opened to the internet.
#
# Run:   powershell -ExecutionPolicy Bypass -File deploy\windows\open-server-dashboard.ps1
# Stop:  close the "Trading Agent tunnel" window (or press Ctrl+C in it).

param(
    [string]$Server = "130.210.18.7",
    [string]$User = "ubuntu",
    [string]$Key = "$HOME\.ssh\oracle_agent",
    [int]$LocalPort = 8788
)

if (-not (Test-Path $Key)) { Write-Error "SSH key not found: $Key"; exit 1 }

# Reuse a tunnel that is already running.
$busy = Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue
if (-not $busy) {
    $sshArgs = "-i `"$Key`" -N -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -L ${LocalPort}:127.0.0.1:8787 $User@$Server"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", "`$Host.UI.RawUI.WindowTitle='Trading Agent tunnel'; ssh $sshArgs"
    # Wait up to 15 s for the tunnel to come up.
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        if (Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue) { break }
    }
}

Start-Process "http://127.0.0.1:$LocalPort/"
