# Pure helpers for server_ctl.ps1 (dot-sourced; unit-tested with mocked process data).

function Get-RunPyToken([string]$CommandLine) {
    # Returns the argument that is a run.py path (bare or absolute), or $null. Rejects other_run.py etc.
    foreach ($m in [regex]::Matches($CommandLine, '"([^"]+)"|(\S+)')) {
        $t = if ($m.Groups[1].Success) { $m.Groups[1].Value } else { $m.Groups[2].Value }
        if ($t -match '(^|[\x5C/])run\.py$') { return $t }
    }
    return $null
}

function Test-OwnsServer($Proc, $Root) {
    # $Proc: @{ ExecutablePath; CommandLine }. True only if it is this checkout's run.py:
    #  - an absolute run.py token that resolves to <Root>\run.py, or
    #  - a bare/relative run.py launched by this checkout's .venv interpreter.
    $token = Get-RunPyToken $Proc.CommandLine
    if (-not $token) { return $false }
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $want = Join-Path $rootFull 'run.py'
    if ([IO.Path]::IsPathRooted($token)) {
        return [string]::Equals([IO.Path]::GetFullPath($token), $want, 'OrdinalIgnoreCase')
    }
    if ($token -ne 'run.py' -and $token -ne '.\run.py' -and $token -ne './run.py') { return $false }
    $venv = Join-Path $rootFull '.venv\'
    return [bool]($Proc.ExecutablePath -and $Proc.ExecutablePath.StartsWith($venv, 'OrdinalIgnoreCase'))
}

function Test-ChainOwnsServer($Chain, $Root) {
    # $Chain: the listener and its ancestors (listener first). The venv launcher may be the parent of the real interpreter.
    foreach ($p in $Chain) { if (Test-OwnsServer $p $Root) { return $true } }
    return $false
}

function Test-InTree([int]$ListenerPid, [int]$LaunchedPid, $ParentOf) {
    # $ParentOf: hashtable pid -> parent pid. True if the listener is the launched process or a descendant.
    $cur = $ListenerPid
    for ($i = 0; $i -lt 8 -and $cur; $i++) {
        if ($cur -eq $LaunchedPid) { return $true }
        $cur = $ParentOf[$cur]
    }
    return $false
}
