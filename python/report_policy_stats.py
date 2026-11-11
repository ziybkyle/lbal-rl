"""Generate offline pick and outcome reports from policy games."""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from gym_env import (  # noqa: E402
    ACTION_SIZE,
    OFFER_BASE,
    REMOVE_BASE,
    offer_index,
    RustGymEnv,
    random_policy,
)

RENT_SCHEDULE = [25, 50, 100, 150, 225, 300, 375, 450, 600, 650, 700, 777]


def rent_key(ordinal: int) -> str:
    """Use the rent due at that stage as the bucket key."""
    if ordinal <= len(RENT_SCHEDULE):
        return str(RENT_SCHEDULE[ordinal - 1])
    return str(1000 + (ordinal - len(RENT_SCHEDULE) - 1) * 500)


def is_offer_decision(phase: str, action: int) -> bool:
    # Removing a symbol leaves the same offer pending; count the offer only
    # once it is selected, skipped or rerolled.
    return phase != "ready" and (action in (1, 2) or offer_index(action) is not None)


class StatsCollector:
    def __init__(self) -> None:
        self.games = 0
        self.wins = 0
        self.bucket_decisions: dict[int, int] = defaultdict(int)
        self.bucket_skips: dict[int, int] = defaultdict(int)
        self.bucket_runs: dict[int, set[int]] = defaultdict(set)
        self.bucket_wins: dict[int, set[int]] = defaultdict(set)
        self.meta: dict[str, dict] = {}  # name -> kind/rarity
        self.offered_runs: dict[str, set[int]] = defaultdict(set)
        self.picked_runs: dict[str, set[int]] = defaultdict(set)
        self.owned_runs: dict[str, set[int]] = defaultdict(set)
        self.owned_wins: dict[str, set[int]] = defaultdict(set)
        self.first_offer_n: dict[str, int] = defaultdict(int)
        self.first_pick_n: dict[str, int] = defaultdict(int)
        self.buckets: dict[str, dict[str, dict[str, int]]] = defaultdict(
            lambda: defaultdict(lambda: {"runs": set(), "wins": set(), "offers": 0, "picks": 0})
        )
        self._reset_game()

    def _reset_game(self) -> None:
        self.g_offered: dict[str, int] = {}  # name -> decision idx of first offer
        self.g_picked: set[str] = set()
        self.g_owned: set[str] = set()

    def start_game(self) -> None:
        self._reset_game()

    def record_decision(
        self, ordinal: int, offers: list[dict], action: int, won_so_far: bool = False
    ) -> None:
        self.bucket_decisions[ordinal] += 1
        self.bucket_runs[ordinal].add(self.games)
        key = rent_key(ordinal)
        for entry in offers:
            name = entry.get("name") or ""
            if not name:
                continue
            kind = entry.get("kind", "symbol")
            self.meta.setdefault(name, {"kind": kind, "rarity": entry.get("rarity", "common")})
            cell = self.buckets[name][key]
            cell["offers"] += 1
            cell["runs"].add(self.games)
            self.offered_runs[name].add(self.games)
        chosen_name = None
        if action == 1:
            self.bucket_skips[ordinal] += 1
        elif offer_index(action) is not None:
            target = offer_index(action)
            for entry in offers:
                if entry.get("index") == target:
                    chosen_name = entry.get("name")
                    break
        if chosen_name:
            cell = self.buckets[chosen_name][key]
            cell["picks"] += 1
            self.picked_runs[chosen_name].add(self.games)
            self.g_picked.add(chosen_name)

    def note_first_offer(self, offers: list[dict], action: int) -> None:
        """Mark whether each newly-seen name is picked at its first offer."""
        chosen_name = None
        if offer_index(action) is not None:
            target = offer_index(action)
            for entry in offers:
                if entry.get("index") == target:
                    chosen_name = entry.get("name")
                    break
        newly_seen = set()
        for entry in offers:
            name = entry.get("name") or ""
            if name and name not in self.g_offered:
                self.g_offered[name] = False  # seen at first offer, not yet picked
                self.first_offer_n[name] += 1
                newly_seen.add(name)
        if chosen_name in newly_seen:
            self.first_pick_n[chosen_name] += 1
            self.g_offered[chosen_name] = True

    def note_owned(self, obs: dict) -> None:
        for entry in obs.get("deck") or []:
            name = entry.get("name") or ""
            if name:
                self.meta.setdefault(
                    name, {"kind": "symbol", "rarity": entry.get("rarity", "common")}
                )
                self.g_owned.add(name)
        for entry in obs.get("items") or []:
            name = entry.get("name") or ""
            if not name:
                continue
            kind = entry.get("kind") or (
                "essence" if name.endswith("_essence") else "item"
            )
            self.meta.setdefault(
                name, {"kind": kind, "rarity": entry.get("rarity", "common")}
            )
            self.g_owned.add(name)

    def end_game(self, won: bool, rents_paid: int) -> None:
        gid = self.games
        if won:
            self.wins += 1
        for name in self.g_owned:
            self.owned_runs[name].add(gid)
            if won:
                self.owned_wins[name].add(gid)
        for name in self.g_offered:
            # bucket wins credit for every bucket where the name appeared
            for key, cell in self.buckets[name].items():
                if gid in cell["runs"] and won:
                    cell["wins"].add(gid)
        for ordinal in self.bucket_runs:
            if gid in self.bucket_runs[ordinal] and won:
                self.bucket_wins[ordinal].add(gid)
        self.games += 1
        self._reset_game()


