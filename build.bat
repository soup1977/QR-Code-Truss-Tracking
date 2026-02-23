@echo off
setlocal EnableDelayedExpansion
rem ============================================================================
rem build.bat — One-command build for Builders Connect
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
rem   dist\BuildersQRLabels\   — PyInstaller bundle
rem   Output\BuildersConnect-0.9.0-Setup.exe — Inno Setup installer
rem ============================================================================

set VERSION=0.9.0
set ISCC_PATH=C:\Program Files (x86)\Inno Setup 6\ISCC.exe

echo.
echo ============================================================
echo  Building Builders Connect v%VERSION%
echo ============================================================
echo.

rem ── Activate virtual environment ────────────────────────────────────────────
if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
) else (
    echo [WARN] .venv not found — using system Python
)

rem ── Step 1: PyInstaller ─────────────────────────────────────────────────────
echo [1/2] PyInstaller — building standalone bundle...
pyinstaller BuildersQRLabels.spec --clean --noconfirm
if !errorlevel! neq 0 (
    echo.
    echo [ERROR] PyInstaller failed. Check output above.
    exit /b 1
)
echo [1/2] PyInstaller complete.
echo.

rem ── Step 2: Inno Setup ──────────────────────────────────────────────────────
echo [2/2] Inno Setup — building installer...
if not exist "%ISCC_PATH%" (
    echo [ERROR] Inno Setup not found at: %ISCC_PATH%
    echo         Install Inno Setup 6 from https://jrsoftware.org/isinfo.php
    exit /b 1
)
"%ISCC_PATH%" installer\BuildersQRLabels.iss
if !errorlevel! neq 0 (
    echo.
    echo [ERROR] Inno Setup failed. Check output above.
    exit /b 1
)
echo [2/2] Inno Setup complete.
echo.

rem ── Done ────────────────────────────────────────────────────────────────────
echo ============================================================
echo  BUILD COMPLETE
echo  Output\BuildersConnect-%VERSION%-Setup.exe
echo ============================================================
echo.
echo Next steps:
echo   1. Copy Output\BuildersConnect-%VERSION%-Setup.exe to the network share
echo   2. Update \\SERVER\BuildersQRLabels\version.json with the new version
echo   3. Run the installer on target machines
echo.
exit /b 0
