param(
    [switch]$SkipTests,
    [switch]$BundleTools,
    [switch]$RequireInstaller
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not $SkipTests) {
    Write-Host "==> Running pytest suite..." -ForegroundColor Cyan
    $env:PYTHONPATH = $Root
    python -m pytest -v
    if ($LASTEXITCODE -ne 0) { throw "Tests failed." }
}

Write-Host "==> Building standalone Windows bundle via PyInstaller..." -ForegroundColor Cyan
python -m PyInstaller --noconfirm --clean "$Root\srt-embedder.spec"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

$DistDir = Join-Path $Root "dist\SRT-Embedder"
$CliExe = Join-Path $DistDir "srt-embedder-cli.exe"
$GuiExe = Join-Path $DistDir "SRT-Embedder.exe"
if (-not (Test-Path $CliExe) -or -not (Test-Path $GuiExe)) {
    throw "Expected executables missing in $DistDir"
}

Write-Host "==> Smoke-testing packaged CLI..." -ForegroundColor Cyan
& $CliExe --version
if ($LASTEXITCODE -ne 0) { throw "CLI --version failed." }
& $CliExe doctor --json | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host "Note: doctor returned non-zero (optional tools missing on host)." -ForegroundColor Yellow }
& $CliExe profile list --json | Out-Null
if ($LASTEXITCODE -ne 0) { throw "CLI profile list failed." }

if ($BundleTools) {
    $BinDir = Join-Path $DistDir "bin"
    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
    foreach ($tool in @("ffmpeg", "ffprobe", "mkvmerge", "mkvextract")) {
        $found = Get-Command $tool -ErrorAction SilentlyContinue
        if ($found) {
            Copy-Item $found.Source -Destination $BinDir -Force
            Write-Host "    Bundled $($found.Name)"
        }
    }
}

$ZipPath = Join-Path $Root "dist\SRT-Embedder-win64-portable.zip"
Remove-Item $ZipPath -ErrorAction SilentlyContinue
Compress-Archive -Path "$DistDir\*" -DestinationPath $ZipPath
Write-Host "==> Created portable archive: $ZipPath" -ForegroundColor Green

$Iscc = Get-Command iscc -ErrorAction SilentlyContinue
if (-not $Iscc -and (Test-Path "C:\Program Files (x86)\Inno Setup 6\ISCC.exe")) {
    $Iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
}
if ($Iscc) {
    Write-Host "==> Building Inno Setup installer..." -ForegroundColor Cyan
    & $Iscc "$Root\installer\SRTEmbedder.iss"
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup compilation failed." }
} elseif ($RequireInstaller) {
    throw "Inno Setup (iscc) is required for release builds but was not found."
} else {
    Write-Host "Note: Inno Setup (iscc) not installed; skipped .exe installer compilation." -ForegroundColor Yellow
}

$SumsPath = Join-Path $Root "dist\SHA256SUMS.txt"
$HashTargets = @($ZipPath)
$SetupExes = Get-ChildItem -Path (Join-Path $Root "installer\Output") -Filter "*.exe" -ErrorAction SilentlyContinue
if ($SetupExes) {
    $HashTargets += $SetupExes.FullName
} elseif ($RequireInstaller) {
    throw "Expected Inno Setup installer .exe in installer\Output\ was not produced."
}

$Lines = foreach ($f in $HashTargets) {
    $h = (Get-FileHash -Path $f -Algorithm SHA256).Hash.ToLowerInvariant()
    "$h  $([System.IO.Path]::GetFileName($f))"
}
$Lines | Set-Content -Path $SumsPath -Encoding ascii
Write-Host "==> Wrote checksums: $SumsPath" -ForegroundColor Green
