[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = (Get-Command python -ErrorAction Stop).Source
Set-Location -LiteralPath $repoRoot

$allowedNames = @(
    'Path',
    'SystemRoot',
    'WINDIR',
    'COMSPEC',
    'PATHEXT',
    'TEMP',
    'TMP',
    'LANG',
    'LC_ALL',
    'HOME',
    'USERPROFILE',
    'APPDATA',
    'LOCALAPPDATA',
    'PROGRAMDATA'
)
$preserved = @{}
foreach ($name in $allowedNames) {
    $item = Get-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
    if ($null -ne $item) {
        $preserved[$name] = $item.Value
    }
}

Get-ChildItem Env: | ForEach-Object {
    Remove-Item -LiteralPath $_.PSPath -ErrorAction SilentlyContinue
}
foreach ($entry in $preserved.GetEnumerator()) {
    Set-Item -LiteralPath "Env:$($entry.Key)" -Value $entry.Value
}
$env:NON_LIVE_GATE = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONHASHSEED = '0'
$env:PYTHONNOUSERSITE = '1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
$env:NON_LIVE_STATIC_GATE_NO_OS_EGRESS_ISOLATION = '1'

& $python -I ops/non_live_gate_runner.py
exit $LASTEXITCODE
