"""Compact continuous-training pages; all variables remain owned by TrainingApp."""
import tkinter as tk
from tkinter import ttk
from reward_schemes import REWARD_MODE, REWARD_NAMES


def build_training_page(a):
    if a.notebook is None:
        a.notebook=ttk.Notebook(a.root);a.notebook.pack(fill='both',expand=True)
    style = ttk.Style(a.root)
    if not getattr(a.root, '_training_tab_style', False):
        # Clam's tab element honors colors even on Windows' native theme.
        style.element_create('TrainingPages.tab', 'from', 'clam', 'Notebook.tab')
        style.layout('TrainingPages.TNotebook.Tab', [('TrainingPages.tab', {'sticky': 'nswe', 'children': [
            ('Notebook.padding', {'side': 'top', 'sticky': 'nswe', 'children': [
                ('Notebook.focus', {'side': 'top', 'sticky': 'nswe', 'children': [
                    ('Notebook.label', {'side': 'top', 'sticky': ''})]})]})]})])
        style.configure('TrainingPages.TNotebook.Tab', padding=(22, 10), font=('Microsoft YaHei UI', 11, 'bold'),
                        background='#e4ebf4', foreground='#334c68', bordercolor='#a5b7cb')
        style.map('TrainingPages.TNotebook.Tab', background=[('selected', '#2868b2'), ('active', '#cbdcf0')],
                  foreground=[('selected', '#ffffff')], lightcolor=[('selected', '#2868b2')])
        style.configure('TrainingPages.TNotebook', tabmargins=(8, 7, 8, 3))
        a.root._training_tab_style = True
    a.notebook.configure(style='TrainingPages.TNotebook')
    a.training_tab=ttk.Frame(a.notebook);a.comparison_tab=ttk.Frame(a.notebook);a.charts_tab=ttk.Frame(a.notebook);a.outcomes_tab=ttk.Frame(a.notebook)
    for frame,label in [(a.training_tab,'训练'),(a.comparison_tab,'结果对比'),(a.charts_tab,'走势图'),(a.outcomes_tab,'死亡与牌组')]:
        prefix={'scratch':'从零','baseline':'基线','grouped':'同种子分组','fresh_rules':''}[a.route]
        a.notebook.add(frame,text=f'{prefix} · {label}' if prefix else label)
    outer=ttk.Frame(a.training_tab,padding=16);outer.pack(fill='both',expand=True)
    heading=ttk.Frame(outer);heading.pack(fill='x',pady=(0,6))
    ttk.Label(heading,text='幸运房东 · 持续训练' if a.route!='grouped' else '幸运房东 · 分组实验',font=('Microsoft YaHei UI',18,'bold')).pack(side='left')
    ttk.Button(heading,text='使用说明',command=a.show_training_help).pack(side='right')
    if a.route=='fresh_rules':ttk.Button(heading,text='当前规则',command=a.show_current_rules_help).pack(side='right',padx=8)
    a.import_button=None
    if a.route=='fresh_rules':
        a.import_button=ttk.Button(heading,text='导入模型包…',command=a.import_release_checkpoint)
        a.import_button.pack(side='right',padx=(8,0))
    ttk.Label(outer,textvariable=a.origin_caption,foreground='#596b7d',wraplength=950).pack(anchor='w',pady=(0,12))
    config=ttk.LabelFrame(outer,text='训练设置',padding=12);config.pack(fill='x')
    config.columnconfigure(1,weight=1)
    ttk.Label(config,text='模型起点').grid(row=0,column=0,padx=(0,8),sticky='w')
    a.run_select=ttk.Combobox(config,state='readonly',textvariable=a.choice,width=45)
    a.run_select.grid(row=0,column=1,sticky='ew');a.run_select.bind('<<ComboboxSelected>>',a.model_changed)
    a.refresh_button=ttk.Button(config,text='刷新',command=a.refresh_runs);a.refresh_button.grid(row=0,column=2,padx=(8,0))
    a.choose_button=ttk.Button(config,text='选择文件夹',command=a.select_run);a.choose_button.grid(row=0,column=3,padx=8)
    a.fresh_button=None
    if a.route=='fresh_rules':
        a.fresh_button=ttk.Button(config,text='全新起点',command=a.select_fresh_start);a.fresh_button.grid(row=0,column=4)
    settings=ttk.Frame(config);settings.grid(row=1,column=0,columnspan=5,sticky='w',pady=(12,0))
    ttk.Label(settings,text='计算线程').pack(side='left')
    a.threads_entry=ttk.Spinbox(settings,from_=1,to=64,textvariable=a.compute_threads,width=4);a.threads_entry.pack(side='left',padx=(8,22))
    ttk.Label(settings,text='每次评估局数').pack(side='left')
    a.eval_entry=ttk.Entry(settings,textvariable=a.eval_games,width=7);a.eval_entry.pack(side='left',padx=(8,22))
    ttk.Label(settings,text=REWARD_NAMES[REWARD_MODE]).pack(side='left',padx=(0,8))
    a.progressive_button=ttk.Button(settings,text='调整方案二',command=a.open_progressive_settings);a.progressive_button.pack(side='left')
    ttk.Button(settings,text='更多设置',command=a.open_advanced_settings).pack(side='left',padx=(8,0))
    a.base_thread_hint='每 1000 局保存；按本实验累计局数，每 10000 局自动评估。线程修改在下个保存阶段生效。'
    a.thread_hint.set(a.base_thread_hint)
    ttk.Label(config,textvariable=a.thread_hint,foreground='#66768a',wraplength=910).grid(row=2,column=0,columnspan=5,sticky='w',pady=(9,0))
    if a.route in ('fresh_rules','baseline'):
        from simple_training_ui import SimpleTrainingControls
        a.experiments=SimpleTrainingControls(a,outer)
    # Legacy API references stay available for old scripts, but have no visible finite-training controls.
    hidden=ttk.Frame(a.training_tab)
    a.games_entry=ttk.Entry(hidden,textvariable=a.train_games)
    a.toggle_entry=ttk.Entry(hidden,textvariable=a.toggle_reward)
    a.toggle_permission=ttk.Checkbutton(hidden,variable=a.allow_item_toggle)
    a.limited_toggle_permission=ttk.Checkbutton(hidden,variable=a.limited_item_toggles)
    a.periodic_check=ttk.Checkbutton(hidden,variable=a.periodic_enabled)
    a.periodic_entry=ttk.Entry(hidden,textvariable=a.periodic_interval)
    a.candidate_rules_check=None
    a.start_button=ttk.Button(hidden,command=a.start)
    duration=ttk.Frame(outer);duration.pack(fill='x',pady=(7,0))
    limit_row=ttk.Frame(duration);limit_row.pack(fill='x')
    a.limit_check=ttk.Checkbutton(limit_row,text='本次持续训练达到',variable=a.continuous_limit_enabled);a.limit_check.pack(side='left')
    a.limit_entry=ttk.Spinbox(limit_row,from_=1000,to=10000000,increment=1000,textvariable=a.continuous_limit_games,width=9);a.limit_entry.pack(side='left',padx=6)
    ttk.Label(limit_row,text='局后停止（1000 的倍数）；不勾选则一直运行。',foreground='#66768a').pack(side='left')
    eval_row=ttk.Frame(duration);eval_row.pack(fill='x',pady=(3,0))
    a.end_eval_check=ttk.Checkbutton(eval_row,text='训练结束后评估',variable=a.continuous_end_eval_enabled);a.end_eval_check.pack(side='left',padx=(20,10))
    ttk.Label(eval_row,text='达到目标后，只评估刚训练好的模型；局数使用上方设置。',foreground='#66768a').pack(side='left')
    actions=ttk.Frame(outer);actions.pack(fill='x',pady=(14,8))
    a.continuous_button=None
    if a.route!='grouped':
        a.continuous_button=ttk.Button(actions,text='开始持续训练',command=a.toggle_continuous);a.continuous_button.pack(side='left')
    else:
        a.start_button=ttk.Button(actions,text='开始分组实验',command=a.start);a.start_button.pack(side='left')
    a.stop_button=ttk.Button(hidden,text='停止并保存',command=a.stop,state='disabled')
    a.manual_eval_button=ttk.Button(actions,text='单独评估…',command=a.open_manual_evaluation);a.manual_eval_button.pack(side='left',padx=8)
    a.report_button=ttk.Button(actions,text='逐局记录',command=a.open_report);a.report_button.pack(side='left')
    a.folder_button=ttk.Button(actions,text='结果文件夹',command=a.open_folder);a.folder_button.pack(side='left',padx=8)
    if a.route=='fresh_rules':ttk.Button(actions,text='模型设置',command=a.show_model_settings).pack(side='left')
    analysis=ttk.LabelFrame(outer,text='浏览器分析',padding=8);analysis.pack(fill='x',pady=(0,12))
    a.analysis_buttons=[]
    for label,kind in [('最新 1000 局评估','evaluation'),('最新 1000 局训练探索','training'),('自定义训练范围…','custom')]:
        b=ttk.Button(analysis,text=label,command=lambda k=kind:a.open_analysis(k));b.pack(side='left',padx=(0,10));a.analysis_buttons.append(b)
    status=ttk.Frame(outer);status.pack(fill='x')
    ttk.Label(status,textvariable=a.status,font=('Microsoft YaHei UI',11,'bold')).pack(side='left')
    ttk.Label(status,textvariable=a.elapsed_display,foreground='#66768a').pack(side='right')
    ttk.Label(outer,textvariable=a.rolling_summary,foreground='#246175',font=('Microsoft YaHei UI',11)).pack(anchor='w',pady=(6,4))
    a.progress=ttk.Progressbar(outer,mode='indeterminate');a.progress.pack(fill='x',pady=(0,6))
    ttk.Label(outer,textvariable=a.latest_summary,wraplength=950,foreground='#66768a').pack(anchor='w',pady=(0,6))
    progress_tabs=ttk.Notebook(outer);progress_tabs.pack(fill='both',expand=True)
    frame=ttk.Frame(progress_tabs,padding=8);trend=ttk.Frame(progress_tabs,padding=8);death=ttk.Frame(progress_tabs,padding=8)
    for child,title in [(frame,'运行进度'),(trend,'滚动训练走势'),(death,'死亡阶段')]:progress_tabs.add(child,text=title)
    a.log=tk.Text(frame,height=10,wrap='word',state='disabled',font=('Microsoft YaHei UI',9),background='#fbfcfe',relief='flat')
    scrollbar=ttk.Scrollbar(frame,command=a.log.yview);a.log.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side='right',fill='y');a.log.pack(side='left',fill='both',expand=True)
    a.make_training_charts(trend,'live',160)
    ttk.Label(death,textvariable=a.death_summary,foreground='#66768a').pack(anchor='w')
    a.death_table=a.make_death_table(death,height=9);a.populate_death_table(a.death_table,a.session_deaths)
    ttk.Button(death,text='按来源、最近窗口及牌组数量分析 →',command=lambda:a.notebook.select(a.outcomes_tab)).pack(anchor='e',pady=6)
    a.update_reward_caption();a._make_comparison_ui();build_chart_page(a)
    from training_outcomes import OutcomePanel
    a.outcomes=OutcomePanel(a,a.outcomes_tab)
    a.notebook.bind('<<NotebookTabChanged>>',lambda e:a.outcomes.refresh() if a.notebook.select()==str(a.outcomes_tab) else None,add='+')


