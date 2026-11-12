$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
# Temporary drive alias avoids non-ASCII paths in the embedded compiler.
if (Test-Path 'P:\') {
    if (-not ('ProjectDriveMapping' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class ProjectDriveMapping {
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    public static extern uint QueryDosDevice(string name, StringBuilder target, int size);
}
'@
    }
    $driveTarget = [System.Text.StringBuilder]::new(32768)
    [void][ProjectDriveMapping]::QueryDosDevice('P:', $driveTarget, $driveTarget.Capacity)
    if ($driveTarget.ToString() -ne "\??\$projectRoot") {
        throw 'P: is already in use. Choose an unused drive letter in scripts/env.ps1.'
    }
} else {
    & subst.exe P: $projectRoot
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the temporary project drive alias.' }
}
$env:IDF_PATH = 'P:\.tools\esp-idf-v5.5.4'
$env:IDF_TOOLS_PATH = 'P:\.tools\idf-tools'
$env:IDF_PYTHON_ENV_PATH = 'P:\.tools\idf-tools\python_env\idf5.5_py3.12_env'
$env:IDF_COMPONENT_CACHE_PATH = 'P:\.cache\components'
# Builds use dependencies.lock; checking every available update adds network waits.
$env:IDF_COMPONENT_CHECK_NEW_VERSION = '0'
$env:PIP_CACHE_DIR = 'P:\.cache\pip'
$env:CCACHE_DIR = 'P:\.cache\ccache'
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
$env:PYTHONNOUSERSITE = '1'
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = 'P:\scripts\python-path'
$env:NANO_PRESERVE_DRIVE_ALIAS = '1'
$env:TEMP = 'P:\.cache\tmp'
$env:TMP = $env:TEMP
$env:IDF_GITHUB_ASSETS = 'dl.espressif.com/github_assets'
# Required P4 submodules are initialized explicitly. Avoid fetching libraries
# for every unrelated ESP32 target during each CMake configuration.
$env:IDF_SKIP_CHECK_SUBMODULES = '1'
New-Item -ItemType Directory -Path $env:TEMP,$env:IDF_COMPONENT_CACHE_PATH -Force | Out-Null
# This project's build subprocesses connect directly. Changes are process-local;
# the Windows proxy configuration and the user's Wi-Fi are not modified.
foreach ($proxyVariable in @('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','FTP_PROXY')) {
    [Environment]::SetEnvironmentVariable($proxyVariable, $null, 'Process')
}
$env:NO_PROXY = '*'
