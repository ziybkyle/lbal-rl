"""Gymnasium-style wrapper and action masks for the Rust simulator.

The action space is a fixed integer layout so masked policies can use one
Discrete-like output head regardless of the current offer size:

    0                      spin (ready phase only)
    1                      skip / decline the current reward
    2                      reroll the current reward (needs a token)
    3..18                  take offer i - 3
    19..127                remove deck symbol i - 19

Managed policies also use 128..383 for toggles and 384..639 for item use.
The expanded rule policy appends offer indices 16 and above at 640, leaving
every previously saved action number unchanged.

`action_mask()` matches that layout; steps outside the mask raise ValueError
instead of silently no-oping.

    python godot_kernel/python/gym_env.py --steps 2000
"""
from __future__ import annotations

import argparse
import math
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from rust_env import RustEnv  # noqa: E402
from item_toggle_policy import normalize_allowlist  # noqa: E402
from reward_schemes import (REWARD_MODE, PROGRESSIVE_SCALE, DEFAULT_PROGRESSIVE_RENTS, DEFAULT_PROGRESSIVE_LOSS,
                            DEFAULT_PROGRESSIVE_WIN, validate_progressive_rents, validate_progressive_outcomes)  # noqa: E402

ACTION_SIZE = 128
OFFER_BASE = 3
MAX_OFFERS = 16
REMOVE_BASE = OFFER_BASE + MAX_OFFERS
MANAGED_SLOTS = 256
TOGGLE_BASE = ACTION_SIZE
USE_BASE = TOGGLE_BASE + MANAGED_SLOTS
MANAGED_ACTION_SIZE = USE_BASE + MANAGED_SLOTS
EXTRA_OFFER_BASE = MANAGED_ACTION_SIZE


def offer_action(index: int) -> int:
    return OFFER_BASE + index if index < MAX_OFFERS else EXTRA_OFFER_BASE + index - MAX_OFFERS


def offer_index(action: int) -> int | None:
    if OFFER_BASE <= action < REMOVE_BASE:
        return action - OFFER_BASE
    if action >= EXTRA_OFFER_BASE:
        return MAX_OFFERS + action - EXTRA_OFFER_BASE
    return None

TOGGLE_COST = -0.05  # Raw points per manual item or essence toggle.


def outcome_reward_breakdown(previous: dict, current: dict, mode: str, gamma: float,
                             progressive_rents=None, progressive_loss=DEFAULT_PROGRESSIVE_LOSS,
                             progressive_win=DEFAULT_PROGRESSIVE_WIN) -> dict[str, float]:
    """Pay each of the first twelve rents once, and score a genuine terminal."""
    if mode != REWARD_MODE:
        raise ValueError(f"Only progressive rent rewards are supported: {mode}")
    if not 0.0 < gamma <= 1.0:
        raise ValueError("Reward discount must be in (0, 1]")
    before = max(0, min(int(previous.get("rents_paid", 0) or 0), 12))
    after = max(0, min(int(current.get("rents_paid", 0) or 0), 12))
    rewards = DEFAULT_PROGRESSIVE_RENTS if progressive_rents is None else progressive_rents
    rent = sum(rewards[before:after])
    ended = bool(current.get("done")) and not previous.get("done")
    terminal = (progressive_win if current.get("won") else progressive_loss) if ended else 0.0
    return {"rent": rent / PROGRESSIVE_SCALE, "terminal": terminal / PROGRESSIVE_SCALE}


