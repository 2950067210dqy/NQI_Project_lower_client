"""PyInstaller 启动钩子：统一打包程序的相对路径根目录。"""
import os
import sys
from pathlib import Path


if getattr(sys, "frozen", False):
    # 下位机日志和现场配置都以 exe 目录为基准，避免启动方式改变造成路径漂移。
    os.chdir(Path(sys.executable).resolve().parent)
