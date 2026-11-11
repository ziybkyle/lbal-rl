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
import json
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
from reward_schemes import (PROGRESSIVE_SCALE, DEFAULT_PROGRESSIVE_RENTS, DEFAULT_PROGRESSIVE_LOSS,
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

# ---- 奖励公式（v3，2026-11-06）-----------------------------------------
# 目标优先级：先最大化通关率（打死房东），胜率 ≥99% 后再放大金钱项微调"赢得
# 更富"。金钱奖励有三个层次（用户要求金钱要有奖励，v3 起全部启用）：
#   1. Φ 金钱进度（权重最大、最安全）：min(金币,本期租金)/租金 的差分。这是
#      势函数塑形，奖得再重也不会教出囤钱/刷分（差分望远镜求和，原地不动赚
#      不到分），且租金归一化天然对暴击旋减力；
#   2. 安全垫奖励 RENT_COVER_BONUS：余额首次跨过"够付下一次租金"时 +2，
#      一次性触发、反复进出无利可图；
#   3. 每旋金钱项（小额）：log1p(income/50) + 单步截断，只当"顺手赚的钱"
#      的甜头，总量 ≈1 个付租里程碑，不会盖过通关目标。
# 付租那一步不做 Φ 塑形（租金上涨导致的 Φ 重置不是退步），由里程碑奖励
# 单独覆盖。
# 物品管理模式在每次旋转前对同一组只允许切换一次，并对每次切换扣分。
RENT_MILESTONE = 10.0  # 每付清一次租金
WIN_REWARD = 100.0  # 击杀房东
LOSS_REWARD = -5.0  # 付不起租输掉
SHAPE_COEF = 4.0  # ΔΦ 权重（Φ 本身即金钱进度奖励，v3 加大）
SHAPE_CLIP = 1.0  # 单步塑形上限（防单次暴击主导梯度）
BOSS_COEF = 5.0  # 房东战内 Δ(伤害/1500) 权重
RENT_COVER_BONUS = 2.0  # 余额首次够付下一次租金（安全垫）
MONEY_BONUS_COEF = 1.0  # 每旋金钱项权重（log+clip，单步 ≤0.15）
MONEY_BONUS_SCALE = 50.0  # log1p(income / SCALE)：对暴击旋减力
MONEY_BONUS_CLIP = 0.15  # 单步金钱项上限
TOGGLE_COST = -0.05  # 每次成功开关物品或精华


def rent_progress(obs: dict) -> float:
    """Φ：离付清本期租金还有多近，∈ [0,1]。租金归一化自带减力。"""
    rent = max(1.0, float(obs.get("rent_due", 1) or 1))
    coins = max(0.0, float(obs.get("coins", 0) or 0))
    return min(coins, rent) / rent


def boss_progress(obs: dict) -> float:
    """已对房东造成的伤害比例（A20 血量 1500）。"""
    hp = max(0.0, float(obs.get("boss_hp", 0) or 0))
    return min(max(0.0, 1500.0 - hp), 1500.0) / 1500.0


REWARD_MODES = ("legacy", "sparse", "potential", "fixed", "win_potential", "progressive", "progress_potential")
WIN_POTENTIAL_FILE = HERE.parent / "data" / "scratch_win_potential_8000.json"
WIN_POTENTIAL_DATA = (json.loads(WIN_POTENTIAL_FILE.read_text(encoding="utf-8"))
                      if WIN_POTENTIAL_FILE.is_file() else None)
WIN_POTENTIALS = (tuple(float(row["probability"]) for row in WIN_POTENTIAL_DATA["stages"])
                  if WIN_POTENTIAL_DATA is not None else ())
if WIN_POTENTIAL_DATA is not None and (len(WIN_POTENTIALS) != 13 or
        any(not math.isfinite(p) or not 0 <= p <= 1 for p in WIN_POTENTIALS)):
    raise ValueError("Invalid scratch win-potential table")


def win_stage_potential(obs: dict) -> float:
    """Terminal potential is zero; truncation alone does not make a state terminal."""
    if not WIN_POTENTIALS:
        raise ValueError("win_potential requires a local stage-potential table")
    if obs.get("done", False):
        return 0.0
    index = max(0, min(12, int(obs.get("rents_paid", 0) or 0)))
    return WIN_POTENTIALS[index]


def progress_potential(obs: dict) -> float:
    """Rent/boss progress advance; only a genuine terminal clears it."""
    if obs.get("done", False):
        return 0.0
    if obs.get("boss_fight", False):
        hp = obs.get("boss_hp")
        hp = 1500.0 if hp is None else max(0.0, min(1500.0, float(hp)))
        return 0.25 + 0.25 * (1500.0 - hp) / 1500.0
    rents = max(0, min(12, int(obs.get("rents_paid", 0) or 0)))
    return 0.25 * rents * (rents + 1) / 156.0


def reward_potential(obs: dict) -> float:
    """Bounded state potential; only true terminal states have zero potential."""
    if obs.get("done", False):
        return 0.0
    rent = max(1.0, float(obs.get("rent_due", 1) or 1))
    coins = max(0.0, float(obs.get("coins", 0) or 0))
    covered = min(coins / rent, 1.0)
    surplus = max(0.0, coins - rent)
    cushion = surplus / (rent + surplus)
    boss = boss_progress(obs) if obs.get("boss_fight", False) else 0.0
    return 0.06 * covered + 0.02 * cushion + 0.06 * boss


def outcome_reward_breakdown(previous: dict, current: dict, mode: str, gamma: float,
                             progressive_rents=None, progressive_loss=DEFAULT_PROGRESSIVE_LOSS,
                             progressive_win=DEFAULT_PROGRESSIVE_WIN) -> dict[str, float]:
    """Win-only reward, optionally with gamma-consistent potential shaping."""
    if mode == "progress_potential":
        if gamma != 1.0:
            raise ValueError("Progress-potential rewards require gamma=1")
        terminal = float(bool(current.get("done")) and bool(current.get("won")) and not previous.get("done"))
        return {"terminal": terminal, "progress_potential": progress_potential(current) - progress_potential(previous)}
    if mode == "win_potential":
        if gamma != 1.0:
            raise ValueError("Win-potential rewards require gamma=1")
        terminal = float(bool(current.get("done")) and bool(current.get("won")) and not previous.get("done"))
        return {"terminal": terminal, "stage_potential": win_stage_potential(current) - win_stage_potential(previous)}
    if mode == "progressive":
        before = max(0, min(int(previous.get("rents_paid", 0) or 0), 12))
        after = max(0, min(int(current.get("rents_paid", 0) or 0), 12))
        rewards = DEFAULT_PROGRESSIVE_RENTS if progressive_rents is None else progressive_rents
        rent = sum(rewards[k - 1] for k in range(before + 1, after + 1))
        ended = bool(current.get("done")) and not previous.get("done")
        terminal = (progressive_win if current.get("won") else progressive_loss) if ended else 0.0
        return {"rent": rent / PROGRESSIVE_SCALE, "terminal": terminal / PROGRESSIVE_SCALE}
    if mode == "fixed":
        rent = max(0, min(int(current.get("rents_paid", 0)), 12) - min(int(previous.get("rents_paid", 0)), 12))
        win = bool(current.get("done")) and bool(current.get("won")) and not previous.get("done")
        return {"rent": float(rent), "terminal": 20.0 if win else 0.0}
    if mode not in ("sparse", "potential"):
        raise ValueError(f"Unsupported outcome reward mode: {mode}")
    if not 0.0 < gamma <= 1.0:
        raise ValueError("Reward discount must be in (0, 1]")
    terminal = float(bool(current.get("done")) and bool(current.get("won")) and not previous.get("done"))
    shaping = gamma * reward_potential(current) - reward_potential(previous) if mode == "potential" else 0.0
    return {"terminal": terminal, "potential": shaping}


class RustGymEnv:
    def __init__(self, seed: int = 0, max_steps: int = 2000, data_dir: str | None = None,
                 reward_mode: str = "legacy", reward_gamma: float = 0.99, enable_management: bool = False,
                 toggle_reward: float = TOGGLE_COST, allow_item_toggle: bool = True,
                 progressive_rents=None, progressive_loss=DEFAULT_PROGRESSIVE_LOSS,
                 progressive_win=DEFAULT_PROGRESSIVE_WIN, offer_capacity: int = MAX_OFFERS,
                 limited_item_toggles: bool = False, item_toggle_allowlist=None, training_practice=None,
                 training_shaping=None, shaping_snapshot=None, full_history: bool = False):
        if reward_mode not in REWARD_MODES:
            raise ValueError(f"Unsupported reward mode: {reward_mode}")
        if reward_mode == "win_potential" and not WIN_POTENTIALS:
            raise ValueError("win_potential requires a local stage-potential table")
        if not 0.0 < reward_gamma <= 1.0:
            raise ValueError("Reward discount must be in (0, 1]")
        if reward_mode in ("win_potential", "progress_potential") and reward_gamma != 1.0:
            raise ValueError("Win-aligned potential rewards require gamma=1")
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
        if self.reward_mode == "legacy":
            reward_parts = self.reward_breakdown(previous, new_obs)
        else:
            reward_parts = outcome_reward_breakdown(previous, new_obs, self.reward_mode, self.reward_gamma,
                                                     self.progressive_rents, self.progressive_loss, self.progressive_win)
        if TOGGLE_BASE <= action < USE_BASE:
            reward_parts["item_toggle"] = self.toggle_reward / (PROGRESSIVE_SCALE if self.reward_mode == "progressive" else 1.0)
        shaping_raw, shaping_detail = {}, {}
        if self.training_shaper:
            target = offer_index(action)
            chosen = next((entry for entry in previous.get('offer', []) if entry['index'] == target), None) if target is not None else None
            shaping_raw, shaping_detail = self.training_shaper.step(previous, new_obs, chosen)
            scale = PROGRESSIVE_SCALE if self.reward_mode == 'progressive' else 1.0
            reward_parts.update({key: value / scale for key, value in shaping_raw.items()})
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

    @staticmethod
    def reward(previous: dict, current: dict) -> float:
        return sum(RustGymEnv.reward_breakdown(previous, current).values())

    @staticmethod
    def reward_breakdown(previous: dict, current: dict) -> dict[str, float]:
        parts = {
            "rent": 0.0,
            "money_progress": 0.0,
            "boss_progress": 0.0,
            "rent_cover": 0.0,
            "spin_income": 0.0,
            "terminal": 0.0,
        }
        rents = max(0, current["rents_paid"] - previous["rents_paid"])
        parts["rent"] = RENT_MILESTONE * rents
        if rents == 0:
            # 付租那一步跳过 Φ 塑形：租金上涨导致的 Φ 重置不是退步。
            delta = rent_progress(current) - rent_progress(previous)
            parts["money_progress"] = SHAPE_COEF * max(
                -SHAPE_CLIP, min(SHAPE_CLIP, delta)
            )
            if current.get("boss_fight"):
                delta = boss_progress(current) - boss_progress(previous)
                parts["boss_progress"] = BOSS_COEF * max(0.0, min(1.0, delta))
        # 安全垫：余额首次跨过"够付下一次租金"给一次性奖励。反复进出无利
        # （只有从不够→够 的那一次触发）。
        prev_due = max(1.0, float(previous.get("rent_due", 1) or 1))
        cur_due = max(1.0, float(current.get("rent_due", 1) or 1))
        prev_coins = max(0.0, float(previous.get("coins", 0) or 0))
        cur_coins = max(0.0, float(current.get("coins", 0) or 0))
        if prev_coins < prev_due and cur_coins >= cur_due:
            parts["rent_cover"] = RENT_COVER_BONUS
        spins = current["total_spins"] - previous["total_spins"]
        income = current.get("last_income", 0) if spins > 0 else 0
        if MONEY_BONUS_COEF > 0 and income > 0:
            bonus = math.log1p(income / MONEY_BONUS_SCALE)
            parts["spin_income"] = MONEY_BONUS_COEF * min(bonus, MONEY_BONUS_CLIP)
        if current["won"] and not previous["won"]:
            parts["terminal"] = WIN_REWARD
        elif current["done"] and not current["won"]:
            parts["terminal"] = LOSS_REWARD
        return parts


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
