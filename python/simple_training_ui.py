"""Practice and inventory shaping controls for the selected checkpoint."""
import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

from training_options import cards, validate_options, options_label
from window_layout import remember_popup


def normalize_decimal(value):
    return value.strip().translate(str.maketrans({'。': '.', '．': '.', '，': '.', ',': '.'}))


class SimpleTrainingControls:
    def __init__(self, app, parent):
        self.app = app
        self.path = app.runs_dir/'training_controls.json'
        try:
            self.saved = json.loads(self.path.read_text(encoding='utf8'))
        except (OSError, ValueError):
            self.saved = {}
        self.restoring = True
        self.practice = self.saved.get('practice')
        self.practice_draft = self.saved.get('practice_draft') or dict(kind='symbol', name='coal', count=1, trigger='start', rent=6, percent=25)
        self.practice_enabled = tk.BooleanVar(value=self.practice is not None)
        from training_shaping import validate as validate_shaping
        try:
            self.shaping = validate_shaping(self.saved.get('shaping'))
        except (ValueError, TypeError):
            self.shaping = validate_shaping()
        self.deck_shaping_enabled = tk.BooleanVar(value=self.shaping['deck_enabled'])
        self.caption = tk.StringVar()
        box = ttk.LabelFrame(parent, text='本次训练设置', padding=9)
        box.pack(fill='x', pady=(0, 10))
        row = ttk.Frame(box);row.pack(fill='x')
        self.practice_check = ttk.Checkbutton(row, text='指定内容练习', variable=self.practice_enabled, command=self.toggle_practice)
        self.practice_check.pack(side='left', padx=(20, 8))
        self.practice_button = ttk.Button(row, text='练习设置…', command=self.practice_dialog)
        self.practice_button.pack(side='left', padx=(0, 8))
        ttk.Button(row, text='设置说明', command=self.help).pack(side='left')
        shaping_row = ttk.Frame(box);shaping_row.pack(fill='x', pady=(7, 0))
        self.deck_shaping_check = ttk.Checkbutton(shaping_row, text='储物间超额扣分', variable=self.deck_shaping_enabled, command=self.changed)
        self.deck_shaping_check.pack(side='left')
        self.shaping_button = ttk.Button(shaping_row, text='调整奖励塑形…', command=self.shaping_dialog)
        self.shaping_button.pack(side='left', padx=(0, 12))
        ttk.Label(shaping_row, text='仅用于训练', foreground='#66768a').pack(side='left')
        ttk.Label(box, textvariable=self.caption, foreground='#526779', wraplength=950).pack(anchor='w', pady=(7, 0))
        self.refresh_status()

    def restore(self):
        selected = self.saved.get('selected_model')
        if selected and (Path(selected)/'summary.json').is_file():
            self.app.refresh_runs(Path(selected))
        self.restoring = False
        self.note_selection()

    def options(self):
        shaping = self.shaping | dict(deck_enabled=self.deck_shaping_enabled.get())
        return validate_options(dict(practice=self.practice, shaping=shaping))

    def changed(self):
        self.refresh_status()
        self.note_selection()

    def toggle_practice(self):
        try:
            self.practice = validate_options(dict(practice=self.practice_draft))['practice'] if self.practice_enabled.get() else None
            self.changed()
        except (ValueError, TypeError) as exc:
            self.practice_enabled.set(self.practice is not None)
            messagebox.showerror('指定内容设置不正确', str(exc), parent=self.app.root)

    def enable_practice(self, draft):
        normalized = validate_options(dict(practice=draft))['practice']
        self.practice_draft = self.practice = normalized
        self.practice_enabled.set(True)
        self.changed()

    def refresh_status(self):
        if self.app.training_active:
            return
        try:
            text = options_label(self.options())
        except (ValueError, TypeError):
            text = '请检查练习与储物间扣分设置。'
        self.caption.set(text + ('；指定内容练习未启用' if self.practice is None else '') + '；评估使用正常开局。')

    def note_selection(self):
        if self.restoring or self.app.training_active:
            return
        try:
            value = self.options()
            selected = self.app.selected_run()
            value.update(selected_model=str(selected.resolve()) if selected and selected != self.app.runs_dir else None,
                         practice_draft=self.practice_draft)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')
            temporary.replace(self.path)
        except (OSError, ValueError, TypeError):
            pass  # Keep the last valid decimal while the user is typing.

    def set_busy(self, busy):
        self.practice_check.configure(state='disabled' if busy else 'normal')
        self.practice_button.configure(state='disabled' if busy else 'normal')
        for widget in (self.deck_shaping_check, self.shaping_button):
            widget.configure(state='disabled' if busy else 'normal')
        if not busy:
            self.refresh_status();self.note_selection()

    def shaping_dialog(self):
        from training_shaping_ui import ShapingDialog
        ShapingDialog(self)

    def help(self):
        messagebox.showinfo('练习与储物间设置',
            '指定内容练习会在选定比例的对局中添加指定符号、物品或精华。'
            '每局最多一次，在开局或成功交完指定次数的租金后添加。\n\n'
            '储物间超额扣分会在符号数超过阈值且继续增长时扣分。'
            '这两项只用于训练，评估使用正常开局。', parent=self.app.root)

    def practice_dialog(self):
        if self.app.training_active:
            return
        a = self.app
        window = tk.Toplevel(a.root);window.title('指定内容练习');window.transient(a.root)
        window.geometry('670x510');window.minsize(560, 470);remember_popup(a, window, 'training_practice')
        window.grab_set()
        frame = ttk.Frame(window, padding=18);frame.pack(fill='both', expand=True)
        current = '已启用' if self.practice is not None else '未启用'
        ttk.Label(frame, text=f'当前{current}；点击“保存并启用”后用于下次训练。').grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 12))
        p = self.practice or self.practice_draft
        labels = {'符号': 'symbol', '物品': 'item', '精华': 'essence'}
        kind = tk.StringVar(value=next(k for k, v in labels.items() if v == p['kind']))
        search, choice = tk.StringVar(), tk.StringVar()
        ttk.Label(frame, text='类型').grid(row=1, column=0, sticky='w', pady=5)
        ttk.Combobox(frame, textvariable=kind, values=tuple(labels), state='readonly', width=25).grid(row=1, column=1, sticky='ew')
        ttk.Label(frame, text='搜索名字').grid(row=2, column=0, sticky='w', pady=5)
        ttk.Entry(frame, textvariable=search).grid(row=2, column=1, sticky='ew')
        ttk.Label(frame, text='指定内容').grid(row=3, column=0, sticky='w', pady=5)
        picker = ttk.Combobox(frame, textvariable=choice, state='readonly', width=42);picker.grid(row=3, column=1, sticky='ew')
        available = {}
        def update_choices(*_):
            nonlocal available
            available = {f'{c["display"]}  ({c["name"]})': c for c in cards()
                         if c['kind'] == labels[kind.get()] and search.get().lower() in (c['display']+' '+c['name']).lower()}
            picker['values'] = list(available)
            if choice.get() not in available:
                choice.set(next((k for k, c in available.items() if c['name'] == p['name']), next(iter(available), '')))
        kind.trace_add('write', update_choices);search.trace_add('write', update_choices);update_choices()
        count = tk.StringVar(value=str(p['count']));percent = tk.StringVar(value=str(p['percent']))
        when = tk.StringVar(value='开局添加' if p['trigger'] == 'start' else '成功交租后添加')
        rent = tk.StringVar(value=str(p['rent']))
        for row, text, widget in [
            (4, '添加数量', ttk.Spinbox(frame, from_=1, to=10, textvariable=count, width=12)),
            (5, '用于多少比例的对局（%）', ttk.Entry(frame, textvariable=percent, width=12)),
            (6, '添加时机', ttk.Combobox(frame, textvariable=when, values=('开局添加', '成功交租后添加'), state='readonly')),
            (7, '成功交租第几次后（1～12）', ttk.Spinbox(frame, from_=1, to=12, textvariable=rent, width=12)),
        ]:
            ttk.Label(frame, text=text).grid(row=row, column=0, sticky='w', padx=(0, 12), pady=6);widget.grid(row=row, column=1, sticky='w')
        rent_field = widget
        def update_timing(*_):
            rent_field.configure(state='normal' if when.get() == '成功交租后添加' else 'disabled')
        when.trace_add('write', update_timing);update_timing()
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text='建议先设 25%，数量 1；精华每局限加 1 个。\n交租后添加：完成该次交租的候选选择、准备旋转时加入。\n尚未活到指定阶段的对局不会添加。评估全部保持正常开局。', foreground='#66768a', wraplength=610).grid(row=8, column=0, columnspan=2, sticky='w', pady=12)
        def save():
            try:
                if choice.get() not in available:
                    raise ValueError('请选择一个有效的内容。')
                card = available[choice.get()]
                draft = dict(kind=card['kind'], name=card['name'], count=float(count.get()),
                    percent=float(normalize_decimal(percent.get())), trigger='start' if when.get() == '开局添加' else 'rent', rent=float(rent.get()))
                self.enable_practice(draft)
                window.destroy()
            except (ValueError, TypeError) as exc:
                messagebox.showerror('设置不正确', str(exc), parent=window)
        def disable():
            self.practice_enabled.set(False)
            self.toggle_practice()
            window.destroy()
        ttk.Button(frame, text='停用练习', command=disable).grid(row=9, column=0, sticky='w')
        ttk.Button(frame, text='保存并启用', command=save).grid(row=9, column=1, sticky='e')
