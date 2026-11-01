param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$BackendHome = '',
    [switch]$SkipPdf,
    [switch]$UseExistingEnvironment
)
$ErrorActionPreference = 'Stop'
$project = (Resolve-Path -LiteralPath $ProjectRoot).Path
if (-not $BackendHome) { $BackendHome = Join-Path $project 'private\data' }
$backendDirectory = [System.IO.Path]::GetFullPath($BackendHome)
New-Item -ItemType Directory -Path $backendDirectory -Force | Out-Null
$python = Join-Path $project '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    & py -3.12 -m venv (Join-Path $project '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 is required. Install from python.org, then retry.' }
}
if (-not $UseExistingEnvironment) {
    $target = if ($SkipPdf) { $project } else { "$project[pdf]" }
    & $python -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw 'pip installation failed' }
    & $python -m pip install --constraint (Join-Path $project 'requirements-windows.lock') $target
    if ($LASTEXITCODE -ne 0) { throw 'Backend dependency installation failed' }
}
$modelDirectory = Join-Path $project '.runtime\models'
if (-not $SkipPdf) {
    if ($UseExistingEnvironment) { & $python (Join-Path $PSScriptRoot 'install_models.py') --directory $modelDirectory --verify-only }
    else { & $python (Join-Path $PSScriptRoot 'install_models.py') --directory $modelDirectory }
    if ($LASTEXITCODE -ne 0) { throw 'Offline model installation failed' }
}
$deployment = @{schema_version=1;python=$python;project=$project;home=$backendDirectory}
$deployment | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $backendDirectory 'deployment.json') -Encoding utf8
$deployment | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $project '.deployment.local.json') -Encoding utf8
$configPath = Join-Path $backendDirectory 'config.json'
$configuration = if (Test-Path -LiteralPath $configPath) { Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json -AsHashtable } else { @{} }
$configuration['models']=$modelDirectory
$configuration | ConvertTo-Json | Set-Content -LiteralPath $configPath -Encoding utf8
$env:PAPER_FORMATTER_HOME=$backendDirectory
$env:PYTHONUTF8='1'
Push-Location -LiteralPath $project
try {
    '{"command":"doctor"}' | & $python -m paper_formatter.cli
    if ($LASTEXITCODE -ne 0) { throw 'Environment check failed' }
    & $python (Join-Path $PSScriptRoot 'build.py')
    if ($LASTEXITCODE -ne 0) { throw 'XPI packaging failed' }
} finally { Pop-Location }
Write-Output 'Install the generated -local.xpi in Zotero. Keep this project folder in place; the backend runs independently of Codex.'