def collect(policy_fn, games: int, seed0: int, max_steps: int, verbose: bool = True) -> StatsCollector:
    col = StatsCollector()
    start = time.perf_counter()
    for g in range(games):
        seed = seed0 + g
        env = RustGymEnv(seed, max_steps, **getattr(policy_fn, 'env_kwargs', {}))
        obs, _ = env.reset(seed)
        col.start_game()
        done = False
        while not done:
            action = policy_fn(env, obs)
            offers = list(obs.get("offer") or [])
            phase = obs.get("phase")
            if is_offer_decision(phase, action):
                ordinal = int(obs.get("rents_paid", 0) or 0) + 1
                col.note_first_offer(offers, action)
                col.record_decision(ordinal, offers, action)
            obs, _, terminated, truncated, _ = env.step(action)
            col.note_owned(obs)
            done = terminated or truncated
        state = env.observation
        col.end_game(bool(state.get("won")), int(state.get("rents_paid", 0) or 0))
        if verbose and (g + 1) % 500 == 0:
            rate = (g + 1) / (time.perf_counter() - start)
            print(f"  played {g + 1}/{games} games ({rate:.0f} games/s)", flush=True)
    return col


def build_dataset(col: StatsCollector, tag: str, label: str) -> dict:
    rows = []
    for name, meta in sorted(col.meta.items()):
        offered = col.offered_runs.get(name, set())
        picked = col.picked_runs.get(name, set())
        owned = col.owned_runs.get(name, set())
        first_offer = col.first_offer_n.get(name, 0)
        first_pick = col.first_pick_n.get(name, 0)
        buckets = {}
        for key, cell in sorted(col.buckets[name].items(), key=lambda kv: int(kv[0])):
            offers = cell["offers"]
            picks = cell["picks"]
            buckets[key] = {
                "runs": len(cell["runs"]),
                "wins": len(cell["wins"]),
                "win_rate": len(cell["wins"]) / len(cell["runs"]) if cell["runs"] else 0.0,
                "offers": offers,
                "picks": picks,
                "pick_rate": picks / offers if offers else 0.0,
            }
        rows.append(
            {
                "name": name,
                "kind": meta["kind"],
                "rarity": meta["rarity"],
                "runs_seen": len(owned),
                "runs_offered": len(offered),
                "runs_picked": len(picked),
                "pick_rate": len(picked) / len(offered) if offered else 0.0,
                "first_offer_n": first_offer,
                "first_pick_n": first_pick,
                "first_pick_rate": first_pick / first_offer if first_offer else 0.0,
                "acq_runs": len(owned),
                "acq_wins": len(col.owned_wins.get(name, set())),
                "acq_win_rate": (
                    len(col.owned_wins.get(name, set())) / len(owned) if owned else 0.0
                ),
                "acq_basis": "owned",
                "buckets": buckets,
            }
        )
    max_ordinal = max(col.bucket_decisions.keys(), default=1)
    buckets = []
    for ordinal in range(1, max_ordinal + 1):
        runs = len(col.bucket_runs.get(ordinal, set()))
        wins = len(col.bucket_wins.get(ordinal, set()))
        decisions = col.bucket_decisions.get(ordinal, 0)
        skips = col.bucket_skips.get(ordinal, 0)
        buckets.append(
            {
                "rent": int(rent_key(ordinal)),
                "ordinal": ordinal,
                "label": f"第{ordinal}次交租前",
                "runs": runs,
                "wins": wins,
                "win_rate": wins / runs if runs else 0.0,
                "decisions": decisions,
                "skips": skips,
                "skip_rate": skips / decisions if decisions else 0.0,
            }
        )
    rarity_counts = defaultdict(int)
    for row in rows:
        rarity_counts[row["rarity"] or "none"] += 1
    return {
        "tag": tag,
        "label": label,
        "ds": "policy",
        "runs": col.games,
        "wins": col.wins,
        "win_rate": col.wins / col.games if col.games else 0.0,
        "symbols": len(rows),
        "rarity_counts": dict(rarity_counts),
        "bucket_basis": "first_acquired_rent",
        "bucket_shift": {"item": -1},
        "acquire_basis": {
            "symbol": "owned",
            "item": "owned",
            "essence": "owned",
            "other": "owned",
        },
        "buckets": buckets,
        "rows": rows,
    }


