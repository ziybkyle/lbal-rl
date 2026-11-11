"""Plain-language editor for optional inventory overflow rewards."""
import tkinter as tk
from tkinter import ttk, messagebox
import json

from simple_training_ui import normalize_decimal
from training_shaping import DEFAULTS, validate, overflow_cost
from window_layout import remember_popup


class ShapingDialog:
    def __init__(self, controls):
        self.controls = controls
        self.app = controls.app
        self.window = tk.Toplevel(self.app.root)
        self.window.title('训练奖励塑形 · 控牌与抓取差值')
        self.window.geometry('960x680')
        self.window.minsize(900, 600)
        remember_popup(self.app, self.window, 'training_shaping')
        self.window.transient(self.app.root)
        self.window.grab_set()
        outer = ttk.Frame(self.window, padding=16)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='原交租、失败与通关奖励保持不变，以下分数额外计入训练。',
                  font=('Microsoft YaHei UI', 11, 'bold')).pack(anchor='w')
        ttk.Label(outer, text='评估关闭这些分数；学到的选牌偏好仍会体现在评估中。参考差距是行为差异，并不等同于选牌优劣。',
                  wraplength=810, foreground='#66768a').pack(anchor='w', pady=(6, 10))
        deck = ttk.Frame(outer, padding=14)
        deck.pack(fill='both', expand=True)
        self.variables = {}
        config = controls.options()['shaping']
        for key, value in config.items():
            self.variables[key] = tk.BooleanVar(value=value) if isinstance(value, bool) else tk.StringVar(value=f'{value:g}')
        ttk.Checkbutton(deck, text='启用超过指定张数的惩罚', variable=self.variables['deck_enabled']).grid(row=0, column=0, columnspan=3, sticky='w', pady=(0, 10))
        ttk.Label(deck, text='按实际储物间张数计算。只有张数增加且超过阈值时扣分；不会每次旋转重复扣。\n删除不退分，自动生成的新符号也会计入；单局有总扣分上限。',
                  wraplength=750, foreground='#66768a').grid(row=1, column=0, columnspan=3, sticky='w', pady=(0, 9))
        for row, data in enumerate([
            ('deck_threshold', '超过多少张开始扣', 0, 150, '建议 22；允许 0～150，填写整数'),
            ('deck_first', '第一张超额符号扣分', 0, 5, '建议 0.2～1；默认 0.5'),
            ('deck_decay', '后续每张乘以', 0, 1, '建议 0.7～0.95；0.8 表示下一张扣前一张的 80%'),
            ('deck_cap', '单局最多扣分', 0, 20, '建议 1～3；默认 3'),
        ], 2):
            self.field(deck, row, *data)
        self.preview = tk.StringVar()
        ttk.Label(deck, textvariable=self.preview, wraplength=750, foreground='#2868b2').grid(row=6, column=0, columnspan=3, sticky='w', pady=16)
        ttk.Label(deck, text='默认：22→23 张扣 0.5；23→24 张再扣 0.4，合计 0.9。\n把首张设为 1，即得到你举的合计 1、1.8、2.44……的曲线。',
                  foreground='#66768a', wraplength=750).grid(row=7, column=0, columnspan=3, sticky='w')
        bottom = ttk.Frame(outer)
        bottom.pack(fill='x', pady=(12, 0))
        ttk.Button(bottom, text='从所选模型读取', command=self.load_selected).pack(side='left')
        ttk.Button(bottom, text='恢复推荐数值', command=self.defaults).pack(side='left', padx=10)
        ttk.Button(bottom, text='保存设置', command=self.save).pack(side='right')
        ttk.Button(bottom, text='取消', command=self.window.destroy).pack(side='right', padx=10)
        for key in ('deck_threshold', 'deck_first', 'deck_decay', 'deck_cap'):
            self.variables[key].trace_add('write', lambda *_: self.update_preview())
        self.update_preview()

    def field(self, parent, row, key, title, low, high, recommendation):
        parent.columnconfigure(1, weight=1)
        ttk.Label(parent, text=title).grid(row=row, column=0, sticky='w', padx=(0, 10), pady=6)
        # Entries deliberately accept intermediate decimal strings such as "0.".
        syncing = False
        def from_slider(value):
            nonlocal syncing
            if not syncing:
                syncing = True
                self.variables[key].set(str(round(float(value))) if key == 'deck_threshold' else f'{float(value):.3f}')
                syncing = False
        bar = ttk.Scale(parent, from_=low, to=high)
        bar.set(float(self.variables[key].get()))
        bar.configure(command=from_slider)
        def from_entry(*_):
            nonlocal syncing
            if syncing:
                return
            try:
                value = float(normalize_decimal(self.variables[key].get()))
                if low <= value <= high:
                    syncing = True
                    bar.set(value)
            except ValueError:
                pass
            finally:
                syncing = False
        self.variables[key].trace_add('write', from_entry)
        bar.grid(row=row, column=1, sticky='ew', padx=6)
        entry = ttk.Entry(parent, textvariable=self.variables[key], width=9)
        entry.grid(row=row, column=2, sticky='e')
        ttk.Label(parent, text=recommendation, foreground='#66768a').grid(row=row, column=3, sticky='w', padx=(8, 0))

    def read(self):
        return validate({key: var.get() if isinstance(var, tk.BooleanVar) else float(normalize_decimal(var.get()))
                         for key, var in self.variables.items()})

    def update_preview(self):
        try:
            c = self.read()
            text = '从阈值起继续增加时，累计扣分：' + '　'.join(f'{n} 张 −{min(c["deck_cap"], overflow_cost(n, c)):.3g}'
                for n in (c['deck_threshold']+1, c['deck_threshold']+2, c['deck_threshold']+3, c['deck_threshold']+8))
            self.preview.set(text)
        except (ValueError, TypeError):
            self.preview.set('输入有效数字后显示扣分示例。')

    def apply(self, config):
        for key, value in validate(config).items():
            self.variables[key].set(value if isinstance(value, bool) else f'{value:g}')

    def defaults(self):
        self.apply(DEFAULTS | {'deck_enabled': self.variables['deck_enabled'].get()})

    def load_selected(self):
        try:
            path = self.app.selected_run()/'summary.json'
            meta = json.loads(path.read_text(encoding='utf8'))['manifest']
            self.apply(meta.get('training_shaping') or (meta.get('training_options') or {}).get('shaping'))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            messagebox.showerror('无法读取模型设置', str(exc), parent=self.window)

    def save(self):
        try:
            config = self.read()
            self.controls.shaping = config
            self.controls.deck_shaping_enabled.set(config['deck_enabled'])
            self.controls.changed()
            self.window.destroy()
        except (ValueError, TypeError) as exc:
            messagebox.showerror('设置不正确', str(exc), parent=self.window)
