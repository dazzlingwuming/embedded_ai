param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$status = $null
try { $status = Invoke-RestMethod "http://127.0.0.1:$Port/api/status" -TimeoutSec 2 } catch { }
if (-not $status) { Write-Output 'Console is not running.'; return }
if ($status.recording) { throw 'Stop the recording in the UI before closing the console.' }
try {
    Invoke-RestMethod -Method Post "http://127.0.0.1:$Port/api/console/stop" -ContentType 'application/json' -Body '{}' -TimeoutSec 3 | Out-Null
    Write-Output 'Console is stopping gracefully.'
} catch {
    # One-time compatibility fallback for the older project-owned launcher.
    $pidFile = Join-Path $projectRoot 'local\console.pid'
    if (-not (Test-Path -LiteralPath $pidFile)) { throw 'Could not identify this project console process.' }
    $consoleProcessId = [int](Get-Content -LiteralPath $pidFile -Raw)
    $consoleProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $consoleProcessId"
    $expectedPython = Join-Path $projectRoot '.tools\python\Scripts\python.exe'
    if (-not $consoleProcess -or $consoleProcess.ExecutablePath -ne $expectedPython -or
        $consoleProcess.CommandLine -notmatch '-m\s+pc_backend\.server') { throw 'Console process identity check failed.' }
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $consoleProcessId" |
        Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match '-m\s+pc_backend\.server' }
    foreach ($child in $children) { Stop-Process -Id $child.ProcessId -Force }
    Stop-Process -Id $consoleProcessId -Force -ErrorAction SilentlyContinue
    Write-Output 'Older project console stopped.'
}
for ($attempt=0; $attempt -lt 30; $attempt++) {
    try { Invoke-RestMethod "http://127.0.0.1:$Port/api/status" -TimeoutSec 1 | Out-Null }
    catch { return }
    Start-Sleep -Milliseconds 200
}
throw 'Console did not stop in time.'
