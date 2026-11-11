"""Generate a simple per-game training report without running evaluations."""
import argparse
import html
import json
from pathlib import Path
from item_toggle_policy import toggle_scope_description


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    root = parser.parse_args().directory
    summary = json.loads((root / 'summary.json').read_text(encoding='utf-8'))
    records = [json.loads(line) for line in (root / 'training_games.jsonl').read_text(encoding='utf-8').splitlines()]
    rows = []
    has_shaping = any('training_shaping_raw' in game for game in records)
    for game in records:
        actions = game.get('actions', {})
        cells = [game.get('game'), game.get('seed'), '胜利' if game.get('won') else '截断' if game.get('truncated') else '失败',
                 game.get('rents_paid'), game.get('spins'), game.get('reward'),
                 actions.get('reroll', 0), actions.get('remove', 0), actions.get('toggle_item', 0)]
        if has_shaping:
            shaping = game.get('training_shaping_raw', {})
            cells.append(f'{shaping.get("deck_overflow", 0):+.4f}')
        rows.append('<tr>' + ''.join('<td>' + html.escape(str(value)) + '</td>' for value in cells) + '</tr>')
    wins = sum(bool(g.get('won')) and not g.get('truncated') for g in records)
    valid = sum(not g.get('truncated') for g in records)
    rents = sum(int(g.get('rents_paid', 0)) for g in records) / len(records) if records else 0
    rate = f'{100 * wins / valid:.2f}%' if valid else '无有效局'
    candidate_note = ('本阶段使用新版候选身份与规则表达，所有候选共用评分。' if summary['manifest'].get('candidate_rules_version') else '本阶段使用旧版候选表达。')
    if summary['manifest'].get('training_route') == 'fresh_rules':
        candidate_note = ('当前规则 PPO 训练。规则版本 '
                          + html.escape(summary['manifest'].get('rules_version', '未记录')) + '。' + candidate_note)
    if summary['manifest'].get('candidate_policy_upgraded'):
        candidate_note += '首次升级已保留局面与价值网络权重，并重建优化器；升级后的训练起点另存为 model_upgraded.zip，升级前模型仍保留在原文件夹。'
    if summary['manifest'].get('candidate_capacity_upgraded'):
        candidate_note += '本轮扩充了信用卡等效果的候选容量，已学权重和优化器状态完整保留，扩容起点另存为 model_upgraded.zip。'
    item_state = summary['manifest'].get('item_state_version')
    candidate_note += (f'物品状态输入 {html.escape(item_state)}，含存款、计数、目标与剩余进度。'
                       if item_state else '物品状态输入为早期格式。')
    if summary['manifest'].get('item_state_upgraded'):
        candidate_note += '本轮首次补齐物品状态；原权重与优化器完整保留，新增输入初始影响为零。升级起点另存 model_upgraded.zip，原模型保留。'
    if summary['manifest'].get('rules_upgraded'):
        candidate_note += '本阶段同时修正重掷规则：仅符号候选可重掷；历史统计保留原规则。'
    candidate_note += html.escape(toggle_scope_description(summary['manifest']))
    from training_shaping import label as shaping_label
    shaping = shaping_label(summary['manifest'].get('training_shaping'))
    if shaping:
        candidate_note += '<br>额外训练奖励：' + html.escape(shaping) + '。下表的控牌扣分为原始分数，方案二送入模型前统一除以 20。正式评估不加入这些分数。'
    shaping_headers = '<th>控牌扣分（原始）</th>' if has_shaping else ''
    page = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>持续训练记录</title>
<style>body{{font:15px system-ui;background:#f4f7fa;color:#172737;max-width:1100px;margin:30px auto;padding:0 20px}}
table{{border-collapse:collapse;width:100%;background:white}}th,td{{padding:8px;border-bottom:1px solid #ddd;text-align:left}}
th{{background:#eff5f5;position:sticky;top:0}}p{{line-height:1.7}}</style><h1>持续训练逐局记录</h1>
<p>本阶段 {len(records)} 局，平均交租 {rents:.3f} 次，训练通关率 {rate}（有效局 {valid} 局）。
训练过程中包含探索，与独立评估不同；本阶段没有进行评估。</p>
<p>本阶段累计训练起点 {summary['manifest']['previous_training_games']} 局；
逐局结果和种子保存在 training_games.jsonl，候选与操作明细保存在 training_decisions.jsonl.gz。</p><p>{candidate_note}</p>
<table><thead><tr><th>局数</th><th>种子</th><th>结果</th><th>交租次数</th><th>旋转次数</th><th>奖励</th><th>重掷</th><th>删除</th><th>开关</th>{shaping_headers}</tr></thead><tbody>{''.join(rows)}</tbody></table></html>'''
    (root / '报告.html').write_text(page, encoding='utf-8')


if __name__ == '__main__':
    main()
