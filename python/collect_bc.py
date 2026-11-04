"""Collect behaviour-cloning samples from v13 playing the Rust simulator.

Each record captures the decision-time observation (scalars, deck size, held
items and the current offer) plus the offer index chosen by v13. Skip actions
are stored as -1. The game result is attached to every record of that game.

Usage:
    python godot_kernel/python/collect_bc.py --games 20 --seed 0 --out data/bc_v13.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

from rust_env import RustEnv  # noqa: E402
from v13_rust import RustV13Policy  # noqa: E402

REWARD_PHASES = {"spin_reward", "symbol_reward", "item_reward", "essence_reward"}


def record_observation(obs: dict, action: int, game: int) -> dict:
    return {
        "game": game,
        "phase": obs["phase"],
        "coins": obs["coins"],
        "rent_due": obs["rent_due"],
        "rents_paid": obs["rents_paid"],
        "spins_left": obs["spins_left"],
        "total_spins": obs["total_spins"],
        "last_income": obs["last_income"],
        "removal_tokens": obs["removal_tokens"],
        "reroll_tokens": obs["reroll_tokens"],
        "essence_tokens": obs["essence_tokens"],
        "can_remove": obs["can_remove"],
        "can_reroll": obs["can_reroll"],
        "deck_len": len(obs["deck"]),
        "item_count": len(obs["items"]),
        "deck": [
            {"name": entry["name"], "rarity": entry["rarity"]}
            for entry in obs["deck"]
        ],
        "items": [
            {"name": entry["name"], "rarity": entry["rarity"], "kind": entry["kind"]}
            for entry in obs["items"]
        ],
        "offer": [
            {
                "index": entry["index"],
                "type_id": entry["type_id"],
                "kind": entry["kind"],
                "rarity": entry["rarity"],
                "base_value": entry.get("base_value", 0),
                "name": entry["name"],
            }
            for entry in obs["offer"]
        ],
        "action": action,
    }


def collect(games: int, seed: int, out_path: Path) -> int:
    env = RustEnv(seed)
    policy = RustV13Policy(seed)
    records: list[dict] = []
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        for index in range(games):
            obs = env.reset(seed + index)
            steps = 0
            while not obs["done"] and steps < 5000:
                action = policy.act(env)
                obs = env.observation
                if obs["phase"] in REWARD_PHASES:
                    records.append(record_observation(obs, action, index))
                obs = env.step(action)
                steps += 1
            result = {"won": bool(obs["won"]), "rents_paid": obs["rents_paid"],
                      "total_spins": obs["total_spins"]}
            for record in records:
                if record["game"] == index:
                    record.update(result)
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return len(records)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--games", type=int, default=20)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", type=str, default=str(ROOT / "data" / "bc_v13.jsonl"))
    args = parser.parse_args()
    count = collect(args.games, args.seed, Path(args.out))
    print(f"games={args.games} decisions={count} out={args.out}")


if __name__ == "__main__":
    main()
