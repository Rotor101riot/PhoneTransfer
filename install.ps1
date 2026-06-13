# install.ps1 — PhoneTransfer dependency installer for Windows
# ─────────────────────────────────────────────────────────────────────────────
# Handles the known MSVC build-tool issue: pylzss and lzfse (transitive deps
# of pyimg4 → pymobiledevice3) require C compilation and will fail on machines
# without Visual Studio Build Tools.  PhoneTransfer never uses IPSW handling,
# so those packages are safe to skip.
#
# Usage:
#   .\install.ps1                    # install all deps (recommended)
#   .\install.ps1 -Lock              # install from pinned requirements-lock.txt
#   .\install.ps1 -Verbose           # show full pip output

param(
    [switch]$Lock,
    [switch]$Verbose
)

$ErrorActionPreference = "Stop"
$Quiet = if (-not $Verbose) { "-q" } else { "" }

$reqFile = if ($Lock) { "requirements-lock.txt" } else { "requirements.txt" }

Write-Host "» PhoneTransfer dependency installer" -ForegroundColor Cyan
Write-Host "  File: $reqFile" -ForegroundColor Gray

# ── Step 1: Install pyimg4 with --no-deps to avoid pulling pylzss/lzfse ──
Write-Host "» Installing pyimg4 (no transitive deps — IPSW not needed)..." -ForegroundColor Yellow
$pyimg4Result = pip install $Quiet --no-deps pyimg4 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "  ✓ pyimg4 installed" -ForegroundColor Green
} else {
    Write-Host "  ⚠ pyimg4 install had issues, continuing anyway..." -ForegroundColor Yellow
    $pyimg4Result | Out-Host
}

# ── Step 2: Install the full requirements ──
Write-Host "» Installing $reqFile ..." -ForegroundColor Yellow
pip install $Quiet -r $reqFile 2>&1
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "`n✓ All dependencies installed successfully!" -ForegroundColor Green
} else {
    # Non-zero exit from pip is usually pylzss/lzfse build failures
    # Check if the important packages actually landed
    $testPkgs = @("pymobiledevice3", "customtkinter", "iOSbackup", "sqlcipher3", "pillow", "pillow-heif")
    $allOk = $true
    foreach ($pkg in $testPkgs) {
        $found = pip show $pkg 2>$null
        if (-not $found) {
            Write-Host "  ✗ $pkg MISSING" -ForegroundColor Red
            $allOk = $false
        }
    }
    if ($allOk) {
        Write-Host "`n✓ All core packages installed" -ForegroundColor Green
        Write-Host "  (Build failures for pylzss/lzfse are expected on Windows without MSVC — safe to ignore)" -ForegroundColor DarkYellow
        $exitCode = 0
    } else {
        Write-Host "`n✗ Some required packages failed to install. Review errors above." -ForegroundColor Red
    }
}

# ── Step 3: Quick verification ──
Write-Host "`n» Verifying imports..." -ForegroundColor Yellow
$testCode = @"
import sys
errors = []
try:
    import pymobiledevice3
except Exception as e:
    errors.append(f"pymobiledevice3: {e}")
try:
    import customtkinter
except Exception as e:
    errors.append(f"customtkinter: {e}")
try:
    import iOSbackup
except Exception as e:
    errors.append(f"iOSbackup: {e}")
try:
    import sqlcipher3
except Exception as e:
    errors.append(f"sqlcipher3: {e}")
try:
    import wa_crypt_tools
except Exception as e:
    errors.append(f"wa_crypt_tools: {e}")
if errors:
    print("IMPORT ISSUES:")
    for e in errors:
        print(f"  ✗ {e}")
    sys.exit(1)
else:
    print("  ✓ All core imports OK")
    sys.exit(0)
"@
$pyResult = python -c $testCode 2>&1
Write-Host $pyResult
if ($LASTEXITCODE -ne 0) {
    Write-Host "`n✗ Import verification failed — check the errors above" -ForegroundColor Red
    exit 1
}

Write-Host "`n» PhoneTransfer is ready to run!" -ForegroundColor Green
Write-Host "  Run: python main.py (or whatever your entry point is)" -ForegroundColor Gray
