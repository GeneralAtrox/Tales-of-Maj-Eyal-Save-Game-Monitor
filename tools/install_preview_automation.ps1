$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $repoRoot

git config core.hooksPath .githooks

Write-Host "Configured core.hooksPath=.githooks"
Write-Host "Preview screenshots will now refresh after pushes from this checkout."
