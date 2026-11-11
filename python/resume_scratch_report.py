"""Create a local, browsable report for the continued scratch experiment."""
from __future__ import annotations
import argparse
from collections import Counter
import html
import json
import math
from pathlib import Path

from scratch_report import details_page
from reward_schemes import (PROGRESSIVE_SCALE, DEFAULT_PROGRESSIVE_RENTS, DEFAULT_PROGRESSIVE_LOSS,
                            DEFAULT_PROGRESSIVE_WIN, validate_progressive_rents, validate_progressive_outcomes)
from seed_policy import FIXED_EVAL_START
from item_toggle_policy import toggle_scope_description

ROOT = Path(__file__).resolve().parent.parent



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('directory', type=Path)
    args = ap.parse_args()
    root = args.directory
    result = json.loads((root/'summary.json').read_text(encoding='utf-8'))
    reward_mode = result['manifest'].get('reward_mode', 'fixed')
    if reward_mode == 'progressive':
        meta = result['manifest']
        rents = validate_progressive_rents(meta.get('progressive_rents', DEFAULT_PROGRESSIVE_RENTS))
        loss, win = validate_progressive_outcomes(meta.get('progressive_loss', DEFAULT_PROGRESSIVE_LOSS),
                                                  meta.get('progressive_win', DEFAULT_PROGRESSIVE_WIN))
        rent_text = '、'.join(f'{v:.3f}' for v in rents)
        reward_description = (f'本轮采用方案二：前 12 次成功交租的原始分数依次为 {rent_text}；'
                              f'正常失败 {loss:+g}，击败房东 {win:+g}；所有分数（包括开关奖赏）统一除以 20 后送给模型。'
                              '步数上限截断不额外扣失败分。该方案兼顾生存进度和通关，不保证只优化胜率。')
    else:
        reward_description = '此历史记录使用早期奖励配置。'
    toggle = float(result['manifest'].get('toggle_reward', -0.05))
    toggle_description = (f'手动开关物品或精华每次原始 {toggle:+g}，实际计入 {toggle / PROGRESSIVE_SCALE:+g}。'
                          if reward_mode == 'progressive' else f'手动开关物品或精华每次 {toggle:+g}。')
    toggle_description += toggle_scope_description(result['manifest'])
    prior_games = int(result['manifest']['previous_training_games'])
    added_games = int(result['training_games'])
    baseline = result['manifest'].get('training_route') == 'baseline'
    grouped = result['manifest'].get('training_route') == 'grouped'
    labels = {'random_initial': ('分组起点（从零训练 19000 局）' if grouped else
                                 '基线初始模型（0 局 PPO）' if baseline else '随机初始（0 局）'),
              'before_continuation': f'续训前（{prior_games} 局）',
              'middle': f'续训中（{prior_games + added_games // 2} 局）',
              'after_continuation': f'续训后（{prior_games + added_games} 局）'}
    upgraded = bool(result['manifest'].get('candidate_policy_upgraded'))
    if upgraded:
        labels['before_continuation'] = f'候选表达升级后、续训前（{prior_games} 局）'
    assoc = json.loads((ROOT/'data'/'association_rules.json').read_text(encoding='utf-8'))['names']
    names = {key: value.get('display', key) for key, value in assoc.items()}
    details = root/'details'
    details.mkdir(exist_ok=True)
    rows_html = []
    all_rows = {}
    for evaluation in result['evaluations']:
        tag = evaluation['label']
        rows = [json.loads(line) for line in (root/f'{tag}_games.jsonl').read_text(encoding='utf-8').splitlines()]
        all_rows[tag] = {row['seed']: row for row in rows}
        path = details/f'{tag}.html'
        details_page(path, labels[tag], rows, names)
        page = path.read_text(encoding='utf-8')
        page = page.replace("reroll:'重掷'}", "reroll:'重掷',destroy_item:'消除物品'}")
        page = page.replace("acts[d.action]||d.action", "d.action==='toggle_item'?(d.managed_target&&d.managed_target.disabled?'启用物品或精华':'禁用物品或精华'):(acts[d.action]||d.action)")
        path.write_text(page, encoding='utf-8')
        actions = evaluation['actions']
        cells=[labels[tag], evaluation['games'], evaluation['wins'], f"{100*evaluation['win_rate']:.2f}%",
               f"{evaluation['avg_rents']:.3f}", actions.get('remove',0), actions.get('reroll',0),
               actions.get('toggle_item',0), actions.get('destroy_item',0), evaluation['truncated_games']]
        rows_html.append('<tr>'+''.join('<td>'+html.escape(str(x))+'</td>' for x in cells)+
                         f'<td><a href="details/{tag}.html">查看每局</a></td></tr>')
    before, after = all_rows['before_continuation'], all_rows['after_continuation']
    seeds = sorted(set(before)&set(after))
    changes = Counter('更好' if after[s]['rents_paid'] > before[s]['rents_paid'] else
                      '更低' if after[s]['rents_paid'] < before[s]['rents_paid'] else '相同' for s in seeds)
    deltas = [after[s]['rents_paid']-before[s]['rents_paid'] for s in seeds]
    mean = sum(deltas)/len(deltas) if deltas else 0
    variance = sum((d-mean)**2 for d in deltas)/(len(deltas)-1) if len(deltas)>1 else 0
    se = math.sqrt(variance/len(deltas)) if deltas else 0
    comparison = {'paired_games':len(seeds),'avg_rent_difference':mean,'approximate_95pct_interval':[mean-1.96*se,mean+1.96*se],
                  'rent_improved':changes['更好'],'rent_unchanged':changes['相同'],'rent_declined':changes['更低']}
    (root/'paired_comparison.json').write_text(json.dumps(comparison,ensure_ascii=False,indent=2),encoding='utf-8')
    page='''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>进阶20从零训练续训报告</title><style>
body{font:15px system-ui,sans-serif;background:#f4f7fa;color:#172737;max-width:1250px;margin:30px auto;padding:0 20px}
h1{font-size:24px}.box{background:white;padding:20px;border-radius:12px;margin:16px 0;overflow:auto}table{border-collapse:collapse;width:100%}th,td{padding:9px;text-align:left;border-bottom:1px solid #dce5e7;white-space:nowrap}th{background:#eff5f5}a{color:#106381}.muted{color:#65777e;line-height:1.8}</style></head><body>
<h1>进阶 20：从零训练续训报告</h1><div class="box"><p>沿用从零训练 __PRIOR__ 局后的模型和优化器，新增训练 __ADDED__ 局；训练不用专家评分。全部模型使用当前 Rust 版本，在相同的 __GAMES__ 个未用于训练的种子上做确定性评估。</p>
<p class="muted">__REWARD_DESCRIPTION__ __TOGGLE_DESCRIPTION__ 非零开关奖赏会改变训练目标，手动消除物品无额外惩罚。此前轮次可能使用不同的奖励。表中局数指模型累计训练局数；统计的动作次数是全部评估局合计。</p>
<table><thead><tr><th>模型</th><th>局数</th><th>胜场</th><th>胜率</th><th>平均交租</th><th>删除</th><th>重掷</th><th>开关</th><th>消除物品</th><th>未完成</th><th>详情</th></tr></thead><tbody>__ROWS__</tbody></table>
<p class="muted">续训前后同种子比较：交租次数增加 __IMPROVED__ 局，相同 __SAME__ 局，减少 __DECLINED__ 局；平均变化 __DELTA__ 次，近似 95% 区间 __CI__。平均交租是过程指标，不能代替胜率。</p>
<p class="muted">每局详情包括候选、选取、跳过、重掷、删符号及物品操作，支持按种子或交租次数排序。不同批次可能使用不同种子或模拟器版本，不宜直接比较；本报告每个阶段均在当前版本、同一批种子上评估。</p></div></body></html>'''
    replacements={'__PRIOR__':str(prior_games),'__ADDED__':str(added_games),'__ROWS__':'\n'.join(rows_html),'__GAMES__':str(len(seeds)),'__IMPROVED__':str(changes['更好']),
                  '__REWARD_DESCRIPTION__':html.escape(reward_description),
                  '__TOGGLE_DESCRIPTION__':html.escape(toggle_description),
                  '__SAME__':str(changes['相同']),'__DECLINED__':str(changes['更低']),
                  '__DELTA__':f'{mean:+.3f}','__CI__':f'[{mean-1.96*se:+.3f}, {mean+1.96*se:+.3f}]'}
    if baseline:
        page = page.replace('进阶20从零训练续训报告', '进阶20基线模型训练报告')
        page = page.replace('进阶 20：从零训练续训报告', '进阶 20：基线模型训练报告')
        page = page.replace('沿用从零训练 __PRIOR__ 局后的模型和优化器，新增训练 __ADDED__ 局；训练不用专家评分。',
                            '以模仿手工策略的模型为起点，累计 PPO 训练 __PRIOR__ 局后，本轮新增 __ADDED__ 局；训练使用手工策略候选评分。历史约 22% 的评估结果来自旧版规则，当前表现以本次评估为准。')
    if grouped:
        excluded = int(result.get('grouped_training', {}).get('truncated_groups_excluded', 0))
        page = page.replace('进阶20从零训练续训报告', '进阶20同种子分组训练报告')
        page = page.replace('进阶 20：从零训练续训报告', '进阶 20：同种子分组训练报告')
        page = page.replace('沿用从零训练 __PRIOR__ 局后的模型和优化器，新增训练 __ADDED__ 局；训练不用专家评分。',
                            f'以从零路线训练 19000 局的模型为起点，分组路线之前训练 __PRIOR__ 局，本轮新增 __ADDED__ 局。每个种子玩 4 局，使用其余 3 局作为比较；不使用价值网络和专家评分。因达到步数上限而排除 {excluded} 组。首次分组训练使用新的优化器。')
    if result['manifest'].get('training_route') == 'fresh_rules':
        page = page.replace('进阶20从零训练续训报告', '进阶20新规则从零训练报告')
        page = page.replace('进阶 20：从零训练续训报告', '进阶 20：新规则从零训练报告')
        version = html.escape(result['manifest'].get('rules_version', '未记录'))
        page = page.replace('沿用从零训练 __PRIOR__ 局后的模型和优化器，新增训练 __ADDED__ 局；训练不用专家评分。',
                            f'本实验从全新模型与优化器开始，不继承其他路线的模型；此前累计 __PRIOR__ 局，本轮新增 __ADDED__ 局。游戏规则版本：{version}。训练不使用专家评分。')
    if int(result['manifest'].get('eval_seed_start', -1)) == FIXED_EVAL_START:
        page = page.replace('不同批次可能使用不同种子或模拟器版本，不宜直接比较；',
                            f'本轮使用从 {FIXED_EVAL_START} 开始的固定评估种子；旧轮次可能使用不同种子，模拟器版本也可能不同；')
    if result['manifest'].get('candidate_rules_version'):
        note = ('本轮使用新版候选身份与游戏规则特征，所有候选共用评分；没有新增库存数量限制或惩罚。')
        if result['manifest'].get('candidate_capacity_upgraded'):
            note += '本轮已扩充信用卡等效果的候选容量，完整保留已学参数和优化器；续训前评估使用扩容后的 model_upgraded.zip。'
        note += ('物品状态输入为完整状态 v1，包含存款、取出金额及触发进度。'
                 if result['manifest'].get('item_state_version') else '物品状态输入为旧版。')
        if result['manifest'].get('item_state_upgraded'):
            note += '本轮首次补齐物品状态，原权重与优化器完整保留；新增输入初始影响为零，续训前评估使用 model_upgraded.zip。'
        if result['manifest'].get('rules_upgraded'):
            note += '本轮已修正非法重掷：只有符号候选可重掷，旧阶段统计保留原规则。'
        if upgraded:
            page = page.replace('沿用从零训练 __PRIOR__ 局后的模型和优化器，', '沿用从零训练 __PRIOR__ 局后的局面与价值网络权重，候选表达升级并重建优化器，')
            note += '首次升级的续训前评估使用 model_upgraded.zip；升级前的原始模型仍在原文件夹，结果不可解释为完全相同架构下的续训。'
        page = page.replace('<table><thead>', '<p class="muted">' + note + '</p><table><thead>', 1)
    for key,value in replacements.items():page=page.replace(key,value)
    (root/'报告.html').write_text(page,encoding='utf-8')
    print(root/'报告.html')
    print(json.dumps(comparison,ensure_ascii=False))


if __name__=='__main__':main()
