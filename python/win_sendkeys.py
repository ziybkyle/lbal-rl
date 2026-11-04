"""Send real keystrokes to the LBALTUI console window (title match)."""
import ctypes
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def find_window_by_title(substr):
    found = ctypes.c_void_p()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, lparam):
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if substr in buf.value:
            found.value = hwnd
            return False
        return True

    user32.EnumWindows(cb, 0)
    return found.value


def main():
    hwnd = find_window_by_title("LBALTUI")
    if not hwnd:
        print("window not found")
        return 1
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    time.sleep(1.5)

    import win32com.client
    shell = win32com.client.Dispatch("WScript.Shell")
    for key, wait in [("1", 4), (" ", 5), ("1", 4), (" ", 5), ("1", 4), ("q", 3)]:
        shell.SendKeys(key, True)
        time.sleep(wait)
    print("keys sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
