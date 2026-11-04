"""Gymnasium-style wrapper and action masks for the Rust simulator.

The action space is a fixed integer layout so masked policies can use one
Discrete-like output head regardless of the current offer size:

    0                      spin (ready phase only)
    1                      skip / decline the current reward
    2                      reroll the current reward (needs a token)
    3..18                  take offer i - 3
    19..127                remove deck symbol i - 19

`action_mask()` matches that layout; steps outside the mask raise ValueError
instead of silently no-oping.

    python godot_kernel/python/gym_env.py --steps 2000
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from rust_env import RustEnv  # noqa: E402

ACTION_SIZE = 128
OFFER_BASE = 3
MAX_OFFERS = 16
REMOVE_BASE = OFFER_BASE + MAX_OFFERS

RENT_REWARD = 10.0
WIN_REWARD = 100.0
LOSS_REWARD = -10.0
INCOME_REWARD_SCALE = 0.02


class RustGymEnv:
    def __init__(self, seed: int = 0, max_steps: int = 2000, data_dir: str | None = None):
        self.env = RustEnv(seed, data_dir)
        self.max_steps = max_steps
        self.steps_taken = 0

    @property
    def observation(self) -> dict:
        return self.env.observation

    @property
    def done(self) -> bool:
        return bool(self.env.observation["done"])

    def reset(self, seed: int | None = None) -> tuple[dict, dict]:
        obs = self.env.reset(self.env.seed if seed is None else seed)
        self.steps_taken = 0
        return obs, {"action_mask": self.action_mask(obs)}

    def action_mask(self, obs: dict | None = None) -> list[bool]:
        obs = self.env.observation if obs is None else obs
        mask = [False] * ACTION_SIZE
        if obs["done"]:
            return mask
        removals = min(len(obs["deck"]), ACTION_SIZE - REMOVE_BASE)
        for index in range(removals):
            entry = obs["deck"][index]
            mask[REMOVE_BASE + index] = bool(obs["can_remove"]) and bool(
                entry.get("can_remove", True)
            )
        if obs["phase"] == "ready":
            mask[0] = True
            return mask
        mask[1] = True
        mask[2] = bool(obs["can_reroll"])
        for entry in obs["offer"][:MAX_OFFERS]:
            mask[OFFER_BASE + entry["index"]] = True
        return mask

    def step(self, action: int) -> tuple[dict, float, bool, bool, dict]:
        obs = self.env.observation
        if obs["done"]:
            raise RuntimeError("episode is done; call reset()")
        mask = self.action_mask(obs)
        if not 0 <= action < ACTION_SIZE or not mask[action]:
            raise ValueError(f"invalid action {action} in phase {obs['phase']}")
        previous = obs
        if action == 0:
            new_obs = self.env.step(0)
        elif action == 1:
            new_obs = self.env.step(-1)
        elif action == 2:
            if not self.env.reroll():
                raise ValueError("reroll token was not available")
            new_obs = self.env.observation
        elif action < REMOVE_BASE:
            new_obs = self.env.step(action - OFFER_BASE)
        else:
            if not self.env.remove(action - REMOVE_BASE):
                raise ValueError("removal token was not available")
            new_obs = self.env.observation
        self.steps_taken += 1
        reward = self.reward(previous, new_obs)
        terminated = bool(new_obs["done"])
        truncated = (not terminated) and self.steps_taken >= self.max_steps
        info = {
            "action_mask": self.action_mask(new_obs),
            "rents_paid": new_obs["rents_paid"],
            "coins": new_obs["coins"],
            "total_spins": new_obs["total_spins"],
        }
        return new_obs, reward, terminated, truncated, info

    @staticmethod
    def reward(previous: dict, current: dict) -> float:
        rents = max(0, current["rents_paid"] - previous["rents_paid"])
        spins = current["total_spins"] - previous["total_spins"]
        income = current.get("last_income", 0) if spins > 0 else 0
        reward = RENT_REWARD * rents + INCOME_REWARD_SCALE * income
        if current["won"] and not previous["won"]:
            reward += WIN_REWARD
        elif current["done"] and not current["won"]:
            reward += LOSS_REWARD
        return reward


class VecRustEnv:
    """Minimal vector wrapper; stepping is sequential (Python-side batching).

    Finished environments are reset lazily on the next step and the reset
    observation is returned with info["auto_reset"]=True. The action for that
    slot is ignored on the reset step, matching common vector-env semantics.
    """

    def __init__(
        self,
        num_envs: int,
        seed: int = 0,
        max_steps: int = 2000,
        data_dir: str | None = None,
    ):
        self.num_envs = num_envs
        self.max_steps = max_steps
        self._next_seed = seed + num_envs
        self._finished = [False] * num_envs
        self.envs = [
            RustGymEnv(seed + index, max_steps, data_dir) for index in range(num_envs)
        ]

    def reset(self, seeds: list[int] | None = None) -> tuple[list[dict], list[dict]]:
        seeds = seeds or [None] * self.num_envs
        pairs = [env.reset(seed) for env, seed in zip(self.envs, seeds)]
        self._finished = [False] * self.num_envs
        return [obs for obs, _ in pairs], [info for _, info in pairs]

    def action_masks(self) -> list[list[bool]]:
        return [env.action_mask() for env in self.envs]

    def step(
        self, actions: list[int]
    ) -> tuple[list[dict], list[float], list[bool], list[bool], list[dict]]:
        if len(actions) != self.num_envs:
            raise ValueError("one action per environment is required")
        results = []
        for index, (env, action) in enumerate(zip(self.envs, actions)):
            if self._finished[index]:
                obs, info = env.reset(self._next_seed)
                self._next_seed += 1
                self._finished[index] = False
                results.append((obs, 0.0, False, False, {**info, "auto_reset": True}))
            else:
                result = env.step(action)
                self._finished[index] = result[2] or result[3]
                results.append(result)
        obs, rewards, terminated, truncated, infos = zip(*results)
        return list(obs), list(rewards), list(terminated), list(truncated), list(infos)


def random_policy(env: RustGymEnv, rng: random.Random) -> int:
    mask = env.action_mask()
    valid = [action for action, allowed in enumerate(mask) if allowed]
    if not valid:
        raise RuntimeError("environment is done")
    return rng.choice(valid)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--steps", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    env = RustGymEnv(args.seed)
    rng = random.Random(args.seed)
    env.reset()
    start = time.perf_counter()
    episodes = 0
    for _ in range(args.steps):
        if env.done:
            env.reset()
            episodes += 1
            continue
        env.step(random_policy(env, rng))
    elapsed = time.perf_counter() - start
    print(
        f"steps={args.steps} episodes={episodes} elapsed={elapsed:.2f}s "
        f"steps_per_sec={args.steps / max(elapsed, 1e-9):.1f}"
    )


if __name__ == "__main__":
    main()
