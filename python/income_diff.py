"""Compare per-slot spin income between the real run and the action replay.

Inputs:
  * the action replay JSON produced by
    `lbal_cli --replay-actions <run dir> --action-json <file>`;
  * the real `game_*.jsonl` trace directory.

Only spins whose board matched exactly and that had no items are used, so the
real `coin_sum` sequence lines up with the Rust per-slot income.

Examples:
    python godot_kernel/python/income_diff.py \
        --action-json godot_kernel/evaluations/action_replay_12g.json \
        --run-dir godot_kernel/runs/20261103T205924413682Z_19a9aa81 \
        --max-spins 12
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def load_events(run_dir: Path, max_spins: int) -> dict[tuple[str, int], list[int]]:
    """Return the coin_sum sequence of the first decision record per spin."""
    events: dict[tuple[str, int], list[int]] = {}
    for path in sorted(run_dir.glob("game_*.jsonl")):
        seen: set[int] = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            state = record.get("state")
            if not state:
                continue
            info = state.get("info", {})
            spin = int(info.get("spins", 0))
            if spin == 0 or spin > max_spins or spin in seen:
                continue
            seen.add(spin)
            events[(path.name, spin)] = [
                int(event.get("amount", 0))
                for event in info.get("events", [])
                if event.get("kind") == "coin_sum"
                and not event.get("rent_payment", False)
            ]
    return events


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--action-json", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--max-spins", type=int, default=12)
    parser.add_argument("--limit", type=int, default=25)
    args = parser.parse_args()

    rows = json.loads(Path(args.action_json).read_text(encoding="utf-8"))
    real_events = load_events(Path(args.run_dir), args.max_spins)

    per_symbol = Counter()
    per_symbol_total = Counter()
    shown = 0
    checked = 0
    for row in rows:
        if "income_ok" not in row or row.get("board_mismatch") or row.get("items"):
            continue
        key = (row["file"], int(row["spin"]))
        events = real_events.get(key, [])
        ours = [entry["value"] for entry in row.get("symbol_income", [])]
        if len(events) != len(ours):
            continue
        checked += 1
        for entry, real in zip(row["symbol_income"], events):
            name = entry["name"]
            per_symbol_total[name] += 1
            if entry["value"] != real:
                per_symbol[name] += 1
                if shown < args.limit:
                    shown += 1
                    print(
                        f"{key[0]} spin={key[1]} slot={entry['slot']} "
                        f"symbol={name} real={real} rust={entry['value']}"
                    )

    print(f"spins with aligned slot sequences: {checked}")
    print("mismatch rate by symbol:")
    for name, total in per_symbol_total.most_common(15):
        bad = per_symbol[name]
        print(f"  {name}: {bad}/{total}")


if __name__ == "__main__":
    main()
