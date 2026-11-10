# PyInstaller build spec for the unified PDF Capture Tool.
#
# Usage:
#   pyinstaller build_config.spec
#
# Produces a folder-based (--onedir) build at dist/PDFCaptureTool/, which is
# generally flagged far less often by antivirus software than a --onefile
# build (no runtime self-extraction step). See README.md for details.
#
# If you have an .ico file for the app, place it next to this spec file as
# "app.ico" -- the icon= line below will pick it up automatically. If you
# don't have one, just delete/comment that line and PyInstaller will use a
# generic default icon instead.

block_cipher = None

a = Analysis(
    ['pdf_capture_tool_unified.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'PIL._tkinter_finder',   # Pillow + tkinter interaction, sometimes missed by auto-detection
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='PDFCaptureTool',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,              # UPX-compressed exes are flagged by AV even more often; keep off
    console=False,           # windowed app, no background console window
    icon='app.ico',          # delete this line if you don't have an icon file
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='PDFCaptureTool',
)
