# Pure helpers for server_ctl.ps1 (dot-sourced; unit-tested with mocked process data).

function Split-CommandLine([string]$CommandLine) {
    $out = @()
    foreach ($m in [regex]::Matches($CommandLine, '"([^"]*)"|(\S+)')) {
        $out += $(if ($m.Groups[1].Success) { $m.Groups[1].Value } else { $m.Groups[2].Value })
    }
    return $out
}

function Get-PythonScript([string]$CommandLine) {
    # Returns the script a Python interpreter command line executes, or $null when the executable is not
    # Python or the run is -c/-m/stdin (no script file). Only the interpreter's own script position counts.
    $tokens = @(Split-CommandLine $CommandLine)
    if ($tokens.Count -lt 2) { return $null }
    $exeName = ($tokens[0] -split '[\x5C/]')[-1]
    if ($exeName -notmatch '^pythonw?(\d+(\.\d+)*)?(\.exe)?$') { return $null }
    for ($i = 1; $i -lt $tokens.Count; $i++) {
        $a = $tokens[$i]
        if ($a -eq '-' -or $a -eq '--') { return $null }
        if ($a -match '^-[A-Za-z]') {
            if ($a -cmatch '^-[A-Za-z]*[cm]') { return $null }        # -c / -m: no script file
            if ($a -ceq '-W' -or $a -ceq '-X') { $i++ }               # options that take a separate value
            continue
        }
        if ($a -match '^--') { continue }
        return $a                                                      # first positional = the script
    }
    return $null
}

function Test-OwnsServer($Proc, $Root) {
    # $Proc: @{ ExecutablePath; CommandLine }. True only for a Python interpreter whose script is this checkout's run.py:
    #  - an absolute script path that resolves to <Root>\run.py, or
    #  - a bare/relative run.py launched by this checkout's own .venv interpreter (the interpreter identifies the
    #    checkout; the working directory of a running process is not readable, so this is the strongest proof available).
    $script = Get-PythonScript $Proc.CommandLine
    if (-not $script) { return $false }
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $want = Join-Path $rootFull 'run.py'
    if ([IO.Path]::IsPathRooted($script)) {
        return [string]::Equals([IO.Path]::GetFullPath($script), $want, 'OrdinalIgnoreCase')
    }
    if ($script -ne 'run.py' -and $script -ne '.\run.py' -and $script -ne './run.py') { return $false }
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
