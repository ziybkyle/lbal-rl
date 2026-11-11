"""Evaluate a saved continuous-training checkpoint without modifying it."""
import argparse
import html
import json
import os
from pathlib import Path
from evaluation_history import register_evaluation

import torch
from seed_policy import FIXED_EVAL_START, MAX_EVAL_GAMES
from train_from_scratch import evaluate
from item_toggle_policy import saved_toggle_policy, toggle_scope_description
from reward_schemes import DEFAULT_PROGRESSIVE_LOSS, DEFAULT_PROGRESSIVE_WIN


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('--games', type=int, required=True)
    parser.add_argument('--threads', type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.games <= MAX_EVAL_GAMES or not 1 <= args.threads <= 64:
        parser.error('invalid evaluation games or thread count')
    root = args.directory
    summary = json.loads((root / 'summary.json').read_text(encoding='utf-8'))
    meta = summary['manifest']
    if meta.get('reward_mode') != 'progressive':
        parser.error('此模型使用早期奖励配置，请选择方案二的保存点。')
    baseline = meta.get('training_route') == 'baseline'
    torch.set_num_threads(args.threads)
    result = evaluate(root / 'model_final.zip', root, 'periodic_eval',
                      range(FIXED_EVAL_START, FIXED_EVAL_START + args.games),
                      scratch=not baseline, baseline_managed=baseline,
                      reward_mode=meta['reward_mode'], gamma=float(meta.get('gamma', 1.0)),
                      toggle_reward=float(meta.get('toggle_reward', 0)),
                      allow_item_toggle=bool(meta.get('allow_item_toggle', True)),
                      **saved_toggle_policy(meta),
                      progressive_rents=meta.get('progressive_rents'),
                      progressive_loss=meta.get('progressive_loss', DEFAULT_PROGRESSIVE_LOSS),
                      progressive_win=meta.get('progressive_win', DEFAULT_PROGRESSIVE_WIN))
    result['seed_start'] = FIXED_EVAL_START
    result['training_games_total'] = int(meta.get('previous_training_games', 0)) + int(summary['training_games'])
    report_tmp = root / '.periodic_eval_report.html.tmp'
    with report_tmp.open('w', encoding='utf-8') as report:
        report.write('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>定期评估报告</title>'
                     '<style>body{font:15px system-ui;max-width:1100px;margin:30px auto;padding:0 20px}'
                     'table{border-collapse:collapse;width:100%}th,td{padding:8px;border-bottom:1px solid #ddd;text-align:left}'
                     'th{background:#eef4f7;position:sticky;top:0}</style><h1>定期评估报告</h1>')
        rate = f"{result['win_rate'] * 100:.2f}%" if result['win_rate'] is not None else '无有效局'
        report.write(f"<p>累计训练 {result['training_games_total']} 局后的模型；固定评估 {result['games']} 局，"
                     f"有效局 {result['valid_games']} 局，通关率 {rate}，平均交租 {result['avg_rents']:.3f} 次。"
                     '评估不更新模型；与训练中的探索结果分开统计。</p>')
        report.write('<p>' + html.escape(toggle_scope_description(meta)) + '</p>')
        report.write('<table><thead><tr><th>种子</th><th>结果</th><th>交租</th><th>重掷</th>'
                     '<th>删除符号</th><th>开关物品</th><th>消除物品</th></tr></thead><tbody>')
        with (root / 'periodic_eval_games.jsonl').open(encoding='utf-8') as source:
            for line in source:
                game = json.loads(line)
                actions = game.get('actions', {})
                outcome = '截断' if game.get('truncated') else '通关' if game.get('won') else '失败'
                values = (game['seed'], outcome, game['rents_paid'], actions.get('reroll', 0),
                          actions.get('remove', 0), actions.get('toggle_item', 0), actions.get('destroy_item', 0))
                report.write('<tr>' + ''.join('<td>' + html.escape(str(v)) + '</td>' for v in values) + '</tr>')
        report.write('</tbody></table></html>')
    os.replace(report_tmp, root / '定期评估报告.html')
    training_report = root / '报告.html'
    if training_report.is_file():
        text = training_report.read_text(encoding='utf-8')
        text = text.replace('本阶段没有进行评估。', '本阶段保存后已单独进行定期评估，<a href="定期评估报告.html">查看评估报告</a>。')
        updated_report = root / '.training_report.html.tmp'
        updated_report.write_text(text, encoding='utf-8')
        os.replace(updated_report, training_report)
    temporary = root / '.periodic_eval.json.tmp'
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, root / 'periodic_eval.json')
    register_evaluation(root, result, root/'periodic_eval_games.jsonl', root/'定期评估报告.html', '累计定期评估')
    print('  periodic evaluation saved', flush=True)


if __name__ == '__main__':
    main()
