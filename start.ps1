# FreeBo launcher (Windows). Clone the repo, run:  ./start.ps1
# Thin wrapper around scripts/bootstrap.py (creates venv, installs deps, builds UI, starts the server).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$boot = Join-Path $root "scripts/bootstrap.py"

function Find-Python {
    foreach ($c in @("python", "python3", "py")) {
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if ($cmd) { return $c }
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Error "Python 3.10+ is required and was not found on PATH. Install it from https://python.org and retry."
    exit 1
}
& $py $boot @args
exit $LASTEXITCODE
