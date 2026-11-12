param([switch]$Flash, [string]$Port = 'COM5')
$projectRoot = Split-Path $PSScriptRoot -Parent
$referenceProject = Join-Path $projectRoot 'sources\examples\esp-idf\00_board_check'
if (-not (Test-Path -LiteralPath $referenceProject -PathType Container)) {
    throw 'This optional legacy check requires the upstream ESP32-P4-Platform repository in sources/. Clone https://github.com/waveshareteam/ESP32-P4-Platform.git into sources/ first. Use scripts/build-gateway.ps1 for the current combined firmware.'
}
. "$PSScriptRoot\activate.ps1"
Push-Location 'P:\sources\examples\esp-idf\00_board_check'
try {
    & "$env:IDF_PYTHON_ENV_PATH\Scripts\python.exe" "$env:IDF_PATH\tools\idf.py" '-B' 'P:\.build\board-check' '-D' 'SDKCONFIG_DEFAULTS=sdkconfig.defaults;P:/config/board-check.defaults' build
    if ($LASTEXITCODE -ne 0) { throw 'Board-check build failed.' }
    if ($Flash) {
        & "$env:IDF_PYTHON_ENV_PATH\Scripts\python.exe" "$env:IDF_PATH\tools\idf.py" '-B' 'P:\.build\board-check' -p $Port -b 921600 flash
        if ($LASTEXITCODE -ne 0) { throw 'Board-check flashing failed.' }
    }
} finally { Pop-Location }
