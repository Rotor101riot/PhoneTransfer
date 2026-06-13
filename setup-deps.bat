@echo off
REM setup-deps.bat — PhoneTransfer dependency installer
REM ─────────────────────────────────────────────────────────────────────────────
REM pylzss and lzfse (transitive deps of pyimg4 -> pymobiledevice3) require C
REM compilation and have no pre-built wheels for Python 3.13+ on Windows.
REM PhoneTransfer never uses IPSW handling, so they're safe to skip entirely.
REM
REM This script uses a constraints file to tell pip to never touch them,
REM then installs everything else cleanly.
REM
REM Usage:  double-click or run from a terminal:
REM   setup-deps.bat
REM   setup-deps.bat --lock   (install from pinned requirements-lock.txt)

setlocal enabledelayedexpansion

set REQ_FILE=requirements.txt
set CONSTRAINTS=constraints.txt
if "%1"=="--lock" set REQ_FILE=requirements-lock.txt
if "%1"=="-l" set REQ_FILE=requirements-lock.txt

echo [PhoneTransfer] Installing dependencies from %REQ_FILE%
echo.

REM Install with constraints to skip pylzss/lzfse (IPSW-only, not needed)
echo [1/1] Installing packages (skipping MSVC-bound transitive deps)...
pip install -q --constraint %CONSTRAINTS% -r %REQ_FILE%
set EXIT_CODE=!ERRORLEVEL!

if !EXIT_CODE! neq 0 (
    echo [FAIL] pip install exited with code !EXIT_CODE!.
    echo        See errors above.
    echo.
    pause
    exit /b !EXIT_CODE!
)

REM Verify core packages import cleanly
echo.
echo [Verify] Checking core packages...
python -c "import pymobiledevice3; import customtkinter; import iOSbackup; import sqlcipher3; print('OK')" 2>nul
if errorlevel 1 (
    echo [FAIL] Some core packages failed to import.
    echo        Try running: pip install -r %REQ_FILE%
    echo.
    pause
    exit /b 1
) else (
    echo   All core packages imported OK
    echo.
    echo   NOTE: pylzss and lzfse were intentionally skipped —
    echo   they are only needed for IPSW handling, which
    echo   PhoneTransfer never uses.
)

echo.
echo [Done] PhoneTransfer dependencies are ready.
echo   Run: python main.py
pause
