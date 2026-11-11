"""MaskablePPO training for the Rust `lbal` environment.

    python godot_kernel/python/train_ppo.py --total-steps 100000 --n-envs 4

The progressive rent reward formula lives in gym_env.py. This script flattens
the JSON observation into a fixed numeric vector, trains SB3-Contrib's
MaskablePPO with action masks, and evaluates on held-out seeds.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import zlib
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import gymnasium as gym  # noqa: E402
from gymnasium import spaces  # noqa: E402

from gym_env import ACTION_SIZE, MAX_OFFERS, REMOVE_BASE, MANAGED_SLOTS, MANAGED_ACTION_SIZE, RustGymEnv  # noqa: E402
from reward_schemes import DEFAULT_PROGRESSIVE_LOSS, DEFAULT_PROGRESSIVE_WIN  # noqa: E402

PHASES = ("ready", "spin_reward", "symbol_reward", "item_reward", "essence_reward")
RARITY = {"common": 0, "uncommon": 1, "rare": 2, "very_rare": 3}
OFFER_SLOTS = 3
OFFER_FEATURES = 9  # present, rarity×4, type_id, bonus, multiplier, expert_score
DECK_VEC = 220  # per-type_id counts (牌组构成，克隆的关键特征)
ITEM_VEC = 64  # held items/essences hashed
LEGACY_OBS_DIM = (
    15  # scalars
    + len(PHASES)  # phase one-hot
    + 7  # deck summary (len, bonus mean, mult mean, rarity fractions ×4)
    + 1  # board occupancy
    + OFFER_SLOTS * OFFER_FEATURES
    + DECK_VEC
    + ITEM_VEC
)
DECK_SLOTS = ACTION_SIZE - REMOVE_BASE
DECK_SLOT_FEATURES = 5  # type_id+presence, bonus, multiplier, lifetime, removable
EXTRA_OFFER_SLOTS = MAX_OFFERS - OFFER_SLOTS
OBS_DIM = LEGACY_OBS_DIM + DECK_SLOTS * DECK_SLOT_FEATURES + EXTRA_OFFER_SLOTS * OFFER_FEATURES


def _rarity_vec(name: str) -> list[float]:
    vec = [0.0] * 4
    index = RARITY.get(name, 0)
    vec[index] = 1.0
    return vec


def candidate_features(entry, use_expert_scores=False):
    return [1.0, *_rarity_vec(entry.get('rarity', 'common')),
            float(entry.get('type_id', 0) or 0) / 200.0,
            float(entry.get('bonus', 0) or 0) / 10.0,
            float(entry.get('multiplier', 1) or 1) / 5.0,
            float(entry.get('expert_score', 0) or 0) / 6.0 if use_expert_scores else 0.0]


def featurize(obs: dict, use_expert_scores: bool = True, with_management: bool = False,
              candidate_rules: bool | str = False, item_state_version=None) -> np.ndarray:
    feats: list[float] = [
        min(float(obs.get("coins", 0) or 0), 5000.0) / 5000.0,
        min(float(obs.get("rent_due", 1) or 1), 2000.0) / 2000.0,
        float(obs.get("rents_paid", 0) or 0) / 12.0,
        min(float(obs.get("spins_left", 0) or 0), 20.0) / 20.0,
        min(float(obs.get("total_spins", 0) or 0), 150.0) / 150.0,
        np.log1p(max(0.0, float(obs.get("last_income", 0) or 0))) / 8.0,
        min(float(obs.get("removal_tokens", 0) or 0), 10.0) / 10.0,
        min(float(obs.get("reroll_tokens", 0) or 0), 10.0) / 10.0,
        min(float(obs.get("essence_tokens", 0) or 0), 10.0) / 10.0,
        float(obs.get("removal_cost", 1) or 1) / 3.0,
        float(obs.get("reroll_cost", 1) or 1) / 3.0,
        min(float(obs.get("boss_hp", 0) or 0), 1500.0) / 1500.0,
        1.0 if obs.get("boss_fight") else 0.0,
        1.0 if obs.get("must_take_symbol") else 0.0,
        1.0 if (obs.get("can_remove") or obs.get("can_reroll")) else 0.0,
    ]
    phase = str(obs.get("phase", "ready"))
    feats += [1.0 if phase == name else 0.0 for name in PHASES]
    deck = obs.get("deck") or []
    deck_len = max(1, len(deck))
    rarity_counts = [0.0] * 4
    bonus_sum = 0.0
    mult_sum = 0.0
    for entry in deck:
        rarity_counts[RARITY.get(entry.get("rarity", "common"), 0)] += 1.0
        bonus_sum += float(entry.get("bonus", 0) or 0)
        mult_sum += float(entry.get("multiplier", 1) or 1)
    feats += [
        min(len(deck), 60) / 60.0,
        bonus_sum / deck_len / 10.0,
        mult_sum / deck_len / 5.0,
    ] + [count / deck_len for count in rarity_counts]
    board = obs.get("board") or []
    occupied = sum(1 for slot in board if slot != -1)
    feats.append(occupied / 20.0)
    # 牌组构成：每个符号 type_id 的张数（组合决策的关键信息）
    deck_vec = [0.0] * DECK_VEC
    for entry in deck:
        tid = int(entry.get("type_id", 0) or 0)
        if 0 <= tid < DECK_VEC:
            deck_vec[tid] += 1.0
    feats += deck_vec
    # 已持有的物品/精华（哈希分桶）
    item_vec = [0.0] * ITEM_VEC
    for entry in obs.get("items") or []:
        name = str(entry.get("name", ""))
        item_vec[zlib.crc32(name.encode("utf-8")) % ITEM_VEC] += 1.0
    feats += item_vec
    offer = obs.get("offer") or []
    for slot in range(OFFER_SLOTS):
        if slot < len(offer):
            entry = offer[slot]
            feats += candidate_features(entry, use_expert_scores)
        else:
            feats += [0.0] * OFFER_FEATURES
    # Appended after the complete legacy vector: the original actor can be
    # migrated with zero weights for new features. Index i describes exactly
    # the deck entry removed by action REMOVE_BASE+i, including its state.
    for entry in deck[:DECK_SLOTS]:
        feats += [
            (int(entry.get("type_id", 0) or 0) + 1) / 220.0,
            float(entry.get("bonus", 0) or 0) / 10.0,
            float(entry.get("multiplier", 1) or 1) / 5.0,
            min(float(entry.get("times_displayed", 0) or 0), 100.0) / 100.0,
            float(bool(entry.get("can_remove", True))),
        ]
    feats += [0.0] * (max(0, DECK_SLOTS - len(deck)) * DECK_SLOT_FEATURES)
    # Preserve the original 16-slot prefix for saved models. Rule v2 appends
    # the remaining candidates after the complete original observation.
    for slot in range(OFFER_SLOTS, MAX_OFFERS):
        if slot < len(offer):
            entry = offer[slot]
            feats += candidate_features(entry, use_expert_scores)
        else:
            feats += [0.0] * OFFER_FEATURES
    if with_management:
        managed = obs.get("managed_items") or []
        for item in managed[:MANAGED_SLOTS]:
            feats += [(int(item.get("type_id", 0)) + 1) / 300.0,
                      float(item.get("kind") == "essence"), float(bool(item.get("disabled"))),
                      min(float(item.get("count", 0)), 10.0) / 10.0,
                      float(bool(item.get("can_toggle"))), float(bool(item.get("can_use")))]
        feats += [0.0] * (max(0, MANAGED_SLOTS - len(managed)) * 6)
    result = np.asarray(feats, dtype=np.float32)
    if candidate_rules:
        if use_expert_scores or not with_management:
            raise ValueError('Rule candidates require the managed scratch observation without expert scores')
        from candidate_rules import catalog, capacity_for
        result = catalog().append(obs, result, capacity_for(candidate_rules))
    if item_state_version:
        if not candidate_rules:
            raise ValueError('Dynamic item observations require the rule-based scratch policy')
        from item_state_features import append, normalize_version
        normalize_version(item_state_version)
        result = append(obs, result, catalog(), item_state_version, capacity_for(candidate_rules))
    return result


MANAGED_OBS_DIM = OBS_DIM + MANAGED_SLOTS * 6


def features_for_model(obs: dict, model) -> np.ndarray:
    """Keep previously trained PPO files usable with their original inputs."""
    version = getattr(model.policy, 'candidate_rules_version', None)
    if version:
        return featurize(obs, False, True, candidate_rules=version,
                         item_state_version=getattr(model.policy, 'item_state_version', None))
    values = featurize(obs)
    width = model.observation_space.shape[0]
    if width == MANAGED_OBS_DIM:
        return featurize(obs, with_management=True)
    if width == LEGACY_OBS_DIM:
        return values[:LEGACY_OBS_DIM]
    if width != OBS_DIM:
        raise ValueError(f"Unsupported model observation width {width}")
    return values


class LbalGym(gym.Env):
    """Gymnasium adapter over RustGymEnv with flat observations + masks."""

    metadata = {"render_modes": []}

    def __init__(
        self,
        seed: int = 0,
        max_steps: int = 2000,
        seed_sequence: list[int] | None = None,
        reward_mode: str = "progressive",
        reward_gamma: float = 1.0,
        use_expert_scores: bool = True,
        enable_management: bool = False,
        toggle_reward: float = -0.05,
        allow_item_toggle: bool = True,
        progressive_rents=None,
        progressive_loss=DEFAULT_PROGRESSIVE_LOSS,
        progressive_win=DEFAULT_PROGRESSIVE_WIN,
        candidate_rules: bool | str = False,
        limited_item_toggles: bool = False,
        item_toggle_allowlist=None,
        item_state_version=None,
        training_practice=None,
        training_shaping=None,
        shaping_snapshot=None,
        full_history: bool = False,
    ):
        super().__init__()
        capacity = MAX_OFFERS
        if candidate_rules:
            from candidate_rules import capacity_for
            capacity = capacity_for(candidate_rules)
        self._env = RustGymEnv(seed, max_steps, reward_mode=reward_mode, reward_gamma=reward_gamma,
                               enable_management=enable_management, toggle_reward=toggle_reward,
                               allow_item_toggle=allow_item_toggle, progressive_rents=progressive_rents,
                               progressive_loss=progressive_loss, progressive_win=progressive_win,
                                offer_capacity=capacity, limited_item_toggles=limited_item_toggles,
                                item_toggle_allowlist=item_toggle_allowlist, training_practice=training_practice,
                                training_shaping=training_shaping, shaping_snapshot=shaping_snapshot,
                                full_history=full_history)
        self.enable_management = enable_management
        self.use_expert_scores = use_expert_scores
        self.candidate_rules = candidate_rules
        from item_state_features import normalize_version
        self.item_state_version = normalize_version(item_state_version)
        if self.item_state_version and not candidate_rules:
            raise ValueError('Dynamic item observations require a rule-based scratch policy')
        if candidate_rules and (use_expert_scores or not enable_management):
            raise ValueError('Rule candidates are available only for the managed scratch route')
        self._seed_sequence = list(seed_sequence) if seed_sequence is not None else None
        self._seed_cursor = 0
        self._next_auto_seed = seed
        width = MANAGED_OBS_DIM if enable_management else OBS_DIM
        if candidate_rules:
            from candidate_rules import catalog
            width += catalog().observation_extra_dim(capacity)
        if self.item_state_version:
            from item_state_features import dimension
            width += dimension(self.item_state_version, capacity)
        self.observation_space = spaces.Box(-np.inf, np.inf, shape=(width,), dtype=np.float32)
        self.action_space = spaces.Discrete(self._env.action_size)

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)
        if seed is None:
            if self._seed_sequence is not None:
                if self._seed_cursor >= len(self._seed_sequence):
                    raise RuntimeError("fixed training seed sequence exhausted")
                seed = self._seed_sequence[self._seed_cursor]
                self._seed_cursor += 1
            else:
                seed = self._next_auto_seed
                self._next_auto_seed += 1
        obs, _ = self._env.reset(seed)
        return featurize(obs, self.use_expert_scores, self.enable_management, self.candidate_rules, self.item_state_version), {"action_mask": self._env.action_mask(obs)}

    def action_masks(self) -> np.ndarray:
        return np.asarray(self._env.action_mask(), dtype=bool)

    def step(self, action):
        obs, reward, terminated, truncated, info = self._env.step(int(action))
        return featurize(obs, self.use_expert_scores, self.enable_management, self.candidate_rules, self.item_state_version), reward, terminated, truncated, info


def evaluate(
    model,
    seeds: list[int],
    max_steps: int = 2000,
    log_path: str | None = None,
    reward_mode: str = "progressive",
    reward_gamma: float = 1.0,
) -> dict:
    wins = 0
    truncated_games = 0
    rents: list[int] = []
    fh = open(log_path, "w", encoding="utf-8") if log_path else None
    for game_index, seed in enumerate(seeds):
        rules = getattr(model.policy, 'candidate_rules_version', False)
        managed = rules or model.observation_space.shape[0] == MANAGED_OBS_DIM
        env = LbalGym(seed, max_steps, reward_mode=reward_mode, reward_gamma=reward_gamma,
                      enable_management=managed, use_expert_scores=not rules, candidate_rules=rules,
                      item_state_version=getattr(model.policy, 'item_state_version', None))
        obs, _ = env.reset(seed=seed)
        done = False
        ep_reward = 0.0
        steps = 0
        while not done:
            action, _ = model.predict(
                obs, action_masks=env.action_masks(), deterministic=True
            )
            obs, r, terminated, truncated, _ = env.step(int(action))
            ep_reward += float(r)
            steps += 1
            done = terminated or truncated
        state = env._env.observation
        truncated_games += int(bool(truncated))
        won = int(bool(state.get("won")))
        wins += won
        rent = int(state.get("rents_paid", 0) or 0)
        rents.append(rent)
        if fh is not None:
            fh.write(
                json.dumps(
                    {
                        "seed": seed,
                        "won": won,
                        "rents_paid": rent,
                        "coins": int(state.get("coins", 0) or 0),
                        "total_spins": int(state.get("total_spins", 0) or 0),
                        "deck_len": len(state.get("deck") or []),
                        "steps": steps,
                        "truncated": bool(truncated),
                        "reward": round(ep_reward, 3),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        if (game_index + 1) % 500 == 0:
            print(f"  eval {game_index + 1}/{len(seeds)} games", flush=True)
    if fh is not None:
        fh.close()
    return {
        "games": len(seeds),
        "truncated_games": truncated_games,
        "win_rate": wins / max(1, len(seeds)),
        "avg_rents": float(np.mean(rents)) if rents else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--games", type=int, default=2_000, help="training budget in finished games"
    )
    parser.add_argument(
        "--total-steps", type=int, default=0, help="optional hard env-step cap (0 = none)"
    )
    parser.add_argument(
        "--n-envs", type=int, default=2, help="parallel envs (keep small to leave CPU headroom)"
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--eval-games", type=int, default=40)
    parser.add_argument("--report-games", type=int, default=2000)
    parser.add_argument("--out", type=str, default=str(HERE / "ppo_lbal"))
    args = parser.parse_args()

    # Leave CPU headroom for other work: cap intra/inter-op threads.
    import os

    os.environ.setdefault("OMP_NUM_THREADS", "2")
    import torch

    torch.set_num_threads(2)

    from sb3_contrib import MaskablePPO
    from sb3_contrib.common.wrappers import ActionMasker
    from stable_baselines3.common.callbacks import BaseCallback
    from stable_baselines3.common.vec_env import SubprocVecEnv

    class GameBudget(BaseCallback):
        """Stop learning once `games` episodes have finished."""

        def __init__(self, games: int, hard_cap: int):
            super().__init__()
            self.games = games
            self.hard_cap = hard_cap
            self.finished = 0

        def _on_step(self) -> bool:
            self.finished += int(self.locals["dones"].sum())
            if self.finished >= self.games:
                return False
            if self.hard_cap and self.num_timesteps >= self.hard_cap:
                return False
            return True

    def make_env(rank: int):
        def _init():
            env = LbalGym(args.seed + rank, 2000)
            return ActionMasker(env, lambda e: e.action_masks())

        return _init

    vec_env = SubprocVecEnv([make_env(i) for i in range(args.n_envs)])
    model = MaskablePPO(
        "MlpPolicy",
        vec_env,
        seed=args.seed,
        verbose=1,
        n_steps=512,
        batch_size=256,
        gamma=1.0,
        gae_lambda=0.95,
        learning_rate=3e-4,
        ent_coef=0.01,
    )
    start = time.perf_counter()
    model.learn(
        total_timesteps=100_000_000,
        callback=GameBudget(args.games, args.total_steps),
    )
    elapsed = time.perf_counter() - start
    model.save(args.out)
    vec_env.close()

    seeds = list(range(10_000, 10_000 + args.eval_games))
    stats = evaluate(model, seeds)
    print(
        f"trained {args.games} games in {elapsed:.1f}s "
        f"({args.games / elapsed:.1f} games/s) -> {args.out}"
    )
    print(
        f"eval: {stats['games']} games  win_rate={stats['win_rate']:.1%}  "
        f"avg_rents={stats['avg_rents']:.2f}"
    )

    if args.report_games > 0:
        import report_policy_stats as rps

        print(f"\ngenerating stats report from {args.report_games} games...")
        policy = rps.make_model_policy(args.out)
        col = rps.collect(policy, args.report_games, seed0=40_000, max_steps=2000)
        tag = f"ppo_{args.report_games}"
        dataset = rps.build_dataset(col, tag, f"PPO({args.games}局训练) · 统计 {args.report_games} 局")
        out_path = HERE.parent.parent / f"统计_报告_{tag}.html"
        rps.write_html(HERE / "templates" / "policy_stats.html", out_path, dataset)
        top = sorted(dataset["rows"], key=lambda r: -r["runs_picked"])[:10]
        print(
            f"统计摘要：{dataset['runs']} 局  胜率 {dataset['win_rate']:.1%}  "
            f"覆盖条目 {dataset['symbols']}"
        )
        for row in top:
            print(
                f"  {row['name']:<28} 选中 {row['runs_picked']:>5} 局  "
                f"选中率 {row['pick_rate']:.1%}  持有胜率 {row['acq_win_rate']:.1%}"
            )


if __name__ == "__main__":
    main()
