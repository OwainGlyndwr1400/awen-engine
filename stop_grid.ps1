# Stops every Awen Grid python service (engine, echo, soul engine, deck).
# Used by Start Awen Grid.bat to guarantee a clean launch; also runnable alone.
param([switch]$List)

# Ask the engine to flush its dirty FAISS indices BEFORE the force-kill.
# Stop-Process -Force skips Python's atexit, so any vectors added since the
# last periodic flush existed only in memory and were lost — that is exactly
# how one Nyx dream ended up in the ledger with no vector (20 Aug). The ledger
# never loses anything, but this makes the on-disk index match it on every
# clean stop, so the INTEGRITY warning at startup becomes a genuine anomaly
# again instead of routine noise.
if (-not $List) {
    try {
        Invoke-WebRequest -Uri "http://127.0.0.1:5000/flush" -Method POST `
            -TimeoutSec 45 -UseBasicParsing | Out-Null
        Write-Output "engine indices flushed to disk"
    } catch {
        Write-Output "engine not reachable for flush (already down, or mid-load) - continuing"
    }
}

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