class RustGymEnv:
    def __init__(self, seed: int = 0, max_steps: int = 2000, data_dir: str | None = None,
                 reward_mode: str = REWARD_MODE, reward_gamma: float = 1.0, enable_management: bool = False,
                 toggle_reward: float = TOGGLE_COST, allow_item_toggle: bool = True,
                 progressive_rents=None, progressive_loss=DEFAULT_PROGRESSIVE_LOSS,
                 progressive_win=DEFAULT_PROGRESSIVE_WIN, offer_capacity: int = MAX_OFFERS,
                 limited_item_toggles: bool = False, item_toggle_allowlist=None, training_practice=None,
                 training_shaping=None, shaping_snapshot=None, full_history: bool = False):
        if reward_mode != REWARD_MODE:
            raise ValueError(f"Only progressive rent rewards are supported: {reward_mode}")
        if not 0.0 < reward_gamma <= 1.0:
            raise ValueError("Reward discount must be in (0, 1]")
        if not math.isfinite(toggle_reward):
            raise ValueError("Toggle reward must be finite")
        self.limited_item_toggles = bool(limited_item_toggles)
        self.item_toggle_allowlist = normalize_allowlist(item_toggle_allowlist) if self.limited_item_toggles else frozenset()
        self.env = RustEnv(seed, data_dir, manual_decisions=enable_management)
        self.practice = None
        self.training_shaper = None
        if training_shaping:
            from training_shaping import TrainingShaper, enabled
            if enabled(training_shaping):
                self.training_shaper = TrainingShaper(training_shaping, shaping_snapshot)
        if training_practice:
            from training_options import PracticeSchedule
            self.practice = PracticeSchedule(training_practice)
        self.max_steps = max_steps
        self.steps_taken = 0
        # Full observation snapshots/deltas are expensive and diagnostic-only.
        self.full_history = bool(full_history)
        self.enable_management = enable_management
        self.allow_item_toggle = bool(allow_item_toggle)
        if offer_capacity < MAX_OFFERS:
            raise ValueError('offer capacity must preserve the 16 legacy slots')
        self.offer_capacity = int(offer_capacity)
        self.action_size = MANAGED_ACTION_SIZE if enable_management else ACTION_SIZE
        if self.offer_capacity > MAX_OFFERS:
            self.action_size = EXTRA_OFFER_BASE + self.offer_capacity - MAX_OFFERS
        self.reward_mode = reward_mode
        self.reward_gamma = reward_gamma
        self.toggle_reward = float(toggle_reward)
        self.progressive_rents = validate_progressive_rents(DEFAULT_PROGRESSIVE_RENTS if progressive_rents is None else progressive_rents)
        self.progressive_loss, self.progressive_win = validate_progressive_outcomes(progressive_loss, progressive_win)

    @property
    def observation(self) -> dict:
        return self.env.observation

    @property
    def done(self) -> bool:
        return bool(self.env.observation["done"])

    def reset(self, seed: int | None = None) -> tuple[dict, dict]:
        obs = self.env.reset(self.env.seed if seed is None else seed)
        self.steps_taken = 0
        if self.training_shaper:
            self.training_shaper.reset()
        if self.practice:
            self.practice.reset(self.env.seed)
            self.practice.apply(self.env)
            obs = self.env.observation
        return obs, {"action_mask": self.action_mask(obs)}

    def action_mask(self, obs: dict | None = None) -> list[bool]:
        obs = self.env.observation if obs is None else obs
        mask = [False] * self.action_size
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
            if self.enable_management:
                for index, item in enumerate(obs.get("managed_items", [])[:MANAGED_SLOTS]):
                    permitted = (not self.limited_item_toggles or
                                 (item.get('kind'), item.get('name')) in self.item_toggle_allowlist)
                    mask[TOGGLE_BASE + index] = self.allow_item_toggle and permitted and bool(item.get("can_toggle"))
                    mask[USE_BASE + index] = bool(item.get("can_use"))
            return mask
        # A forced spin reward cannot be declined (e.g. a forced symbol curse).
        mask[1] = not (obs["phase"] == "spin_reward" and obs.get("must_take_symbol", False))
        mask[2] = obs['phase'] in ('spin_reward', 'symbol_reward') and bool(obs["can_reroll"])
        for entry in obs["offer"]:
            index = int(entry['index'])
            if 0 <= index < self.offer_capacity:
                mask[offer_action(index)] = True
            elif self.offer_capacity > MAX_OFFERS:
                raise ValueError(f'offer index {index} exceeds the model capacity {self.offer_capacity}')
        return mask

    def step(self, action: int) -> tuple[dict, float, bool, bool, dict]:
        obs = self.env.observation
        if obs["done"]:
            raise RuntimeError("episode is done; call reset()")
        mask = self.action_mask(obs)
        if not 0 <= action < self.action_size or not mask[action]:
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
        elif offer_index(action) is not None:
            new_obs = self.env.step(offer_index(action))
        elif action < ACTION_SIZE:
            if not self.env.remove(action - REMOVE_BASE):
                raise ValueError("removal token was not available")
            new_obs = self.env.observation
        else:
            is_use = action >= USE_BASE
            index = action - (USE_BASE if is_use else TOGGLE_BASE)
            item = previous["managed_items"][index]
            ok = (self.env.use_managed(item["kind"] == "essence", item["type_id"]) if is_use else
                  self.env.toggle_managed(item["kind"] == "essence", item["type_id"]))
            if not ok:
                raise ValueError("item action was not available")
            new_obs = self.env.observation
        if self.practice:
            self.practice.apply(self.env)
            new_obs = self.env.observation
        self.steps_taken += 1
        reward_parts = outcome_reward_breakdown(previous, new_obs, self.reward_mode, self.reward_gamma,
                                                 self.progressive_rents, self.progressive_loss, self.progressive_win)
        if TOGGLE_BASE <= action < USE_BASE:
            reward_parts["item_toggle"] = self.toggle_reward / PROGRESSIVE_SCALE
        shaping_raw, shaping_detail = {}, {}
        if self.training_shaper:
            target = offer_index(action)
            chosen = next((entry for entry in previous.get('offer', []) if entry['index'] == target), None) if target is not None else None
            shaping_raw, shaping_detail = self.training_shaper.step(previous, new_obs, chosen)
            reward_parts.update({key: value / PROGRESSIVE_SCALE for key, value in shaping_raw.items()})
        reward = sum(reward_parts.values())
        terminated = bool(new_obs["done"])
        truncated = (not terminated) and self.steps_taken >= self.max_steps
        info = {
            "rules_version": new_obs.get("rules_version"),
            "action_mask": self.action_mask(new_obs),
            "rents_paid": new_obs["rents_paid"],
            "coins": new_obs["coins"],
            "total_spins": new_obs["total_spins"],
            "won": bool(new_obs.get("won")),
            "last_income": int(new_obs.get("last_income", 0) or 0),
            "deck_len": len(new_obs.get("deck") or []),
            "step_record": self._step_record(
                previous, new_obs, action, reward, reward_parts, truncated
            ),
        }
        if self.practice:
            info['training_practice_events'] = self.practice.drain_events()
        if self.training_shaper:
            info['training_shaping_raw'] = shaping_raw
            info['step_record']['training_shaping'] = dict(raw=shaping_raw, **shaping_detail)
            if info['step_record']['analysis_decision'] is not None and any(shaping_raw.values()):
                info['step_record']['analysis_decision']['training_shaping_raw'] = shaping_raw
        return new_obs, reward, terminated, truncated, info

    def _step_record(
        self,
        previous: dict,
        current: dict,
        action: int,
        reward: float,
        reward_parts: dict,
        truncated: bool,
    ) -> dict:
        """Compact, JSON-safe per-action record for reward/behaviour analysis."""
        if action == 0:
            action_kind = "pay_rent" if previous.get("rent_pending") else "spin"
        elif action == 1:
            action_kind = "skip"
        elif action == 2:
            action_kind = "reroll"
        elif offer_index(action) is not None:
            action_kind = "take"
        elif action < ACTION_SIZE:
            action_kind = "remove"
        else:
            action_kind = "destroy_item" if action >= USE_BASE else "toggle_item"

        def state_summary(obs: dict) -> dict:
            return {
                "coins": int(obs.get("coins", 0) or 0),
                "rent_due": int(obs.get("rent_due", 0) or 0),
                "rents_paid": int(obs.get("rents_paid", 0) or 0),
                "spins_left": int(obs.get("spins_left", 0) or 0),
                "total_spins": int(obs.get("total_spins", 0) or 0),
                "removal_tokens": int(obs.get("removal_tokens", 0) or 0),
                "reroll_tokens": int(obs.get("reroll_tokens", 0) or 0),
                "essence_tokens": int(obs.get("essence_tokens", 0) or 0),
                "deck_len": len(obs.get("deck") or []),
                "boss_fight": bool(obs.get("boss_fight", False)),
                "boss_hp": int(obs.get("boss_hp", 0) or 0),
                "rent_pending": bool(obs.get("rent_pending", False)),
            }

        def names(entries: list[dict]) -> dict[str, int]:
            counts: dict[str, int] = {}
            for entry in entries:
                name = str(entry.get("name", ""))
                if name:
                    counts[name] = counts.get(name, 0) + 1
            return counts

        before_deck = names(previous.get("deck") or [])
        after_deck = names(current.get("deck") or [])
        deck_added = []
        deck_removed = []
        for name in sorted(set(before_deck) | set(after_deck)):
            delta = after_deck.get(name, 0) - before_deck.get(name, 0)
            target = deck_added if delta > 0 else deck_removed
            target.extend([name] * abs(delta))

        def offer_names(obs: dict) -> list[str]:
            return [
                str(entry.get("name", ""))
                for entry in (obs.get("offer") or [])
                if entry.get("name")
            ]

        decision = None
        if action_kind != 'spin':
            offered = [{k: entry.get(k) for k in ('name', 'kind', 'rarity', 'index')}
                       for entry in previous.get('offer', [])]
            choice = None
            target = offer_index(action)
            if target is not None:
                choice = next((e['name'] for e in offered if e['index'] == target), None)
            elif action_kind == 'remove':
                choice = previous['deck'][action - REMOVE_BASE]['name']
            elif action_kind in ('toggle_item', 'destroy_item'):
                choice = previous['managed_items'][action - (USE_BASE if action >= USE_BASE else TOGGLE_BASE)]['name']
            decision = dict(spin=previous.get('total_spins', 0), phase=previous.get('phase'),
                            rents_paid=previous.get('rents_paid', 0), action=action_kind,
                            action_id=action, candidates=offered, choice=choice,
                            step=self.steps_taken)
        newly_owned = set(deck_added) | (set(names(current.get('items') or [])) - set(names(previous.get('items') or [])))
        owned = []
        for state in ([previous, current] if self.steps_taken == 1 else [current]):
            for source, default in (('deck', 'symbol'), ('items', 'item')):
                for entry in state.get(source, []):
                    if self.steps_taken == 1 or entry['name'] in newly_owned:
                        owned.append(dict(name=entry['name'], kind=entry.get('kind', default),
                                          rarity=entry.get('rarity', 'common')))

        record = {
            "step": self.steps_taken,
            "seed": int(self.env.seed),
            "phase": previous.get("phase"),
            "next_phase": current.get("phase"),
            "action": int(action),
            "action_kind": action_kind,
            "managed_target": (previous.get("managed_items", [])[action - (USE_BASE if action >= USE_BASE else TOGGLE_BASE)] if TOGGLE_BASE <= action < MANAGED_ACTION_SIZE else None),
            "pillow_declined": action == 0 and not previous.get("rent_pending") and any(
                item.get("can_use") and item.get("name") in ("comfy_pillow", "comfy_pillow_essence")
                for item in previous.get("managed_items", [])),
            "reward_mode": self.reward_mode,
            "rules_version": current.get("rules_version"),
            "reward": round(reward, 5),
            "reward_parts": {
                key: round(value, 5) for key, value in reward_parts.items()
            },
            "before": state_summary(previous),
            "after": state_summary(current),
            "deck_added": deck_added,
            "deck_removed": deck_removed,
            "offer_before": offer_names(previous),
            "offer_after": offer_names(current),
            "truncated": truncated,
            "analysis_decision": decision,
            "analysis_owned": owned,
        }
        if self.full_history:
            from episode_history import attach_episode_history
            return attach_episode_history(self, previous, current, record)
        if current.get('done') or truncated:
            # The vector wrapper may reset immediately. Retain only the few
            # scalar terminal columns needed by the summary, with no deep copy.
            record['episode_end_state'] = {key: current.get(key) for key in
                ('reroll_tokens', 'removal_tokens', 'essence_tokens',
                 'destroyed_symbols', 'removed_symbols', 'failure_reason')}
        return record


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
        env.step(random_policy(env, rng))
    elapsed = time.perf_counter() - start
    print(
        f"steps={args.steps} episodes={episodes} elapsed={elapsed:.2f}s "
        f"steps_per_sec={args.steps / max(elapsed, 1e-9):.1f}"
    )


if __name__ == "__main__":
    main()
