"""videoworks — агентна обробка відео."""

from videoworks._cuda import enable_cuda_dlls

# Має спрацювати до першого імпорту ctranslate2 у будь-якому місці пакета.
enable_cuda_dlls()

__version__ = "0.1.0"
