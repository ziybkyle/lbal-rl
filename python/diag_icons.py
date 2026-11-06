"""Per-slot income diff between the real game trace and a Rust replay.

The real-game trace records `info.income_icons` as a growing list of
[x, y, name, value] entries; each spin appends one entry per displayed icon.
The Rust replay records `symbol_income` as [slot, value] for the same spin.

Usage:
    python godot_kernel/python/diag_icons.py <trace-game.jsonl> <eval.json> <file-name>

Example:
    python godot_kernel/python/diag_icons.py \
        runs/_trace42/<run>/game_00039.jsonl evaluations/_verify_trace42_v42.json game_00039.jsonl
"""
from __future__ import annotations

import collections
import json
import sys


def load_real_slots(trace_path: str) -> dict[int, dict[int, tuple[str, int]]]:
    """Return {spin: {slot: (name, value)}} from a real-game trace.

    `income_icons` is a cumulative list that also carries entries from earlier
    games in the same batch, so the current spin's board is the trailing block
    of at most 20 entries.
    """
    per_spin: dict[int, dict[int, tuple[str, int]]] = {}
    seen_spin = None
    for line in open(trace_path, encoding="utf-8"):
        record = json.loads(line)
        if record.get("record") != "decision":
            continue
        info = record["state"].get("info") or {}
        spin = info.get("spins")
        icons = info.get("income_icons") or []
        if not spin or spin == seen_spin or not icons:
            continue
        seen_spin = spin
        slots: dict[int, tuple[str, int]] = {}
        for entry in icons[-20:]:
            if len(entry) < 4:
                continue
            x, y, name, value = entry[0], entry[1], entry[2], entry[3]
            if name == "empty":
                continue
            slots[y * 5 + x] = (name, value)
        per_spin.setdefault(spin, {}).update(slots)
    return per_spin


def load_rust_slots(eval_path: str, file_name: str) -> dict[int, dict[int, int]]:
    records = json.load(open(eval_path, encoding="utf-8"))
    per_spin: dict[int, dict[int, int]] = {}
    for record in records:
        if record.get("file") != file_name:
            continue
        spin = record.get("spin")
        if not spin or not record.get("symbol_income"):
            continue
        slots = {entry["slot"]: entry["value"] for entry in record["symbol_income"]}
        per_spin.setdefault(spin, {}).update(slots)
    return per_spin


def main() -> None:
    if len(sys.argv) < 4:
        print(__doc__)
        raise SystemExit(2)
    trace_path, eval_path, file_name = sys.argv[1:4]
    real = load_real_slots(trace_path)
    rust = load_rust_slots(eval_path, file_name)

    totals = collections.Counter()
    for spin in sorted(set(real) | set(rust)):
        real_slots = real.get(spin, {})
        rust_slots = rust.get(spin, {})
        rows = []
        for slot in sorted(set(real_slots) | set(rust_slots)):
            real_name, real_value = real_slots.get(slot, ("-", 0))
            rust_value = rust_slots.get(slot, 0)
            if real_name != "-" and real_value != rust_value:
                rows.append((slot, real_name, real_value, rust_value))
        for slot, name, real_value, rust_value in rows:
            totals[name] += real_value - rust_value
        if rows:
            print(f"spin {spin}:")
            for slot, name, real_value, rust_value in rows:
                print(f"  slot {slot:2d} {name:18s} real {real_value:3d}  rust {rust_value:3d}  diff {real_value - rust_value:+d}")
    if totals:
        print("summary (real - rust):")
        for name, delta in totals.most_common():
            print(f"  {name:18s} {delta:+d}")


if __name__ == "__main__":
    main()
