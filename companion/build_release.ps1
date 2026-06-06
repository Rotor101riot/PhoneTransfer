# build_release.ps1 — build and sign the PhoneTransfer Companion release APK
#
# Prerequisites:
#   1. Copy keystore.properties.example to keystore.properties and fill in your keystore details.
#   2. Ensure JAVA_HOME or a local JDK is on PATH (required by gradlew).
#
# Usage:
#   .\build_release.ps1 [-SkipTests] [-OutputDir <path>]
#
# The signed APK is placed in app\build\outputs\apk\release\ by Gradle.
# If -OutputDir is provided, the APK is also copied there.

param(
    [switch]$SkipTests,
    [string]$OutputDir = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# Verify keystore.properties exists
if (-not (Test-Path "keystore.properties")) {
    Write-Error @"
keystore.properties not found.
Copy keystore.properties.example to keystore.properties and fill in your signing credentials.
"@
    exit 1
}

# Clean previous outputs
Write-Host "Cleaning previous build outputs..." -ForegroundColor Cyan
& .\gradlew.bat clean

# Optionally run unit tests before assembling
if (-not $SkipTests) {
    Write-Host "Running unit tests..." -ForegroundColor Cyan
    & .\gradlew.bat testReleaseUnitTest
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Unit tests failed — aborting release build."
        exit 1
    }
}

# Assemble the signed release APK
Write-Host "Assembling release APK..." -ForegroundColor Cyan
& .\gradlew.bat assembleRelease
if ($LASTEXITCODE -ne 0) {
    Write-Error "assembleRelease failed."
    exit 1
}

# Locate the output APK
$ApkPath = Get-ChildItem -Path "app\build\outputs\apk\release" -Filter "*.apk" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1 -ExpandProperty FullName

if (-not $ApkPath) {
    Write-Error "Release APK not found under app\build\outputs\apk\release\"
    exit 1
}

Write-Host ""
Write-Host "Release APK ready: $ApkPath" -ForegroundColor Green

# Optionally copy to OutputDir
if ($OutputDir) {
    $null = New-Item -ItemType Directory -Force -Path $OutputDir
    Copy-Item $ApkPath -Destination $OutputDir -Force
    $Dest = Join-Path $OutputDir (Split-Path $ApkPath -Leaf)
    Write-Host "Copied to: $Dest" -ForegroundColor Green
}
