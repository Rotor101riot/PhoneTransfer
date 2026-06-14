# setup-deps.ps1 — PhoneTransfer dependency installer (orchestrator)
#
# Calls each install step in sequence.  Output streams through from
# each sub-script so you see every package as it installs.
#
# Usage:
#   .\setup-deps.ps1            # requirements.txt
#   .\setup-deps.ps1 -Lock      # requirements-lock.txt

param(
    [switch]$Lock
)

$ErrorActionPreference = "Continue"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " PhoneTransfer — Dependency Installer"                       -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  This runs three scripts in order:"                                      -ForegroundColor Gray
Write-Host "    scripts\install-pyimg4.ps1       (pyimg4, no C deps)"                  -ForegroundColor Gray
Write-Host "    scripts\install-direct.ps1       (requirements.txt)"                   -ForegroundColor Gray
Write-Host "    scripts\install-transitive.ps1   (requirements-safe.txt)"              -ForegroundColor Gray
Write-Host ""

$step1 = Join-Path $scriptDir "scripts\install-pyimg4.ps1"
$step2 = Join-Path $scriptDir "scripts\install-direct.ps1"
$step3 = Join-Path $scriptDir "scripts\install-transitive.ps1"

if (-not (Test-Path $step1)) {
    Write-Host "[FAIL] scripts\install-pyimg4.ps1 not found." -ForegroundColor Red
    Write-Host "       Make sure you're running this from the PhoneTransfer folder." -ForegroundColor Red
    pause
    exit 1
}

# ── Step 1 ─────────────────────────────────────────────────────────────────
& $step1
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "[FAIL] Step 1 (pyimg4) failed.  Cannot continue." -ForegroundColor Red
    pause
    exit 1
}

# ── Step 2 ─────────────────────────────────────────────────────────────────
if ($Lock) {
    & $step2 -Lock
} else {
    & $step2
}
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "[FAIL] Step 2 (direct deps) failed.  Cannot continue." -ForegroundColor Red
    pause
    exit 1
}

# ── Step 3 ─────────────────────────────────────────────────────────────────
& $step3
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "[WARN] Step 3 had issues but core packages may be OK." -ForegroundColor Yellow
    Write-Host "       See output above." -ForegroundColor Yellow
}

# ── Verify ─────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Verification"                                                -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Checking that core packages import correctly ..." -ForegroundColor Gray
Write-Host ""

$testCode = @'
import pymobiledevice3, customtkinter, iOSbackup, sqlcipher3
import Pillow, pycryptodome, vobject, iphone_backup_decrypt
print("All core packages import OK")
'@

$result = python -c $testCode 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "  $result" -ForegroundColor Green
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host " PhoneTransfer dependencies are ready."                      -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  Run:  python main.py" -ForegroundColor Gray
    Write-Host ""
    Write-Host "  For HEIC photo conversion (optional):" -ForegroundColor DarkYellow
    Write-Host "    pip install pillow-heif"              -ForegroundColor DarkYellow
} else {
    Write-Host ""
    Write-Host "[FAIL] Import check failed.  Errors above." -ForegroundColor Red
    Write-Host $result -ForegroundColor Red
    pause
    exit 1
}

Write-Host ""
pause
