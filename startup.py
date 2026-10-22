"""Optional start-with-the-computer integration (Windows registry / Linux autostart)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from config import BASE_DIR

_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
_REG_NAME = "LocalDrop"
_DESKTOP = Path.home() / ".config" / "autostart" / "localdrop.desktop"


def supported() -> bool:
    return sys.platform.startswith("win") or sys.platform.startswith("linux")


def _command() -> list[str]:
    exe = Path(sys.executable)
    if sys.platform.startswith("win"):
        w = exe.with_name("pythonw.exe")   # no console window
        if w.exists():
            exe = w
    return [str(exe), str(BASE_DIR / "main.py"), "--background"]


def is_enabled() -> bool:
    if sys.platform.startswith("win"):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_PATH) as key:
                winreg.QueryValueEx(key, _REG_NAME)
                return True
        except OSError:
            return False
    return _DESKTOP.exists()


def set_enabled(enabled: bool) -> None:
    if sys.platform.startswith("win"):
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, _REG_NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(_command()))
            else:
                try:
                    winreg.DeleteValue(key, _REG_NAME)
                except FileNotFoundError:
                    pass
    elif sys.platform.startswith("linux"):
        if enabled:
            _DESKTOP.parent.mkdir(parents=True, exist_ok=True)
            _DESKTOP.write_text(
                "[Desktop Entry]\nType=Application\nName=LocalDrop\n"
                f"Exec={subprocess.list2cmdline(_command())}\nX-GNOME-Autostart-enabled=true\n",
                encoding="utf-8",
            )
        else:
            _DESKTOP.unlink(missing_ok=True)
    else:
        raise OSError("Start-with-system is not supported on this platform.")
