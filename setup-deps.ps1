# setup-deps.ps1 — PhoneTransfer dependency installer
# ─────────────────────────────────────────────────────────────────────────────
# Three-step install that skips pylzss and lzfse (transitive deps of pyimg4
# → pymobiledevice3), which require C compilation from source and fail on
# Windows without MSVC Build Tools — especially Python 3.13+ where pre-built
# wheels do not exist.
#
# PhoneTransfer never uses IPSW firmware handling, so these are safe to skip.
#
# Usage:
#   .\setup-deps.ps1            # install from requirements.txt
#   .\setup-deps.ps1 -Lock      # install from requirements-lock.txt

param(
    [switch]$Lock
)

$ErrorActionPreference = "Continue"
$reqFile  = if ($Lock) { "requirements-lock.txt" } else { "requirements.txt" }
$safeFile = "requirements-safe.txt"

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " PhoneTransfer Dependency Installer"                       -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Source : " -NoNewline; Write-Host $reqFile  -ForegroundColor Gray
Write-Host "  Safe   : " -NoNewline; Write-Host $safeFile -ForegroundColor Gray
Write-Host ""

# ═══════════════════════════════════════════════════════════════════════════
# Step 1: pyimg4 without its MSVC-bound transitive deps
# ═══════════════════════════════════════════════════════════════════════════
Write-Host "── Step 1/3: pyimg4 (without pylzss / lzfse) ───" -ForegroundColor Yellow
Write-Host ""
Write-Host "  pyimg4 is a dependency of pymobiledevice3.  Its transitive deps"          -ForegroundColor Gray
Write-Host "  pylzss and lzfse require C compilation from source, which fails"              -ForegroundColor Gray
Write-Host "  on this system without MSVC Build Tools.  PhoneTransfer never"                -ForegroundColor Gray
Write-Host "  uses the IPSW features that need those packages, so we install"               -ForegroundColor Gray
Write-Host "  pyimg4 with --no-deps to skip them entirely."                                 -ForegroundColor Gray
Write-Host ""

pip install --no-deps pyimg4
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "[FAIL] Could not install pyimg4.  Is Python on your PATH?" -ForegroundColor Red
    Write-Host "       Try:  pip install --no-deps pyimg4"                  -ForegroundColor Red
    pause
    exit 1
}
Write-Host ""

# ═══════════════════════════════════════════════════════════════════════════
# Step 2: Direct dependencies (no transitive deps)
# ═══════════════════════════════════════════════════════════════════════════
Write-Host "── Step 2/3: Direct dependencies ($reqFile) ───" -ForegroundColor Yellow
Write-Host ""
Write-Host "  Installing the 11 direct dependencies WITHOUT their"           -ForegroundColor Gray
Write-Host "  transitive deps.  This avoids re-pulling pylzss/lzfse"                       -ForegroundColor Gray
Write-Host "  through the resolver."                                                         -ForegroundColor Gray
Write-Host ""

pip install --no-deps -r $reqFile
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "[FAIL] pip exited with error." -ForegroundColor Red
    Write-Host "       See output above."       -ForegroundColor Red
    pause
    exit 1
}
Write-Host ""

# ═══════════════════════════════════════════════════════════════════════════
# Step 3: Transitive dependencies (safe list only)
# ═══════════════════════════════════════════════════════════════════════════
Write-Host "── Step 3/3: Transitive dependencies ($safeFile) ───" -ForegroundColor Yellow
Write-Host ""
Write-Host "  Installing the transitive dependency tree, skipping"           -ForegroundColor Gray
Write-Host "  pylzss, lzfse, and pillow-heif (optional HEIC converter)."                   -ForegroundColor Gray
Write-Host "  This file is generated from requirements-lock.txt and"                        -ForegroundColor Gray
Write-Host "  contains exact pinned versions."                                              -ForegroundColor Gray
Write-Host ""

pip install -r $safeFile
$pipExit = $LASTEXITCODE

Write-Host ""
if ($pipExit -eq 0) {
    Write-Host "  All transitive dependencies installed successfully." -ForegroundColor Green
} else {
    Write-Host "  pip exited with code $pipExit." -ForegroundColor Yellow
    Write-Host "  If you saw MSVC errors, try updating pip first:" -ForegroundColor Yellow
    Write-Host "    python -m pip install --upgrade pip"           -ForegroundColor Yellow
    Write-Host "    .\setup-deps.ps1"                              -ForegroundColor Yellow
}

# ═══════════════════════════════════════════════════════════════════════════
# Verification
# ═══════════════════════════════════════════════════════════════════════════
Write-Host ""
Write-Host "── Verification ──" -ForegroundColor Yellow
Write-Host ""

$testCode = @'
import pymobiledevice3, customtkinter, iOSbackup, sqlcipher3
import Pillow, pycryptodome, wa_crypt_tools, vobject
import iphone_backup_decrypt
print("SUCCESS: all core packages import OK")
'@

$result = python -c $testCode 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host $result -ForegroundColor Green
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host " PhoneTransfer dependencies are ready."                      -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  Run:  python main.py" -ForegroundColor Gray
    Write-Host ""
    Write-Host "  NOTE: pylzss and lzfse were skipped — only needed"         -ForegroundColor DarkYellow
    Write-Host "  for IPSW firmware handling, which PhoneTransfer never uses."-ForegroundColor DarkYellow
    Write-Host ""
    Write-Host "  For HEIC photo conversion:  pip install pillow-heif"      -ForegroundColor DarkYellow
    Write-Host "  (optional — requires C build tools or pre-built wheels)"  -ForegroundColor DarkYellow
} else {
    Write-Host ""
    Write-Host "[FAIL] Import check failed.  Errors above." -ForegroundColor Red
    Write-Host $result -ForegroundColor Red
    pause
    exit 1
}

Write-Host ""
pause
