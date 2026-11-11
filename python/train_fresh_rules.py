"""An isolated PPO lineage, initialized from random weights under current rules."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import time
import uuid

import torch
from sb3_contrib import MaskablePPO
from sb3_contrib.common.wrappers import ActionMasker
from stable_baselines3.common.vec_env import DummyVecEnv

from candidate_policy import RuleCandidatePolicy
from candidate_rules import LATEST_VERSION, catalog
from item_toggle_policy import toggle_policy_metadata
from resume_scratch import ContinueBudget, sha
from reward_schemes import (REWARD_NAMES, PROGRESSIVE_SCALE, DEFAULT_PROGRESSIVE_RENTS,
                            DEFAULT_PROGRESSIVE_LOSS, DEFAULT_PROGRESSIVE_WIN,
                            parse_progressive_rents, validate_progressive_outcomes)
from rust_env import lbal_py
from seed_policy import overlaps_fixed_evaluation
from train_from_scratch import evaluate
from train_ppo import LbalGym
from item_state_features import LATEST_VERSION as ITEM_STATE_VERSION, LATEST_SCHEMA_HASH as ITEM_STATE_SCHEMA
from training_options import validate_options, model_label, save_id
from simulator_compatibility import checkpoint_compatible, COMPATIBILITY_FILE
from published_checkpoint import is_published_origin, sha256 as checkpoint_sha256

ROOT = Path(__file__).resolve().parents[1]
ROUTE = 'fresh_rules'
EVALUATION_TAGS = ('random_initial', 'before_continuation', 'after_continuation')


def load_ancestors(previous, rules_version, simulator_sha256, route=ROUTE):
    """Reject foreign models and mixed-rule chains before loading any weights."""
    ancestors, seen = [], set()
    current = previous.resolve() if previous else None
    experiment = None
    while current is not None:
        if current in seen:
            raise ValueError('模型的历史记录存在循环。')
        seen.add(current)
        summary = json.loads((current / 'summary.json').read_text(encoding='utf-8'))
        meta = summary['manifest']
        if meta.get('training_route') != route or meta.get('expert_score_input') is not False:
            raise ValueError('只能续训当前路线、当前规则的完整身份与物品状态模型，不能混入其他路线。')
        if not checkpoint_compatible(meta.get('rules_version'), meta.get('simulator_sha256'), rules_version, simulator_sha256):
            raise ValueError('该模型使用的游戏规则与当前版本不同。请选择“全新起点”，原模型仍会保留。')
        if not ancestors and meta.get('reward_mode') != 'progressive':
            raise ValueError('此模型使用已停用的奖励方案，请选择方案二的保存点。')
        if not meta.get('experiment_id'):
            raise ValueError('缺少本路线的独立实验标识。')
        experiment = experiment or meta['experiment_id']
        if meta['experiment_id'] != experiment:
            raise ValueError('模型历史连接到了另一个独立实验。')
        records = [json.loads(line) for line in (current / 'training_games.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
        if len(records) != meta.get('train_games') or len(records) != summary.get('training_games'):
            raise ValueError(f'逐局记录与训练局数不一致：{current}')
        imported = is_published_origin(meta)
        model_file = current / 'model_final.zip'
        if (not records and not imported) or not model_file.is_file():
            raise ValueError(f'模型保存不完整：{current}')
        if imported and checkpoint_sha256(model_file) != meta.get('published_model_sha256'):
            raise ValueError('发布起点的模型文件已变更，请重新导入。')
        ancestors.append((current, summary, records))
        parent = meta.get('previous_run')
        current = Path(parent).resolve() if parent else None
    if ancestors:
        first = ancestors[-1][1]['manifest']
        branch = first.get('branch_id')
        published = is_published_origin(first)
        base = int(first.get('origin_training_games', 0)) if branch or published else 0
        initializations = ('behavior_cloned',) if route == 'baseline' else ('random_weights', 'checkpoint_branch', 'published_checkpoint')
        if first.get('initialization') not in initializations or first.get('previous_training_games') != base:
            raise ValueError('这条路线的起点必须是没有经过训练的全新模型。')
        initial = Path(first['initial_checkpoint']) if branch or published else ancestors[-1][0] / 'model_initial.zip'
        if not initial.is_file():
            raise ValueError('找不到该实验自己的初始模型。')
        total = base
        for _, summary, records in reversed(ancestors):
            if summary['manifest'].get('previous_training_games') != total:
                raise ValueError('模型历史的累计训练局数不一致。')
            total += len(records)
    return ancestors


def validate_seeds(train, evaluation, ancestors):
    if min(train.start, evaluation.start) < 0 or max(train.stop, evaluation.stop) > 2**31:
        raise ValueError('种子超出可用范围。')
    overlap = lambda a, b: a.start < b.stop and b.start < a.stop
    if overlap(train, evaluation) or overlaps_fixed_evaluation(train.start, train.stop):
        raise ValueError('训练种子不能与评估种子重合。')
    used = [int(row['seed']) for _, _, rows in ancestors for row in rows]
    if len(used) != len(set(used)) or any(seed in train or seed in evaluation for seed in used):
        raise ValueError('训练种子与历史训练重复，或被用于评估。')
    for _, summary, _ in ancestors:
        meta = summary['manifest']
        previous_eval = range(meta['eval_seed_start'], meta['eval_seed_start'] + meta['eval_games'])
        if any((train.start < hi and lo < train.stop) or (evaluation.start < hi and lo < evaluation.stop)
               for lo, hi in meta.get('origin_seed_ranges', [])):
            raise ValueError('训练或评估种子与模型起点的历史训练重复。')
        if overlap(train, previous_eval):
            raise ValueError('训练种子与历史评估重复。')


def main(route=ROUTE, initial_model=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--previous', type=Path)
    parser.add_argument('--training-options', help='explicit JSON snapshot: fixed-strength assistance and optional practice')
    parser.add_argument('--games', type=int, default=1000)
    parser.add_argument('--eval-games', type=int, default=1000)
    parser.add_argument('--train-seed-start', type=int, default=2_000_000)
    parser.add_argument('--eval-seed-start', type=int, default=1_900_000_000)
    parser.add_argument('--sampling-seed', type=int, default=85)
    parser.add_argument('--torch-threads', type=int, default=2)
    parser.add_argument('--reward-mode', choices=tuple(REWARD_NAMES), default='progressive')
    parser.add_argument('--toggle-reward', type=float, default=0.0)
    parser.add_argument('--no-toggle-actions', action='store_true')
    parser.add_argument('--limited-item-toggles', action='store_true', help='only toggle the player-selected items and essences')
    parser.add_argument('--candidate-rules', action='store_true', help='always enabled for this route')
    parser.add_argument('--expected-rules-version', help='refuse a different engine than the one shown in the UI')
    parser.add_argument('--progressive-rents', type=parse_progressive_rents, default=DEFAULT_PROGRESSIVE_RENTS)
    parser.add_argument('--progressive-loss', type=float, default=DEFAULT_PROGRESSIVE_LOSS)
    parser.add_argument('--progressive-win', type=float, default=DEFAULT_PROGRESSIVE_WIN)
    parser.add_argument('--no-eval', action='store_true')
    parser.add_argument('--stop-file', type=Path, help='UI stop request, checked between saved stages')
    parser.add_argument('--full-history', action='store_true',
                        help='diagnostic only: preserve complete per-action states; normal training keeps compact logs')
    args = parser.parse_args()
    try:
        validate_progressive_outcomes(args.progressive_loss, args.progressive_win)
        if not (1 if args.no_eval else 2) <= args.games <= 1_000_000 or not 1 <= args.eval_games <= 100_000:
            raise ValueError('训练局数应为 2–1000000，评估局数应为 1–100000。')
        if not 1 <= args.torch_threads <= 64:
            raise ValueError('计算线程应为 1–64。')
        if not math.isfinite(args.toggle_reward) or abs(args.toggle_reward) > 1:
            raise ValueError('开关奖赏应为 −1 到 1 的有限数字。')
        rules_version = getattr(lbal_py, 'RULES_VERSION', None)
        if not rules_version:
            raise ValueError('当前 Rust 模块缺少规则版本，请先更新模块。')
        if args.expected_rules_version and args.expected_rules_version != rules_version:
            raise ValueError('Rust 规则与界面所选版本不一致，请重新打开训练界面。')
        simulator = Path(lbal_py.__file__).resolve()
        simulator_hash = sha(simulator)
        ancestors = load_ancestors(args.previous, rules_version, simulator_hash, route)
        initial_meta = json.loads((initial_model.parent/'summary.json').read_text(encoding='utf8'))['manifest'] if initial_model else None
        if initial_meta and route != 'baseline':
            raise ValueError('手工模仿起点只能用于独立的手工模仿路线。')
        options = validate_options(args.training_options)
        if options and options['practice'] and not hasattr(lbal_py.Env, 'training_grant'):
            raise ValueError('当前 Rust 模块未提供指定内容练习接口。请更新后重开训练面板。')
        train = range(args.train_seed_start, args.train_seed_start + args.games + 2)
        evaluation = range(args.eval_seed_start, args.eval_seed_start + args.eval_games)
        validate_seeds(train, evaluation, ancestors)
        if initial_meta and any((train.start < hi and lo < train.stop) or (evaluation.start < hi and lo < evaluation.stop)
                                for lo,hi in initial_meta.get('origin_seed_ranges', [])):
            raise ValueError('训练或评估种子不能与手工示范种子重合。')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    args.out.mkdir(parents=True, exist_ok=False)
    torch.set_num_threads(args.torch_threads)
    rules = catalog()
    from training_shaping import freeze_snapshot, enabled as shaping_enabled
    shaping_config = options['shaping']
    shaping_snapshot = freeze_snapshot(args.out, ancestors, shaping_config)
    if shaping_enabled(shaping_config):
        print(f'training shaping: status={shaping_snapshot["status"]}; '
              'evaluation rewards=unmodified', flush=True)
    def make_env():
        env = LbalGym(args.sampling_seed, seed_sequence=list(train), reward_mode=args.reward_mode,
                      reward_gamma=1.0, use_expert_scores=False, enable_management=True,
                      toggle_reward=args.toggle_reward, allow_item_toggle=not args.no_toggle_actions,
                      limited_item_toggles=args.limited_item_toggles,
                      progressive_rents=args.progressive_rents, progressive_loss=args.progressive_loss,
                      progressive_win=args.progressive_win, candidate_rules=LATEST_VERSION,
                      item_state_version=ITEM_STATE_VERSION,
                      training_practice=options['practice'],
                      training_shaping=shaping_config, shaping_snapshot=shaping_snapshot,
                      full_history=args.full_history)
        return ActionMasker(env, lambda e: e.action_masks())
    vec = DummyVecEnv([make_env])
    inherited_base = int(ancestors[-1][1]['manifest'].get('origin_training_games', 0)) if ancestors else 0
    previous_total = sum(len(records) for _, _, records in ancestors) + inherited_base
    if ancestors:
        checkpoint = args.previous / 'model_final.zip'
        initial = Path(ancestors[-1][1]['manifest'].get('initial_checkpoint') or ancestors[-1][0] / 'model_initial.zip')
        previous_meta = ancestors[0][1]['manifest']
        experiment_id = previous_meta['experiment_id']
        prior_updated = ancestors[0][1]['checkpoints'][-1]['updated_decisions']
        model = MaskablePPO.load(str(checkpoint), device='cpu')
        if getattr(model.policy, 'candidate_rules_version', None) != LATEST_VERSION or model.gamma != 1.0:
            raise ValueError('模型架构或折扣不属于当前新规则路线。')
        source_checkpoint = checkpoint
        item_state_upgraded = getattr(model.policy, 'item_state_version', None) != ITEM_STATE_VERSION
        if item_state_upgraded:
            from candidate_policy import upgrade_item_state_model
            model = upgrade_item_state_model(model, vec, args.sampling_seed)
            checkpoint = args.out / 'model_upgraded.zip'
            model.save(str(checkpoint))
            print('expanded rule/item-state inputs; learned weights and optimizer preserved', flush=True)
        else:
            model.set_env(vec)
        initial_seed = previous_meta['random_model_seed']
        model.set_random_seed(args.sampling_seed)
    elif initial_model is not None:
        experiment_id = uuid.uuid4().hex
        initial_seed = initial_meta['random_model_seed']
        prior_updated = 0
        source_checkpoint = initial_model
        model = MaskablePPO.load(str(initial_model), device='cpu')
        if getattr(model.policy, 'candidate_rules_version', None) != LATEST_VERSION or model.gamma != 1.0:
            raise ValueError('手工模仿起点缺少完整候选和物品状态。')
        item_state_upgraded = getattr(model.policy, 'item_state_version', None) != ITEM_STATE_VERSION
        if item_state_upgraded:
            from candidate_policy import upgrade_item_state_model
            model = upgrade_item_state_model(model, vec, args.sampling_seed)
        else:
            model.set_env(vec)
        initial = checkpoint = args.out / 'model_initial.zip'
        model.set_random_seed(args.sampling_seed)
        model.save(str(initial))
    else:
        experiment_id = uuid.uuid4().hex
        initial_seed = args.sampling_seed
        prior_updated = 0
        item_state_upgraded = False
        model = MaskablePPO(RuleCandidatePolicy, vec, seed=initial_seed, device='cpu', verbose=0,
                            n_steps=512, batch_size=256, n_epochs=10, gamma=1.0, gae_lambda=0.95,
                            learning_rate=0.0003, ent_coef=0.01, target_kl=0.05,
                            policy_kwargs={'net_arch': {'pi': [256, 256], 'vf': [256, 256]},
                                           'rule_schema_hash': rules.schema_hash,
                                           'item_state_schema_hash': ITEM_STATE_SCHEMA})
        initial = checkpoint = args.out / 'model_initial.zip'
        source_checkpoint = checkpoint
        model.save(str(initial))
    for legacy_key in ('exploration_prior', 'exploration_state', 'training_options'):
        model.__dict__.pop(legacy_key, None)
    start_steps = model.num_timesteps
    vec._reset_seeds()
    sources = [Path(__file__), ROOT/'python/resume_scratch.py', ROOT/'python/train_from_scratch.py',
               ROOT/'python/train_ppo.py', ROOT/'python/gym_env.py', ROOT/'python/rust_env.py',
               ROOT/'python/reward_schemes.py', ROOT/'python/seed_policy.py', ROOT/'python/item_toggle_policy.py',
               ROOT/'python/training_shaping.py', ROOT/'python/training_options.py',
               ROOT/'python/candidate_policy.py', ROOT/'python/candidate_rules.py', simulator,
               ROOT/'python/rule_context_features.py', ROOT/'python/episode_history.py',
               ROOT/'python/item_state_features.py', ROOT/'python/simulator_compatibility.py', COMPATIBILITY_FILE,
               ROOT/'python/published_checkpoint.py',
               checkpoint, initial, source_checkpoint, *rules.sources]
    if shaping_enabled(shaping_config):
        sources.append(args.out/'training_shaping_snapshot.json')
    manifest = {
        'purpose': 'isolated PPO lineage under current rules; no expert score input',
        'training_route': route, 'experiment_id': experiment_id, 'model_id': save_id(args.out), 'initialization': (ancestors[-1][1]['manifest'].get('initialization','random_weights') if ancestors else 'behavior_cloned' if initial_model else 'random_weights'),
        'fresh_start': not ancestors, 'previous_run': str(args.previous.resolve()) if args.previous else None,
        'checkpoint': str(checkpoint.resolve()), 'initial_checkpoint': str(initial.resolve()),
        'source_checkpoint': str(source_checkpoint.resolve()),
        'random_model_seed': initial_seed, 'previous_training_games': previous_total,
        'previous_sampled_decisions': start_steps, 'previous_updated_decisions': prior_updated,
        'train_games': args.games, 'train_seed_start': train.start,
        'eval_games': 0 if args.no_eval else args.eval_games, 'eval_seed_start': evaluation.start,
        'sampling_seed': args.sampling_seed, 'torch_threads': args.torch_threads,
        'reward_mode': args.reward_mode, 'gamma': 1.0, 'expert_score_input': False,
        'training_shaping': shaping_config,
        'training_shaping_status': shaping_snapshot['status'],
        'training_shaping_snapshot': str((args.out/'training_shaping_snapshot.json').resolve()) if shaping_enabled(shaping_config) else None,
        'training_shaping_snapshot_sha256': sha(args.out/'training_shaping_snapshot.json') if shaping_enabled(shaping_config) else None,
        'toggle_reward': args.toggle_reward, 'allow_item_toggle': not args.no_toggle_actions,
        **toggle_policy_metadata(args.limited_item_toggles),
        'reward_scale_divisor': PROGRESSIVE_SCALE,
        'progressive_rents': list(args.progressive_rents),
        'progressive_loss': args.progressive_loss,
        'progressive_win': args.progressive_win,
        'candidate_rules_version': LATEST_VERSION, 'candidate_rules_schema': rules.schema_hash,
        'candidate_offer_capacity': rules.max_offers, 'rules_version': rules_version,
        'item_state_version': ITEM_STATE_VERSION, 'item_state_schema': ITEM_STATE_SCHEMA,
        'item_state_upgraded': item_state_upgraded,
        'rule_context_version': getattr(lbal_py, 'RULE_CONTEXT_VERSION', None),
        'observation_upgrade_note': 'Append landlord clauses, per-symbol destroyed/removed pools and pre-spin candidate facts. Previous weights and optimizer are preserved; new feature projections start at zero.',
        'history_mode': 'full' if args.full_history else 'compact',
        'rules_upgraded': bool(ancestors and ancestors[0][1]['manifest'].get('rules_version') != rules_version),
        'rules_upgrade_note': 'Only symbol offers can reroll; item/essence rerolls are illegal. Historical statistics retain their original rule labels.',
        'simulator_sha256': simulator_hash, 'continuous_training': bool(args.no_eval),
        'evaluated_checkpoints': [] if args.no_eval else list(EVALUATION_TAGS),
        'files_sha256': {str(path.resolve()): sha(path) for path in sources},
    }
    if route == 'baseline':
        manifest.update(purpose='current-rules behavior cloning followed by independent PPO continuation',
                        baseline_initial_directory=str(initial_model.parent.resolve()),
                        origin_seed_ranges=initial_meta.get('origin_seed_ranges', []))
    manifest.update(training_options=options, normal_start_evaluation=True,
                    source_checkpoint_sha256=sha(source_checkpoint))
    manifest['model_label'] = model_label(manifest, previous_total + args.games)
    (args.out/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    # Finish the current saved stage. The UI checks its stop request before
    # starting the next one; stopping midway would discard the final rollout.
    cb = ContinueBudget(args.games, args.out, start_steps, prior_updated, train.start)
    print(f'{("starting current-rules imitation model" if initial_model else "starting fresh random model") if not ancestors else "continuing new-rules model"}: '
          f'{args.games} games; prior decisions={start_steps}; rules={rules_version}', flush=True)
    start = time.perf_counter()
    print(f'training options: {manifest["model_label"]}', flush=True)
    model.learn(total_timesteps=100_000_000, callback=cb, reset_num_timesteps=False)
    elapsed = time.perf_counter() - start
    final = args.out/'model_final.zip'
    model.save(str(final))
    vec.close()
    manifest['train_games'] = cb.finished
    manifest['model_label'] = model_label(manifest, previous_total + cb.finished)
    (args.out/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf8')
    (args.out/'模型说明.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf8')
    initial_stats = (ancestors[-1][1]['checkpoints'][0] if ancestors and
                     is_published_origin(ancestors[-1][1]['manifest']) else {})
    checkpoints = [
        {'tag': 'random_initial', 'path': str(initial), 'completed_games': 0,
         'sampled_decisions': initial_stats.get('sampled_decisions', 0),
         'updated_decisions': initial_stats.get('updated_decisions', 0)},
        {'tag': 'before_continuation', 'path': str(checkpoint), 'completed_games': 0,
         'sampled_decisions': start_steps, 'updated_decisions': prior_updated},
        *cb.checkpoints,
        {'tag': 'after_continuation', 'path': str(final), 'completed_games': cb.finished,
         'sampled_decisions': model.num_timesteps, 'updated_decisions': cb.updated_steps(model.num_timesteps)},
    ]
    evaluations = []
    for entry in checkpoints:
        if args.no_eval or entry['tag'] not in EVALUATION_TAGS:
            continue
        result = evaluate(Path(entry['path']), args.out, entry['tag'], evaluation, scratch=True,
                          reward_mode=args.reward_mode, gamma=1.0, toggle_reward=args.toggle_reward,
                          allow_item_toggle=not args.no_toggle_actions, progressive_rents=args.progressive_rents,
                          limited_item_toggles=args.limited_item_toggles,
                          progressive_loss=args.progressive_loss, progressive_win=args.progressive_win,
                          full_history=args.full_history)
        result.update({'additional_training_games': entry['completed_games'],
                       'sampled_training_decisions': entry['sampled_decisions'],
                       'updated_training_decisions': entry['updated_decisions']})
        evaluations.append(result)
    summary = {'manifest': manifest, 'training_seconds': elapsed, 'training_games': cb.finished,
               'training_wins': cb.wins, 'checkpoints': checkpoints, 'evaluations': evaluations,
               'note': 'Final incomplete rollout is recorded, not applied to PPO updates.'}
    (args.out/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    if route == 'baseline':
        from evaluation_history import latest_evaluations, register_evaluation
        assessed = latest_evaluations(initial_model.parent).get('after_continuation')
        if assessed:
            result = dict(assessed['result'], label='random_initial', model_path=str(initial.resolve()))
            register_evaluation(args.out, result, assessed['games_path'], assessed['report_path'], '手工模仿初始评估')
    print(json.dumps({'training_games': cb.finished, 'training_wins': cb.wins,
                      'training_seconds': elapsed}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
