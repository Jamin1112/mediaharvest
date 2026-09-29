param(
    [switch]$NoBrowser,
    [switch]$NoInstaller
)

$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Venv = Join-Path $Root ".venv-build-win"
$BuildDir = Join-Path $Root "build\pyinstaller-win"
$DistDir = Join-Path $Root "dist"
$Browsers = Join-Path $Root ".playwright-browsers-win"
$Spec = Join-Path $Root "packaging\MediaHarvest.windows.spec"
$AppDist = Join-Path $DistDir "MediaHarvest"

if ([System.Environment]::OSVersion.Platform -ne "Win32NT") {
    throw "Windows package builds must run on Windows."
}

Write-Host "==> MediaHarvest standalone Windows build"
Write-Host "    Project: $Root"

$Python = Get-Command python -ErrorAction SilentlyContinue
if (-not $Python) {
    $Python = Get-Command py -ErrorAction SilentlyContinue
}
if (-not $Python) {
    throw "Python 3.8+ was not found. Install Python first, then rerun this script."
}

$PythonVersion = & $Python.Source -c "import sys; print('%d.%d' % sys.version_info[:2]); raise SystemExit(0 if sys.version_info >= (3, 8) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.8+ is required. Current Python is $PythonVersion."
}
Write-Host "    Python: $PythonVersion"

if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) {
    Write-Host "==> Creating build virtual environment .venv-build-win"
    & $Python.Source -m venv $Venv
}

$VenvPython = Join-Path $Venv "Scripts\python.exe"

& $VenvPython -m pip install --upgrade pip

Write-Host "==> Installing build dependencies"
& $VenvPython -m pip install --upgrade pyinstaller
& $VenvPython -m pip install --upgrade -e "$Root[music]"

if (-not $NoBrowser) {
    $HasChromium = $false
    if (Test-Path $Browsers) {
        $HasChromium = [bool](Get-ChildItem $Browsers -Directory -Filter "chromium*" -ErrorAction SilentlyContinue | Select-Object -First 1)
    }
    if (-not $HasChromium) {
        Write-Host "==> Installing Chromium into .playwright-browsers-win"
        $env:PLAYWRIGHT_BROWSERS_PATH = $Browsers
        & $VenvPython -m playwright install chromium
    } else {
        Write-Host "==> Reusing existing .playwright-browsers-win"
    }
}

Write-Host "==> Running PyInstaller"
Remove-Item -Recurse -Force $BuildDir, $AppDist -ErrorAction SilentlyContinue
& $VenvPython -m PyInstaller `
    --clean `
    --noconfirm `
    --workpath $BuildDir `
    --distpath $DistDir `
    $Spec

if ((-not $NoBrowser) -and (Test-Path $Browsers)) {
    Write-Host "==> Copying Chromium browser resources"
    $TargetBrowsers = Join-Path $AppDist ".playwright-browsers"
    Remove-Item -Recurse -Force $TargetBrowsers -ErrorAction SilentlyContinue
    Copy-Item -Recurse $Browsers $TargetBrowsers
}

if (-not $NoInstaller) {
    $Iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    $IsccPath = if ($Iscc) { $Iscc.Source } else { $null }
    if (-not $Iscc) {
        $DefaultIscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
        if (Test-Path $DefaultIscc) {
            $IsccPath = $DefaultIscc
        }
    }
    if ($IsccPath) {
        Write-Host "==> Building Inno Setup installer"
        & $IsccPath (Join-Path $Root "packaging\MediaHarvest.iss")
    } else {
        Write-Host "==> Inno Setup was not found; skipped installer."
        Write-Host "    Install Inno Setup 6 and rerun without -NoInstaller to create MediaHarvest-Windows-Setup.exe."
    }
}

Write-Host ""
Write-Host "==> Build complete"
Write-Host "    $AppDist\MediaHarvest.exe"
if (Test-Path (Join-Path $DistDir "MediaHarvest-Windows-Setup.exe")) {
    Write-Host "    $(Join-Path $DistDir "MediaHarvest-Windows-Setup.exe")"
}
