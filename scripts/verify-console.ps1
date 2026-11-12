$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$env:PYTHONUTF8 = '1'
Set-Location -LiteralPath $projectRoot
. "$PSScriptRoot\start-console.ps1" -Background
$ready = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    try {
        $aiStatus = Invoke-RestMethod 'http://127.0.0.1:8765/api/ai/status' -TimeoutSec 2
        $ready = $true
        break
    } catch { Start-Sleep -Milliseconds 300 }
}
if (-not $ready) { throw 'Console did not start.' }
& "$projectRoot\.tools\python\Scripts\python.exe" -u -m unittest discover -s tests -p 'test_*.py' -v *> "$projectRoot\logs\unit-tests-final.log"
if ($LASTEXITCODE -ne 0) { throw 'Unit tests failed.' }

function Invoke-ProjectBrowser([string[]]$CliArguments) {
    $result = & "$PSScriptRoot\browser.ps1" @CliArguments 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0 -or $result -match '### Error') { throw "Browser check failed: $result" }
    return $result
}
function Find-PageReference([string]$Snapshot, [string]$Role, [string]$Label) {
    $pattern = '(?m)\b' + [regex]::Escape($Role) + ' "' + [regex]::Escape($Label) + '"[^\r\n]*\[ref=([^\]]+)\]'
    $matched = [regex]::Match($Snapshot, $pattern)
    if (-not $matched.Success) { throw "Visible UI element not found: $Role $Label" }
    return $matched.Groups[1].Value
}

Invoke-ProjectBrowser @('reload') | Out-Null
$page = Invoke-ProjectBrowser @('snapshot')
$aiTab = Find-PageReference $page 'button' 'AI 助手'
Invoke-ProjectBrowser @('click',$aiTab) | Out-Null
$page = Invoke-ProjectBrowser @('snapshot')
$messageBox = Find-PageReference $page 'textbox' '消息'
$sendButton = Find-PageReference $page 'button' '发送消息'
Invoke-ProjectBrowser @('fill',$messageBox,'请只回复：网页 AI 连接测试完成。') | Out-Null
Invoke-ProjectBrowser @('click',$sendButton) | Out-Null
$answered = $false
for ($attempt = 0; $attempt -lt 12; $attempt++) {
    $page = Invoke-ProjectBrowser @('snapshot')
    if ($page -match '回答完成') { $answered = $true; break }
    Start-Sleep -Milliseconds 300
}
if (-not $answered) { throw 'AI answer did not finish in the UI.' }
$page | Set-Content -LiteralPath "$projectRoot\logs\browser-ai-final-snapshot.txt" -Encoding utf8
Invoke-ProjectBrowser @('screenshot','--full-page','--filename','output/playwright/ai-final-desktop.png') | Out-Null
Invoke-ProjectBrowser @('resize','390','844') | Out-Null
$metrics = Invoke-ProjectBrowser @('eval','() => ({viewport:innerWidth,pageWidth:document.documentElement.scrollWidth})')
$metrics | Set-Content -LiteralPath "$projectRoot\logs\browser-ai-mobile-metrics.log" -Encoding utf8
Invoke-ProjectBrowser @('screenshot','--full-page','--filename','output/playwright/ai-final-mobile.png') | Out-Null
Invoke-ProjectBrowser @('resize','1280','900') | Out-Null
$page = Invoke-ProjectBrowser @('snapshot')
$workbench = Find-PageReference $page 'button' '采集工作台'
Invoke-ProjectBrowser @('click',$workbench) | Out-Null
$console = Invoke-ProjectBrowser @('console','error')
$console | Set-Content -LiteralPath "$projectRoot\logs\browser-ai-errors.log" -Encoding utf8
if ($console -match 'Errors: [1-9]') { throw 'Browser console has errors; inspect log.' }
Write-Output 'Console, AI streaming UI, and unit checks passed.'
