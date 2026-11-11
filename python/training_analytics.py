"""Read existing games into the original sortable analysis template; never play new games."""
from collections import Counter
from datetime import datetime
import gzip
import html
import json
import re
from pathlib import Path

from evaluation_history import latest_evaluations
from candidate_stages import staged_decisions, stage_label, STAGE_BASIS

ROOT=Path(__file__).resolve().parents[1]
TEMPLATE=ROOT/'python/templates/policy_stats.html'


def lineage(directory):
    rows=[];seen=set();expected=None
    while directory:
        directory=Path(directory).resolve()
        if directory in seen:raise ValueError('训练历史存在循环')
        seen.add(directory)
        data=json.loads((directory/'summary.json').read_text(encoding='utf8'))
        meta=data['manifest'];identity=(meta.get('training_route'),meta.get('experiment_id'))
        if expected is None:expected=identity
        if identity!=expected:raise ValueError('不能合并不同路线或实验的数据')
        rows.append((directory,data))
        directory=meta.get('previous_run')
    return list(reversed(rows))


def read_jsonl(path,allow_incomplete=False):
    opener=gzip.open if str(path).endswith('.gz') else open
    try:
        with opener(path,'rt',encoding='utf8') as source:
            for line in source:
                if allow_incomplete and not line.endswith('\n'):break
                if line.strip():yield json.loads(line)
    except EOFError:
        if not allow_incomplete:raise


def analysis_history(directory,live=None):
    history=lineage(directory) if (Path(directory)/'summary.json').is_file() else []
    if live and Path(live).resolve() not in {p for p,_ in history}:
        live=Path(live).resolve();manifest=live/'manifest.json'
        if manifest.is_file() and (live/'training_games.jsonl').is_file():
            meta=json.loads(manifest.read_text(encoding='utf8'))
            if history:
                if Path(meta.get('previous_run') or '').resolve()!=history[-1][0]:raise ValueError('运行阶段不属于所选训练分支。')
                last_meta=history[-1][1]['manifest']
                if (meta.get('training_route'),meta.get('experiment_id')) != (last_meta.get('training_route'),last_meta.get('experiment_id')):
                    raise ValueError('运行阶段不能混入另一实验。')
            elif meta.get('previous_run') or (meta.get('previous_training_games',0) and not
                    (meta.get('branch_id') and meta.get('initialization')=='checkpoint_branch'
                     and meta.get('previous_training_games')==meta.get('origin_training_games'))):
                raise ValueError('找不到运行阶段的历史起点。')
            count=sum(1 for _ in read_jsonl(live/'training_games.jsonl',True))
            if count:history.append((live,dict(manifest=meta,training_games=count,live=True)))
    if not history:raise ValueError('尚无已完成且已写入记录的训练对局。')
    return history


def training_records(history,first,last):
    for directory,data in history:
        offset=int(data['manifest'].get('previous_training_games',0))
        count=int(data['training_games'])
        if offset+count<first or offset+1>last:continue
        details_path=directory/'training_decisions.jsonl.gz'
        # Statistical tables need choices/ownership, not the complete state
        # journal. Retaining 1000 full trajectories here would unnecessarily
        # multiply memory use on machines with limited RAM.
        details={int(r['game']):{k:v for k,v in r.items() if k not in ('events','end_state')}
                 for r in read_jsonl(details_path,data.get('live',False))} if details_path.is_file() else {}
        for i,row in enumerate(read_jsonl(directory/'training_games.jsonl',data.get('live',False)),1):
            number=offset+i
            if first<=number<=last:
                yield dict(row,**{k:v for k,v in details.get(i,{}).items() if k not in row},absolute_game=number)


def latest_eval_source(history):
    # Choose one checkpoint's latest post-training evaluation; never pool different models.
    for directory,data in reversed(history):
        registered=latest_evaluations(directory).get('after_continuation')
        if registered:return registered
        periodic=directory/'periodic_eval.json'
        if periodic.is_file():
            return dict(result=json.loads(periodic.read_text(encoding='utf8')),
                        games_path=str(directory/'periodic_eval_games.jsonl'),source='定期评估')
        for result in reversed(data.get('evaluations',[])):
            if result['label'] in ('after_continuation','final'):
                return dict(result=result,games_path=str(directory/f'{result["label"]}_games.jsonl'),source='训练后评估')
    raise ValueError('本实验尚无续训后评估数据，请先进行一次单独评估，或等待累计评估。')


