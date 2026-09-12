param(
    [string]$PythonHome = "",
    [string]$OutputDirectory = "",
    [string]$ExpectedVersion = "3.13.15"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $Root "runtime"
}

if (-not $PythonHome) {
    $Probe = Join-Path $Root ".venv\Scripts\python.exe"
    if (-not (Test-Path $Probe)) {
        throw "A working .venv or -PythonHome is required to build the Windows bootstrap runtime."
    }
    $PythonHome = (& $Probe -c "import sys; print(sys.base_prefix)").Trim()
}

$Python = Join-Path $PythonHome "python.exe"
if (-not (Test-Path $Python)) {
    throw "python.exe was not found in $PythonHome"
}

$Version = (& $Python -c "import sys; print('.'.join(map(str, sys.version_info[:3])))").Trim()
if ($LASTEXITCODE -ne 0) { throw "Could not query bootstrap Python version." }
if ($Version -ne $ExpectedVersion) {
    throw "The Windows bootstrap runtime is pinned to CPython $ExpectedVersion; found $Version."
}

& $Python -c "import tkinter, venv, ensurepip; print('bootstrap source runtime OK')"
if ($LASTEXITCODE -ne 0) {
    throw "The source Python runtime does not include Tk, venv and ensurepip."
}

$StageRoot = Join-Path $Root ".portamcp\bootstrap-runtime-stage"
$Archive = Join-Path $OutputDirectory "python-bootstrap.zip"
$HashFile = Join-Path $OutputDirectory "python-bootstrap.sha256"

if (Test-Path $StageRoot) { Remove-Item $StageRoot -Recurse -Force }
New-Item -ItemType Directory -Path $StageRoot -Force | Out-Null
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null

$PythonAbi = (& $Python -c "import sys; print(f'{sys.version_info.major}{sys.version_info.minor}')").Trim()
if ($LASTEXITCODE -ne 0 -or $PythonAbi -notmatch '^3[0-9]+$') {
    throw "Could not determine the CPython ABI filename."
}

$TopFiles = @(
    "python.exe",
    "pythonw.exe",
    "python3.dll",
    "python$PythonAbi.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
    "LICENSE.txt"
)
foreach ($Name in $TopFiles) {
    $Source = Join-Path $PythonHome $Name
    if (Test-Path $Source) {
        Copy-Item $Source (Join-Path $StageRoot $Name) -Force
    }
}

foreach ($Directory in @("DLLs", "Lib", "tcl")) {
    $Source = Join-Path $PythonHome $Directory
    $Destination = Join-Path $StageRoot $Directory
    if (-not (Test-Path $Source)) {
        throw "Required bootstrap runtime directory is missing: $Source"
    }
    $RobocopyArgs = @($Source, $Destination, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/XF", "*.pyc", "*.pyo")
    if ($Directory -eq "Lib") {
        $RobocopyArgs += @("/XD", (Join-Path $Source "site-packages"))
    }
    & robocopy @RobocopyArgs | Out-Null
    if ($LASTEXITCODE -gt 7) {
        throw "robocopy failed while staging $Directory (exit code $LASTEXITCODE)."
    }
}

$SitePackages = Join-Path $StageRoot "Lib\site-packages"
New-Item -ItemType Directory -Path $SitePackages -Force | Out-Null
Get-ChildItem $StageRoot -Recurse -Directory -Force | Where-Object { $_.Name -eq "__pycache__" } | Sort-Object FullName -Descending | Remove-Item -Recurse -Force
Get-ChildItem $StageRoot -Recurse -File -Force | Where-Object { $_.Extension -in @(".pyc", ".pyo") } | Remove-Item -Force

$RuntimePython = Join-Path $StageRoot "python.exe"
& $RuntimePython -c "import sys, tkinter, venv, ensurepip; print(sys.version); print('portable bootstrap runtime OK')"
if ($LASTEXITCODE -ne 0) {
    throw "The staged bootstrap runtime failed validation."
}

if (Test-Path $Archive) { Remove-Item $Archive -Force }
Add-Type -AssemblyName System.IO.Compression
$FileStream = [System.IO.File]::Open($Archive, [System.IO.FileMode]::CreateNew)
try {
    $Zip = New-Object System.IO.Compression.ZipArchive($FileStream, [System.IO.Compression.ZipArchiveMode]::Create, $false)
    try {
        $FixedTime = [DateTimeOffset]::Parse("2000-01-01T00:00:00+00:00")
        $Files = Get-ChildItem $StageRoot -Recurse -File -Force | Sort-Object { $_.FullName.Substring($StageRoot.Length) }
        foreach ($File in $Files) {
            $Relative = $File.FullName.Substring($StageRoot.Length).TrimStart('\').Replace('\','/')
            $Entry = $Zip.CreateEntry($Relative, [System.IO.Compression.CompressionLevel]::Optimal)
            $Entry.LastWriteTime = $FixedTime
            $Input = [System.IO.File]::OpenRead($File.FullName)
            try {
                $Output = $Entry.Open()
                try { $Input.CopyTo($Output) } finally { $Output.Dispose() }
            } finally { $Input.Dispose() }
        }
    } finally { $Zip.Dispose() }
} finally { $FileStream.Dispose() }

$Hash = (Get-FileHash $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -Path $HashFile -Value $Hash -Encoding ascii -NoNewline

Remove-Item $StageRoot -Recurse -Force
Write-Host "Built $Archive"
Write-Host "CPython $Version"
Write-Host "SHA256 $Hash"
