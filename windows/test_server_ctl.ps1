# Run: powershell -NoProfile -File windows\test_server_ctl.ps1
. "$PSScriptRoot\server_ctl_lib.ps1"
$root = 'C:\Apps\agentchattr'
$fail = 0
function Check($name, $got, $want) { if ($got -ne $want) { Write-Host "FAIL $name (got $got, want $want)"; $script:fail++ } else { Write-Host "ok   $name" } }
function P($exe, $cmd) { @{ ExecutablePath = $exe; CommandLine = $cmd } }
$venvPy = "$root\.venv\Scripts\python.exe"

Check 'abs run.py of this checkout'      (Test-OwnsServer (P 'C:\Python\python.exe' "python $root\run.py") $root) $true
Check 'abs quoted run.py of this checkout' (Test-OwnsServer (P 'C:\Python\python.exe' "python `"$root\run.py`" --port 8300") $root) $true
Check 'relative run.py via our venv (legacy launch, refused)' (Test-OwnsServer (P $venvPy 'python run.py') $root) $false
Check 'unrelated abs run.py'             (Test-OwnsServer (P 'C:\Python\python.exe' 'python C:\OtherApp\run.py') $root) $false
Check 'other_run.py'                     (Test-OwnsServer (P $venvPy 'python other_run.py') $root) $false
Check 'relative run.py, system python'   (Test-OwnsServer (P 'C:\Python\python.exe' 'python run.py') $root) $false
Check 'relative run.py, other venv'      (Test-OwnsServer (P 'C:\OtherApp\.venv\Scripts\python.exe' 'python run.py') $root) $false
Check 'sibling dir prefix (agentchattr2)' (Test-OwnsServer (P 'C:\Apps\agentchattr2\.venv\Scripts\python.exe' 'python run.py') $root) $false
Check 'run.py only as arg of another script' (Test-OwnsServer (P $venvPy "python C:\OtherApp\worker.py $root\run.py") $root) $false
Check 'python -m http.server --directory run.py' (Test-OwnsServer (P 'C:\Python\python.exe' "python -m http.server --directory $root\run.py") $root) $false
Check 'python -c with run.py arg'        (Test-OwnsServer (P $venvPy "python -c print(1) $root\run.py") $root) $false
Check 'cmd /c type run.py'               (Test-OwnsServer (P 'C:\Windows\System32\cmd.exe' "cmd /c type $root\run.py") $root) $false
Check 'non-python exe with abs run.py'   (Test-OwnsServer (P 'C:\Tools\node.exe' "node $root\run.py") $root) $false
Check 'python flags before script'       (Test-OwnsServer (P 'C:\Python\python.exe' "python -u -X utf8 $root\run.py") $root) $true
Check 'pythonw quoted abs run.py'        (Test-OwnsServer (P 'C:\Python\pythonw.exe' "`"C:\Python\pythonw.exe`" `"$root\run.py`"") $root) $true
Check 'no run.py'                      (Test-OwnsServer (P $venvPy 'python -m http.server 8300') $root) $false
Check 'chain: child base python, parent venv launcher (abs)' (Test-ChainOwnsServer @((P 'C:\Python\python.exe' "python $root\run.py"), (P $venvPy "python $root\run.py")) $root) $true
Check 'chain: legacy bare run.py refused' (Test-ChainOwnsServer @((P 'C:\Python\python.exe' 'python run.py'), (P $venvPy 'python run.py')) $root) $false
Check 'chain: unrelated'                 (Test-ChainOwnsServer @((P 'C:\Python\python.exe' 'python C:\OtherApp\run.py')) $root) $false

$parents = @{ 300 = 200; 200 = 100; 999 = 1 }
Check 'listener is launched pid'         (Test-InTree 100 100 $parents) $true
Check 'listener is grandchild'           (Test-InTree 300 100 $parents) $true
Check 'unexpected listener'              (Test-InTree 999 100 $parents) $false
if ($fail) { Write-Host "$fail failed"; exit 1 } else { Write-Host 'all passed' }
