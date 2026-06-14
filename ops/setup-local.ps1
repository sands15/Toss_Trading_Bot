$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

$PythonCandidates = @(
    @("py", "-3.12"),
    @("python", ""),
    @("python3", "")
)
$Python = $null
$PythonArgs = @()
foreach ($Candidate in $PythonCandidates) {
    $Command = $Candidate[0]
    $Args = @($Candidate[1]) | Where-Object { $_ -ne "" }
    & $Command @Args --version *> $null
    if ($LASTEXITCODE -eq 0) {
        $Python = $Command
        $PythonArgs = $Args
        break
    }
}

if ($null -eq $Python) {
    throw "Python 3.11+ is required but was not found on PATH."
}

if (!(Test-Path ".venv")) {
    & $Python @PythonArgs -m venv .venv
}

$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
& $VenvPython -m pip install -U pip
& $VenvPython -m pip install -e ".[dev]"

New-Item -ItemType Directory -Force -Path "state", "logs" | Out-Null

if (!(Test-Path "config\local.yaml")) {
    Copy-Item "config\local.example.yaml" "config\local.yaml"
    Write-Host "Created config\local.yaml from config\local.example.yaml"
} else {
    Write-Host "config\local.yaml already exists; left unchanged"
}

Write-Host ""
Write-Host "Next steps:"
Write-Host "1. Fill toss.account_seq in config\local.yaml"
Write-Host "2. Set TOSS_CLIENT_ID and TOSS_CLIENT_SECRET as environment variables"
Write-Host "3. Run: .\.venv\Scripts\python.exe -m turtle_bot --config config\local.yaml --state-db state\turtle.sqlite3 --log-dir logs --ops-check"
Write-Host "4. Run: .\.venv\Scripts\python.exe -m turtle_bot --config config\local.yaml --state-db state\turtle.sqlite3 --log-dir logs --shadow-service --once"
