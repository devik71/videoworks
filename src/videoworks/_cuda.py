"""Робить CUDA-бібліотеки з pip-пакетів видимими для CTranslate2 на Windows.

CTranslate2 лінкується з cuDNN/cuBLAS ліниво: імпорт проходить, а падає вже при
першому forward — класичне ``Could not locate cudnn_ops64_9.dll``. Колеса
``nvidia-cudnn-cu12`` і ``nvidia-cublas-cu12`` кладуть DLL у
``site-packages/nvidia/*/bin``, якого немає в шляху пошуку. Додаємо його самі,
до того як завантажиться сам ctranslate2.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_applied = False


def enable_cuda_dlls() -> list[Path]:
    """Додає site-packages/nvidia/*/bin у шлях пошуку DLL. Ідемпотентна."""
    global _applied
    if _applied or sys.platform != "win32":
        return []

    base = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    if not base.is_dir():
        _applied = True
        return []

    added: list[Path] = []
    for package in sorted(base.iterdir()):
        bin_dir = package / "bin"
        if not bin_dir.is_dir():
            continue
        os.add_dll_directory(str(bin_dir))
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        added.append(bin_dir)

    _applied = True
    return added
