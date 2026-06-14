# install-pyimg4.ps1 — Step 1: pyimg4 without its MSVC-bound transitive deps
#
# pyimg4 depends on pylzss and lzfse, which are C extensions with no
# pre-built wheels for Python 3.13+.  Installing pyimg4 with --no-deps
# skips them entirely — PhoneTransfer never uses the IPSW features
# that need these packages.

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Step 1/3: pyimg4 (without pylzss / lzfse)"               -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  pyimg4 is a transitive dependency of pymobiledevice3.  Its own"         -ForegroundColor Gray
Write-Host "  dependencies pylzss and lzfse require C compilation from source,"        -ForegroundColor Gray
Write-Host "  which fails on this system without MSVC Build Tools (especially"          -ForegroundColor Gray
Write-Host "  on Python 3.13+ where no pre-built wheels exist)."                        -ForegroundColor Gray
Write-Host ""
Write-Host "  PhoneTransfer never uses the IPSW firmware handling that needs"           -ForegroundColor Gray
Write-Host "  those packages, so we install pyimg4 with --no-deps to skip them."        -ForegroundColor Gray
Write-Host ""

pip install --no-deps pyimg4
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "[FAIL] pyimg4 install failed." -ForegroundColor Red
    exit 1
}
Write-Host ""
Write-Host "[OK] pyimg4 installed" -ForegroundColor Green
exit 0
