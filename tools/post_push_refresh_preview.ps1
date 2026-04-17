$ErrorActionPreference = "Stop"

if ($env:SKIP_PREVIEW_AUTOMATION -eq "1") {
    exit 0
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $repoRoot

$branch = (git branch --show-current).Trim()
if ($branch -ne "main") {
    exit 0
}

$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$python = if (Test-Path $venvPython) { $venvPython } else { "py -3" }

try {
    if (Test-Path $venvPython) {
        & $python "tools/update_preview_screenshots.py"
    }
    else {
        py -3 "tools/update_preview_screenshots.py"
    }
}
catch {
    Write-Warning "Preview refresh failed: $($_.Exception.Message)"
    exit 0
}

$changed = git status --porcelain -- README.md docs/screenshots
if (-not $changed) {
    exit 0
}

git add README.md docs/screenshots

$staged = git diff --cached --name-only
if (-not $staged) {
    exit 0
}

git commit -m "Refresh README preview screenshots"

$env:SKIP_PREVIEW_AUTOMATION = "1"
git push origin main
