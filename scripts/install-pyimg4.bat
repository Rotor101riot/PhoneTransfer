@echo off
REM install-pyimg4.bat — Step 1: pyimg4 without its MSVC-bound transitive deps
REM
REM pyimg4 depends on pylzss and lzfse, which are C extensions with no
REM pre-built wheels for Python 3.13+.  Installing pyimg4 with --no-deps
REM skips them entirely — PhoneTransfer never uses the IPSW features
REM that need these packages.

echo.
echo ============================================================
echo  Step 1/3: pyimg4 ^(without pylzss / lzfse^)
echo ============================================================
echo.
echo  pyimg4 is a transitive dependency of pymobiledevice3.  Its own
echo  dependencies pylzss and lzfse require C compilation from source,
echo  which fails on this system without MSVC Build Tools (especially
echo  on Python 3.13+ where no pre-built wheels exist).
echo.
echo  PhoneTransfer never uses the IPSW firmware handling that needs
echo  those packages, so we install pyimg4 with --no-deps to skip them.
echo.

pip install --no-deps pyimg4
if errorlevel 1 (
    echo.
    echo [FAIL] pyimg4 install failed.
    exit /b 1
)
echo.
echo [OK] pyimg4 installed
exit /b 0
