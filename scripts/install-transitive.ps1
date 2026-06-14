# install-transitive.ps1 — Step 3: safe transitive dependencies
#
# Installs requirements-safe.txt, which is generated from
# requirements-lock.txt with pylzss, lzfse, and pillow-heif excluded.
# Each package is pinned to an exact version.

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Step 3/3: Transitive deps (requirements-safe.txt)"        -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Installing the transitive dependency tree from pinned"                 -ForegroundColor Gray
Write-Host "  versions, skipping pylzss, lzfse, and pillow-heif."                    -ForegroundColor Gray
Write-Host ""

pip install -r requirements-safe.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "[WARN] pip exited with error.  If you saw MSVC build"   -ForegroundColor Yellow
    Write-Host "       failures, try updating pip first:"               -ForegroundColor Yellow
    Write-Host "         python -m pip install --upgrade pip"           -ForegroundColor Yellow
    Write-Host "         .\scripts\install-transitive.ps1"              -ForegroundColor Yellow
    exit 1
}
Write-Host ""
Write-Host "[OK] Transitive dependencies installed" -ForegroundColor Green
exit 0