def aggregate_records(records,title):
    from gym_env import offer_action
    from report_policy_stats import StatsCollector, build_dataset
    col=StatsCollector();summary=[];detailed=truncated=missing_buckets=0
    for row in records:
        decisions=row.get('decisions');has_details=isinstance(decisions,list)
        detailed+=int(has_details)
        truncated+=int(bool(row.get('truncated')))
        removed=[d.get('choice') for d in decisions or [] if d.get('action')=='remove']
        summary.append({k:row.get(k) for k in ('absolute_game','game','seed','won','rents_paid','spins','actions','truncated')}|{'removed':removed})
        if row.get('truncated'):continue
        col.start_game()
        for decision, ordinal in staged_decisions(row):
            kind=decision.get('action');offers=decision.get('candidates') or []
            if kind not in ('take','skip','reroll') or not offers:continue
            action=1 if kind=='skip' else 2 if kind=='reroll' else decision.get('action_id')
            if action is None:
                target=next((o for o in offers if o['name']==decision.get('choice')),None)
                if target is None:continue
                action=offer_action(int(target['index']))
            if decision.get('rents_paid') is None:missing_buckets+=1
            col.note_first_offer(offers,action)
            col.record_decision(ordinal,offers,action)
        # "Ever owned" is distinct from merely seeing or selecting a candidate.
        owned=row.get('owned')
        if isinstance(owned,list):col.note_owned(dict(deck=[o for o in owned if o.get('kind')=='symbol'],items=[o for o in owned if o.get('kind')!='symbol']))
        for ordinal in range(1,int(row.get('rents_paid',0))+2):col.bucket_runs[ordinal].add(col.games)
        col.end_game(bool(row.get('won')),int(row.get('rents_paid',0)))
    if not summary:raise ValueError('所选范围没有已保存的训练或评估记录。')
    dataset=build_dataset(col,'policy',title)
    dataset['bucket_basis']=STAGE_BASIS
    dataset['bucket_shift']={}
    for bucket in dataset['buckets']:
        bucket['label']=stage_label(bucket['ordinal'])
    if missing_buckets:
        # Older records did not record paid rents at each decision. Do not invent stage data.
        dataset['buckets']=[]
        for row in dataset['rows']:row['buckets']={}
    owned_complete=all(isinstance(r.get('owned'),list) for r in records) if isinstance(records,list) else False
    if not owned_complete:
        dataset['acquire_basis']={k:'unavailable' for k in ('symbol','item','essence','other')}
        for row in dataset['rows']:
            row.update(acq_basis='unavailable',acq_runs=None,acq_wins=None,acq_win_rate=None,runs_seen=None)
    return dataset,summary,dict(games=len(summary),detailed_games=detailed,truncated=truncated,missing_bucket_decisions=missing_buckets,practice_games=sum(bool(r.get('training_practice_events')) for r in records))


