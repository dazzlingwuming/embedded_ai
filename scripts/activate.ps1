. "$PSScriptRoot\env.ps1"
$idfPython = "$env:IDF_PYTHON_ENV_PATH\Scripts\python.exe"
$exported = & $idfPython "$env:IDF_PATH\tools\idf_tools.py" export --format key-value
if ($LASTEXITCODE -ne 0) { throw 'ESP-IDF tools are not ready.' }
foreach ($entry in $exported) {
    if ($entry -match '^([A-Za-z_][A-Za-z_0-9]*)=(.*)$') {
        $value = $Matches[2].Trim('"')
        if ($Matches[1] -eq 'PATH') {
            $value = $value.Replace('$PATH', $env:PATH).Replace('%PATH%', $env:PATH)
        }
        [Environment]::SetEnvironmentVariable($Matches[1], $value, 'Process')
    }
}
$env:PATH = "$env:IDF_PYTHON_ENV_PATH\Scripts;$env:PATH"
$env:IDF_PYTHON_CHECK_CONSTRAINTS = 'yes'
