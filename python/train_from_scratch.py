"""Small, reproducible headless PPO experiment with no teacher or expert scores.

Training starts from fresh random weights with progressive rent rewards. Saved
checkpoints are evaluated on the same held-out seeds, and the full non-spin
decisions are kept.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import gzip
from contextlib import nullcontext
import json
import sys
import time
from pathlib import Path

import numpy as np
import torch
from sb3_contrib import MaskablePPO
from sb3_contrib.common.wrappers import ActionMasker
from stable_baselines3.common.callbacks import BaseCallback
from stable_baselines3.common.vec_env import DummyVecEnv

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from gym_env import OFFER_BASE, REMOVE_BASE, RustGymEnv, offer_index, TOGGLE_BASE, USE_BASE  # noqa: E402
from scratch_policy import NoExpertScoresExtractor  # noqa: E402
from train_ppo import LbalGym, featurize, features_for_model  # noqa: E402
from seed_policy import overlaps_fixed_evaluation  # noqa: E402
from reward_schemes import DEFAULT_PROGRESSIVE_LOSS, DEFAULT_PROGRESSIVE_WIN  # noqa: E402
from episode_history import (ACTION_SCHEMA, COMPACT_DETAILS_SCHEMA, DETAILS_SCHEMA, HISTORY_SCHEMA,
                             compact_record, history_record, terminal_metrics)  # noqa: E402


class ScratchBudget(BaseCallback):
    """Stop exactly at the game budget and save an intermediate checkpoint."""
    def __init__(self, games: int, out: Path):
        super().__init__()
        self.games = games
        self.out = out
        self.finished = 0
        self.rewards = 0.0
        self.steps = 0
        self.actions: Counter = Counter()
        self.wins = 0
        self.truncated_games = 0
        self.total_rents = 0
        self.checkpoints = []
        self.file = None
        self.details_file = None
        self.decisions = []
        self.history_events = []
        self.action_ids = []
        self.owned = {}
        self.practice_events = []
        self.shaping_totals = Counter()

    def _on_training_start(self) -> None:
        self.progress_started = time.perf_counter()
        self.file = (self.out / "training_games.jsonl").open("w", encoding="utf-8")
        self.details_file = gzip.open(self.out / 'training_decisions.jsonl.gz', 'wt', encoding='utf8', compresslevel=3)

    def _save_checkpoint(self, tag: str) -> None:
        # on_step precedes the PPO update for the current, still-incomplete
        # rollout; only earlier complete rollouts have influenced the model.
        rollout = self.model.n_steps * self.training_env.num_envs
        trained_steps = max(0, ((self.num_timesteps - 1) // rollout) * rollout)
        path = self.out / f"model_{tag}.zip"
        self.model.save(str(path))
        self.checkpoints.append({"tag": tag, "path": str(path),
                                 "completed_games": self.finished,
                                 "sampled_decisions": self.num_timesteps,
                                 "updated_decisions": trained_steps})

    def _on_step(self) -> bool:
        info = self.locals["infos"][0]
        self.practice_events.extend(info.get('training_practice_events', []))
        for key, value in info.get('training_shaping_raw', {}).items():
            self.shaping_totals[key] += value
        self.rewards += float(self.locals["rewards"][0])
        self.steps += 1
        step = info.get("step_record") or {}
        self.action_ids.append(step.get('action'))
        if step.get('history_event') is not None:
            self.history_events.append(step['history_event'])
        if step.get('analysis_decision') is not None:
            self.decisions.append(step['analysis_decision'])
        for owned in step.get('analysis_owned', []):
            self.owned[owned['name']] = owned
        self.actions[step.get("action_kind", "unknown")] += 1
        if not self.locals["dones"][0]:
            return True
        self.finished += 1
        self.truncated_games += int(bool(step.get('truncated')))
        self.wins += int(bool(info.get("won")))
        self.total_rents += int(info.get("rents_paid", 0) or 0)
        record = {"game": self.finished, "seed": step.get("seed"),
                  "won": bool(info.get("won")), "rents_paid": info.get("rents_paid", 0),
                  "spins": info.get("total_spins", 0), "steps": self.steps,
                  "reward": self.rewards, "truncated": bool(step.get("truncated")),
                  "actions": dict(self.actions)}
        record.update(deck_len=info.get('deck_len'), end_coins=info.get('coins'),
                      boss_hp=(step.get('after') or {}).get('boss_hp'))
        end_state = step.get('episode_end_state')
        record.update(terminal_metrics(end_state))
        if self.history_events:
            record.update(history_schema=HISTORY_SCHEMA,
                          history_ref=dict(file='training_decisions.jsonl.gz', game=self.finished))
        if getattr(self.model, 'training_options', {}).get('practice'):
            record['training_practice_events'] = list(self.practice_events)
        if self.shaping_totals:
            record['training_shaping_raw'] = dict(self.shaping_totals)
        self.file.write(json.dumps(record, ensure_ascii=False) + "\n")
        if self.history_events:
            detail = history_record(self.finished, record['seed'], self.history_events, end_state,
                                    decisions=self.decisions, owned=list(self.owned.values()),
                                    schema=DETAILS_SCHEMA)
        else:
            detail = compact_record(self.finished, record['seed'], self.action_ids, end_state,
                                    decisions=self.decisions, owned=list(self.owned.values()),
                                    schema=COMPACT_DETAILS_SCHEMA)
        self.details_file.write(json.dumps(detail, ensure_ascii=False, separators=(',', ':'))+'\n')
        # Completed games survive a later interruption without waiting for the
        # next 100-game progress message. No per-action disk sync is needed.
        self.file.flush()
        self.details_file.flush()
        if self.finished % 100 == 0 or self.finished == self.games:
            self.file.flush()
            self.details_file.flush()
            print(f"  training {self.finished}/{self.games}; wins={self.wins}; steps={self.num_timesteps}; "
                  f"avg_rents={self.total_rents / self.finished:.6f}; "
                  f"games_per_min={60 * self.finished / max(time.perf_counter() - self.progress_started, .001):.3f}", flush=True)
        # Only final saved stages are used by the continuous UI. Do not create
        # a second, unused model halfway through every 1000-game stage.
        self.rewards = 0.0
        self.steps = 0
        self.actions.clear()
        self.decisions.clear()
        self.history_events.clear()
        self.action_ids.clear()
        self.owned.clear()
        self.practice_events.clear()
        self.shaping_totals.clear()
        return self.finished < self.games

    def _on_training_end(self) -> None:
        if self.file:
            self.file.close()
            self.file = None
        if self.details_file:
            self.details_file.close()
            self.details_file = None


def evaluate(model_path: Path, out: Path, label: str, seeds: range,
             scratch: bool, reward_mode: str, gamma: float, toggle_reward: float = -0.05,
             allow_item_toggle: bool = True, baseline_managed: bool = False, progressive_rents=None,
             progressive_loss=DEFAULT_PROGRESSIVE_LOSS, progressive_win=DEFAULT_PROGRESSIVE_WIN,
             limited_item_toggles: bool = False, item_toggle_allowlist=None,
             full_history: bool = False) -> dict:
    """Evaluate deterministically; record actions and offers for every game."""
    if reward_mode != "progressive":
        raise ValueError("Only progressive rent rewards are supported for evaluation")
    model = MaskablePPO.load(str(model_path), device="cpu")
    rule_version = getattr(model.policy, 'candidate_rules_version', False)
    from candidate_rules import capacity_for
    action_totals: Counter = Counter()
    games_with_action: Counter = Counter()
    phases_rerolled: Counter = Counter()
    removed: Counter = Counter()
    wins = total_rents = total_steps = truncated_games = 0
    start = time.perf_counter()
    output = out / f"{label}_games.jsonl"
    history_output = out / f'{label}_history.jsonl.gz'
    history_context = (gzip.open(history_output, 'wt', encoding='utf-8', compresslevel=3)
                       if full_history else nullcontext(None))
    with output.open("w", encoding="utf-8") as fh, history_context as history_fh:
        for game, seed in enumerate(seeds, 1):
            managed = scratch or baseline_managed
            env = RustGymEnv(seed, max_steps=2000, reward_mode=reward_mode, reward_gamma=gamma,
                             enable_management=managed, toggle_reward=toggle_reward, allow_item_toggle=allow_item_toggle,
                             progressive_rents=progressive_rents, progressive_loss=progressive_loss, progressive_win=progressive_win,
                              offer_capacity=capacity_for(rule_version), limited_item_toggles=limited_item_toggles,
                              item_toggle_allowlist=item_toggle_allowlist, full_history=full_history)
            obs, _ = env.reset(seed)
            done = truncated = False
            steps = 0
            acts: Counter = Counter()
            decisions = []
            history_events = []
            action_ids = []
            owned = {}
            while not done:
                for group, default in (('deck', 'symbol'), ('items', 'item')):
                    for entry in obs.get(group, []):
                        owned[entry['name']] = dict(name=entry['name'], kind=entry.get('kind', default), rarity=entry.get('rarity', 'common'))
                features = (featurize(obs, use_expert_scores=baseline_managed and not rule_version, with_management=managed,
                                      candidate_rules=rule_version, item_state_version=getattr(model.policy, 'item_state_version', None))
                            if scratch or baseline_managed else features_for_model(obs, model))
                action, _ = model.predict(features, action_masks=np.asarray(env.action_mask(), dtype=bool),
                                          deterministic=True)
                action = int(action)
                action_ids.append(action)
                phase = obs.get("phase")
                turn = obs.get("total_spins", 0)
                paid = obs.get('rents_paid', 0)
                offered = [{"name": e.get("name"), "rarity": e.get("rarity"),
                            "kind": e.get("kind", "symbol"), "index": e.get("index")}
                           for e in obs.get("offer", [])]
                chosen = None
                target_offer = offer_index(action)
                if target_offer is not None:
                    chosen = next((e["name"] for e in offered if e["index"] == target_offer), None)
                elif action >= TOGGLE_BASE:
                    index = action - (USE_BASE if action >= USE_BASE else TOGGLE_BASE)
                    chosen = obs["managed_items"][index].get("name")
                elif action >= REMOVE_BASE:
                    index = action - REMOVE_BASE
                    chosen = obs["deck"][index].get("name")
                obs, _, terminated, truncated, info = env.step(action)
                kind = info["step_record"]["action_kind"]
                step_record = info['step_record']
                if step_record.get('history_event') is not None:
                    history_events.append(step_record['history_event'])
                acts[kind] += 1
                if kind == "reroll":
                    phases_rerolled[phase] += 1
                elif kind == "remove" and chosen:
                    removed[chosen] += 1
                if kind != "spin":
                    decision = {"spin": turn, "phase": phase, "action": kind,
                                      "rents_paid": paid, "action_id": action,
                                      "candidates": offered, "choice": chosen,
                                      "managed_target": step_record.get("managed_target")}
                    # Training and evaluation expose the same compact context.
                    recorded = step_record.get('analysis_decision') or {}
                    decision.update({key: recorded[key] for key in ('step', 'context', 'after') if key in recorded})
                    decisions.append(decision)
                for entry in info['step_record'].get('analysis_owned', []):
                    owned[entry['name']] = entry
                steps += 1
                done = terminated or truncated
            for kind, count in acts.items():
                action_totals[kind] += count
                games_with_action[kind] += int(count > 0)
            won = bool(obs.get("won"))
            wins += int(won)
            truncated_games += int(truncated)
            total_rents += int(obs.get("rents_paid", 0))
            total_steps += steps
            row = {"seed": seed, "won": won, "truncated": bool(truncated),
                   "rents_paid": int(obs.get("rents_paid", 0)), "spins": int(obs.get("total_spins", 0)),
                   "steps": steps, "actions": dict(acts), "decisions": decisions, "owned": list(owned.values()),
                   "deck_len": len(obs.get('deck') or []), "end_coins": obs.get('coins'), "boss_hp": obs.get('boss_hp')}
            row.update(terminal_metrics(obs))
            if history_fh is not None:
                row.update(history_schema=HISTORY_SCHEMA,
                           history_ref=dict(file=history_output.name, game=game))
                history_fh.write(json.dumps(history_record(game, seed, history_events, obs),
                                           ensure_ascii=False, separators=(',', ':')) + '\n')
            else:
                row.update(action_schema=ACTION_SCHEMA, action_ids=action_ids)
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            if history_fh is not None:
                history_fh.flush()
            fh.flush()
            if game % 250 == 0 or game == len(seeds):
                fh.flush()
                print(f"  eval {label}: {game}/{len(seeds)}; avg_rents={total_rents / game:.6f}; "
                      f"wins={wins}; valid_games={game - truncated_games}; "
                      f"games_per_min={60 * game / max(time.perf_counter() - start, .001):.3f}", flush=True)
    valid = len(seeds) - truncated_games
    from item_toggle_policy import toggle_policy_metadata
    return {"label": label, "games": len(seeds), "valid_games": valid,
            'history_schema': HISTORY_SCHEMA if full_history else None,
            'history_file': history_output.name if full_history else None,
            'full_history': bool(full_history),
            "training_practice": False, "normal_start": True,
            "seed_start": seeds.start if isinstance(seeds, range) else seeds[0], "rules_version": obs.get('rules_version'),
            "allow_item_toggle": allow_item_toggle,
            **toggle_policy_metadata(limited_item_toggles, item_toggle_allowlist),
            'item_state_version': getattr(model.policy, 'item_state_version', None),
            "truncated_games": truncated_games, "wins": wins,
            "win_rate": wins / valid if valid else None,
            "avg_rents": total_rents / len(seeds), "avg_steps": total_steps / len(seeds),
            "actions": dict(action_totals), "games_with_action": dict(games_with_action),
            "reroll_phases": dict(phases_rerolled), "most_removed": removed.most_common(15),
            "seconds": time.perf_counter() - start, "games_path": str(output)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--mode", choices=["progressive"], default="progressive")
    parser.add_argument("--games", type=int, default=500)
    parser.add_argument("--eval-games", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--train-seed-start", type=int, default=900_000)
    parser.add_argument("--eval-seed-start", type=int, default=1_200_000)
    parser.add_argument("--reference-model", type=Path)
    parser.add_argument("--candidate-rules", action="store_true", help="start with identity/rule candidates and shared scoring")
    args = parser.parse_args()
    if args.games < 2 or args.eval_games < 1:
        parser.error("game counts must be positive and training needs at least two games")
    # Numeric Rust seeds are masked to 31 bits; prevent aliases or overlap.
    train_seeds = range(args.train_seed_start, args.train_seed_start + args.games + 2)
    eval_seeds = range(args.eval_seed_start, args.eval_seed_start + args.eval_games)
    if min(train_seeds.start, eval_seeds.start) < 0 or max(train_seeds.stop, eval_seeds.stop) > 2**31:
        parser.error("seed ranges must fit the simulator's 31-bit numeric seed space")
    if overlaps_fixed_evaluation(train_seeds.start, train_seeds.stop):
        parser.error('training seeds must not overlap the reserved fixed evaluation range')
    if train_seeds.start < eval_seeds.stop and eval_seeds.start < train_seeds.stop:
        parser.error("training and evaluation seeds must not overlap")
    args.out.mkdir(parents=True, exist_ok=False)
    torch.set_num_threads(1)
    gamma = 1.0
    manifest = {"purpose": "scratch PPO without imitation or expert-score input",
                "reward_mode": args.mode, "gamma": gamma, "games_budget": args.games,
                "eval_games": args.eval_games, "train_seed_start": args.train_seed_start,
                "eval_seed_start": args.eval_seed_start, "random_model_seed": args.seed,
                "policy": "fresh random initialization", "expert_score_input": False,
                "reward_description": "progressive rent and terminal rewards, scaled by 20",
                "rollout_size": 512, "batch_size": 256, "learning_rate": 0.0003,
                "entropy_coefficient": 0.01, "gae_lambda": 0.95, "files_sha256": {}}
    for path in [HERE / "train_from_scratch.py", HERE / "scratch_policy.py", HERE / "train_ppo.py",
                 HERE / "gym_env.py", HERE.parent / "rust" / "python_bindings" / "lbal_py.pyd",
                 HERE.parent / "data" / "baseline_priors.json"]:
        if path.exists():
            manifest["files_sha256"][str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    (args.out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    def make_env():
        env = LbalGym(args.seed, seed_sequence=list(train_seeds), reward_mode=args.mode,
                      reward_gamma=gamma, use_expert_scores=False, enable_management=True,
                      candidate_rules=args.candidate_rules)
        return ActionMasker(env, lambda e: e.action_masks())
    vec = DummyVecEnv([make_env])
    if args.candidate_rules:
        from candidate_policy import RuleCandidatePolicy
        from candidate_rules import catalog, LATEST_VERSION
        policy = RuleCandidatePolicy
        policy_kwargs = {'net_arch': {'pi': [256, 256], 'vf': [256, 256]},
                         'rule_schema_hash': catalog().schema_hash}
        manifest.update({'candidate_rules_version': LATEST_VERSION, 'candidate_rules_schema': catalog().schema_hash,
                         'candidate_offer_capacity': catalog().max_offers})
        for path in [HERE / 'candidate_policy.py', HERE / 'candidate_rules.py', *catalog().sources]:
            manifest['files_sha256'][str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        (args.out / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    else:
        policy = 'MlpPolicy'
        policy_kwargs = {'net_arch': {'pi': [256, 256], 'vf': [256, 256]},
                         'features_extractor_class': NoExpertScoresExtractor}
    model = MaskablePPO(policy, vec, seed=args.seed, device="cpu", verbose=0,
                        n_steps=512, batch_size=256, n_epochs=10, gamma=gamma, gae_lambda=0.95,
                        learning_rate=0.0003, ent_coef=0.01, target_kl=0.05,
                        policy_kwargs=policy_kwargs)
    initial = args.out / "model_initial.zip"
    model.save(str(initial))
    cb = ScratchBudget(args.games, args.out)
    print(f"training from random initialization: mode={args.mode}, games={args.games}", flush=True)
    start = time.perf_counter()
    model.learn(total_timesteps=100_000_000, callback=cb)
    elapsed = time.perf_counter() - start
    final = args.out / "model_final.zip"
    model.save(str(final))
    vec.close()
    checkpoints = [{"tag": "initial", "path": str(initial), "completed_games": 0,
                    "sampled_decisions": 0, "updated_decisions": 0}] + cb.checkpoints
    rollout = model.n_steps
    checkpoints.append({"tag": "final", "path": str(final), "completed_games": cb.finished,
                        "sampled_decisions": model.num_timesteps,
                        "updated_decisions": max(0, ((model.num_timesteps - 1) // rollout) * rollout)})
    results = []
    for checkpoint in checkpoints:
        result = evaluate(Path(checkpoint["path"]), args.out, checkpoint["tag"], eval_seeds,
                          scratch=True, reward_mode=args.mode, gamma=gamma)
        result.update({"completed_training_games": checkpoint["completed_games"],
                       "sampled_training_decisions": checkpoint["sampled_decisions"],
                       "updated_training_decisions": checkpoint["updated_decisions"]})
        results.append(result)
    reference = None
    if args.reference_model:
        reference = evaluate(args.reference_model, args.out, "previous_ppo", eval_seeds,
                             scratch=False, reward_mode="progressive", gamma=1.0)
    summary = {"manifest": manifest, "training_seconds": elapsed,
               "training_wins": cb.wins, "training_games": cb.finished,
               "checkpoints": checkpoints, "evaluations": results, "previous_ppo": reference,
               "note": "The final incomplete rollout is logged but not used in a PPO update."}
    (args.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"mode": args.mode, "training_games": cb.finished, "training_wins": cb.wins,
                      "training_seconds": elapsed, "evaluations": results, "previous_ppo": reference},
                     ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
