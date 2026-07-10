# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


project_root = Path(SPECPATH).resolve() if "SPECPATH" in globals() else Path(__file__).resolve().parent


def add_tree(source_dir, target_dir, skip_suffixes=None):
    """Collect runtime data files needed by the lower-client GUI."""
    skip_suffixes = set(skip_suffixes or [])
    source_path = project_root / source_dir
    datas = []
    if not source_path.exists():
        return datas

    for file_path in source_path.rglob("*"):
        if not file_path.is_file():
            continue
        relative_path = file_path.relative_to(source_path)
        if "__pycache__" in relative_path.parts:
            continue
        if file_path.suffix.lower() in skip_suffixes:
            continue
        datas.append((str(file_path), str(Path(target_dir) / relative_path.parent)))
    return datas


datas = []
config_file = project_root / "lower_config.ini"
if config_file.exists():
    datas.append((str(config_file), "."))
datas += add_tree("static", "static", skip_suffixes={".pyc", ".pyo"})


a = Analysis(
    [str(project_root / "main.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PySide2",
        "PySide6",
        "tkinter",
        "pytest",
        "websockets",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NQI_Lower_Client",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="NQI_Lower_Client",
)
