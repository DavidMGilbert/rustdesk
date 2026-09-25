# Copy this package into a RustDesk checkout as ylts/, and install the Windows workflow.
# The checkout must be tag 1.5.0 with submodules (git submodule update --init).
param(
    [Parameter(Mandatory = $true)]
    [string]$Fork
)

$ErrorActionPreference = "Stop"
$package = Split-Path $PSScriptRoot -Parent
$fork = (Resolve-Path $Fork).Path

if (-not (Test-Path (Join-Path $fork "libs\hbb_common"))) {
    throw "That folder is not a RustDesk checkout with submodules (libs\hbb_common is missing)."
}

$dest = Join-Path $fork "ylts"
if (Test-Path $dest) {
    Remove-Item -Recurse -Force $dest
}
New-Item -ItemType Directory -Force -Path $dest | Out-Null
# Robocopy uses 0-7 for success, including "extra files".
& robocopy $package $dest /E /XD .git /NFL /NDL /NJH /NJS /NP | Out-Host
if ($LASTEXITCODE -ge 8) {
    throw "robocopy failed with exit code $LASTEXITCODE"
}

$workflows = Join-Path $fork ".github\workflows"
New-Item -ItemType Directory -Force -Path $workflows | Out-Null
Copy-Item (Join-Path $PSScriptRoot "ylts-windows.yml") (Join-Path $workflows "ylts-windows.yml") -Force

Write-Host ""
Write-Host "Package:  $dest"
Write-Host "Workflow: $(Join-Path $workflows 'ylts-windows.yml')"
Write-Host "Commit both, then set secrets RUSTDESK_HOST and RUSTDESK_KEY and run the YLTS Windows workflow."
