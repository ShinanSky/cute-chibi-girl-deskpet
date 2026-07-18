# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_submodules

keyring_datas, keyring_binaries, keyring_hidden = collect_all("keyring")
keyring_hidden += collect_submodules("keyring.backends")

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=keyring_binaries,
    datas=keyring_datas + [("assets", "assets")],
    hiddenimports=keyring_hidden,
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[],
    noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="MoeDesktopPet", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=True, upx_exclude=[], runtime_tmpdir=None,
    console=False, disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
)
