[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$utf8Encoding = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8Encoding
[Console]::OutputEncoding = $utf8Encoding
$OutputEncoding = $utf8Encoding
$env:PYTHONUTF8 = "1"

$projectRoot = $PSScriptRoot
$venvPath = Join-Path $projectRoot ".venv"
$venvPython = Join-Path $venvPath "Scripts\python.exe"
$requirementsPath = Join-Path $projectRoot "requirements.txt"
$generatorPath = Join-Path $projectRoot "sitemap_generator.py"

Set-Location -LiteralPath $projectRoot

if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "[setup] Creating a Python virtual environment."

    $pyLauncher = Get-Command "py" -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        & $pyLauncher.Source -3 -m venv $venvPath
    }
    else {
        $pythonLauncher = Get-Command "python" -ErrorAction SilentlyContinue
        if (-not $pythonLauncher) {
            throw "Python was not found. Install Python 3.10 or later and try again."
        }
        & $pythonLauncher.Source -m venv $venvPath
    }

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create the Python virtual environment."
    }
}

& $venvPython -c "import requests, bs4" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[setup] Installing required Python packages."
    & $venvPython -m pip install -r $requirementsPath

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install the required Python packages."
    }
}

Write-Host "[run] Enter a website address to generate its sitemap."
& $venvPython $generatorPath
exit $LASTEXITCODE