def build_chart_page(a):
    outer=ttk.Frame(a.charts_tab,padding=16);outer.pack(fill='both',expand=True)
    ttk.Label(outer,text='训练与评估走势图',font=('Microsoft YaHei UI',17,'bold')).pack(anchor='w')
    ttk.Label(outer,text='上排：固定种子评估　下排：训练探索的滚动表现。点击数据点或悬停查看详情。',foreground='#66768a').pack(anchor='w',pady=(4,10))
    content=ttk.Frame(outer);content.pack(fill='both',expand=True);content.columnconfigure(0,weight=1);content.rowconfigure(0,weight=1);content.rowconfigure(1,weight=1)
    evaluation=ttk.Frame(content);evaluation.grid(row=0,column=0,sticky='nsew',pady=(0,10))
    a.canvases={}
    for col,(metric,title) in enumerate([('rate','评估通关率（%）'),('rents','评估平均交租次数')]):
        evaluation.columnconfigure(col,weight=1,uniform='evaluation');evaluation.rowconfigure(0,weight=1)
        box=ttk.LabelFrame(evaluation,text=title,padding=8);box.grid(row=0,column=col,sticky='nsew',padx=(0,6) if col==0 else (6,0))
        canvas=tk.Canvas(box,background='#fbfcfe',highlightthickness=0);canvas.pack(fill='both',expand=True);a.canvases[metric]=canvas
        canvas.bind('<Configure>',lambda e,m=metric:a.draw_chart(m))
        canvas.bind('<Button-1>',lambda e,m=metric:a.chart_click(m,e.x))
        canvas.bind('<Motion>',lambda e,m=metric,c=canvas:a.evaluation_chart_hover(c,m,e.x))
        canvas.bind('<Leave>',lambda e,c=canvas:c.delete('hover'))
    training=ttk.Frame(content);training.grid(row=1,column=0,sticky='nsew');a.make_training_charts(training,'dashboard',220)
