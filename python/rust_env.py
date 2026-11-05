"""Drive the Rust simulator from Python through the PyO3 bindings.

Build the extension once per Rust change:

    cd godot_kernel/rust
    PYO3_USE_RAW_DYLIB=0 cargo build --release -p lbal_py
    cp target/release/lbal_py.dll python_bindings/lbal_py.pyd

`PYO3_USE_RAW_DYLIB=0` avoids rustc's dlltool step on the Windows GNU
toolchain; the module then links against the installed CPython library.

Run a short multi-game smoke test:

    python godot_kernel/python/rust_env.py --games 20
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
import time
from pathlib import Path

try:
    import numpy as _np
except Exception:  # pragma: no cover - numpy is optional
    _np = None

ROOT = Path(__file__).resolve().parent.parent
BINDINGS = ROOT / "rust" / "python_bindings"
if str(BINDINGS) not in sys.path:
    sys.path.insert(0, str(BINDINGS))

import lbal_py  # noqa: E402

PRIORS = json.loads((ROOT / "data" / "baseline_priors.json").read_text(encoding="utf-8"))
FILLING_SCORES = PRIORS["filling_symbol_scores"]
EARLY_ITEM_SCORES = PRIORS["early_item_scores"]
RARITY_RANK = {"none": 0, "common": 1, "uncommon": 2, "rare": 3, "very_rare": 4}


class RustEnv:
    def __init__(self, seed: int = 0, data_dir: str | None = None, seed_text: str | None = None):
        self._env = lbal_py.Env(seed, data_dir, seed_text)
        self.seed = seed
        self.seed_text = seed_text
        self.observation = json.loads(self._env.observe())

    def reset(self, seed: int) -> dict:
        self.seed = seed
        self.seed_text = None
        self.observation = json.loads(self._env.reset(seed))
        return self.observation

    def reset_text(self, seed_text: str) -> dict:
        self.seed_text = seed_text
        self.observation = json.loads(self._env.reset_text(seed_text))
        return self.observation

    def step(self, action: int) -> dict:
        self.observation = json.loads(self._env.step(action))
        return self.observation

    def reroll(self) -> bool:
        ok = self._env.reroll()
        if ok:
            self.observation = json.loads(self._env.observe())
        return ok

    def remove(self, index: int) -> bool:
        ok = self._env.remove(index)
        if ok:
            self.observation = json.loads(self._env.observe())
        return ok


class BatchRustEnv:
    """Runs many Rust environments and resolves their steps in parallel."""

    def __init__(self, count: int, seed: int = 0, data_dir: str | None = None):
        self._env = lbal_py.BatchEnv(count, seed, data_dir)
        self.count = count
        self.seed = seed
        self.observations = [json.loads(text) for text in self._env.observe_all()]

    def reset_all(self, seed: int | None = None) -> list[dict]:
        self.seed = self.seed if seed is None else seed
        self.observations = [json.loads(text) for text in self._env.reset_all(self.seed)]
        return self.observations

    def reset_at(self, index: int, seed: int) -> dict:
        self.observations[index] = json.loads(self._env.reset_at(index, seed))
        return self.observations[index]

    def step(self, index: int, action: int) -> dict:
        self.observations[index] = json.loads(self._env.step(index, action))
        return self.observations[index]

    def step_batch(self, actions: list[int]) -> list[dict]:
        self.observations = [
            json.loads(text) for text in self._env.step_batch(list(actions))
        ]
        return self.observations


COMPACT_HEADER_FIELDS = (
    "magic", "phase", "coins", "rent_due", "rents_paid", "spins_left",
    "total_spins", "last_income", "removal_tokens", "reroll_tokens",
    "essence_tokens", "boss_fight", "boss_hp", "deck_len", "item_count",
    "candidate_count", "done", "won",
)


def parse_compact(buffer: bytes) -> dict:
    """Parse the packed i32 observation produced by `observe_compact`."""
    if _np is not None:
        words = _np.frombuffer(buffer, dtype="<i4")
        header = {name: int(words[index]) for index, name in enumerate(COMPACT_HEADER_FIELDS)}
        offset = len(COMPACT_HEADER_FIELDS)
        deck = words[offset:offset + header["deck_len"] * 4].reshape(-1, 4)
        offset += header["deck_len"] * 4
        items = words[offset:offset + header["item_count"] * 2].reshape(-1, 2)
        offset += header["item_count"] * 2
        candidates = words[offset:offset + header["candidate_count"] * 2].reshape(-1, 2)
        offset += header["candidate_count"] * 2
        board = words[offset:offset + 20]
        return {"header": header, "deck": deck, "items": items,
                "candidates": candidates, "board": board}
    header_values = struct.unpack_from("<18i", buffer, 0)
    header = dict(zip(COMPACT_HEADER_FIELDS, header_values))
    offset = 18 * 4
    deck = []
    for _ in range(header["deck_len"]):
        deck.append(struct.unpack_from("<4i", buffer, offset))
        offset += 16
    items = []
    for _ in range(header["item_count"]):
        items.append(struct.unpack_from("<2i", buffer, offset))
        offset += 8
    candidates = []
    for _ in range(header["candidate_count"]):
        candidates.append(struct.unpack_from("<2i", buffer, offset))
        offset += 8
    board = struct.unpack_from("<20i", buffer, offset)
    return {"header": header, "deck": deck, "items": items,
            "candidates": candidates, "board": board}


class CompactBatchEnv:
    """Batch environment using packed observations instead of JSON."""

    def __init__(self, count: int, seed: int = 0, data_dir: str | None = None):
        self._env = lbal_py.BatchEnv(count, seed, data_dir)
        self.count = count
        self.seed = seed
        self.observations = [parse_compact(buffer) for buffer in self._env.observe_compact_all()]

    def reset_all(self, seed: int | None = None) -> list[dict]:
        self.seed = self.seed if seed is None else seed
        self.observations = [parse_compact(buffer) for buffer in self._env.reset_all_compact(self.seed)]
        return self.observations

    def reset_at(self, index: int, seed: int) -> dict:
        self.observations[index] = parse_compact(self._env.reset_at_compact(index, seed))
        return self.observations[index]

    def step_batch(self, actions: list[int]) -> list[dict]:
        self.observations = [parse_compact(buffer) for buffer in self._env.step_batch_compact(list(actions))]
        return self.observations


def compact_policy(obs: dict) -> int:
    header = obs["header"]
    phase = header["phase"]
    if phase in (0, 5):
        return 0
    if header["candidate_count"] > 0:
        return 0
    return -1


def greedy_policy(obs: dict) -> int:
    phase = obs["phase"]
    if phase == "ready":
        return 0
    offer = obs["offer"]
    if phase in ("spin_reward", "symbol_reward"):
        def symbol_score(entry):
            name = entry["name"]
            score = FILLING_SCORES.get(name)
            if score is None:
                score = entry.get("base_value", 0) + 2
            return score

        best = max(range(len(offer)), key=lambda index: symbol_score(offer[index]))
        if len(obs["deck"]) >= 20 and symbol_score(offer[best]) < 2:
            return -1
        return best
    if phase == "item_reward":
        return max(
            range(len(offer)),
            key=lambda index: (
                EARLY_ITEM_SCORES.get(offer[index]["name"], 0),
                RARITY_RANK.get(offer[index]["rarity"], 0),
            ),
        )
    return -1


def play_game(env: RustEnv, seed: int, policy=greedy_policy, max_steps: int = 5000) -> dict:
    obs = env.reset(seed)
    steps = 0
    while not obs["done"] and steps < max_steps:
        obs = env.step(policy(obs))
        steps += 1
    return obs


def benchmark(games: int, seed: int) -> None:
    env = RustEnv(seed)
    start = time.perf_counter()
    results = [play_game(env, seed + index) for index in range(games)]
    elapsed = time.perf_counter() - start
    wins = sum(1 for obs in results if obs["won"])
    avg_rents = sum(obs["rents_paid"] for obs in results) / max(1, len(results))
    avg_spins = sum(obs["total_spins"] for obs in results) / max(1, len(results))
    avg_deck = sum(len(obs["deck"]) for obs in results) / max(1, len(results))
    print(
        f"games={games} wins={wins} win_rate={wins / max(1, games):.1%} "
        f"avg_rents={avg_rents:.2f} avg_spins={avg_spins:.1f} avg_deck={avg_deck:.1f}"
    )
    print(f"elapsed={elapsed:.2f}s games_per_sec={games / max(elapsed, 1e-9):.1f}")


def benchmark_batch(games: int, seed: int, batch_size: int) -> None:
    env = BatchRustEnv(batch_size, seed)
    observations = env.reset_all(seed)
    next_seed = seed + batch_size
    results: list[dict] = []
    start = time.perf_counter()
    while len(results) < games:
        actions = [greedy_policy(obs) for obs in observations]
        observations = env.step_batch(actions)
        for index, obs in enumerate(observations):
            if not obs["done"]:
                continue
            if len(results) < games:
                results.append(obs)
            observations[index] = env.reset_at(index, next_seed)
            next_seed += 1
    elapsed = time.perf_counter() - start
    wins = sum(1 for obs in results if obs["won"])
    avg_rents = sum(obs["rents_paid"] for obs in results) / max(1, len(results))
    avg_spins = sum(obs["total_spins"] for obs in results) / max(1, len(results))
    avg_deck = sum(len(obs["deck"]) for obs in results) / max(1, len(results))
    print(
        f"games={games} batch={batch_size} wins={wins} "
        f"win_rate={wins / max(1, games):.1%} avg_rents={avg_rents:.2f} "
        f"avg_spins={avg_spins:.1f} avg_deck={avg_deck:.1f}"
    )
    print(f"elapsed={elapsed:.2f}s games_per_sec={games / max(elapsed, 1e-9):.1f}")


def benchmark_compact(games: int, seed: int, batch_size: int) -> None:
    env = CompactBatchEnv(batch_size, seed)
    observations = env.reset_all(seed)
    next_seed = seed + batch_size
    results: list[dict] = []
    start = time.perf_counter()
    while len(results) < games:
        actions = [compact_policy(obs) for obs in observations]
        observations = env.step_batch(actions)
        for index, obs in enumerate(observations):
            if not obs["header"]["done"]:
                continue
            if len(results) < games:
                results.append(obs)
            env.reset_at(index, next_seed)
            next_seed += 1
    elapsed = time.perf_counter() - start
    wins = sum(1 for obs in results if obs["header"]["won"])
    avg_rents = sum(obs["header"]["rents_paid"] for obs in results) / max(1, len(results))
    avg_spins = sum(obs["header"]["total_spins"] for obs in results) / max(1, len(results))
    avg_deck = sum(obs["header"]["deck_len"] for obs in results) / max(1, len(results))
    print(
        f"games={games} batch={batch_size} compact=1 wins={wins} "
        f"win_rate={wins / max(1, games):.1%} avg_rents={avg_rents:.2f} "
        f"avg_spins={avg_spins:.1f} avg_deck={avg_deck:.1f}"
    )
    print(f"elapsed={elapsed:.2f}s games_per_sec={games / max(elapsed, 1e-9):.1f}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--games", type=int, default=20)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--batch", type=int, default=1)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    if args.compact:
        benchmark_compact(args.games, args.seed, max(1, args.batch))
    elif args.batch > 1:
        benchmark_batch(args.games, args.seed, args.batch)
    else:
        benchmark(args.games, args.seed)


if __name__ == "__main__":
    main()
