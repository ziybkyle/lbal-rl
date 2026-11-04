"""Windows keyboard end-to-end test for tui_play.py (ctypes only).

Spawns the TUI in a new console window, injects real keystrokes via
WScript.Shell SendKeys, and checks the process exits cleanly on 'q'
(which requires the full chain: key -> curses -> act() -> engine -> quit).
"""
import ctypes
import subprocess
import time

from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def find_window_by_pid(pid):
    found = ctypes.c_void_p()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, lparam):
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid:
            found.value = hwnd
            return False
        return True

    user32.EnumWindows(cb, 0)
    return found.value


def main():
    proc = subprocess.Popen(
        ["python", "tui_play.py", "WINKEY0001"],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        cwd=".",
    )
    print(f"tui pid={proc.pid}; waiting for engine boot + first render...")
    time.sleep(45)

    import win32com.client
    shell = win32com.client.Dispatch("WScript.Shell")
    hwnd = find_window_by_pid(proc.pid)
    if hwnd:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        time.sleep(1)
        print("window found; sending keys: 1 SPACE 1 SPACE 1 q")
        for key, wait in [("1", 3), (" ", 4), ("1", 3), (" ", 4), ("1", 3), ("q", 2)]:
            shell.SendKeys(key, True)
            time.sleep(wait)
    else:
        print("WARN: window not found; sending keys to whatever is focused")
        for key, wait in [("1", 3), (" ", 4), ("q", 2)]:
            shell.SendKeys(key, True)
            time.sleep(wait)

    try:
        rc = proc.wait(timeout=30)
        print(f"EXIT CODE = {rc}")
        print("KEYBOARD E2E: PASS" if rc == 0 else "KEYBOARD E2E: FAIL")
        return 0 if rc == 0 else 1
    except subprocess.TimeoutExpired:
        print("KEYBOARD E2E: FAIL (process did not exit on 'q')")
        proc.kill()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
