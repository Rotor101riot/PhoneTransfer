@echo off
setlocal enabledelayedexpansion

echo ============================================================
echo  PhoneTransfer Dependency Installer
echo ============================================================
echo.
echo  This will install all Python packages needed to run
echo  PhoneTransfer.  Three steps, no MSVC Build Tools required.
echo.

REM ── Resolve file arguments ───────────────────────────────────────────────
set REQ_FILE=requirements.txt
set SAFE_FILE=requirements-safe.txt
if /i "%1"=="--lock" set REQ_FILE=requirements-lock.txt
if /i "%1"=="-l"    set REQ_FILE=requirements-lock.txt

echo  Source file : %REQ_FILE%
echo  Safe deps   : %SAFE_FILE%
echo.

REM ── Step 1: pyimg4 without its MSVC-bound transitive deps ─────────────────
echo ── Step 1/3: pyimg4 (without pylzss / lzfse) ───────────────────────────
echo.
echo  pyimg4 is a dependency of pymobiledevice3.  Its transitive deps
echo  pylzss and lzfse require C compilation from source, which fails
echo  on this system without MSVC Build Tools.  PhoneTransfer never
echo  uses the IPSW features that need those packages, so we install
echo  pyimg4 with --no-deps to skip them entirely.
echo.

pip install --no-deps pyimg4
if errorlevel 1 (
    echo.
    echo [FAIL] Could not install pyimg4.  Is Python on your PATH?
    echo        Try running:  pip install --no-deps pyimg4
    echo.
    pause
    exit /b 1
)
echo.

REM ── Step 2: direct dependencies (no transitive deps) ─────────────────────
echo ── Step 2/3: Direct dependencies (%REQ_FILE%) ──────────────────────────
echo.
echo  Installing the 11 direct dependencies listed in %REQ_FILE%
echo  WITHOUT their transitive dependencies.  This avoids re-pulling
echo  pylzss/lzfse through the resolver.
echo.

pip install --no-deps -r %REQ_FILE%
if errorlevel 1 (
    echo.
    echo [FAIL] pip exited with error code %ERRORLEVEL%.
    echo        See the output above for details.
    echo.
    pause
    exit /b 1
)
echo.

REM ── Step 3: transitive dependencies (safe list only) ─────────────────────
echo ── Step 3/3: Transitive dependencies (%SAFE_FILE%) ─────────────────────
echo.
echo  Installing the transitive dependency tree, skipping pylzss,
echo  lzfse, and pillow-heif (optional HEIC converter).  This file
echo  is generated from requirements-lock.txt and contains exact
echo  pinned versions for reproducible installs.
echo.

pip install -r %SAFE_FILE%
set PIP_EXIT=%ERRORLEVEL%

echo.
if %PIP_EXIT% equ 0 (
    echo  All transitive dependencies installed successfully.
) else (
    echo  pip exited with code %PIP_EXIT%.
    echo  If you saw MSVC errors above, try updating pip first:
    echo    python -m pip install --upgrade pip
    echo    setup-deps.bat
    echo.
)

echo.
echo ── Verification ────────────────────────────────────────────────────────
echo.
echo  Checking that core packages import correctly . . .
python -c "import pymobiledevice3; import customtkinter; import iOSbackup; import sqlcipher3; import Pillow; import pycryptodome; import wa_crypt_tools; import vobject; import iphone_backup_decrypt; print('SUCCESS: all core packages import OK')" 2>&1
if errorlevel 1 (
    echo.
    echo [FAIL] Import check failed.  See errors above.
    echo.
    pause
    exit /b 1
) else (
    echo.
    echo ============================================================
    echo  PhoneTransfer dependencies are ready.
    echo ============================================================
    echo.
    echo  Run the app with:  python main.py
    echo.
    echo  NOTE: pylzss and lzfse were skipped — they are only needed
    echo  for IPSW firmware handling, which PhoneTransfer never uses.
    echo.
    echo  For HEIC photo conversion:  pip install pillow-heif
    echo  (optional — requires C build tools or pre-built wheels)
    echo.
)

pause
