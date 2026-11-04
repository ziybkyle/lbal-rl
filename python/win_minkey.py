"""Minimal repro: curses getch loop in a child console + WriteConsoleInput injection."""
import subprocess
import sys
import time
from ctypes import wintypes

import ctypes

kernel32 = ctypes.windll.kernel32
kernel32.CreateFileW.restype = ctypes.c_void_p

CHILD = r'''
import curses, sys
log = open("minkey_child.log", "w", buffering=1)
def run(scr):
    curses.curs_set(0)
    scr.nodelay(False)
    scr.addstr(0, 0, "waiting for keys; q quits")
    scr.refresh()
    while True:
        k = scr.getch()
        log.write(f"key={k}\n")
        if k == ord("q"):
            break
curses.wrapper(run)
log.write("done\n")
'''

if __name__ == "__main__":
    open("minkey_child.py", "w").write(CHILD)
    proc = subprocess.Popen([sys.executable, "minkey_child.py"],
                            creationflags=subprocess.CREATE_NEW_CONSOLE)
    print("child pid:", proc.pid)
    time.sleep(4)

    kernel32.FreeConsole()
    if not kernel32.AttachConsole(proc.pid):
        print("attach fail", kernel32.GetLastError())
        proc.kill()
        sys.exit(1)
    GENERIC_RW, OPEN_EXISTING, SHARE = 0xC0000000, 3, 3
    conin = kernel32.CreateFileW("CONIN$", GENERIC_RW, SHARE, None, OPEN_EXISTING, 0, None)

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
            _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("pad", ctypes.c_byte * 40)]
        _fields_ = [("EventType", wintypes.WORD), ("U", _U)]

    def rec(ch, down):
        ir = INPUT_RECORD()
        ir.EventType = 1
        ir.U.KeyEvent.bKeyDown = down
        ir.U.KeyEvent.wRepeatCount = 1
        ir.U.KeyEvent.wVirtualKeyCode = ord(ch.upper())
        ir.U.KeyEvent.u.u.UnicodeChar = ch
        return ir

    for k, wait in [("1", 1.5), ("a", 1.5), ("q", 2.5)]:
        arr = (INPUT_RECORD * 2)(rec(k, True), rec(k, False))
        n = wintypes.DWORD()
        ok = kernel32.WriteConsoleInputW(conin, arr, 2, ctypes.byref(n))
        print(f"inject {k!r}: ok={ok} n={n.value}")
        time.sleep(wait)

    kernel32.CloseHandle(conin)
    kernel32.FreeConsole()
    try:
        rc = proc.wait(timeout=10)
        print("child exit:", rc)
    except subprocess.TimeoutExpired:
        print("child did NOT exit")
        proc.kill()
    print("child log:", open("minkey_child.log").read())
