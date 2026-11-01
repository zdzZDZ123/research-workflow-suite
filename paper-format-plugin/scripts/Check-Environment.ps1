param([string]$BackendHome = '')
$ErrorActionPreference='Stop'
$binding = if ($BackendHome) { Join-Path $BackendHome 'deployment.json' } else { Join-Path (Split-Path -Parent $PSScriptRoot) '.deployment.local.json' }
$deployment=Get-Content -LiteralPath $binding -Raw | ConvertFrom-Json
$env:PAPER_FORMATTER_HOME=$deployment.home
$env:PYTHONUTF8='1'
Push-Location -LiteralPath $deployment.project
try { '{"command":"doctor"}' | & $deployment.python -m paper_formatter.cli; if($LASTEXITCODE -ne 0){throw 'Environment check failed'} }
finally { Pop-Location }
