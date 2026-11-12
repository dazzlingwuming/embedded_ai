param([switch]$BuildOnly, [string]$Port = 'COM5')
. "$PSScriptRoot\activate.ps1"
& "$env:IDF_PYTHON_ENV_PATH\Scripts\python.exe" 'P:\scripts\generate-wifi.py'
if ($LASTEXITCODE -ne 0) { throw 'Check local/wifi.json.' }
Push-Location 'P:\camera'
try {
    & "$env:IDF_PYTHON_ENV_PATH\Scripts\python.exe" "$env:IDF_PATH\tools\idf.py" '-B' 'P:\.build\camera' '-D' 'SDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.defaults.nano' build
    if ($LASTEXITCODE -ne 0) { throw 'Camera build failed.' }
    if (-not $BuildOnly) {
        & "$env:IDF_PYTHON_ENV_PATH\Scripts\python.exe" "$env:IDF_PATH\tools\idf.py" '-B' 'P:\.build\camera' -p $Port -b 921600 flash
        if ($LASTEXITCODE -ne 0) { throw 'Camera flashing failed.' }
    }
} finally { Pop-Location }
