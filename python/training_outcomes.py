"""Read-only failure-stage and terminal-deck comparisons for one experiment."""
from collections import Counter
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox
import webbrowser

from training_analytics import lineage, latest_eval_source, read_jsonl
from evaluation_history import evaluation_sources


def outcome_summary(rows):
    valid=[r for r in rows if not r.get('truncated')]
    wins=[r for r in valid if r.get('won')]
    losses=[r for r in valid if not r.get('won')]
    def deck(group):
        values=[r['deck_len'] for r in group if isinstance(r.get('deck_len'),(int,float))]
        return dict(mean=sum(values)/len(values) if values else None,known=len(values),total=len(group))
    return dict(games=len(rows),valid=len(valid),wins=len(wins),losses=len(losses),
        truncated=len(rows)-len(valid),rate=100*len(wins)/len(valid) if valid else None,
        rents=sum(r.get('rents_paid',0) for r in rows)/len(rows) if rows else None,
        deaths=dict(Counter(min(int(r.get('rents_paid',0)),12) for r in losses)),
        win_deck=deck(wins),loss_deck=deck(losses))


def load_outcome_rows(directory,source,live=None):
    history=lineage(directory) if directory and (Path(directory)/'summary.json').is_file() else []
    if source=='evaluation':
        if not history:return []
        return list(read_jsonl(latest_eval_source(history)['games_path']))
    rows=[]
    for folder,_ in history:
        rows.extend(read_jsonl(folder/'training_games.jsonl'))
    if live and Path(live) not in {folder for folder,_ in history}:
        path=Path(live)/'training_games.jsonl'
        if path.is_file():
            # A writer may be between lines. Only complete, flushed games count.
            with path.open(encoding='utf8') as stream:
                for line in stream:
                    if line.endswith('\n') and line.strip():rows.append(json.loads(line))
    return rows


@lru_cache(maxsize=6)
def _evaluation_outcomes(path, modified_ns, size):
    # Assessment logs also contain large candidate histories. Keep only the
    # small terminal records required here, and avoid reparsing them on refresh.
    fields=('game','seed','won','rents_paid','deck_len','truncated')
    return tuple({key:row[key] for key in fields if key in row} for row in read_jsonl(path))


def _read_evaluation_outcomes(path):
    stat=Path(path).stat()
    return _evaluation_outcomes(str(path),stat.st_mtime_ns,stat.st_size)


def evaluation_window_pair(sources, selected_key=None, count=1000):
    """Compare separate assessments, never two slices of one assessment."""
    index = next((i for i, entry in enumerate(sources) if entry['key'] == selected_key), 0)
    if not sources:
        return [], [], None, None
    current = sources[index]
    previous = sources[index + 1] if index + 1 < len(sources) else None
    rows = list(_read_evaluation_outcomes(current['games_path']))
    if count:
        rows = rows[-count:]
    # A smaller selected assessment determines the actual comparison length.
    # A shorter prior assessment remains shorter; do not mix in another model.
    prior = list(_read_evaluation_outcomes(previous['games_path']))[-len(rows):] if previous and rows else []
    return rows, prior, current, previous


