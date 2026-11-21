# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller multi-entrypoint spec: builds SRT-Embedder.exe (GUI) + srt-embedder-cli.exe (CLI)."""

from pathlib import Path

ROOT = Path(SPECPATH)

datas = [
    (str(ROOT / "srt_embedder" / "ui"), "srt_embedder/ui"),
    (str(ROOT / "srt_embedder" / "assets"), "srt_embedder/assets"),
]

hiddenimports = [
    "webview",
    "webview.platforms.edgechromium",
    "clr_loader",
    "pythonnet",
    "charset_normalizer",
    "rich",
    "send2trash",
    "watchdog",
    "pystray",
    "PIL",
]

a_gui = Analysis(
    [str(ROOT / "srt_embedder" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "scipy", "torch", "cv2"],
    noarchive=False,
)
pyz_gui = PYZ(a_gui.pure)

exe_gui = EXE(
    pyz_gui,
    a_gui.scripts,
    [],
    exclude_binaries=True,
    name="SRT-Embedder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=str(ROOT / "srt_embedder" / "assets" / "icon.ico"),
)

exe_cli = EXE(
    pyz_gui,
    a_gui.scripts,
    [],
    exclude_binaries=True,
    name="srt-embedder-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    icon=str(ROOT / "srt_embedder" / "assets" / "icon.ico"),
)

coll = COLLECT(
    exe_gui,
    exe_cli,
    a_gui.binaries,
    a_gui.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="SRT-Embedder",
)
