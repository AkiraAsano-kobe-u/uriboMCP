import ctypes
import subprocess
import sys
import time

import pytest

from uribo_mcp.subprocesses import (
    hidden_process_kwargs,
    hidden_selenium_service_kwargs,
)


def test_hidden_process_kwargs_hide_windows_children() -> None:
    kwargs = hidden_process_kwargs()
    if sys.platform != "win32":
        assert kwargs == {}
        return

    assert kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW
    startup_info = kwargs["startupinfo"]
    assert startup_info.dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert startup_info.wShowWindow == subprocess.SW_HIDE
    assert hidden_selenium_service_kwargs() == {
        "creation_flags": subprocess.CREATE_NO_WINDOW
    }


@pytest.mark.skipif(sys.platform != "win32", reason="Windows visibility integration test")
def test_hidden_console_child_has_no_visible_window() -> None:
    process = subprocess.Popen(
        ["cmd.exe", "/c", "ping", "127.0.0.1", "-n", "3"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **hidden_process_kwargs(),
    )
    try:
        time.sleep(0.25)
        visible_windows: list[int] = []
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def collect_window(window_handle: int, _parameter: int) -> bool:
            process_id = ctypes.c_ulong()
            ctypes.windll.user32.GetWindowThreadProcessId(
                window_handle, ctypes.byref(process_id)
            )
            if (
                process_id.value == process.pid
                and ctypes.windll.user32.IsWindowVisible(window_handle)
            ):
                visible_windows.append(window_handle)
            return True

        callback = callback_type(collect_window)
        ctypes.windll.user32.EnumWindows(callback, 0)
        assert visible_windows == []
    finally:
        process.terminate()
        process.wait(timeout=5)
