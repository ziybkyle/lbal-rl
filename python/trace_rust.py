"""Export a Rust run trace in the alignment format.

Usage:
    python godot_kernel/python/trace_rust.py --seed 0 --out alignment/traces/rust/0.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from rust_env import RustEnv  # noqa: E402
from v13_rust import RustV13Policy  # noqa: E402

SYMBOLS = json.loads((ROOT / "game" / "JSON" / "Symbols - JSON.json").read_text(encoding="utf-8"))
ITEMS = json.loads((ROOT / "game" / "JSON" / "Items - JSON.json").read_text(encoding="utf-8"))
ESSENCES = json.loads((ROOT / "game" / "JSON" / "Essences - JSON.json").read_text(encoding="utf-8"))
MANIFEST = json.loads((ROOT / "alignment" / "manifest.json").read_text(encoding="utf-8"))


def symbol_name(type_id: int) -> str:
    if type_id < 0:
        return "empty"
    names = list(SYMBOLS)
    return names[type_id] if type_id < len(names) else f"missing_{type_id}"


def normalize_deck(deck: list[dict]) -> list[str]:
    return sorted(entry["name"] for entry in deck if entry.get("name"))


def normalize_items(items: list[dict]) -> list[str]:
    return sorted(entry["name"] for entry in items if entry.get("name"))


def normalize_offer(offer: list[dict]) -> list[dict]:
    return [
        {"index": 0, "type_id": 413, "name": "skip"},
        *[
            {
                "index": index + 1,
                "type_id": entry.get("type_id"),
                "name": entry.get("name"),
            }
            for index, entry in enumerate(offer)
        ],
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--seed-text", default="")
    parser.add_argument("--out", default="")
    parser.add_argument("--max-spins", type=int, default=50)
    args = parser.parse_args()

    if args.seed_text:
        env = RustEnv(0, seed_text=args.seed_text)
        policy = RustV13Policy(0)
        obs = env.reset_text(args.seed_text)
        seed_label = args.seed_text
    else:
        env = RustEnv(args.seed)
        policy = RustV13Policy(args.seed)
        obs = env.reset(args.seed)
        seed_label = str(args.seed)
    output = Path(args.out) if args.out else (
        ROOT / "alignment" / "traces" / "rust" / f"{seed_label}.jsonl"
    )
    output.parent.mkdir(parents=True, exist_ok=True)

    header = {
        "type": "header",
        "schema": "lbal-trace-v1",
        "seed_text": seed_label,
        "floor": 20,
        "build": MANIFEST["steam_build"],
        "data_sha256": MANIFEST["data_files"]["Symbols - JSON.json"],
        "mods": [],
        "source": "rust",
        "initial_deck": normalize_deck(obs["deck"]),
        "initial_items": normalize_items(obs["items"]),
    }
    spins: list[dict] = []
    total_income = 0
    current: dict | None = None
    steps = 0
    while not obs["done"] and steps < 5000 and len(spins) < args.max_spins:
        if obs["phase"] == "ready":
            current = {
                "type": "spin",
                "spin": obs["total_spins"] + 1,
                "rent_due": obs["rent_due"],
                "rents_paid": obs["rents_paid"],
                "coins": obs["coins"],
                "earned": total_income,
                "spins_left": obs["spins_left"],
                "tokens": {
                    "reroll": obs["reroll_tokens"],
                    "removal": obs["removal_tokens"],
                    "essence": obs["essence_tokens"],
                },
                "board": [],
                "deck": normalize_deck(obs["deck"]),
                "items": normalize_items(obs["items"]),
                "candidates": [],
                "chosen": None,
                "events": [],
                "outcome": "running",
            }
            obs = env.step(0)
            total_income += int(obs.get("last_income", 0))
            current["coins"] = obs["coins"]
            current["earned"] = total_income
            current["spins_left"] = obs["spins_left"]
            current["board"] = [symbol_name(int(type_id)) for type_id in obs.get("board", [])]
            current["outcome"] = "win" if obs["won"] else ("done" if obs["done"] else "running")
            current["candidates"] = normalize_offer(obs["offer"])
            spins.append(current)
            steps += 1
            continue
        action = policy.act(env)
        obs = env.observation
        if current is not None and obs["phase"] in (
            "spin_reward", "symbol_reward", "item_reward", "essence_reward"
        ):
            current["candidates"] = normalize_offer(obs["offer"])
            if action >= 0 and action < len(obs["offer"]):
                current["chosen"] = current["candidates"][action + 1]
            elif action < 0:
                current["chosen"] = current["candidates"][0]
        obs = env.step(action)
        steps += 1

    with output.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(header, ensure_ascii=False) + "\n")
        for spin in spins:
            handle.write(json.dumps(spin, ensure_ascii=False) + "\n")
    print(f"wrote {len(spins)} spins to {output}")


if __name__ == "__main__":
    main()
