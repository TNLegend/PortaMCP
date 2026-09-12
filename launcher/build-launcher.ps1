$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
$RuntimeArchive = Join-Path $Root "runtime\python-bootstrap.zip"
$RuntimeHash = Join-Path $Root "runtime\python-bootstrap.sha256"
if (-not (Test-Path $RuntimeArchive) -or -not (Test-Path $RuntimeHash)) {
    throw "The Windows bootstrap runtime is missing. Build it first with launcher\build-bootstrap-runtime.ps1."
}
$ExpectedHash = (Get-Content $RuntimeHash -Raw).Trim().ToLowerInvariant()
if ($ExpectedHash -notmatch '^[0-9a-f]{64}$') { throw "The Windows bootstrap runtime checksum file is invalid." }
$ActualHash = (Get-FileHash $RuntimeArchive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($ActualHash -ne $ExpectedHash) { throw "The Windows bootstrap runtime checksum does not match the archive." }

$Compiler = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $Compiler)) { throw "The .NET Framework C# compiler was not found at $Compiler" }
Push-Location $Root
try {
    & $Compiler /nologo /target:winexe /platform:anycpu /win32icon:assets\portamcp.ico /reference:System.Windows.Forms.dll /reference:System.IO.Compression.dll /reference:System.IO.Compression.FileSystem.dll /out:PortaMCP.exe launcher\PortaMCPLauncher.cs
    if ($LASTEXITCODE -ne 0) { throw "Launcher compilation failed with exit code $LASTEXITCODE" }
    Write-Host "Built PortaMCP.exe" -ForegroundColor Green
}
finally { Pop-Location }
