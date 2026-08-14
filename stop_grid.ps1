# Stops every Awen Grid python service (engine, echo, soul engine, deck).
# Used by Start Awen Grid.bat to guarantee a clean launch; also runnable alone.
param([switch]$List)

$pattern = 'Gnostic Engine|Gnostic Echo|Tesla Soul|Awen Command Deck'
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match $pattern }

if (-not $procs) { Write-Output "no grid processes running"; exit 0 }

foreach ($p in $procs) {
    $name = ($p.CommandLine -split '"')[-2]
    if ($List) {
        Write-Output ("would stop {0}: {1}" -f $p.ProcessId, $name)
    } else {
        Write-Output ("stopping {0}: {1}" -f $p.ProcessId, $name)
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
}
