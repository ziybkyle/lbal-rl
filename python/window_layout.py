"""Persist training-window layouts without importing the training engine."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tkinter as tk
import uuid


def visible_bounds(window):
    """Use actual monitor rectangles, including monitors left of the primary."""
    if os.name == 'nt':
        try:
            import ctypes
            from ctypes import wintypes
            rectangles = []
            callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE,
                                             wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
            class MonitorInfo(ctypes.Structure):
                _fields_ = [('cbSize', wintypes.DWORD), ('monitor', wintypes.RECT),
                            ('work', wintypes.RECT), ('flags', wintypes.DWORD)]
            get_info = ctypes.windll.user32.GetMonitorInfoW
            get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
            get_info.restype = wintypes.BOOL
            def collect(monitor, dc, rect, data):
                r = rect.contents
                info = MonitorInfo()
                info.cbSize = ctypes.sizeof(info)
                if get_info(monitor, ctypes.byref(info)):
                    r = info.work
                rectangles.append((r.left, r.top, r.right, r.bottom))
                return True
            callback = callback_type(collect)
            if ctypes.windll.user32.EnumDisplayMonitors(None, None, callback, 0) and rectangles:
                return rectangles
        except (AttributeError, OSError):
            pass
    return [(0, 0, window.winfo_screenwidth(), window.winfo_screenheight())]


def fit_layout(saved, monitors, minimum=(1, 1)):
    """Keep a saved window on an available monitor after display changes."""
    width, height, x, y = (int(saved[k]) for k in ('width', 'height', 'x', 'y'))
    if width < 1 or height < 1:
        raise ValueError('Invalid window size')
    def overlap(rect):
        left, top, right, bottom = rect
        return max(0, min(x + width, right) - max(x, left)) * max(0, min(y + height, bottom) - max(y, top))
    left, top, right, bottom = max(monitors, key=overlap)
    width = min(max(width, minimum[0]), right - left)
    height = min(max(height, minimum[1]), bottom - top)
    x = max(left, min(x, right - width))
    y = max(top, min(y, bottom - height))
    # Tk's "-100" means distance from the right edge; "+-100" is absolute x=-100.
    return f'{width}x{height}+{x}+{y}'


class WindowLayouts:
    def __init__(self, root, path):
        self.root, self.path = root, Path(path)
        self.windows, self.ready, self.jobs = {}, set(), {}
        self.save_job = None
        try:
            data = json.loads(self.path.read_text(encoding='utf8'))
            self.saved = data['windows'] if isinstance(data.get('windows'), dict) else {}
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self.saved = {}

    def remember(self, window, key):
        self.windows[window] = key
        # Geometry defaults and children are set by callers before this idle runs.
        def restore():
            self.jobs.pop(window, None)
            if not window.winfo_exists():
                return
            saved = self.saved.get(key)
            if isinstance(saved, dict):
                try:
                    window.geometry(fit_layout(saved, visible_bounds(window), window.minsize()))
                    if saved.get('maximized'):
                        window.state('zoomed')
                except (KeyError, ValueError, TypeError, OverflowError, tk.TclError):
                    pass
            self.ready.add(window)
        self.jobs[window] = window.after_idle(restore)
        window.bind('<Configure>', lambda event: self.changed(window) if event.widget is window else None, add='+')
        window.bind('<Destroy>', lambda event: self.destroyed(window) if event.widget is window else None, add='+')

    def capture(self, window):
        key = self.windows[window]
        try:
            state = window.state()
            if state in ('withdrawn', 'iconic'):
                return
            if state == 'zoomed':
                if key in self.saved:
                    self.saved[key]['maximized'] = True
                return
            if window.winfo_width() <= 1 or window.winfo_height() <= 1:
                return
            self.saved[key] = dict(width=window.winfo_width(), height=window.winfo_height(),
                                   x=window.winfo_x(), y=window.winfo_y(), maximized=False)
        except tk.TclError:
            pass

    def changed(self, window):
        if window not in self.ready:
            return
        self.capture(window)
        if self.save_job:
            self.root.after_cancel(self.save_job)
        self.save_job = self.root.after(500, self.flush)

    def destroyed(self, window):
        job = self.jobs.pop(window, None)
        if job:
            try:
                window.after_cancel(job)
            except tk.TclError:
                pass
        self.windows.pop(window, None)
        self.ready.discard(window)
        # Destroy events still reach this handler for button-driven closes.
        self.flush()

    def flush(self):
        if self.save_job:
            try:
                self.root.after_cancel(self.save_job)
            except tk.TclError:
                pass
            self.save_job = None
        temporary = self.path.with_name(f'.{self.path.name}.{uuid.uuid4().hex}.tmp')
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps({'version': 1, 'windows': self.saved}, ensure_ascii=False, indent=2), encoding='utf8')
            os.replace(temporary, self.path)
        except OSError:
            pass
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def remember_popup(app, window, key):
    layouts = getattr(app.root, '_window_layouts', None)
    if layouts:
        layouts.remember(window, f'{app.route}/{key}')
    return window
