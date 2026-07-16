# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import shutil

from PyInstaller.utils.hooks import collect_data_files


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
# static/lower_config.ini 是业务可见资源，构建完成后复制到 exe 同级目录；PyInstaller 依赖仍放 _internal。
# requests 的 CA 证书显式放入 _internal，避免部分机器 HTTPS 请求因证书文件缺失失败。
datas += collect_data_files("certifi")
icon_file = project_root / "window_icon.ico"
if icon_file.exists():
    datas.append((str(icon_file), "."))

a = Analysis(
    [str(project_root / "main.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "PyQt6.sip",
        "charset_normalizer.md__mypyc",
        "multiprocessing.popen_spawn_win32",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(project_root / "pyinstaller_runtime_hook.py")],
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
    name="NQI下位机",
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
    icon=str(icon_file) if icon_file.exists() else None,
    contents_directory="_internal",
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


def copy_tree_to_exe_dir(source_name, target_name=None):
    """把下位机界面静态资源复制到 dist/exe 同级目录。"""
    source = project_root / source_name
    if not source.exists():
        return
    target = Path(DISTPATH) / "NQI_Lower_Client" / (target_name or source_name)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))


def copy_file_to_exe_dir(source_name):
    """把下位机配置文件复制到 dist/exe 同级目录，方便打包后直接修改。"""
    source = project_root / source_name
    if not source.exists():
        return
    target = Path(DISTPATH) / "NQI_Lower_Client" / source_name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


copy_tree_to_exe_dir("static")
copy_file_to_exe_dir("lower_config.ini")
