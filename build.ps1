# build.ps1 — Build PhoneTransfer.exe and run post-build smoke test.
#
# Usage (from the project root):
#   .\build.ps1
#   .\build.ps1 -SkipSmoke      # build only, skip smoke test
#   .\build.ps1 -Clean          # delete dist/ and build/ before building
#
# Requirements:
#   - Python 3.10+ on PATH as `py` or `python`
#   - All requirements.txt deps installed in the active environment
#   - pyinstaller is installed automatically if missing

param(
    [switch]$SkipSmoke,
    [switch]$Clean
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = $PSScriptRoot
$Spec  = Join-Path $Root "PhoneTransfer.spec"
$Dist  = Join-Path $Root "dist\PhoneTransfer"
$Build = Join-Path $Root "build"

# ── Resolve Python interpreter ────────────────────────────────────────────────
$Py = $null
foreach ($candidate in @("py", "python")) {
    try {
        $ver = & $candidate --version 2>&1
        if ($ver -match "Python 3\.([0-9]+)" -and [int]$Matches[1] -ge 10) {
            $Py = $candidate
            break
        }
    } catch {}
}
if (-not $Py) {
    Write-Error "Python 3.10+ not found on PATH. Install from https://www.python.org/"
    exit 1
}
Write-Host "Using interpreter: $Py ($( & $Py --version 2>&1 ))" -ForegroundColor Cyan

# ── Clean ─────────────────────────────────────────────────────────────────────
if ($Clean) {
    Write-Host "`nCleaning dist/ and build/ ..." -ForegroundColor Yellow
    if (Test-Path $Dist)  { Remove-Item $Dist  -Recurse -Force }
    if (Test-Path $Build) { Remove-Item $Build -Recurse -Force }
}

# ── Ensure PyInstaller is installed ──────────────────────────────────────────
Write-Host "`nChecking for PyInstaller ..." -ForegroundColor Cyan
$piCheck = & $Py -m pip show pyinstaller 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "  PyInstaller not found — installing ..." -ForegroundColor Yellow
    & $Py -m pip install "pyinstaller>=6.0.0"
    if ($LASTEXITCODE -ne 0) { Write-Error "pip install pyinstaller failed."; exit 1 }
}

# ── Build ─────────────────────────────────────────────────────────────────────
Write-Host "`nBuilding ..." -ForegroundColor Cyan
Push-Location $Root
try {
    & $Py -m PyInstaller PhoneTransfer.spec --noconfirm
    if ($LASTEXITCODE -ne 0) { Write-Error "PyInstaller build failed."; exit 1 }
} finally {
    Pop-Location
}

$Exe = Join-Path $Dist "PhoneTransfer.exe"
if (-not (Test-Path $Exe)) {
    Write-Error "Build completed but $Exe not found."
    exit 1
}

$SizeMB = [math]::Round((Get-Item $Exe).Length / 1MB, 1)
Write-Host "`nBuild succeeded: $Exe  ($SizeMB MB)" -ForegroundColor Green

# ── Smoke test ────────────────────────────────────────────────────────────────
if (-not $SkipSmoke) {
    Write-Host "`nRunning post-build smoke test ..." -ForegroundColor Cyan
    & $Py (Join-Path $Root "scripts\post_build_smoke.py") --dist-dir $Dist
    if ($LASTEXITCODE -ne 0) {
        Write-Host "`nSmoke test FAILED. The exe was built but has import errors." -ForegroundColor Red
        Write-Host "Check the output above for the specific module(s) that failed to load." -ForegroundColor Red
        exit 1
    }
    Write-Host "`nSmoke test PASSED." -ForegroundColor Green
}

Write-Host "`nDone. Distributable folder: $Dist" -ForegroundColor Green
