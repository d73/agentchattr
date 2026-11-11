# agentchattr server control: server_ctl.ps1 stop|start|restart
# Finds the server via the configured web port (config.toml + config.local.toml + AGENTCHATTR_* env),
# verifies the listener is this checkout's run.py, and never touches agent wrappers.
param([Parameter(Mandatory)][ValidateSet('stop', 'start', 'restart')][string]$Action)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$venvPy = Join-Path $root '.venv\Scripts\python.exe'
$py = if (Test-Path $venvPy) { $venvPy } else { 'python' }

$port = & $py -c "from pathlib import Path; from config_loader import load_config; print(load_config(Path('.'))['server']['port'])"
if ($LASTEXITCODE -ne 0 -or $port -notmatch '^\d+$') { Write-Error "Could not read server.port from config."; exit 1 }
$port = [int]$port

function Get-ServerPid {
    $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { $c.OwningProcess } else { $null }
}

function Stop-Server {
    $srvPid = Get-ServerPid
    if (-not $srvPid) { Write-Host "No server listening on port $port."; return }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$srvPid"
    if (-not $proc -or $proc.CommandLine -notmatch 'run\.py') {
        Write-Error "Port $port is held by PID $srvPid ($($proc.CommandLine)), which is not agentchattr's run.py. Not stopping it."
        exit 2
    }
    # the process tree too: the venv python launcher can sit in front of the real interpreter
    $tree = @($srvPid) + @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$srvPid" | ForEach-Object { $_.ProcessId })
    & taskkill /PID $srvPid /T /F | Out-Null
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        $alive = $tree | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue }
        if (-not $alive -and -not (Get-ServerPid)) { Write-Host "Server (PID $srvPid) stopped."; return }
        Start-Sleep -Milliseconds 300
    }
    Write-Error "Server PID $srvPid did not exit within 30s."
    exit 3
}

function Start-Server {
    if (Get-ServerPid) { Write-Error "Port $port is already in use; not starting."; exit 4 }
    if (-not (Test-Path $venvPy)) {
        & python -m venv .venv; if ($LASTEXITCODE -ne 0) { Write-Error "venv creation failed."; exit 5 }
        & $venvPy -m pip install -q -r requirements.txt; if ($LASTEXITCODE -ne 0) { Write-Error "pip install failed."; exit 5 }
    }
    $p = Start-Process -FilePath $venvPy -ArgumentList 'run.py' -WorkingDirectory $root -WindowStyle Hidden -PassThru
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        if ($p.HasExited) { Write-Error "Server exited during startup (code $($p.ExitCode)). Run `python run.py` to see the error."; exit 6 }
        if (Get-ServerPid) { Write-Host "Server running on port $port (PID $($p.Id))."; return }
        Start-Sleep -Milliseconds 300
    }
    Write-Error "Server did not start listening on port $port within 30s."
    exit 7
}

switch ($Action) {
    'stop'    { Stop-Server }
    'start'   { Start-Server }
    'restart' { Stop-Server; Start-Server }
}