def evaluation_label(entry, latest=False):
    try:
        stamp = datetime.fromisoformat(entry['created_at'].replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        stamp = stamp.astimezone(timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')
    except (ValueError, KeyError, TypeError):
        stamp = '时间未记录'
    return (('最新 · ' if latest else '') + f'累计 {entry["total"]:,} 局 · {stamp} · '
            f'{entry["games"]:,} 局 · {entry["source"]}')


class OutcomePanel:
    def __init__(self,app,parent):
        self.app=app;self.parent=parent;self.source=tk.StringVar(value='训练探索');self.window=tk.StringVar(value='最近 1000 局')
        self.metrics=tk.StringVar(value='暂无数据');self.deck=tk.StringVar();self.note=tk.StringVar();self.rows=[];self.previous=[]
        self.evaluation=tk.StringVar();self.evaluations=[];self.selected_evaluation_key=None;self.current_evaluation=None;self.current_directory=None
        outer=ttk.Frame(parent,padding=16);outer.pack(fill='both',expand=True)
        ttk.Label(outer,text='死亡阶段与牌组规模',font=('Microsoft YaHei UI',17,'bold')).pack(anchor='w')
        controls=ttk.Frame(outer);controls.pack(fill='x',pady=10)
        for variable,values,width in [(self.source,('训练探索','模型评估（含历史）'),19),(self.window,('最近 1000 局','最近 5000 局','最近 10000 局','全部'),17)]:
            select=ttk.Combobox(controls,textvariable=variable,values=values,state='readonly',width=width)
            select.pack(side='left',padx=(0,10));select.bind('<<ComboboxSelected>>',lambda e:self.refresh())
        ttk.Button(controls,text='刷新',command=self.refresh).pack(side='left')
        self.report_button=ttk.Button(controls,text='打开对应排名分析',command=self.open_report)
        self.report_button.pack(side='left',padx=10)
        self.evaluation_controls=ttk.Frame(outer)
        ttk.Label(self.evaluation_controls,text='评估场次').pack(side='left',padx=(0,10))
        self.evaluation_select=ttk.Combobox(self.evaluation_controls,textvariable=self.evaluation,state='readonly',width=74)
        self.evaluation_select.pack(side='left',fill='x',expand=True)
        self.evaluation_select.bind('<<ComboboxSelected>>',self.select_evaluation)
        ttk.Label(outer,textvariable=self.metrics,font=('Microsoft YaHei UI',11,'bold'),foreground='#254d68').pack(anchor='w',pady=(4,8))
        ttk.Label(outer,textvariable=self.deck,foreground='#536779').pack(anchor='w')
        self.canvas=tk.Canvas(outer,height=260,background='#fbfcfe',highlightthickness=0)
        self.canvas.pack(fill='both',expand=True,pady=12);self.canvas.bind('<Configure>',lambda e:self.draw())
        frame=ttk.Frame(outer);frame.pack(fill='x')
        self.table=ttk.Treeview(frame,columns=('stage','count','share','previous','change'),show='headings',height=6)
        for name,label,width in [('stage','失败时已成功交租',160),('count','本窗口失败局数',135),('share','占有效对局比例',140),('previous','前一个等长窗口',145),('change','占比变化',120)]:
            self.table.heading(name,text=label);self.table.column(name,width=width,anchor='center')
        bar=ttk.Scrollbar(frame,command=self.table.yview);self.table.configure(yscrollcommand=bar.set)
        self.table.pack(side='left',fill='x',expand=True);bar.pack(side='right',fill='y')
        ttk.Label(outer,textvariable=self.note,foreground='#66768a',wraplength=950).pack(anchor='w',pady=8)

    def select_evaluation(self,event=None):
        index=self.evaluation_select.current()
        # The first option follows newly completed assessments; older choices stay pinned.
        self.selected_evaluation_key=self.evaluations[index]['key'] if index>0 else None
        self.refresh()

    def open_report(self):
        if self.source.get()=='训练探索':
            self.app.open_analysis('training');return
        report=Path(self.current_evaluation.get('report_path','')) if self.current_evaluation else None
        if report and report.is_file():
            webbrowser.open(report.resolve().as_uri())
        else:
            messagebox.showinfo('报告尚不可用','这次评估的逐局数据仍可查看，但没有找到原始报告。',parent=self.app.root)

    def refresh(self):
        try:
            source='training' if self.source.get()=='训练探索' else 'evaluation'
            directory=self.app.selected_run()
            count=None if self.window.get()=='全部' else int(self.window.get().split()[1])
            previous_entry=None
            if source=='evaluation':
                self.current_directory=directory
                history=lineage(directory) if directory and (Path(directory)/'summary.json').is_file() else []
                self.evaluations=evaluation_sources(history)
                keys={entry['key'] for entry in self.evaluations}
                if self.selected_evaluation_key not in keys:self.selected_evaluation_key=None
                labels=[evaluation_label(entry,index==0) for index,entry in enumerate(self.evaluations)]
                self.evaluation_select.configure(values=labels)
                self.rows,self.previous,self.current_evaluation,previous_entry=evaluation_window_pair(
                    self.evaluations,self.selected_evaluation_key,count)
                if self.current_evaluation:
                    selected=next(i for i,e in enumerate(self.evaluations) if e['key']==self.current_evaluation['key'])
                    self.evaluation_select.current(selected)
                else:self.evaluation.set('本训练分支尚无已完成的模型评估')
                self.evaluation_controls.pack(fill='x',after=self.report_button.master,pady=(0,5))
                self.report_button.configure(text='打开所选评估报告')
                self.table.heading('previous',text='上一次独立评估')
            else:
                self.evaluation_controls.pack_forget();self.current_evaluation=None
                self.report_button.configure(text='打开对应排名分析')
                self.table.heading('previous',text='前一个等长窗口')
                rows=load_outcome_rows(directory,source,self.app.current_dir if self.app.training_active else None)
                self.rows=rows[-count:] if count else rows
                self.previous=rows[-2*count:-count] if count and len(rows)>count else []
            data=outcome_summary(self.rows);prior=outcome_summary(self.previous)
            rate=f'{data["rate"]:.2f}%' if data['rate'] is not None else '—'
            avg=f'{data["rents"]:.3f}' if data['rents'] is not None else '—'
            self.metrics.set(f'样本 {data["games"]:,} 局 · 通关 {data["wins"]} 局 · 通关率 {rate} · 平均交租 {avg} 次 · 截断 {data["truncated"]} 局')
            def deck_text(label,entry):
                value=f'{entry["mean"]:.2f} 个' if entry['mean'] is not None else '未记录'
                return f'{label}：{value}（有记录 {entry["known"]}/{entry["total"]} 局）'
            self.deck.set(deck_text('通关局结束时平均符号数',data['win_deck'])+'　　'+deck_text('失败局结束时平均符号数',data['loss_deck']))
            self.table.delete(*self.table.get_children())
            for stage in range(13):
                current=data['deaths'].get(stage,0);old=prior['deaths'].get(stage,0)
                share=current/data['valid']*100 if data['valid'] else 0
                previous=old/prior['valid']*100 if prior['valid'] else None
                self.table.insert('', 'end',values=('12 次及以上（房东战）' if stage==12 else f'{stage} 次',current,f'{share:.2f}%',
                    f'{old} 局 / {previous:.2f}%' if previous is not None else '暂无',f'{share-previous:+.2f} 个百分点' if previous is not None else '—'))
            comparison=' 前窗口与当前窗口相邻，用于观察变化。'
            if source=='evaluation':
                if previous_entry:
                    comparison=f' 对比上一次独立评估：{evaluation_label(previous_entry)}；当前 {len(self.rows):,} 局，上次取最近 {len(self.previous):,} 局。'
                    if len(self.rows)!=len(self.previous):comparison+=' 上次样本不足，按各自实际有效局数计算比例。'
                else:comparison=' 当前场次之前没有独立评估记录；不会把同一次评估拆成两份。'
            self.note.set('符号数为游戏结束时储物间实际数量。缺失旧数据不按零计算；截断局单列，不当作失败。'+comparison)
            self.draw()
        except (OSError,ValueError,KeyError,TypeError) as exc:
            self.rows=[];self.previous=[];self.table.delete(*self.table.get_children())
            self.metrics.set('暂无可用数据');self.deck.set('');self.note.set(str(exc));self.draw()

    def draw(self):
        c=self.canvas;c.delete('all');data=outcome_summary(self.rows)
        w,h=max(400,c.winfo_width()),max(140,c.winfo_height())
        if not self.rows:c.create_text(w/2,h/2,text='选择数据来源和范围，查看失败阶段',fill='#718096');return
        left,right,top,bottom=45,w-15,28,h-45
        maximum=max(1,*data['deaths'].values());step=(right-left)/13
        for stage in range(13):
            count=data['deaths'].get(stage,0);x=left+(stage+.5)*step;y=bottom-(bottom-top)*count/maximum
            color='#d78b51' if stage<12 else '#8469b0'
            c.create_rectangle(x-step*.32,y,x+step*.32,bottom,fill=color,outline='')
            c.create_text(x,max(12,y-13),text=f'{count}',fill='#3d5266')
            c.create_text(x,bottom+14,text=str(stage) if stage<12 else '12+',fill='#66768a')
        c.create_text((left+right)/2,h-10,text='失败时已成功交租次数（12+ 为房东战）',fill='#66768a')
