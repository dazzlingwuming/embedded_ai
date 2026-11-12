param([string]$Board = '', [int]$Port = 8765, [switch]$Background)
$projectRoot = Split-Path $PSScriptRoot -Parent
$env:PYTHONUTF8 = '1'
Push-Location $projectRoot
try {
    $existingConsole = $null
    try { $existingConsole = Invoke-RestMethod "http://127.0.0.1:$Port/api/status" -TimeoutSec 2 } catch { }
    if ($existingConsole -and $existingConsole.board_url) {
        Write-Output "Console is already running at http://127.0.0.1:$Port"
        return
    }
    $consoleArguments = @('-u','-m','pc_backend.server','--port',"$Port")
    if ($Board) { $consoleArguments += @('--board',$Board) }
    if ($Background) {
        New-Item -ItemType Directory -Path "$projectRoot\logs","$projectRoot\local" -Force | Out-Null
        $consoleProcess = Start-Process -FilePath "$projectRoot\.tools\python\Scripts\python.exe" `
            -ArgumentList $consoleArguments -WorkingDirectory $projectRoot -WindowStyle Hidden `
            -RedirectStandardOutput "$projectRoot\logs\console-background.log" `
            -RedirectStandardError "$projectRoot\logs\console-background-error.log" -PassThru
        $consoleProcess.Id | Set-Content -LiteralPath "$projectRoot\local\console.pid"
        Write-Output "Console starting at http://127.0.0.1:$Port (PID $($consoleProcess.Id))"
    } else {
        & "$projectRoot\.tools\python\Scripts\python.exe" @consoleArguments
    }
} finally { Pop-Location }
