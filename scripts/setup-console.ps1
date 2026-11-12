param([string]$Python = '', [switch]$DryRun)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$venvRoot = Join-Path $projectRoot '.tools\python'
$venvPython = Join-Path $venvRoot 'Scripts\python.exe'
$requirements = Join-Path $projectRoot 'requirements.txt'

# Keep this installer independent of ESP-IDF, activate.ps1 and system settings.
$probeCode = @'
import importlib.util, json, platform, struct, sys
print(json.dumps({'major':sys.version_info.major, 'minor':sys.version_info.minor,
                  'version':platform.python_version(), 'implementation':platform.python_implementation(),
                  'bits':struct.calcsize('P')*8, 'audioop':importlib.util.find_spec('audioop') is not None}))
'@
$packageCode = @'
import importlib.metadata as m, json, pathlib, sys
missing = []
for line in pathlib.Path(sys.argv[1]).read_text(encoding='utf-8-sig').splitlines():
    line = line.split('#', 1)[0].strip()
    if not line:
        continue
    name, wanted = line.split('==', 1)
    try:
        installed = m.version(name)
    except m.PackageNotFoundError:
        installed = None
    if installed != wanted:
        missing.append({'name':name, 'wanted':wanted, 'installed':installed})
print(json.dumps({'changes':missing}))
'@
$importCode = @'
import aiohttp, serial
from aiohttp import ClientWSTimeout
import pc_backend.server
print('Console imports: OK (no server or cloud session started)')
'@

function Get-PythonInfo([string]$Command, [string[]]$PrefixArgs = @()) {
    try {
        $raw = & $Command @PrefixArgs -B -c $probeCode 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        $info = ($raw -join "`n") | ConvertFrom-Json
        if ($info.implementation -eq 'CPython' -and $info.major -eq 3 -and
            $info.minor -in @(11,12) -and $info.bits -eq 64 -and $info.audioop) {
            return $info
        }
    } catch { }
    return $null
}

function Get-PackageState {
    $raw = & $venvPython -B -c $packageCode $requirements
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect project Python packages.' }
    return (($raw -join "`n") | ConvertFrom-Json)
}

function Assert-ConsoleStoppedForInstall {
    # A Windows venv launcher can delegate to its base Python executable, so
    # check the command line as well as ExecutablePath. Never stop a process.
    $processes = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'"
    $running = @($processes | Where-Object {
        $_.CommandLine -match '-m\s+pc_backend\.server' -and
        ($_.ExecutablePath -eq $venvPython -or
         $_.CommandLine -match [regex]::Escape($venvPython))
    })
    if ($running.Count -gt 0) {
        throw 'The project console is running. Dependencies were left unchanged. End its call/recording and stop it before installing packages.'
    }
}

$environmentNames = @('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','FTP_PROXY','NO_PROXY',
    'PIP_CONFIG_FILE','PIP_PROXY','PIP_EXTRA_INDEX_URL','PIP_TRUSTED_HOST','PIP_CACHE_DIR',
    'PIP_TARGET','PIP_PREFIX','PIP_ROOT','PIP_USER','PIP_LOG','PIP_BUILD_TRACKER','PIP_SRC',
    'PYTHONNOUSERSITE','PYTHONUTF8','PYTHONDONTWRITEBYTECODE','TEMP','TMP')
$previousEnvironment = @{}
foreach ($name in $environmentNames) {
    $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}

