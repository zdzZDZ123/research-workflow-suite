param([switch]$SkipConfigure,[switch]$InstallApps,[switch]$WithFormatter,[switch]$WithPdf)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
function Run-Checked([string]$exe, [string[]]$arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed: $exe (exit $LASTEXITCODE)" }
}
if ($InstallApps) {
    foreach ($id in @('Python.Python.3.12','DigitalScholar.Zotero','Microsoft.PowerShell')) {
        & winget list --id $id --exact --accept-source-agreements | Out-Host
        if ($LASTEXITCODE -ne 0) {
            Run-Checked 'winget' @('install','--id',$id,'--exact','--accept-source-agreements','--accept-package-agreements')
        }
    }
    Write-Host 'If Python is not found below, reopen Install.cmd so PATH refreshes.'
}
$python = Join-Path $root '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $python)) {
    if (!(Get-Command py -ErrorAction SilentlyContinue)) { throw 'Install Python 3.12 from python.org or run Install.ps1 -InstallApps, then retry.' }
    Run-Checked 'py' @('-3.12','-m','venv',(Join-Path $root '.venv'))
}
Run-Checked $python @('-m','pip','install','-r',(Join-Path $root 'notion-workflows\requirements.txt'))
Run-Checked $python @((Join-Path $root 'notion-workflows\build_extension.py'))
if ($WithFormatter -or $WithPdf) {
    if (!(Get-Command pwsh -ErrorAction SilentlyContinue)) { throw 'PowerShell 7 is required for the formatter. Install Microsoft.PowerShell with winget.' }
    $arguments = @('-NoProfile','-File',(Join-Path $root 'paper-format-plugin\scripts\Install-Backend.ps1'))
    if (!$WithPdf) { $arguments += '-SkipPdf' }
    Run-Checked 'pwsh' $arguments
}
if (!$SkipConfigure) { Run-Checked $python @((Join-Path $root 'notion-workflows\configure_workspace.py')) }
Write-Host 'Backend ready. Install the generated XPI from notion-workflows/dist in Zotero > Tools > Plugins.'
Write-Host 'Citation workbench: double-click Launch-Citations.cmd. Setup again: Configure.cmd.'
