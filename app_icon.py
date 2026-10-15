# -*- coding: utf-8 -*-
"""アプリ専用のアイコンを、ウインドウとタスクバーに付ける小さな部品(PyQt5 / PyQt6 どちらでも動く)。

Windowsは「Pythonで動くプログラム」を全部まとめて Python のアイコンで表示してしまう。
そこで、アプリごとに専用の名前(AppUserModelID)を付けて別のアプリとして扱わせ、
同じフォルダの .ico を表示させる。.ico が無くても、アプリ自体は今まで通り動く。
"""
import importlib
import sys
from pathlib import Path


def apply_app_icon(app, ico_name, app_id, folder=None):
    """app: QApplication / ico_name: 例 'ytdlp.ico' / app_id: 例 'Muramatsu.YtDlpStudio'"""
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass
    try:
        qt = type(app).__module__.split(".")[0]          # "PyQt5" または "PyQt6"
        QIcon = importlib.import_module(qt + ".QtGui").QIcon
        path = Path(folder or Path(__file__).parent) / ico_name
        if path.exists():
            app.setWindowIcon(QIcon(str(path)))
    except Exception:
        pass
