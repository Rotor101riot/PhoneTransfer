@echo off
REM install-transitive.bat — Step 3: safe transitive dependencies
REM
REM Installs requirements-safe.txt, which is generated from
REM requirements-lock.txt with pylzss, lzfse, and pillow-heif excluded.
REM Each package is pinned to an exact version for reproducible installs.

echo.
echo ============================================================
echo  Step 3/3: Transitive dependencies ^(requirements-safe.txt^)
echo ============================================================
echo.
echo  Installing the transitive dependency tree from pinned
echo  versions, skipping pylzss, lzfse, and pillow-heif.
echo.

pip install -r requirements-safe.txt
if errorlevel 1 (
    echo.
    echo [WARN] pip exited with error.  If you saw MSVC build
    echo        failures, try updating pip first:
    echo          python -m pip install --upgrade pip
    echo          scripts\install-transitive.bat
    echo.
    exit /b 1
)
echo.
echo [OK] Transitive dependencies installed
exit /b 0