def write_html(template_path: Path, out_path: Path, dataset: dict) -> None:
    text = template_path.read_text(encoding="utf-8")
    marker = "const DATASETS = "
    start = text.index(marker) + len(marker)
    end = start + json.JSONDecoder().raw_decode(text[start:])[1]
    payload = json.dumps({"policy": dataset}, ensure_ascii=False).replace('<', '\\u003c')
    text = text[:start] + payload + text[end:]
    text = text.replace('const PRIMARY = "asc20"', 'const PRIMARY = "policy"', 1)
    out_path.write_text(text, encoding="utf-8")
    print(f"report written: {out_path}  ({out_path.stat().st_size / 1e6:.2f} MB)")


def make_model_policy(model_path: str):
    from sb3_contrib import MaskablePPO

    model = MaskablePPO.load(model_path)

    def policy(env: RustGymEnv, obs):
        mask = np.asarray(env.action_mask(), dtype=bool)
        from train_ppo import features_for_model

        action, _ = model.predict(features_for_model(obs, model), action_masks=mask, deterministic=True)
        return int(action)

    from train_ppo import MANAGED_OBS_DIM
    from candidate_rules import capacity_for
    version = getattr(model.policy, 'candidate_rules_version', False)
    policy.env_kwargs = {'enable_management': bool(version) or model.observation_space.shape[0] == MANAGED_OBS_DIM,
                         'offer_capacity': capacity_for(version)}
    return policy


def random_masked(env: RustGymEnv, obs) -> int:
    return random_policy(env, random.Random(env.env.seed + env.steps_taken))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--games", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=30_000)
    parser.add_argument("--model", type=str, default="")
    parser.add_argument("--tag", type=str, default="")
    parser.add_argument("--out", type=str, default="")
    parser.add_argument(
        "--template",
        type=str,
        default=str(HERE / 'templates' / 'policy_stats.html'),
    )
    args = parser.parse_args()

    policy_fn = make_model_policy(args.model) if args.model else random_masked
    tag = args.tag or (
        f"ppo_{args.games}" if args.model else f"random_{args.games}"
    )
    label = (
        f"PPO · {args.games} 局" if args.model else f"随机(掩码) · {args.games} 局"
    )
    print(f"playing {args.games} games ({'model ' + args.model if args.model else 'random'})...")
    col = collect(policy_fn, args.games, args.seed, 2000)
    dataset = build_dataset(col, tag, label)
    out = Path(args.out) if args.out else ROOT / f"统计_报告_{tag}.html"
    write_html(Path(args.template), out, dataset)

    # TUI-style console summary
    top = sorted(dataset["rows"], key=lambda r: -r["runs_picked"])[:10]
    print(
        f"\n=== 统计摘要 {tag} ===\n"
        f"局数 {dataset['runs']}  胜率 {dataset['win_rate']:.1%}  "
        f"覆盖条目 {dataset['symbols']}"
    )
    for bucket in dataset["buckets"][:12]:
        print(
            f"  {bucket['label']:<10} 到达 {bucket['runs']:>5}  胜率 "
            f"{bucket['win_rate']:>6.1%}  决策 {bucket['decisions']:>6}  "
            f"跳过率 {bucket['skip_rate']:.1%}"
        )
    print("  最常选中的条目：")
    for row in top:
        print(
            f"    {row['name']:<28} 选中 {row['runs_picked']:>5} 局  "
            f"选中率 {row['pick_rate']:.1%}  持有胜率 {row['acq_win_rate']:.1%}"
        )


if __name__ == "__main__":
    main()
