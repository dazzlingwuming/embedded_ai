param([Parameter(ValueFromRemainingArguments=$true)][string[]]$BrowserArguments)
$projectRoot = Split-Path $PSScriptRoot -Parent
$env:npm_config_cache = Join-Path $projectRoot '.cache\npm'
$env:TEMP = Join-Path $projectRoot '.cache\tmp'
$env:TMP = $env:TEMP
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $projectRoot '.cache\browsers'
$env:PWTEST_DAEMON_SESSION_DIR = Join-Path $projectRoot '.cache\playwright-daemon'
$env:NO_UPDATE_NOTIFIER = '1'
$env:PLAYWRIGHT_CLI_SESSION = 'benchlink'
New-Item -ItemType Directory -Path (Join-Path $projectRoot '.playwright'),(Join-Path $projectRoot 'output\playwright') -Force | Out-Null
Push-Location $projectRoot
try { & npx.cmd --yes --package '@playwright/cli' playwright-cli @BrowserArguments }
finally { Pop-Location }
