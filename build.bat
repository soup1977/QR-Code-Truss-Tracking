@echo off
setlocal EnableDelayedExpansion
rem ============================================================================
rem build.bat One-command build for Builders Truss QR
rem
rem Requirements (on the build machine):
rem   • Python 3.10+ with .venv set up:  python -m venv .venv
rem   • Dependencies installed:          .venv\Scripts\pip install -r requirements.txt
rem   • PyInstaller installed:           .venv\Scripts\pip install pyinstaller
rem   • Inno Setup 6 installed at default path (see ISCC_PATH below)
rem
rem Usage (from project root):
rem   build.bat
rem
rem Output:
rem   dist\BuildersQRLabels\                      — PyInstaller bundle
rem   Output\BuildersTrussQR-0.9.0-Portable.zip   — primary distributable (no installer needed)
rem   Output\BuildersTrussQR-0.9.0-Setup.exe      — Inno Setup installer (optional, if ISCC found)
rem   Output\version.json                         — copy to network share alongside the ZIP
rem ============================================================================

set VERSION=0.9.1
set ISCC_PATH=C:\Users\craig\AppData\Local\Programs\Inno Setup 6\ISCC.exe

rem ── Network share path where the ZIP and version.json will be deployed ───────
rem    Must match the folder containing db_path in config.json on all machines.
rem    Trailing backslash required.
set SHARE_PATH=X:\PROJECT\QRCodes\Database\

echo.
echo ============================================================
echo  Building Builders Truss QR v%VERSION%
echo ============================================================
echo.

rem ── Activate virtual environment ────────────────────────────────────────────
if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
) else (
    echo [WARN] .venv not found — using system Python
)

rem ── Step 1: PyInstaller ─────────────────────────────────────────────────────
echo [1/4] PyInstaller — building standalone bundle...
pyinstaller BuildersQRLabels.spec --clean --noconfirm
if !errorlevel! neq 0 (
    echo.
    echo [ERROR] PyInstaller failed. Check output above.
    exit /b 1
)
echo [1/4] PyInstaller complete.
echo.

rem ── Step 2: Inno Setup (optional — skipped if ISCC not found) ───────────────
echo [2/4] Inno Setup — building installer (optional)...
if not exist "%ISCC_PATH%" (
    echo [SKIP] Inno Setup not found at: %ISCC_PATH% — skipping installer build.
    echo        Install Inno Setup 6 from https://jrsoftware.org/isinfo.php if needed.
) else (
    "%ISCC_PATH%" installer\BuildersQRLabels.iss
    if !errorlevel! neq 0 (
        echo.
        echo [ERROR] Inno Setup failed. Check output above.
        exit /b 1
    )
    echo [2/4] Inno Setup complete.
)
echo.

rem ── Step 3: ZIP portable bundle ─────────────────────────────────────────────
echo [3/4] Zipping portable bundle...
if not exist "Output" mkdir Output
powershell -NoProfile -Command "Compress-Archive -Force -Path 'dist\BuildersQRLabels\*' -DestinationPath 'Output\BuildersTrussQR-%VERSION%-Portable.zip'"
if !errorlevel! neq 0 (
    echo.
    echo [ERROR] ZIP failed. Check output above.
    exit /b 1
)
echo [3/4] ZIP complete.
echo.

rem ── Step 4: Generate version.json ───────────────────────────────────────────
echo [4/4] Generating version.json...
powershell -NoProfile -Command "[ordered]@{version='%VERSION%';installer_path='%SHARE_PATH%BuildersTrussQR-%VERSION%-Portable.zip';release_notes=''} | ConvertTo-Json | Set-Content -Encoding UTF8 'Output\version.json'"
if !errorlevel! neq 0 (
    echo.
    echo [ERROR] version.json generation failed.
    exit /b 1
)
echo [4/4] version.json complete.
echo.

rem ── Done ────────────────────────────────────────────────────────────────────
echo ============================================================
echo  BUILD COMPLETE
echo  Output\BuildersTrussQR-%VERSION%-Portable.zip  (distribute this)
echo  Output\version.json                            (deploy alongside ZIP)
echo  Output\BuildersTrussQR-%VERSION%-Setup.exe     (if Inno Setup ran)
echo ============================================================
echo.
echo Next steps:
echo   1. Fill in "release_notes" in Output\version.json
echo   2. Copy both files to %SHARE_PATH%
echo   3. Users: extract the ZIP anywhere and run BuildersQRLabels.exe
echo
pause
rem exit /b 0
