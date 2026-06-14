@echo off
setlocal enabledelayedexpansion

echo.
echo ============================================================
echo  PhoneTransfer — Dependency Installer
echo ============================================================
echo.
echo  This runs three scripts in order to install all packages
echo  without requiring MSVC Build Tools:
echo.
echo    scripts\install-pyimg4.bat        ^(pyimg4, no C deps^)
echo    scripts\install-direct.bat        ^(requirements.txt^)
echo    scripts\install-transitive.bat    ^(requirements-safe.txt^)
echo.

if not exist "scripts\install-pyimg4.bat" (
    echo [FAIL] scripts\install-pyimg4.bat not found.
    echo        Make sure you're running this from the PhoneTransfer folder.
    pause
    exit /b 1
)

REM ── Step 1 ───────────────────────────────────────────────────────────────
call scripts\install-pyimg4.bat
if errorlevel 1 (
    echo.
    echo [FAIL] Step 1 ^(pyimg4^) failed.  Cannot continue.
    pause
    exit /b 1
)

REM ── Step 2 ───────────────────────────────────────────────────────────────
call scripts\install-direct.bat %*
if errorlevel 1 (
    echo.
    echo [FAIL] Step 2 ^(direct deps^) failed.  Cannot continue.
    pause
    exit /b 1
)

REM ── Step 3 ───────────────────────────────────────────────────────────────
call scripts\install-transitive.bat
if errorlevel 1 (
    echo.
    echo [WARN] Step 3 had issues but core packages may be OK.
    echo        See output above.
)

REM ── Verify ───────────────────────────────────────────────────────────────
echo.
echo ============================================================
echo  Verification
echo ============================================================
echo.
echo  Checking that core packages import correctly . . .
echo.

python -c "import pymobiledevice3; import customtkinter; import iOSbackup; import sqlcipher3; import Pillow; import pycryptodome; import vobject; import iphone_backup_decrypt; print('All core packages import OK')" 2>&1
if errorlevel 1 (
    echo.
    echo [FAIL] Import check failed.  See errors above.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  PhoneTransfer dependencies are ready.
echo ============================================================
echo.
echo  Run:  python main.py
echo.
echo  For HEIC photo conversion ^(optional^):
echo    pip install pillow-heif
echo.
pause
