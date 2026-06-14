@echo off
REM install-direct.bat — Step 2: direct dependencies (no transitive deps)
REM
REM Installs the 11 packages listed in requirements.txt with --no-deps.
REM This prevents pip's resolver from re-walking pyimg4's dependency tree
REM and re-pulling pylzss/lzfse.

set REQ_FILE=requirements.txt
if /i "%1"=="--lock" set REQ_FILE=requirements-lock.txt
if /i "%1"=="-l"    set REQ_FILE=requirements-lock.txt

echo.
echo ============================================================
echo  Step 2/3: Direct dependencies ^(%REQ_FILE%^)
echo ============================================================
echo.
echo  Installing the direct dependencies WITHOUT their transitive
echo  deps.  This avoids pip re-pulling pylzss/lzfse through the
echo  resolver after pyimg4 was handled in Step 1.
echo.

pip install --no-deps -r %REQ_FILE%
if errorlevel 1 (
    echo.
    echo [FAIL] pip install failed for %REQ_FILE%.
    echo        See output above.
    exit /b 1
)
echo.
echo [OK] Direct dependencies installed
exit /b 0