def build_analysis(directory,mode,first=None,last=None,live=None):
    from report_policy_stats import write_html
    history=analysis_history(directory,live if mode!='evaluation' else None)
    total=int(history[-1][1]['manifest'].get('previous_training_games',0))+int(history[-1][1]['training_games'])
    lower=int(history[0][1]['manifest'].get('previous_training_games',0))+1
    if mode=='evaluation':
        source=latest_eval_source(history)
        records=list(read_jsonl(source['games_path']))[-1000:]
        title=f'最新评估 · {len(records)} 局'
        context=f'来自同一个模型的{source["source"]}；评估与训练探索分别统计。'
    else:
        first=max(lower,total-999) if mode=='training' else int(first)
        last=total if mode=='training' else int(last)
        if not lower<=first<=last<=total:raise ValueError(f'本分支局数范围应在 {lower}～{total} 之间，起点不能大于终点。')
        records=list(training_records(history,first,last))
        if len(records)!=last-first+1:raise ValueError('所选范围的训练记录不完整，无法准确统计。')
        title=f'训练探索 · 第 {first}～{last} 局'
        context='按照所选实验的累计局数统计，跨保存阶段合并；训练中包括已完成并写入日志的对局，进行中的对局不计入。'
    dataset,games,coverage=aggregate_records(records,title)
    out=Path(directory)/'analyses'/(datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'_'+mode)
    out.mkdir(parents=True)
    destination=out/'分析.html'
    write_html(TEMPLATE,destination,dataset)
    avg=sum(float(g.get('rents_paid') or 0) for g in games)/len(games)
    warning=(f'有候选明细的对局：{coverage["detailed_games"]}/{len(games)}。旧训练没有记录的候选不能补算；选择统计只反映实际有明细的数据。'
             if coverage['detailed_games']<len(games) else '所有所选对局均有候选明细。')
    if coverage['missing_bucket_decisions']:warning+=' 部分旧记录缺少决策时交租次数，已隐藏按交租阶段的拆分。'
    if any(not isinstance(r.get('owned'),list) for r in records):warning+=' 部分旧记录没有完整持有历史，已隐藏“持有后胜率”，避免误导。'
    if coverage['practice_games']:
        warning += f' 指定内容练习实际添加了 {coverage["practice_games"]}/{len(games)} 局，训练练习胜率不能当正常开局评估胜率。'
    banner=f'<div class="analysis-status">平均交租 <b>{avg:.3f}</b> 次 · 候选明细 {coverage["detailed_games"]}/{len(games)} 局 · 截断 {coverage["truncated"]} 局<details><summary>范围与数据说明</summary>{html.escape(context)}<br>{html.escape(warning)}<br>持有后胜率是相关性统计，不代表某个符号直接造成了胜率变化。</details></div>'
    rows=[]
    for game in games:
        values=(game.get('absolute_game') or game.get('game') or '—',game.get('seed'),
                '截断' if game.get('truncated') else '通关' if game.get('won') else '失败',
                game.get('rents_paid'),game.get('spins'),(game.get('actions') or {}).get('reroll',0),
                ' → '.join(game['removed']) or '—')
        rows.append('<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in values)+'</tr>')
    per_game='<section class="wrap"><h2>逐局结果与删除顺序</h2><div style="max-height:520px;overflow:auto"><table><thead><tr><th>累计局数/序号</th><th>种子</th><th>结果</th><th>成功交租</th><th>旋转</th><th>重掷</th><th>删除顺序</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table></div></section>'
    page=destination.read_text(encoding='utf8').replace('</header>','</header>'+banner,1).replace('</body>',per_game+'</body>',1)
    compact='''<style>
header{padding:10px 20px 5px}h1{font-size:17px}#sub,#dstool,.legend{display:none!important}
.wrap{padding:7px 20px 14px;max-width:none}.cards{display:flex;flex-wrap:wrap;gap:7px;margin:4px 0 7px}
.card{display:flex;align-items:baseline;gap:8px;padding:4px 10px;border-radius:6px}.card .v{font-size:16px;margin:0}.card .k{font-size:11px}
.toolbar{margin-bottom:5px;gap:5px}.chip{padding:3px 9px;font-size:12px}.notebox{display:none}.khead{margin:5px 0}
.toolbar.ktabs,.ktabs + .toolbar{display:inline-flex;vertical-align:middle;margin-right:14px}.card:nth-child(n+4){display:none}
@media(min-width:1000px){#cards{position:absolute;right:20px;top:8px;margin:0}}
.tw{height:calc(100vh - 225px);max-height:none;min-height:260px}.analysis-status{margin:4px 20px;font-size:12px;color:var(--dim)}
.analysis-status details{display:inline-block;margin-left:12px;vertical-align:top}.analysis-status details[open]{display:block;margin:5px 0}
</style>'''
    page=page.replace('</head>',compact+'</head>',1)
    page=re.sub(r'<h1>.*?</h1>','<h1>'+html.escape(title)+'</h1>',page,count=1)
    note='交租成功后的符号、物品和精华候选（含重掷及连续精华）归入刚结束的区间；后续旋转候选归入新区间。每个交租阶段统计该阶段实际出现的候选及选择；重掷前后的候选分别计数。首次选择率是每局第一次见到该候选时选择它的比例。持有胜率是相关性统计，不用于证明因果。'
    replacement='function renderNotes() { for (const id of ["note","note-item","note-essence"]) document.getElementById(id).textContent='+json.dumps(note,ensure_ascii=False)+'; }\n'
    page=re.sub(r'function renderNotes\(\)\s*\{.*?\n\}\n',lambda m:replacement,page,count=1,flags=re.S)
    translations=json.loads((ROOT/'data/association_rules.json').read_text(encoding='utf8'))['names']
    display={key:value.get('display',key) for key,value in translations.items()}
    page=page.replace('const DATASETS = ', 'const DISPLAY_NAMES = '+json.dumps(display,ensure_ascii=False)+';\nconst DATASETS = ',1)
    page=page.replace('${esc(r.name)}','${esc(DISPLAY_NAMES[r.name]||r.name)}')
    page=page.replace('${r.acq_runs.toLocaleString()}',"${r.acq_runs==null?'—':r.acq_runs.toLocaleString()}")
    page=page.replace('r.name.toLowerCase().includes(q)',"(r.name+' '+(DISPLAY_NAMES[r.name]||'')).toLowerCase().includes(q)")
    page=page.replace('i + (cfg.shift || 0)', 'i')
    page=page.replace('const shiftNote = cfg.shift', 'const shiftNote = false')
    page=page.replace('String(b.ordinal), String(b.rent)', 'b.label, String(b.rent)')
    page=page.replace('let sortK = "runs_offered"','let sortK = "first_pick_rate"',1)
    destination.write_text(page,encoding='utf8')
    (out/'metadata.json').write_text(json.dumps(dict(title=title,mode=mode,coverage=coverage,source_run=str(Path(directory).resolve()),first=first,last=last),ensure_ascii=False,indent=2),encoding='utf8')
    return destination


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('directory',type=Path)
    parser.add_argument('--mode',choices=('training','evaluation','custom'),required=True)
    parser.add_argument('--first',type=int);parser.add_argument('--last',type=int)
    parser.add_argument('--live',type=Path)
    args=parser.parse_args()
    print(json.dumps(dict(path=str(build_analysis(args.directory,args.mode,args.first,args.last,args.live))),ensure_ascii=False),flush=True)
