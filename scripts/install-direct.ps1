# install-direct.ps1 — Step 2: direct dependencies (no transitive deps)
#
# Installs the packages listed in requirements.txt with --no-deps.
# This avoids pip re-walking pyimg4's dependency tree and pulling
# pylzss/lzfse through the resolver.

param(
    [switch]$Lock
)

$reqFile = if ($Lock) { "requirements-lock.txt" } else { "requirements.txt" }

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Step 2/3: Direct dependencies ($reqFile)"               -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Installing the direct dependencies WITHOUT their transitive"            -ForegroundColor Gray
Write-Host "  deps.  This avoids pip re-pulling pylzss/lzfse through the"              -ForegroundColor Gray
Write-Host "  resolver after pyimg4 was handled in Step 1."                            -ForegroundColor Gray
Write-Host ""

pip install --no-deps -r $reqFile
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "[FAIL] pip install failed for $reqFile." -ForegroundColor Red
    Write-Host "       See output above."                 -ForegroundColor Red
    exit 1
}
Write-Host ""
Write-Host "[OK] Direct dependencies installed" -ForegroundColor Green
exit 0
