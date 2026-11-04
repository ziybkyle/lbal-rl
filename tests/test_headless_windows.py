"""Check Windows automation does not make the game's GUI visible."""
import ctypes
from ctypes import wintypes
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from v3_client import V3Client


@unittest.skipUnless(sys.platform == "win32", "Windows window enumeration")
class HiddenWindowTests(unittest.TestCase):
    def test_window_remains_hidden_during_real_decisions(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visible_windows(pid):
            result = []
            @callback_type
            def visit(hwnd, _):
                owner = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                if owner.value == pid and user32.IsWindowVisible(hwnd):
                    title = ctypes.create_unicode_buffer(512)
                    user32.GetWindowTextW(hwnd, title, 512)
                    result.append(title.value)
                return True
            user32.EnumWindows(visit, 0)
            return result
        with V3Client() as client:
            self.assertEqual(visible_windows(client.proc.pid), [])
            frame = client.reset("HIDDEN001", 20)
            for _ in range(25):
                self.assertEqual(visible_windows(client.proc.pid), [])
                if frame.terminated:
                    break
                candidates = ([c for c in frame.candidates if c.type_id == 430] or
                              [c for c in frame.candidates if c.type_id not in (414, 417, 418, 431)] or
                              list(frame.candidates))
                frame = client.step(candidates[0].index)
            self.assertEqual(visible_windows(client.proc.pid), [])


if __name__ == "__main__":
    unittest.main()
