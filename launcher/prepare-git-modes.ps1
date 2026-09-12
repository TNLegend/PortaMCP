param(
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

if (-not (Test-Path (Join-Path $Root ".git"))) {
    throw "This command must be run after git init or from a Git clone. It does not initialize Git."
}

$ExecutablePaths = @(
    "PortaMCP",
    "portamcp.sh",
    "launcher/build-launcher-linux.sh"
)

foreach ($Path in $ExecutablePaths) {
    if (-not (Test-Path (Join-Path $Root $Path) -PathType Leaf)) {
        throw "Required Linux launcher file is missing: $Path"
    }
}

if (-not $CheckOnly) {
    & git update-index --add --chmod=+x -- @ExecutablePaths
    if ($LASTEXITCODE -ne 0) {
        throw "git update-index failed while setting Linux executable modes."
    }
}

$StageLines = @(& git ls-files --stage -- @ExecutablePaths)
if ($LASTEXITCODE -ne 0) {
    throw "git ls-files failed while verifying Linux executable modes."
}

$Modes = @{}
foreach ($Line in $StageLines) {
    if ($Line -match '^(\d{6})\s+[0-9a-f]+\s+\d+\t(.+)$') {
        $Modes[$Matches[2]] = $Matches[1]
    }
}

$Bad = @()
foreach ($Path in $ExecutablePaths) {
    if (-not $Modes.ContainsKey($Path) -or $Modes[$Path] -ne "100755") {
        $Current = if ($Modes.ContainsKey($Path)) { $Modes[$Path] } else { "missing" }
        $Bad += "$Path ($Current)"
    }
}

if ($Bad.Count -gt 0) {
    throw "Linux executable Git modes are not ready: $($Bad -join ', '). Expected 100755."
}

Write-Host "Linux Git executable modes are ready (100755):"
$ExecutablePaths | ForEach-Object { Write-Host "  $_" }
