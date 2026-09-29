# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for the standalone MediaHarvest Windows app."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


SPEC_DIR = Path(SPECPATH)
PROJECT_ROOT = SPEC_DIR.parent

datas = []
datas += collect_data_files("playwright")
datas += collect_data_files("yt_dlp")
datas += [
    (str(PROJECT_ROOT / "mediaharvest" / "static" / "logo.png"), "mediaharvest/static"),
]

hiddenimports = []
hiddenimports += collect_submodules("playwright")
hiddenimports += collect_submodules("yt_dlp")
hiddenimports += [
    "flask",
    "jinja2",
    "werkzeug",
    "bs4",
    "lxml",
    "m3u8",
    "Crypto",
]


a = Analysis(
    [str(PROJECT_ROOT / "desktop_launcher.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "tests"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MediaHarvest",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=str(PROJECT_ROOT / "packaging" / "assets" / "app-icon.ico"),
    disable_windowed_traceback=False,
    target_arch=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="MediaHarvest",
)