Push-Location $projectRoot
try {
    # Direct applies only to this script and its children; restore the caller's
    # process environment in finally. No system/user proxy settings are edited.
    foreach ($name in @('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','FTP_PROXY','PIP_PROXY',
                        'PIP_EXTRA_INDEX_URL','PIP_TRUSTED_HOST','PIP_TARGET','PIP_PREFIX',
                        'PIP_ROOT','PIP_USER','PIP_LOG','PIP_BUILD_TRACKER','PIP_SRC')) {
        [Environment]::SetEnvironmentVariable($name, $null, 'Process')
    }
    $env:NO_PROXY = '*'
    $env:PIP_CONFIG_FILE = 'NUL'
    $env:PYTHONNOUSERSITE = '1'
    $env:PYTHONUTF8 = '1'
    $env:PYTHONDONTWRITEBYTECODE = '1'

    $createEnvironment = -not (Test-Path -LiteralPath $venvRoot)
    $pythonCommand = ''
    $pythonArguments = @()
    if (-not $createEnvironment) {
        if (-not (Test-Path -LiteralPath $venvPython)) {
            throw 'The existing .tools/python directory is not a usable Windows venv. It was left unchanged; choose a fresh checkout or move it aside yourself.'
        }
        $pythonInfo = Get-PythonInfo $venvPython
        if (-not $pythonInfo) {
            throw 'The existing project venv needs its original base Python and 64-bit CPython 3.11/3.12. It was left unchanged. On another computer, create a fresh project venv instead of copying .tools/python.'
        }
        Write-Output "Reusing project Python $($pythonInfo.version): $venvPython"
    } else {
        if ($Python) {
            $resolved = Get-Command $Python -CommandType Application -ErrorAction Stop | Select-Object -First 1
            $pythonCommand = $resolved.Source
            $pythonInfo = Get-PythonInfo $pythonCommand
        } else {
            $launcher = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue
            if ($launcher) {
                foreach ($version in @('-3.12','-3.11')) {
                    $pythonInfo = Get-PythonInfo $launcher.Source @($version)
                    if ($pythonInfo) {
                        $pythonCommand = $launcher.Source
                        $pythonArguments = @($version)
                        break
                    }
                }
            }
            if (-not $pythonCommand) {
                foreach ($candidate in @('python.exe','python3.exe')) {
                    $resolved = Get-Command $candidate -CommandType Application -ErrorAction SilentlyContinue
                    if (-not $resolved -or $resolved.Source -match '\\WindowsApps\\') { continue }
                    $pythonInfo = Get-PythonInfo $resolved.Source
                    if ($pythonInfo) { $pythonCommand = $resolved.Source; break }
                }
            }
        }
        if (-not $pythonCommand -or -not $pythonInfo) {
            throw 'Install 64-bit CPython 3.12 (3.11 is also supported), or pass -Python C:\Python312\python.exe. Python 3.13+ removed audioop and is not supported by this project.'
        }
        Write-Output "Base Python $($pythonInfo.version): $pythonCommand $($pythonArguments -join ' ')"
    }

    $changes = @()
    if (-not $createEnvironment) {
        $state = Get-PackageState
        $changes = @($state.changes)
        foreach ($change in $changes) {
            $found = if ($change.installed) { $change.installed } else { 'not installed' }
            Write-Output "Required: $($change.name)==$($change.wanted); current: $found"
        }
    }
    if ($DryRun) {
        if ($createEnvironment) {
            Write-Output "Dry run: would create $venvRoot and install requirements.txt from PyPI using Direct."
        } elseif ($changes.Count -gt 0) {
            Write-Output 'Dry run: would install the fixed requirements into the existing project venv using Direct.'
        } else {
            Write-Output 'Dry run: all fixed dependencies are already installed; no package installation is needed.'
        }
    }
    if (-not $DryRun) {
        # Tests and diagnostics also use .cache/tmp on an already installed PC.
        New-Item -ItemType Directory -Path (Join-Path $projectRoot '.cache\tmp'),(Join-Path $projectRoot '.cache\pip') -Force | Out-Null
    }
    if (-not $DryRun -and ($createEnvironment -or $changes.Count -gt 0)) {
        Assert-ConsoleStoppedForInstall
        $cacheRoot = Join-Path $projectRoot '.cache'
        $pipCache = Join-Path $cacheRoot 'pip'
        $temporaryRoot = Join-Path $cacheRoot 'tmp'
        New-Item -ItemType Directory -Path (Join-Path $projectRoot '.tools'),$pipCache,$temporaryRoot -Force | Out-Null
        $env:PIP_CACHE_DIR = $pipCache
        $env:TEMP = $temporaryRoot
        $env:TMP = $temporaryRoot
        if ($createEnvironment) {
            & $pythonCommand @pythonArguments -B -m venv $venvRoot
            if ($LASTEXITCODE -ne 0) { throw 'Project venv creation failed; no system packages were changed.' }
        }
        & $venvPython -B -m pip install --require-virtualenv --no-input --only-binary=:all: `
            --disable-pip-version-check --cache-dir $pipCache --index-url https://pypi.org/simple -r $requirements
        if ($LASTEXITCODE -ne 0) { throw 'Project dependency installation failed. Re-run this script to retry.' }
        $state = Get-PackageState
        if (@($state.changes).Count -gt 0) { throw 'Installed package versions do not match requirements.txt.' }
    } elseif (-not $DryRun) {
        Write-Output 'All fixed dependencies are installed; no changes are needed.'
    }

    if (Test-Path -LiteralPath $venvPython) {
        & $venvPython -B -m pip --disable-pip-version-check check
        if ($LASTEXITCODE -ne 0) { throw 'Python dependency consistency check failed.' }
        if ($changes.Count -eq 0 -or -not $DryRun) {
            & $venvPython -B -c $importCode
            if ($LASTEXITCODE -ne 0) { throw 'Console import check failed.' }
        }
    }
    Write-Output 'Next: copy your private config, then run scripts/start-console.ps1 -Board http://BOARD_IP -Background'
} finally {
    Pop-Location
    foreach ($name in $environmentNames) {
        [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], 'Process')
    }
}
