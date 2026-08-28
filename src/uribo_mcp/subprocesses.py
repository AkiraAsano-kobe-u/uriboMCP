"""Cross-platform subprocess settings used by background synchronization."""

from __future__ import annotations

import subprocess
import sys
from typing import Any


def hidden_process_kwargs() -> dict[str, Any]:
    """Prevent Windows child processes from creating or activating a window."""
    if sys.platform != "win32":
        return {}
    startup_info = subprocess.STARTUPINFO()
    startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup_info.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startup_info,
    }


def hidden_selenium_service_kwargs() -> dict[str, Any]:
    """Return Selenium Service's intentionally different popen_kw spelling."""
    if sys.platform != "win32":
        return {}
    return {"creation_flags": subprocess.CREATE_NO_WINDOW}


__all__ = ["hidden_process_kwargs", "hidden_selenium_service_kwargs"]
