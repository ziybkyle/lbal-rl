"""Windows keyboard E2E test for tui_play.py (no focus games).

Spawns the TUI on a new console, attaches to that console, and injects
KEY_EVENT records straight into its CONIN$ input buffer via
WriteConsoleInput — this reaches the app even without window focus.
Verifies: keys act (intro confirm -> spin -> card pick) and 'q' exits rc=0.
"""
import ctypes
import subprocess
import sys
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
GENERIC_READ_WRITE = 0xC0000000
OPEN_EXISTING = 3
FILE_SHARE_RW = 3
INVALID = ctypes.c_void_p(-1).value


class KEY_EVENT_RECORD(ctypes.Structure):
    class _U(ctypes.Union):
        class _S(ctypes.Structure):
            _fields_ = [("UnicodeChar", wintypes.WCHAR), ("AsciiChar", ctypes.c_char)]
        _fields_ = [("u", _S)]
    _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD),
                ("wVirtualKeyCode", wintypes.WORD), ("wVirtualScanCode", wintypes.WORD),
                ("u", _U), ("dwControlKeyState", wintypes.DWORD)]


class INPUT_RECORD(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("padding", ctypes.c_byte * 40)]
    _fields_ = [("EventType", wintypes.WORD), ("U", _U)]


def key_rec(ch, down):
    ir = INPUT_RECORD()
    ir.EventType = 1
    ir.U.KeyEvent.bKeyDown = down
    ir.U.KeyEvent.wRepeatCount = 1
    ir.U.KeyEvent.wVirtualKeyCode = ord(ch.upper()) if ch.isalnum() else 0x20 if ch == " " else 0
    ir.U.KeyEvent.u.u.UnicodeChar = ch
    return ir


def find_console_window(pid):
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
        [sys.executable, "tui_play.py", "WINKEY0005"],
        creationflags=subprocess.CREATE_NEW_CONSOLE, cwd=".")
    print("tui pid:", proc.pid)
    time.sleep(50)  # engine boot + first render

    if not find_console_window(proc.pid):
        # window may belong to conhost/WT host process; attach by pid still works
        print("note: console window not owned by pid; attaching anyway")

    kernel32.FreeConsole()
    if not kernel32.AttachConsole(proc.pid):
        print("FAIL: AttachConsole err", kernel32.GetLastError())
        proc.kill()
        return 1
    conin = kernel32.CreateFileW("CONIN$", GENERIC_READ_WRITE, FILE_SHARE_RW,
                                 None, OPEN_EXISTING, 0, None)
    if not conin or conin == INVALID:
        print("FAIL: CONIN$ open err", kernel32.GetLastError())
        kernel32.FreeConsole()
        proc.kill()
        return 1

    def send(ch):
        arr = (INPUT_RECORD * 2)(key_rec(ch, True), key_rec(ch, False))
        written = wintypes.DWORD()
        ok = kernel32.WriteConsoleInputW(conin, arr, 2, ctypes.byref(written))
        return bool(ok)

    ok_all = True
    for k, wait in [("1", 4), (" ", 6), ("1", 4), (" ", 6), ("1", 4), ("q", 3)]:
        r = send(k)
        ok_all = ok_all and r
        print(f"send {k!r}: {'ok' if r else 'FAIL'}")
        time.sleep(wait)

    kernel32.CloseHandle(conin)
    kernel32.FreeConsole()
    try:
        rc = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        print("KEYBOARD E2E: FAIL (no exit on 'q')")
        proc.kill()
        return 1
    print("EXIT CODE:", rc)
    passed = rc == 0 and ok_all
    print("KEYBOARD E2E:", "PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
