param([switch]$BuildOnly, [string]$Port = 'COM5')
. "$PSScriptRoot\activate.ps1"
& "$env:IDF_PYTHON_ENV_PATH\Scripts\python.exe" 'P:\scripts\generate-wifi.py'
if ($LASTEXITCODE -ne 0) { throw 'Check local/wifi.json.' }
Push-Location 'P:\firmware'
try {
    & "$env:IDF_PYTHON_ENV_PATH\Scripts\python.exe" "$env:IDF_PATH\tools\idf.py" -B P:\.build\gateway build
    if ($LASTEXITCODE -ne 0) { throw 'Gateway build failed.' }
    if (-not $BuildOnly) {
        & "$env:IDF_PYTHON_ENV_PATH\Scripts\python.exe" "$env:IDF_PATH\tools\idf.py" -B P:\.build\gateway -p $Port -b 921600 flash
        if ($LASTEXITCODE -ne 0) { throw 'Gateway flashing failed.' }
    }
} finally { Pop-Location }
