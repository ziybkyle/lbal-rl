"""Compare real-rule batches against a Rust evaluation report.

Examples:
    python godot_kernel/python/compare_runs.py \
        --real-runs runs/20261103T195835516121Z_d70346ad runs/20261103T205924413682Z_19a9aa81 runs/20261104T164112742370Z_1fb33e4c \
        --rust-report evaluations/v13-seed0_600.json
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def wilson_interval(wins: int, games: int, z: float = 1.96) -> tuple[float, float]:
    if games == 0:
        return 0.0, 0.0
    p = wins / games
    denominator = 1.0 + z * z / games
    centre = (p + z * z / (2.0 * games)) / denominator
    half = z * math.sqrt(p * (1.0 - p) / games + z * z / (4.0 * games * games)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def summarize(rows: list[dict], won_key: str, rents_key: str, spins_key: str) -> dict:
    games = len(rows)
    wins = sum(1 for row in rows if row[won_key])
    low, high = wilson_interval(wins, games)
    rents = [int(row[rents_key]) for row in rows]
    return {
        "games": games,
        "wins": wins,
        "win_rate": wins / games,
        "win_rate_low": low,
        "win_rate_high": high,
        "avg_rents": sum(rents) / games,
        "avg_spins": sum(int(row[spins_key]) for row in rows) / games,
        "pass_rates": {
            str(k): sum(1 for value in rents if value >= k) / games for k in range(1, 13)
        },
    }


def read_real(run_dirs: list[Path]) -> list[dict]:
    rows: list[dict] = []
    for run_dir in run_dirs:
        path = run_dir / "summary.jsonl"
        if not path.exists():
            raise SystemExit(f"missing {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            rows.append({
                "won": item.get("outcome") == "win",
                "rents_paid": int(item.get("rents_paid", 0)),
                "total_spins": int(item.get("spins", 0)),
            })
    return rows


def read_rust(report_path: Path) -> list[dict]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return report["rows"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--real-runs", nargs="+", required=True)
    parser.add_argument("--rust-report", required=True)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    real_rows = read_real([Path(path) for path in args.real_runs])
    rust_rows = read_rust(Path(args.rust_report))
    real = summarize(real_rows, "won", "rents_paid", "total_spins")
    rust = summarize(rust_rows, "won", "rents_paid", "total_spins")

    print(
        f"real: games={real['games']} win={real['win_rate']:.1%} "
        f"CI={real['win_rate_low']:.1%}-{real['win_rate_high']:.1%} "
        f"rents={real['avg_rents']:.2f} spins={real['avg_spins']:.1f}"
    )
    print(
        f"rust: games={rust['games']} win={rust['win_rate']:.1%} "
        f"CI={rust['win_rate_low']:.1%}-{rust['win_rate_high']:.1%} "
        f"rents={rust['avg_rents']:.2f} spins={rust['avg_spins']:.1f}"
    )
    print("rent  real   rust   diff")
    for key in map(str, range(1, 13)):
        real_rate = real["pass_rates"][key]
        rust_rate = rust["pass_rates"][key]
        print(f"{key:>4}  {real_rate:>5.1%}  {rust_rate:>5.1%}  {rust_rate - real_rate:>+6.1%}")

    output = Path(args.output) if args.output else (
        Path(__file__).resolve().parent.parent / "evaluations" / "compare_real_vs_rust.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps({"real": real, "rust": rust}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"saved {output}")


if __name__ == "__main__":
    main()
