"""Evaluate selected saved checkpoints without changing training history."""
import argparse
import html
import json
from pathlib import Path

import torch

from reward_schemes import DEFAULT_PROGRESSIVE_LOSS, DEFAULT_PROGRESSIVE_WIN
from scratch_report import details_page
from seed_policy import FIXED_EVAL_START, MAX_EVAL_GAMES
from train_from_scratch import evaluate
from item_toggle_policy import saved_toggle_policy, toggle_scope_description
from evaluation_history import register_evaluation

ROOT = Path(__file__).resolve().parent.parent
LABELS = {'random_initial': '初始模型', 'before_continuation': '所选阶段训练前模型',
          'after_continuation': '所选模型'}


def checkpoint_paths(summary, directory):
    entries = {item['tag']: Path(item['path']) for item in summary['checkpoints']}
    return {'random_initial': entries['random_initial'],
            'before_continuation': entries['before_continuation'],
            'after_continuation': directory / 'model_final.zip'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--games', type=int, required=True)
    parser.add_argument('--threads', type=int, default=2)
    parser.add_argument('--source', choices=('单独评估', '训练结束评估'), default='单独评估')
    parser.add_argument('--models', nargs='+', choices=tuple(LABELS), required=True)
    args = parser.parse_args()
    if not 1 <= args.games <= MAX_EVAL_GAMES or not 1 <= args.threads <= 64:
        parser.error('invalid evaluation games or thread count')
    summary = json.loads((args.directory / 'summary.json').read_text(encoding='utf-8'))
    meta = summary['manifest']
    paths = checkpoint_paths(summary, args.directory)
    for tag in args.models:
        if not paths[tag].is_file():
            raise FileNotFoundError(f'{LABELS[tag]}不存在：{paths[tag]}')
    baseline = meta.get('training_route') == 'baseline'
    torch.set_num_threads(args.threads)
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out / 'details').mkdir()
    translated = json.loads((ROOT / 'data' / 'association_rules.json').read_text(encoding='utf-8'))['names']
    names = {key: value.get('display', key) for key, value in translated.items()}
    results = []
    for tag in args.models:
        result = evaluate(paths[tag], args.out, tag,
                          range(FIXED_EVAL_START, FIXED_EVAL_START + args.games),
                          scratch=not baseline, baseline_managed=baseline,
                          reward_mode=meta['reward_mode'], gamma=float(meta.get('gamma', 1.0)),
                          toggle_reward=float(meta.get('toggle_reward', 0)),
                          allow_item_toggle=bool(meta.get('allow_item_toggle', True)),
                          **saved_toggle_policy(meta),
                          progressive_rents=meta.get('progressive_rents'),
                          progressive_loss=(meta.get('progressive_loss') if meta.get('progressive_loss') is not None else DEFAULT_PROGRESSIVE_LOSS),
                          progressive_win=(meta.get('progressive_win') if meta.get('progressive_win') is not None else DEFAULT_PROGRESSIVE_WIN))
        result['model_path'] = str(paths[tag].resolve())
        rows = [json.loads(line) for line in (args.out / f'{tag}_games.jsonl').read_text(encoding='utf-8').splitlines()]
        details_page(args.out / 'details' / f'{tag}.html', LABELS[tag], rows, names)
        results.append(result)
    data = {'source_run': str(args.directory.resolve()), 'seed_start': FIXED_EVAL_START,
            'evaluation_source': args.source,
            'allow_item_toggle': bool(meta.get('allow_item_toggle', True)), **saved_toggle_policy(meta),
            'settings_source': '所选模型的训练设置', 'evaluations': results}
    (args.out / 'summary.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    rows = []
    for result in results:
        rate = '—' if result['win_rate'] is None else f"{result['win_rate'] * 100:.2f}%"
        rows.append('<tr>' + ''.join(f'<td>{html.escape(str(v))}</td>' for v in
                    (LABELS[result['label']], result['games'], result['wins'], rate,
                     f"{result['avg_rents']:.3f}", result['truncated_games'])) +
                    f'<td><a href="details/{result["label"]}.html">查看逐局</a></td></tr>')
    upgrade_note = ('<p>所选阶段首次升级候选表达，阶段训练前模型为升级完成但尚未续训的 model_upgraded.zip。</p>'
                    if meta.get('candidate_policy_upgraded') else '')
    if meta.get('candidate_capacity_upgraded'):
        upgrade_note += '<p>所选阶段扩充了候选容量，训练前模型为保留全部已学权重及优化器状态的 model_upgraded.zip。</p>'
    if meta.get('item_state_upgraded'):
        upgrade_note += '<p>所选阶段首次补齐物品状态；阶段训练前模型是保留原权重与优化器的 model_upgraded.zip。初始模型仍使用自身原有输入。</p>'
    upgrade_note += '<p>只有符号候选可以重掷；所有模型均在当前安装的规则下评估。</p>'
    page = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>' + args.source + '报告</title>'
            '<style>body{font:15px system-ui;max-width:1100px;margin:30px auto;padding:0 20px}'
            'table{border-collapse:collapse;width:100%}th,td{padding:9px;border-bottom:1px solid #ddd;text-align:left}'
            'th{background:#eef4f7}</style><h1>' + args.source + '报告</h1>'
            f'<p>所选模型：{html.escape(str(args.directory))}。每个模型使用相同的 {args.games} 个固定种子，'
            '使用所选模型保存的环境设置。评估不会训练或修改模型。</p>'
            f'<p>{html.escape(toggle_scope_description(meta))}</p>'
            '<p>训练前模型指所选模型最近一个保存阶段开始时的模型。</p>' + upgrade_note +
            '<table><thead><tr><th>模型</th><th>局数</th><th>通关</th><th>通关率</th><th>平均交租</th>'
            '<th>未完成</th><th>详情</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></html>')
    (args.out / '报告.html').write_text(page, encoding='utf-8')
    for result in results:
        register_evaluation(args.directory, result, args.out/f'{result["label"]}_games.jsonl', args.out/'报告.html', args.source)
    print('  manual evaluation saved', flush=True)


if __name__ == '__main__':
    main()
