@echo off
REM install.bat — PhoneTransfer dependency installer for Windows
REM ─────────────────────────────────────────────────────────────────────────────
REM Handles the known MSVC build-tool issue: pylzss and lzfse (transitive deps
REM of pyimg4 -> pymobiledevice3) need C compilation and crash on machines
REM without Visual Studio Build Tools.  PhoneTransfer never uses IPSW handling,
REM so those packages are safe to skip.
REM
REM Usage:
REM   install           install deps from requirements.txt
REM   install --lock    install from pinned requirements-lock.txt

setlocal enabledelayedexpansion

set REQ_FILE=requirements.txt
if "%1"=="--lock" set REQ_FILE=requirements-lock.txt
if "%1"=="-l" set REQ_FILE=requirements-lock.txt

echo [PhoneTransfer] Installing dependencies from %REQ_FILE%
echo.

REM Step 1: Install pyimg4 with --no-deps to break the pylzss/lzfse chain
echo [1/2] Installing pyimg4 (without transitive deps - IPSW handling not needed)...
pip install -q --no-deps pyimg4 2>nul
if errorlevel 1 (
    echo   [WARN] pyimg4 pre-install had issues, continuing anyway...
) else (
    echo   OK
)

REM Step 2: Install the full requirements
echo [2/2] Installing %REQ_FILE%...
pip install -q -r %REQ_FILE% 2>&1
set EXIT_CODE=!ERRORLEVEL!

REM Check if the important packages landed (ignore pylzss/lzfse failures)
echo.
echo [Verify] Checking core packages...
python -c "import pymobiledevice3; import customtkinter; import iOSbackup; import sqlcipher3; print('All core packages imported OK')" 2>nul
if errorlevel 1 (
    echo [FAIL] Some core packages failed to install.
    echo        Try: pip install -r %REQ_FILE%
    echo.
    pause
    exit /b 1
) else (
    echo   All core packages imported OK
    echo   (Build failures for pylzss/lzfse are expected on Windows
    echo    without MSVC.  Safe to ignore - PhoneTransfer never uses them.)
)

echo.
echo [Done] PhoneTransfer is ready.  Run: python main.py
pause
