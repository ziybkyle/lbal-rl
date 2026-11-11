"""Continue the scratch PPO experiment, retaining old checkpoints and evaluating on held-out seeds."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import subprocess
import time

import torch
from sb3_contrib import MaskablePPO
from sb3_contrib.common.wrappers import ActionMasker
from stable_baselines3.common.vec_env import DummyVecEnv

from train_from_scratch import ScratchBudget, evaluate
from train_ppo import LbalGym
from gym_env import WIN_POTENTIAL_DATA, WIN_POTENTIAL_FILE
from item_toggle_policy import toggle_policy_metadata
from item_state_features import VERSION as ITEM_STATE_VERSION, SCHEMA_HASH as ITEM_STATE_SCHEMA
from reward_schemes import (PROGRESSIVE_SCALE, REWARD_NAMES, DEFAULT_PROGRESSIVE_RENTS, parse_progressive_rents,
                            DEFAULT_PROGRESSIVE_LOSS, DEFAULT_PROGRESSIVE_WIN, validate_progressive_outcomes)
from seed_policy import overlaps_fixed_evaluation

ROOT = Path(__file__).resolve().parent.parent
PREVIOUS = ROOT / 'runs' / 'scratch_resume_500_heldout1000_v2_20261107'
EVALUATION_TAGS = ('random_initial', 'before_continuation', 'after_continuation')


class ContinueBudget(ScratchBudget):
    def __init__(self, games: int, out: Path, start_steps: int, prior_updated: int, train_seed_start: int, stop_file: Path | None = None):
        super().__init__(games, out)
        self.start_steps = start_steps
        self.prior_updated = prior_updated
        self.train_seed_start = train_seed_start
        self.stop_file = stop_file

    def _on_step(self) -> bool:
        if self.locals['dones'][0]:
            actual = (self.locals['infos'][0].get('step_record') or {}).get('seed')
            expected = self.train_seed_start + self.finished
            if actual != expected:
                raise RuntimeError(f'training seed mismatch: expected {expected}, got {actual}')
        done = bool(self.locals['dones'][0])
        keep_going = super()._on_step()
        if done and self.stop_file and self.stop_file.exists():
            self.file.flush()
            print(f'  training {self.finished}/{self.games}; wins={self.wins}; steps={self.num_timesteps}; avg_rents={self.total_rents / self.finished:.6f}', flush=True)
            return False
        return keep_going

    def updated_steps(self, sampled: int) -> int:
        # The callback runs before an update, including at the last step of a
        # full rollout; stopping there also discards that complete rollout.
        rollout = self.model.n_steps * self.training_env.num_envs
        new_decisions = max(0, sampled - self.start_steps)
        return self.prior_updated + max(0, (new_decisions - 1) // rollout) * rollout

    def _save_checkpoint(self, tag: str) -> None:
        path = self.out / f'model_{tag}.zip'
        self.model.save(str(path))
        self.checkpoints.append({'tag': tag, 'path': str(path), 'completed_games': self.finished,
                                 'sampled_decisions': self.num_timesteps,
                                 'updated_decisions': self.updated_steps(self.num_timesteps)})


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--previous', type=Path, default=PREVIOUS)
    parser.add_argument('--games', type=int, default=500)
    parser.add_argument('--eval-games', type=int, default=1000)
    parser.add_argument('--train-seed-start', type=int, default=920_000)
    parser.add_argument('--eval-seed-start', type=int, default=1_500_000)
    parser.add_argument('--sampling-seed', type=int, default=85)
    parser.add_argument('--torch-threads', type=int, default=2)
    parser.add_argument('--toggle-reward', type=float, default=0.0)
    parser.add_argument('--no-toggle-actions', action='store_true', help='disallow manual item/essence toggles')
    parser.add_argument('--limited-item-toggles', action='store_true', help='only toggle the player-selected items and essences')
    parser.add_argument('--candidate-rules', action='store_true', help='upgrade a legacy scratch model to shared identity/rule candidate scoring')
    parser.add_argument('--reward-mode', choices=tuple(REWARD_NAMES), default='fixed')
    parser.add_argument('--progressive-rents', type=parse_progressive_rents, default=DEFAULT_PROGRESSIVE_RENTS)
    parser.add_argument('--progressive-loss', type=float, default=DEFAULT_PROGRESSIVE_LOSS)
    parser.add_argument('--progressive-win', type=float, default=DEFAULT_PROGRESSIVE_WIN)
    parser.add_argument('--no-eval', action='store_true')
    parser.add_argument('--stop-file', type=Path)
    args = parser.parse_args()
    try:
        validate_progressive_outcomes(args.progressive_loss, args.progressive_win)
    except ValueError as exc:
        parser.error(str(exc))
    if args.games < 2 or args.eval_games < 1:
        parser.error('need at least 2 training games and 1 evaluation seed')
    if not 1 <= args.torch_threads <= 64:
        parser.error('torch threads must be between 1 and 64')
    if not math.isfinite(args.toggle_reward) or abs(args.toggle_reward) > 1:
        parser.error('toggle reward must be a number between -1 and 1')
    previous_summary = json.loads((args.previous / 'summary.json').read_text(encoding='utf-8'))
    # Walk back every earlier training round and evaluation range. The very
    # first run used an unexpected seed, so inspect actual logs, not metadata.
    ancestors = []
    seen_paths = set()
    directory = args.previous.resolve()
    while True:
        if directory in seen_paths:
            parser.error('cycle in previous-run chain')
        seen_paths.add(directory)
        summary = json.loads((directory / 'summary.json').read_text(encoding='utf-8'))
        meta = summary['manifest']
        if (meta.get('reward_mode') not in REWARD_NAMES or meta.get('expert_score_input') is not False
                or meta.get('training_route') not in (None, 'scratch')):
            parser.error('every previous run must be a compatible scratch run without expert input')
        log_path = directory / 'training_games.jsonl'
        rows = [json.loads(line) for line in log_path.read_text(encoding='utf-8').splitlines()]
        expected_games = meta.get('train_games', meta.get('games_budget'))
        if len(rows) != expected_games:
            parser.error(f'training log does not match game budget: {directory}')
        ancestors.append((directory, meta, rows, log_path))
        if not meta.get('previous_run'):
            break
        directory = Path(meta['previous_run']).resolve()
    previous_games = ancestors[0][2]
    previous_games_path = ancestors[0][3]
    previous_training_seeds = {int(row['seed']) for _, _, rows, _ in ancestors for row in rows}
    previous_eval_ranges = [range(meta['eval_seed_start'], meta['eval_seed_start'] + meta['eval_games'])
                            for _, meta, _, _ in ancestors]
    prior_games_total = sum(len(rows) for _, _, rows, _ in ancestors)
    if len(previous_training_seeds) != prior_games_total:
        parser.error('earlier training rounds contain repeated seeds')
    train = range(args.train_seed_start, args.train_seed_start + args.games + 2)
    evaluation = range(args.eval_seed_start, args.eval_seed_start + args.eval_games)
    def overlap(a: range, b: range) -> bool:
        return a.start < b.stop and b.start < a.stop
    if min(train.start, evaluation.start) < 0 or max(train.stop, evaluation.stop) > 2**31:
        parser.error('seeds must fit the simulator 31-bit seed space')
    if (overlaps_fixed_evaluation(train.start, train.stop) or
            any(seed in train or seed in evaluation for seed in previous_training_seeds) or
            any(overlap(train, r) for r in previous_eval_ranges) or
            overlap(train, evaluation)):
        parser.error('training seeds must not overlap evaluation seeds or historical training seeds')
    checkpoint = args.previous / 'model_final.zip'
    initial = ancestors[-1][0] / 'model_initial.zip'
    prior_updated = previous_summary['checkpoints'][-1]['updated_decisions']
    args.out.mkdir(parents=True, exist_ok=False)
    torch.set_num_threads(args.torch_threads)
    source_model = MaskablePPO.load(str(checkpoint), device='cpu')
    already_rules = bool(getattr(source_model.policy, 'candidate_rules_version', None))
    use_candidate_rules = bool(args.candidate_rules or already_rules)
    upgraded = use_candidate_rules and not already_rules
    capacity_upgraded = False
    item_state_upgraded = use_candidate_rules and not getattr(source_model.policy, 'item_state_version', None)
    if already_rules:
        from candidate_rules import LATEST_VERSION
        capacity_upgraded = source_model.policy.candidate_rules_version != LATEST_VERSION
    def make_env():
        env = LbalGym(args.sampling_seed, seed_sequence=list(train), reward_mode=args.reward_mode,
                      reward_gamma=1.0, use_expert_scores=False, enable_management=True,
                      toggle_reward=args.toggle_reward, allow_item_toggle=not args.no_toggle_actions,
                      limited_item_toggles=args.limited_item_toggles,
                      progressive_rents=args.progressive_rents, progressive_loss=args.progressive_loss,
                      progressive_win=args.progressive_win, candidate_rules=use_candidate_rules,
                      item_state_version=ITEM_STATE_VERSION if use_candidate_rules else None)
        return ActionMasker(env, lambda e: e.action_masks())
    vec = DummyVecEnv([make_env])
    if upgraded:
        from candidate_policy import upgrade_model
        model = upgrade_model(source_model, vec, args.sampling_seed)
        model.save(str(args.out / 'model_upgraded.zip'))
    elif capacity_upgraded or item_state_upgraded:
        from candidate_policy import expand_rule_model
        model = expand_rule_model(source_model, vec, args.sampling_seed)
        model.save(str(args.out / 'model_upgraded.zip'))
        print('expanded candidate/item-state inputs; learned weights and optimizer preserved', flush=True)
    else:
        model = source_model
        model.set_env(vec)
    start_steps = model.num_timesteps
    if model.gamma != 1.0:
        raise ValueError('unexpected gamma in previous model')
    model.set_random_seed(args.sampling_seed)
    # SB3 also queues an environment seed; clear it so the explicit training
    # seed sequence, rather than the exploration seed, starts the first game.
    vec._reset_seeds()
    sources = [Path(__file__), ROOT/'python'/'train_from_scratch.py', ROOT/'python'/'train_ppo.py',
               ROOT/'python'/'scratch_policy.py', ROOT/'python'/'gym_env.py', ROOT/'python'/'rust_env.py',
               ROOT/'rust'/'python_bindings'/'lbal_py.pyd', ROOT/'data'/'baseline_priors.json',
               ROOT/'data'/'association_rules.json', WIN_POTENTIAL_FILE, ROOT/'python'/'reward_schemes.py', ROOT/'python'/'seed_policy.py',
               checkpoint, initial, previous_games_path, ROOT/'python'/'item_toggle_policy.py', ROOT/'python'/'item_state_features.py']
    if use_candidate_rules:
        from candidate_rules import catalog, LATEST_VERSION
        sources += [ROOT/'python'/'candidate_rules.py', ROOT/'python'/'candidate_policy.py', *catalog().sources]
    manifest = {'purpose': 'continue scratch PPO, no imitation or expert-score input',
                'previous_run': str(args.previous.resolve()), 'checkpoint': str(checkpoint.resolve()),
                'previous_training_games': prior_games_total, 'previous_actual_first_seed': previous_games[0]['seed'],
                'previous_training_seeds_checked_from_log': len(previous_training_seeds),
                'previous_eval_ranges': [[r.start, r.stop - 1] for r in previous_eval_ranges],
                'previous_updated_decisions': prior_updated,
                'previous_sampled_decisions': start_steps, 'train_games': args.games,
                'train_seed_start': train.start, 'eval_games': 0 if args.no_eval else args.eval_games,
                'eval_seed_start': evaluation.start, 'sampling_seed': args.sampling_seed,
                'torch_threads': args.torch_threads,
                'reward_mode': args.reward_mode, 'gamma': 1.0, 'expert_score_input': False,
                'toggle_reward': args.toggle_reward,
                'allow_item_toggle': not args.no_toggle_actions,
                **toggle_policy_metadata(args.limited_item_toggles),
                'reward_scale_divisor': PROGRESSIVE_SCALE if args.reward_mode == 'progressive' else 1.0,
                'progressive_rents': list(args.progressive_rents) if args.reward_mode == 'progressive' else None,
                'progressive_loss': args.progressive_loss if args.reward_mode == 'progressive' else None,
                'progressive_win': args.progressive_win if args.reward_mode == 'progressive' else None,
                'win_potential': WIN_POTENTIAL_DATA if args.reward_mode == 'win_potential' else None,
                'candidate_rules_version': LATEST_VERSION if use_candidate_rules else None,
                'candidate_rules_schema': catalog().schema_hash if use_candidate_rules else None,
                'candidate_offer_capacity': model.policy.offer_capacity if use_candidate_rules else 16,
                'candidate_policy_upgraded': upgraded,
                'candidate_capacity_upgraded': capacity_upgraded,
                'item_state_version': ITEM_STATE_VERSION if use_candidate_rules else None,
                'item_state_schema': ITEM_STATE_SCHEMA if use_candidate_rules else None,
                'item_state_upgraded': item_state_upgraded,
                'optimizer_reset_for_candidate_upgrade': upgraded,
                'before_evaluation_note': ('before_continuation evaluates the upgraded model before PPO; original source remains unchanged' if upgraded else
                                           'item-state inputs expanded; all learned weights and Adam moments preserved' if item_state_upgraded else
                                           'candidate capacity expanded; all learned weights and Adam moments preserved' if capacity_upgraded else None),
                'evaluated_checkpoints': [] if args.no_eval else list(EVALUATION_TAGS),
                'continuous_training': bool(args.no_eval),
                'sampling_note': 'continued model weights and optimizer; exploration RNG reseeded',
                'simulator_note': 'Selected checkpoints reevaluated under the current simulator',
                'git_revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'files_sha256': {str(path.resolve()): sha(path) for path in sources}}
    (args.out/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    cb = ContinueBudget(args.games, args.out, start_steps, prior_updated, train.start, args.stop_file)
    print(f'continuing checkpoint: {args.games} new games; prior decisions={start_steps}', flush=True)
    start = time.perf_counter()
    model.learn(total_timesteps=100_000_000, callback=cb, reset_num_timesteps=False)
    elapsed = time.perf_counter()-start
    final = args.out/'model_final.zip'
    model.save(str(final))
    vec.close()
    if args.no_eval:
        manifest['train_games'] = cb.finished
        (args.out/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    checkpoints = [
        {'tag': 'random_initial', 'path': str(initial), 'completed_games': 0,
         'sampled_decisions': 0, 'updated_decisions': 0},
        {'tag': 'before_continuation', 'path': str(args.out / 'model_upgraded.zip' if upgraded or capacity_upgraded or item_state_upgraded else checkpoint), 'completed_games': 0,
         'sampled_decisions': start_steps, 'updated_decisions': prior_updated},
        *cb.checkpoints,
        {'tag': 'after_continuation', 'path': str(final), 'completed_games': cb.finished,
         'sampled_decisions': model.num_timesteps, 'updated_decisions': cb.updated_steps(model.num_timesteps)},
    ]
    evaluations=[]
    for entry in checkpoints:
        if args.no_eval or entry['tag'] not in EVALUATION_TAGS:
            continue
        result=evaluate(Path(entry['path']), args.out, entry['tag'], evaluation,
                        scratch=True, reward_mode=args.reward_mode, gamma=1.0, toggle_reward=args.toggle_reward,
                        allow_item_toggle=not args.no_toggle_actions, progressive_rents=args.progressive_rents,
                        limited_item_toggles=args.limited_item_toggles,
                        progressive_loss=args.progressive_loss, progressive_win=args.progressive_win)
        result.update({'additional_training_games': entry['completed_games'],
                       'sampled_training_decisions': entry['sampled_decisions'],
                       'updated_training_decisions': entry['updated_decisions']})
        evaluations.append(result)
    summary={'manifest': manifest, 'training_seconds': elapsed, 'training_games': cb.finished,
             'training_wins': cb.wins, 'checkpoints': checkpoints, 'evaluations': evaluations,
             'note': 'Incomplete final rollout is logged but not applied to PPO updates.'}
    (args.out/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'training_games': cb.finished, 'training_wins': cb.wins,
                      'training_seconds': elapsed, 'evaluations': evaluations},
                     ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
