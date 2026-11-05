"""Compare two alignment traces and report the first differences.

Usage:
    python godot_kernel/python/trace_diff.py --godot alignment/traces/godot/B2AG00000000.jsonl --rust alignment/traces/rust/0.jsonl
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

IGNORE_KEYS = {"source", "build", "data_sha256", "mods", "initial_deck", "initial_items", "type_id"}


def load(path: Path) -> tuple[dict | None, dict[int, dict]]:
    header = None
    spins: dict[int, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("type") == "header":
            header = record
        elif record.get("type") == "spin":
            spins[int(record["spin"])] = record
    return header, spins


def compare(path: str, left, right, diffs: list[dict], limit: int) -> None:
    if len(diffs) >= limit:
        return
    if type(left) is not type(right):
        diffs.append({"path": path, "left": left, "right": right})
        return
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            if key in IGNORE_KEYS:
                continue
            if key not in left:
                diffs.append({"path": f"{path}.{key}", "left": None, "right": right[key]})
            elif key not in right:
                diffs.append({"path": f"{path}.{key}", "left": left[key], "right": None})
            else:
                compare(f"{path}.{key}", left[key], right[key], diffs, limit)
            if len(diffs) >= limit:
                return
        return
    if isinstance(left, list):
        if len(left) != len(right):
            diffs.append({"path": f"{path}.length", "left": len(left), "right": len(right)})
            return
        for index, (a, b) in enumerate(zip(left, right)):
            compare(f"{path}[{index}]", a, b, diffs, limit)
            if len(diffs) >= limit:
                return
        return
    if left != right:
        diffs.append({"path": path, "left": left, "right": right})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--godot", required=True)
    parser.add_argument("--rust", required=True)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    left_header, left = load(Path(args.godot))
    right_header, right = load(Path(args.rust))
    diffs: list[dict] = []
    compare("header", left_header, right_header, diffs, args.limit)
    for spin in sorted(set(left) | set(right)):
        if spin not in left:
            diffs.append({"path": f"spin[{spin}]", "left": None, "right": "present"})
        elif spin not in right:
            diffs.append({"path": f"spin[{spin}]", "left": "present", "right": None})
        else:
            compare(f"spin[{spin}]", left[spin], right[spin], diffs, args.limit)
        if len(diffs) >= args.limit:
            break

    if not diffs:
        print(f"traces match: {len(left)} spins")
        return
    print(f"differences: {len(diffs)} (limit {args.limit})")
    for diff in diffs:
        left_value = json.dumps(diff["left"], ensure_ascii=False)[:120]
        right_value = json.dumps(diff["right"], ensure_ascii=False)[:120]
        print(f"  {diff['path']}\n    left : {left_value}\n    right: {right_value}")
    raise SystemExit(1)


if __name__ == "__main__":
    main()
