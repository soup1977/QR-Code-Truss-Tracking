# BuildersQRLabels.spec
# PyInstaller build spec for Builders Connect.
#
# Usage (from project root, with .venv active):
#   pyinstaller BuildersQRLabels.spec --clean --noconfirm
#
# Output: dist\BuildersQRLabels\BuildersQRLabels.exe  (directory bundle)
# The dist\ folder is then consumed by the Inno Setup script.

block_cipher = None

a = Analysis(
    ["BuildersQRLabels.py"],
    pathex=["."],
    binaries=[],
    datas=[
        # Include the icons folder if it exists (cosmetic — OK if absent)
        ("icons", "icons"),
    ],
    hiddenimports=[
        # pywin32 — dynamic imports not detected by PyInstaller
        "win32api",
        "win32con",
        "win32print",
        "pywintypes",
        # svglib / reportlab
        "svglib.svglib",
        "reportlab.graphics.renderPDF",
        # pandas optional C extensions
        "pandas._libs.tslibs.np_datetime",
        "pandas._libs.tslibs.nattype",
        "pandas._libs.tslibs.timedeltas",
        # pkg_resources compat shim
        "pkg_resources.py2_compat",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Strip test packages to reduce bundle size
        "test",
        "tests",
        "unittest",
        "pydoc",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BuildersQRLabels",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # UPX can trigger false-positive AV alerts; keep off
    console=False,       # no console window (GUI app)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Uncomment and set path if you have an .ico file:
    # icon="icons\\app.ico",
    version_info=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="BuildersQRLabels",
)
