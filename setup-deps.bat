@echo off
REM setup-deps.bat — PhoneTransfer dependency installer
REM ─────────────────────────────────────────────────────────────────────────────
REM pylzss and lzfse (transitive deps of pyimg4 -> pymobiledevice3) require C
REM compilation and fail on Windows without MSVC Build Tools, especially on
REM Python 3.13+.  PhoneTransfer never uses IPSW handling, so they're safe to
REM skip.  This script pre-installs pyimg4 with --no-deps to break the chain.
REM
REM Usage:  double-click or run from a terminal:
REM   setup-deps.bat
REM   setup-deps.bat --lock   (install from pinned requirements-lock.txt)

setlocal enabledelayedexpansion

set REQ_FILE=requirements.txt
if "%1"=="--lock" set REQ_FILE=requirements-lock.txt
if "%1"=="-l" set REQ_FILE=requirements-lock.txt

echo [PhoneTransfer] Installing dependencies from %REQ_FILE%
echo.

REM Step 1: Pre-install pyimg4 without its transitive deps (pylzss, lzfse)
echo [1/2] Installing pyimg4 (without MSVC-bound deps)...
pip install -q --no-deps pyimg4
if errorlevel 1 (
    echo   [WARN] pyimg4 pre-install had issues, continuing anyway...
) else (
    echo   OK
)

REM Step 2: Install the full requirements file
echo [2/2] Installing %REQ_FILE%...
pip install -q -r %REQ_FILE%
set EXIT_CODE=!ERRORLEVEL!

REM Verify core packages import cleanly
echo.
echo [Verify] Checking core packages...
python -c "import pymobiledevice3; import customtkinter; import iOSbackup; import sqlcipher3; print('OK')" 2>nul
if errorlevel 1 (
    echo [FAIL] Some core packages failed to install.
    echo        Try running: pip install -r %REQ_FILE%
    echo.
    pause
    exit /b 1
) else (
    echo   All core packages imported OK
    echo.
    echo   NOTE: If you saw build warnings for pylzss/lzfse, those are
    echo   expected on this system — PhoneTransfer doesn't need them.
)

echo.
echo [Done] PhoneTransfer dependencies are ready.
echo   Run: python main.py
pause
