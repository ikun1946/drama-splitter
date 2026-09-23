"""检查本项目所需的 Qt 模块是否可用。"""
import importlib
import sys

MODULES = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtNetwork",
    "PySide6.QtSvg",
]

available, missing = [], []
for name in MODULES:
    try:
        importlib.import_module(name)
        available.append(name)
    except Exception as exc:  # noqa: BLE001
        missing.append(f"{name} ({type(exc).__name__})")

import PySide6  # noqa: E402

print("PySide6 版本:", PySide6.__version__)
print("可用:")
for name in available:
    print("  OK  ", name)
print("缺失:")
for name in missing:
    print("  --  ", name)
sys.exit(0)
