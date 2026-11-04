"""Run the existing Python v13 heuristic against the Rust simulator.

This is the M4b bridge used for apples-to-apples evaluation: the same policy
logic that played the real Godot traces can now drive the Rust environment.
Symbol/item reward choices, skips, reward rerolls and deck removals are wired.

    python godot_kernel/python/v13_rust.py --games 10 --seed 0
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from baseline_policy import BaselinePolicy  # noqa: E402
from rust_env import RustEnv  # noqa: E402
from v3_client import Candidate, Frame  # noqa: E402

ROOT = HERE.parent
SYMBOLS = json.loads((ROOT / "game" / "JSON" / "Symbols - JSON.json").read_text(encoding="utf-8"))
ITEMS = json.loads((ROOT / "game" / "JSON" / "Items - JSON.json").read_text(encoding="utf-8"))
ESSENCES = json.loads((ROOT / "game" / "JSON" / "Essences - JSON.json").read_text(encoding="utf-8"))
VALUES = json.loads((ROOT / "data" / "symbol_values.json").read_text(encoding="utf-8"))
RAW_BASE = {
    name: float(meta.get("raw_base", meta.get("base", 0.0)))
    for name, meta in VALUES["symbols"].items()
}
RARITY = {}
for table in (SYMBOLS, ITEMS):
    for name, entry in table.items():
        RARITY[name] = entry.get("rarity", "none")
for name in ESSENCES:
    RARITY[name] = "essence"
ITEM_NAMES = set(ITEMS)

SKIP_ID = 413
REROLL_ID = 414
ITEM_CANDIDATE_BASE = 400
EMPTY_ID = 999
BOARD_COLS = 5
BOARD_ROWS = 4

BOARD_NEIGHBORS = tuple(
    tuple(
        neighbor_y * BOARD_COLS + neighbor_x
        for dy in (-1, 0, 1)
        for dx in (-1, 0, 1)
        if not (dx == 0 and dy == 0)
        for neighbor_x, neighbor_y in ((x + dx, y + dy),)
        if 0 <= neighbor_x < BOARD_COLS and 0 <= neighbor_y < BOARD_ROWS
    )
    for y in range(BOARD_ROWS)
    for x in range(BOARD_COLS)
)


class FakeClient:
    def __init__(self, game: Path):
        self.game = game
        self.hello = {"vocab": {"rarity": {"symbols": [], "items": [], "essences": []}}}
        self.names: dict[int, str] = {}

    def register(self, type_id: int, name: str) -> None:
        self.names[type_id] = name

    def type_name(self, type_id: int) -> str:
        return self.names.get(type_id, "missing")


class RustV13Policy(BaselinePolicy):
    def __init__(self, seed: int):
        self.client = FakeClient(ROOT / "game")
        super().__init__(self.client, seed)
        self.rarity = RARITY
        self.raw_base = RAW_BASE
        self.use_removals = True
        self.removals = 0

    def _rarity_label(self, candidate):
        return self.rarity.get(self.client.type_name(candidate.type_id), "none")

    def _first_rate(self, name, kind, rarity):
        if name in ITEM_NAMES:
            kind = "item"
        return super()._first_rate(name, kind, rarity)

    def _symbol_features(self, entry):
        raw = self.raw_base.get(entry["name"], float(entry.get("base_value", 0)))
        return (
            raw + float(entry["bonus"]),
            0.0,
            float(entry["multiplier"]),
            0.0,
            float(entry["times_displayed"]),
            0.0,
        )

    def _make_frame(self, obs, candidates, email):
        deck_types = []
        deck_features = []
        for entry in obs["deck"]:
            self.client.register(entry["type_id"], entry["name"])
            deck_types.append(entry["type_id"])
            deck_features.append(self._symbol_features(entry))
        item_types = []
        item_features = []
        for index, entry in enumerate(obs["items"]):
            type_id = 168 + index
            self.client.register(type_id, entry["name"])
            item_types.append(type_id)
            item_features.append((1.0, 0.0, 0.0, 0.0, 0.0, 0.0))
        board_ids = []
        for type_id in obs.get("board", ()):
            if type_id < 0:
                self.client.register(EMPTY_ID, "empty")
                board_ids.append(EMPTY_ID)
            else:
                if type_id not in self.client.names:
                    self.client.register(type_id, "missing")
                board_ids.append(type_id)
        info = {
            "rent_due": obs["rent_due"],
            "rents_paid": obs["rents_paid"],
            "spins": obs["total_spins"],
            "spins_left": obs["spins_left"],
            "deck_len": len(obs["deck"]),
            "email": email,
            "removal_tokens": obs["removal_tokens"],
            "removal_cost": obs["removal_cost"],
            "reroll_tokens": obs["reroll_tokens"],
            "reroll_cost": obs["reroll_cost"],
            "unremovable_types": ["coin", "dud"],
            "removal_tokens_at_rent": obs["removal_tokens"],
        }
        return Frame(
            frame_id=0,
            episode=1,
            kind=0,
            version=4,
            vocab_version=2,
            phi=0.0,
            reward=0.0,
            terminated=False,
            truncated=False,
            end_reason=0,
            spins_elapsed=obs["total_spins"],
            auto_resolved=0,
            scalars=(),
            destroyed_history=(),
            removed_history=(),
            board=tuple(board_ids),
            board_neighbors=BOARD_NEIGHBORS if board_ids else (),
            deck_types=tuple(deck_types),
            deck_features=tuple(deck_features),
            deck_mask=tuple([True] * len(deck_types)),
            deck_uids=tuple(range(len(deck_types))),
            item_types=tuple(item_types),
            item_features=tuple(item_features),
            item_mask=tuple([True] * len(item_types)),
            item_uids=tuple(range(len(item_types))),
            offer_types=tuple(candidate.type_id for candidate in candidates),
            offer_features=tuple(candidate.aux for candidate in candidates),
            candidates=tuple(candidates),
            info=info,
            raw=b"",
        )

    def _candidates(self, obs):
        phase = obs["phase"]
        candidates = []
        mapping = {}
        for entry in obs["offer"]:
            index = entry["index"]
            if phase == "item_reward":
                type_id = ITEM_CANDIDATE_BASE + index
                self.client.register(type_id, entry["name"])
                aux = (1.0, 0.0, 0.0, 0.0, 0.0, 0.0)
            elif phase == "essence_reward":
                type_id = 282 + entry["type_id"]
                self.client.register(type_id, entry["name"])
                aux = (1.0, 0.0, 0.0, 0.0, 0.0, 0.0)
            else:
                type_id = entry["type_id"]
                self.client.register(type_id, entry["name"])
                aux = self._symbol_features(
                    {
                        "name": entry["name"],
                        "base_value": entry.get("base_value", 0),
                        "bonus": 0,
                        "multiplier": 1,
                        "times_displayed": 0,
                    }
                )
            candidates.append(
                Candidate(
                    index=len(candidates),
                    kind=0,
                    reference=index,
                    type_id=type_id,
                    aux=aux,
                )
            )
            mapping[index] = index
        candidates.append(
            Candidate(index=len(candidates), kind=1, reference=None, type_id=SKIP_ID, aux=(0.0,) * 6)
        )
        if phase == "symbol_reward" and obs["can_reroll"]:
            candidates.append(
                Candidate(
                    index=len(candidates),
                    kind=1,
                    reference=None,
                    type_id=REROLL_ID,
                    aux=(0.0,) * 6,
                )
            )
        return candidates

    def _try_remove(self, env: RustEnv, obs) -> bool:
        if not self.use_removals:
            return False
        if not obs["can_remove"] or self.last_removal_spin == obs["total_spins"]:
            return False
        self.unremovable_types = {"coin", "dud"}
        self.current_due = obs["rent_due"]
        tokens = obs["removal_tokens"]
        cost = max(1, obs["removal_cost"])
        reserve = 2 if obs["rent_due"] >= 600 else 1 if obs["rent_due"] >= 300 else 0
        if tokens - cost < reserve:
            return False
        frame = self._make_frame(obs, [], "")
        deck = Counter(entry["name"] for entry in obs["deck"])
        item_counts = Counter(entry["name"] for entry in obs["items"])
        held = set(item_counts)
        owned = set(deck) | held
        ranked = []
        for index, entry in enumerate(obs["deck"]):
            name = entry["name"]
            if name in self.unremovable_types or not entry.get("can_remove", True):
                continue
            features = self._symbol_features(entry)
            score = self._removal_score(entry["type_id"], features, frame, deck, item_counts)
            rank = self._removal_target_rank(
                name, self.rarity.get(name, "none"), features[0], features[1], features[2],
                owned, deck, held, obs["rent_due"], score=score,
                deck_len=len(obs["deck"]), tokens=tokens, cost=cost)
            if rank is not None:
                ranked.append((rank, index))
        if not ranked:
            return False
        _, index = min(ranked)
        if env.remove(index):
            self.last_removal_spin = obs["total_spins"]
            self.removals += 1
            return True
        return False

    def act(self, env: RustEnv) -> int:
        obs = env.observation
        for _ in range(8):
            phase = obs["phase"]
            if phase == "ready":
                self._try_remove(env, obs)
                return 0
            email = (
                "add_tile" if phase == "symbol_reward"
                else "add_item" if phase in ("item_reward", "essence_reward")
                else ""
            )
            candidates = self._candidates(obs)
            frame = self._make_frame(obs, candidates, email)
            chosen, _text = self.choose(frame)
            if chosen.type_id == REROLL_ID:
                if env.reroll():
                    obs = env.observation
                    continue
                return -1
            if chosen.type_id in (412, SKIP_ID):
                return -1
            if chosen.reference is not None:
                return chosen.reference
            return -1
        return -1


def play_game(env: RustEnv, policy: RustV13Policy, seed: int, max_steps: int = 5000) -> dict:
    obs = env.reset(seed)
    steps = 0
    while not obs["done"] and steps < max_steps:
        action = policy.act(env)
        obs = env.step(action)
        steps += 1
    return obs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--games", type=int, default=10)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--no-removal", action="store_true")
    args = parser.parse_args()
    env = RustEnv(args.seed)
    policy = RustV13Policy(args.seed)
    policy.use_removals = not args.no_removal
    results = [play_game(env, policy, args.seed + index) for index in range(args.games)]
    wins = sum(1 for obs in results if obs["won"])
    avg_rents = sum(obs["rents_paid"] for obs in results) / len(results)
    avg_spins = sum(obs["total_spins"] for obs in results) / len(results)
    avg_deck = sum(len(obs["deck"]) for obs in results) / len(results)
    print(
        f"games={args.games} wins={wins} win_rate={wins / args.games:.1%} "
        f"avg_rents={avg_rents:.2f} avg_spins={avg_spins:.1f} avg_deck={avg_deck:.1f} "
        f"removals={policy.removals} per_game={policy.removals / args.games:.1f}"
    )
    print("rents", [obs["rents_paid"] for obs in results])


if __name__ == "__main__":
    main()
