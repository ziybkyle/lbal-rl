"""Play the Rust simulator with the trained numpy behaviour-cloning policy.

Usage:
    python godot_kernel/python/bc_policy.py --games 20 --seed 100
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

from bc_model import BCPolicyNet  # noqa: E402
from collect_bc import record_observation  # noqa: E402
from rust_env import RustEnv  # noqa: E402
from v13_rust import REROLL_ID, RustV13Policy  # noqa: E402

REWARD_PHASES = {"spin_reward", "symbol_reward", "item_reward", "essence_reward"}


def play_game(env: RustEnv, model: BCPolicyNet, teacher: RustV13Policy,
              seed: int, max_steps: int = 5000) -> dict:
    obs = env.reset(seed)
    teacher.last_removal_spin = -1
    steps = 0
    while not obs["done"] and steps < max_steps:
        phase = obs["phase"]
        if phase == "ready":
            teacher._try_remove(env, obs)
            obs = env.step(0)
        elif phase in REWARD_PHASES:
            email = (
                "add_tile" if phase == "symbol_reward"
                else "add_item" if phase in ("item_reward", "essence_reward")
                else ""
            )
            for _ in range(8):
                candidates = teacher._candidates(obs)
                frame = teacher._make_frame(obs, candidates, email)
                chosen, _text = teacher.choose(frame)
                if chosen.type_id == REROLL_ID and env.reroll():
                    obs = env.observation
                    continue
                break
            record = record_observation(obs, 0, 0)
            action = model.predict_action(record)
            obs = env.step(action)
        else:
            obs = env.step(0)
        steps += 1
    return obs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--games", type=int, default=20)
    parser.add_argument("--seed", type=int, default=100)
    parser.add_argument("--model", type=str, default=str(ROOT / "data" / "bc_model.npz"))
    args = parser.parse_args()
    model = BCPolicyNet.load(Path(args.model))
    env = RustEnv(args.seed)
    teacher = RustV13Policy(args.seed)
    start = time.perf_counter()
    results = [
        play_game(env, model, teacher, args.seed + index) for index in range(args.games)
    ]
    elapsed = time.perf_counter() - start
    wins = sum(1 for obs in results if obs["won"])
    rents = [obs["rents_paid"] for obs in results]
    print(
        f"games={args.games} wins={wins} win_rate={wins / max(1, args.games):.1%} "
        f"avg_rents={sum(rents) / max(1, len(rents)):.2f} "
        f"avg_spins={sum(obs['total_spins'] for obs in results) / max(1, len(results)):.1f}"
    )
    print(f"rents={rents}")
    print(f"elapsed={elapsed:.2f}s games_per_sec={args.games / max(elapsed, 1e-9):.1f}")


if __name__ == "__main__":
    main()
