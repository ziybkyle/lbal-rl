"""Convert a Godot run directory into the alignment trace format.

Usage:
    python godot_kernel/python/trace_godot.py --run-dir runs/20261103T205924413682Z_19a9aa81
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MANIFEST = ROOT / "alignment" / "manifest.json"


def read_manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def seed_from_state(state: dict) -> str:
    return str(state.get("info", {}).get("seed") or "unknown")


def normalize_candidate(candidate: dict | None) -> dict | None:
    if not candidate:
        return None
    return {
        "index": candidate.get("index"),
        "name": candidate.get("name"),
        "type_id": candidate.get("type_id"),
    }


def convert_file(path: Path, max_spins: int) -> dict | None:
    header = None
    spins: list[dict] = []
    last_spin = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        state = record.get("state")
        if not isinstance(state, dict):
            continue
        info = state.get("info") or {}
        spin = int(info.get("spins") or 0)
        if spin <= 0:
            continue
        if header is None:
            header = {
                "type": "header",
                "schema": "lbal-trace-v1",
                "seed_text": seed_from_state(state),
                "floor": int(info.get("floor") or 0),
                "build": "",
                "data_sha256": "",
                "mods": [],
                "source": f"godot:{path.name}",
                "initial_deck": sorted(
                    entry.get("name") for entry in state.get("deck", []) if entry.get("name")
                ),
                "initial_items": sorted(
                    entry.get("name") or entry.get("type")
                    for entry in state.get("items", [])
                    if entry.get("name") or entry.get("type")
                ),
            }
        if spin > max_spins:
            break
        if spin != last_spin:
            spins.append({
                "type": "spin",
                "spin": spin,
                "rent_due": int(info.get("rent_due") or 0),
                "rents_paid": int(info.get("rents_paid") or 0),
                "coins": int(info.get("coins") or 0),
                "earned": int(info.get("earned") or 0),
                "spins_left": int(info.get("spins_left") or 0),
                "tokens": {
                    "reroll": int(info.get("reroll_tokens") or 0),
                    "removal": int(info.get("removal_tokens") or 0),
                    "essence": int(info.get("essence_tokens") or 0),
                },
                "board": state.get("board", []),
                "deck": sorted(
                    entry.get("name") for entry in state.get("deck", []) if entry.get("name")
                ),
                "items": sorted(
                    entry.get("name") or entry.get("type")
                    for entry in state.get("items", [])
                    if entry.get("name") or entry.get("type")
                ),
                "candidates": [
                    normalize_candidate(candidate) for candidate in state.get("candidates", [])
                ],
                "chosen": normalize_candidate(record.get("chosen")),
                "events": info.get("events", []),
                "outcome": state.get("outcome"),
            })
            last_spin = spin
        elif record.get("chosen") is not None and spins[-1].get("chosen") is None:
            spins[-1]["chosen"] = normalize_candidate(record.get("chosen"))
    if header is None:
        return None
    header["build"] = read_manifest()["steam_build"]
    header["data_sha256"] = read_manifest()["data_files"]["Symbols - JSON.json"]
    header["mods"] = [f"{key} {value}" for key, value in read_manifest()["mods"].items()]
    return {"header": header, "spins": spins}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--out", default=str(ROOT / "alignment" / "traces" / "godot"))
    parser.add_argument("--max-spins", type=int, default=50)
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    converted = 0
    for path in sorted(run_dir.glob("game_*.jsonl")):
        trace = convert_file(path, args.max_spins)
        if not trace:
            continue
        seed = trace["header"]["seed_text"] or path.stem
        target = out_dir / f"{seed}.jsonl"
        suffix = 1
        while target.exists():
            suffix += 1
            target = out_dir / f"{seed}_{suffix}.jsonl"
        with target.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(trace["header"], ensure_ascii=False) + "\n")
            for spin in trace["spins"]:
                handle.write(json.dumps(spin, ensure_ascii=False) + "\n")
        converted += 1
    print(f"converted {converted} runs to {out_dir}")


if __name__ == "__main__":
    main()
