[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$venvPath = Join-Path $projectRoot ".venv"
$venvPython = Join-Path $venvPath "Scripts\python.exe"
$requirementsPath = Join-Path $projectRoot "requirements.txt"
$generatorPath = Join-Path $projectRoot "sitemap_generator.py"

Set-Location -LiteralPath $projectRoot

if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "[준비] Python 가상환경을 생성합니다."

    $pyLauncher = Get-Command "py" -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        & $pyLauncher.Source -3 -m venv $venvPath
    }
    else {
        $pythonLauncher = Get-Command "python" -ErrorAction SilentlyContinue
        if (-not $pythonLauncher) {
            throw "Python을 찾을 수 없습니다. Python 3.10 이상을 설치한 뒤 다시 실행해 주세요."
        }
        & $pythonLauncher.Source -m venv $venvPath
    }

    if ($LASTEXITCODE -ne 0) {
        throw "Python 가상환경 생성에 실패했습니다."
    }
}

& $venvPython -c "import requests, bs4" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[준비] 필요한 Python 패키지를 설치합니다."
    & $venvPython -m pip install -r $requirementsPath

    if ($LASTEXITCODE -ne 0) {
        throw "Python 패키지 설치에 실패했습니다."
    }
}

Write-Host "[실행] 사이트 주소를 입력하면 사이트맵 생성을 시작합니다."
& $venvPython $generatorPath
exit $LASTEXITCODE
