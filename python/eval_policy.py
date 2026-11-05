"""Run a policy on a fixed seed range and report a comparable score.

Examples:
    python godot_kernel/python/eval_policy.py --policy native --games 2000 --seed 1
    python godot_kernel/python/eval_policy.py --policy v13 --games 600 --seed 0
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from rust_env import RustEnv  # noqa: E402
from v13_rust import RustV13Policy, play_game  # noqa: E402

CLI = ROOT / "rust" / "target" / "release" / "lbal_cli.exe"
if not CLI.exists():
    CLI = ROOT / "rust" / "target" / "release" / "lbal_cli"
DATA_DIR = ROOT / "game" / "JSON"


def wilson_interval(wins: int, games: int, z: float = 1.96) -> tuple[float, float]:
    if games == 0:
        return 0.0, 0.0
    p = wins / games
    denominator = 1.0 + z * z / games
    centre = (p + z * z / (2.0 * games)) / denominator
    half = z * math.sqrt(p * (1.0 - p) / games + z * z / (4.0 * games * games)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def summarize(rows: list[dict], label: str) -> dict:
    games = len(rows)
    wins = sum(1 for row in rows if row["won"])
    low, high = wilson_interval(wins, games)
    rents = [int(row["rents_paid"]) for row in rows]
    histogram = {str(value): rents.count(value) for value in range(13)}
    pass_rates = {
        str(threshold): sum(1 for value in rents if value >= threshold) / games
        for threshold in range(1, 13)
    }
    return {
        "label": label,
        "games": games,
        "wins": wins,
        "losses": games - wins,
        "win_rate": wins / games,
        "win_rate_low": low,
        "win_rate_high": high,
        "avg_rents": sum(rents) / games,
        "avg_spins": sum(int(row["total_spins"]) for row in rows) / games,
        "avg_deck": sum(int(row["deck_len"]) for row in rows) / games,
        "avg_final_rent": sum(int(row["final_rent"]) for row in rows) / games,
        "rent_histogram": histogram,
        "rent_pass_rates": pass_rates,
    }


def run_native(games: int, seed: int, threads: int, data_dir: Path) -> list[dict]:
    output = HERE / "_eval_native.json"
    command = [
        str(CLI),
        "--games", str(games),
        "--seed", str(seed),
        "--threads", str(threads),
        "--results-json", str(output),
        "--data-dir", str(data_dir),
    ]
    subprocess.run(command, check=True, cwd=str(ROOT / "rust"))
    rows = json.loads(output.read_text(encoding="utf-8"))
    output.unlink(missing_ok=True)
    return rows


def run_v13(games: int, seed: int, use_removals: bool) -> list[dict]:
    env = RustEnv(seed, str(DATA_DIR))
    policy = RustV13Policy(seed)
    policy.use_removals = use_removals
    rows: list[dict] = []
    start = time.perf_counter()
    previous_removals = 0
    for index in range(games):
        obs = play_game(env, policy, seed + index)
        removals = policy.removals - previous_removals
        previous_removals = policy.removals
        rows.append({
            "seed": seed + index,
            "won": bool(obs["won"]),
            "rents_paid": int(obs["rents_paid"]),
            "total_spins": int(obs["total_spins"]),
            "deck_len": len(obs["deck"]),
            "final_rent": int(obs["rent_due"]),
            "coins": int(obs["coins"]),
            "removals": int(removals),
        })
        if (index + 1) % 100 == 0:
            print(f"  {index + 1}/{games} games", file=sys.stderr)
    print(f"  elapsed={time.perf_counter() - start:.1f}s", file=sys.stderr)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", choices=("native", "v13"), default="v13")
    parser.add_argument("--games", type=int, default=600)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--threads", type=int, default=16)
    parser.add_argument("--data-dir", type=str, default=str(DATA_DIR))
    parser.add_argument("--output", type=str, default="")
    parser.add_argument("--label", type=str, default="")
    parser.add_argument("--no-removal", action="store_true")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if args.policy == "native":
        rows = run_native(args.games, args.seed, args.threads, data_dir)
        default_label = f"native-seed{args.seed}"
    else:
        rows = run_v13(args.games, args.seed, not args.no_removal)
        default_label = f"v13-seed{args.seed}"
    label = args.label or default_label
    report = summarize(rows, label)
    report["policy"] = args.policy
    report["seed"] = args.seed
    report["rows"] = rows

    output = Path(args.output) if args.output else (
        ROOT / "evaluations" / f"{label}_{args.games}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(
        f"{label}: games={report['games']} wins={report['wins']} "
        f"win_rate={report['win_rate']:.1%} "
        f"95%CI={report['win_rate_low']:.1%}-{report['win_rate_high']:.1%} "
        f"avg_rents={report['avg_rents']:.2f} avg_spins={report['avg_spins']:.1f} "
        f"avg_deck={report['avg_deck']:.1f} avg_final_rent={report['avg_final_rent']:.1f}"
    )
    print("rent_pass", " ".join(
        f"{threshold}:{rate:.1%}" for threshold, rate in report["rent_pass_rates"].items()
    ))
    print(f"saved {output}")


if __name__ == "__main__":
    main()
