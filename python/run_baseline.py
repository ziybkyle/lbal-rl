"""Run floor-20 baselines with durable per-decision JSONL records."""
from __future__ import annotations

import argparse
import base64
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
import shutil
import sys
import time
import uuid

from baseline_policy import BaselinePolicy
from report_batch import render_batch
from v3_client import Candidate, EngineError, Frame, ROOT, V3Client

SCHEMA = 1


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_record(stream, value) -> None:
    stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    stream.flush()


def candidate_data(c: Candidate, frame: Frame, client: V3Client) -> dict:
    replies = frame.info.get("candidate_replies", [])
    name = client.type_name(c.type_id)
    data = {"index": c.index, "kind": c.kind, "type_id": c.type_id,
            "name": name, "display_name": getattr(client, "name_labels", {}).get(name, name), "reference": c.reference,
            "reply": replies[c.index] if c.index < len(replies) else None, "aux": c.aux}
    if c.kind == 3 and c.reference is not None:
        index = c.reference - 116
        if 0 <= index < len(frame.item_types) and frame.item_mask[index]:
            item = client.type_name(frame.item_types[index])
            data["target_item"] = item
            data["target_item_display"] = getattr(client, "name_labels", {}).get(item, item)
    return data


def snapshot(frame: Frame, client: V3Client) -> dict:
    labels = getattr(client, "name_labels", {})
    deck = [{"type_id": t, "name": client.type_name(t), "display_name": labels.get(client.type_name(t)), "uid": uid, "features": features}
            for t, uid, features, present in zip(frame.deck_types, frame.deck_uids,
                                                  frame.deck_features, frame.deck_mask) if present]
    items = [{"type_id": t, "name": client.type_name(t), "display_name": labels.get(client.type_name(t)), "uid": uid, "features": features}
             for t, uid, features, present in zip(frame.item_types, frame.item_uids,
                                                   frame.item_features, frame.item_mask) if present]
    return {"frame_id": frame.frame_id, "episode": frame.episode, "kind": frame.kind,
            "version": frame.version, "vocab_version": frame.vocab_version,
            "outcome": frame.outcome, "end_reason": frame.end_reason,
            "terminated": frame.terminated, "truncated": frame.truncated,
            "spins_elapsed": frame.spins_elapsed, "auto_resolved": frame.auto_resolved,
            "info": frame.info, "board": [client.type_name(t) for t in frame.board],
            "deck": deck, "items": items,
            "candidates": [candidate_data(c, frame, client) for c in frame.candidates],
            # Allows later extraction of all original observation features.
            "raw_frame_base64": base64.b64encode(frame.raw).decode("ascii")}


class RandomPolicy:
    VERSION = "random-v1"

    def __init__(self, client: V3Client, seed: int):
        self.rng = random.Random(seed)

    def choose(self, frame: Frame) -> tuple[Candidate, str]:
        # Spin at ready; otherwise uniformly choose without spending resources
        # or toggling items. This intentionally weak policy tests the machinery.
        choices = [c for c in frame.candidates if c.type_id == 430]
        if not choices:
            choices = [c for c in frame.candidates if c.type_id not in (414, 417, 418, 431)]
        if not choices:
            choices = list(frame.candidates)
        if not choices:
            raise ValueError("No legal candidates in a running frame")
        return self.rng.choice(choices), "随机基线"


def run_game(client: V3Client, file: Path, seed: str, policy_name: str,
             policy_seed: int, max_decisions: int) -> dict:
    start = time.perf_counter()
    frame = None
    count = 0
    action_counts = Counter()
    events = []
    error = None
    outcome = "invalid"
    policy = (BaselinePolicy if policy_name == "heuristic" else RandomPolicy)(client, policy_seed)
    engine_log = client._root / "engine.log"
    log_offset = engine_log.stat().st_size if engine_log.exists() else 0
    with file.open("x", encoding="utf-8", newline="\n") as stream:
        append_record(stream, {"record": "episode_start", "schema": SCHEMA,
                               "seed": seed, "floor": 20, "policy": policy.VERSION,
                               "policy_seed": policy_seed})
        try:
            frame = client.reset(seed, floor=20)
            if (frame.info.get("actual_floor") != 20 or not frame.info.get("boss_unlocked") or
                    not frame.info.get("essences_unlocked")):
                raise ValueError("Floor 20, boss or essences were not enabled")
            while True:
                events.extend(frame.info.get("events", []))
                if frame.outcome != "running":
                    outcome = frame.outcome
                    append_record(stream, {"record": "terminal_frame", "state": snapshot(frame, client)})
                    break
                if count >= max_decisions:
                    outcome = "decision_limit"
                    append_record(stream, {"record": "limit_frame", "state": snapshot(frame, client)})
                    break
                action, explanation = policy.choose(frame)
                if action not in frame.candidates:
                    raise ValueError("Policy returned an unavailable action")
                append_record(stream, {"record": "decision", "step": count,
                                       "state": snapshot(frame, client),
                                       "chosen": candidate_data(action, frame, client),
                                       "explanation": explanation})
                action_counts[f"{action.kind}:{client.type_name(action.type_id)}"] += 1
                count += 1
                frame = client.step(action.index)
        except (EngineError, OSError, ValueError, KeyError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            append_record(stream, {"record": "error", "step": count, "error": error})
        # Godot can report a script error and still return a frame. Such a run
        # must not count as a win or loss even when the protocol remains alive.
        if engine_log.exists():
            with engine_log.open("rb") as log:
                log.seek(log_offset)
                text = log.read().decode("utf-8", errors="replace")
            bad = [line.strip() for line in text.splitlines()
                   if "SCRIPT ERROR" in line or "Parse Error" in line or "[RL] FATAL" in line]
            if bad:
                error = "Engine script error: " + " | ".join(bad[:5])
                outcome = "invalid"
                append_record(stream, {"record": "error", "step": count, "error": error})
        info = frame.info if frame else {}
        result = {"schema": SCHEMA, "seed": seed, "floor": 20, "policy": policy.VERSION,
                  "outcome": outcome, "end_reason": frame.end_reason if frame else None,
                  "decisions": count, "spins": info.get("spins"),
                  "rents_paid": info.get("rents_paid"), "rent_due": info.get("rent_due"),
                  "balance": info.get("coins"), "boss_defeated": info.get("boss_defeated"),
                  "boss_hp": info.get("hp") if info.get("boss") or info.get("boss_defeated") else None,
                  "unresolved": info.get("unresolved"), "truncated": frame.truncated if frame else None,
                  "rent_events": [e for e in events if e.get("kind") == "rent_paid"],
                  "action_counts": dict(action_counts), "error": error,
                  "seconds": round(time.perf_counter() - start, 3), "record_file": file.name}
        append_record(stream, {"record": "episode_end", "summary": result})
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games", type=int, default=100)
    parser.add_argument("--policy", choices=("heuristic", "random"), default="heuristic")
    parser.add_argument("--seed", type=int, default=1, help="Batch seed (non-negative integer)")
    parser.add_argument("--max-decisions", type=int, default=3000)
    parser.add_argument("--output", type=Path, default=ROOT / "runs")
    args = parser.parse_args(argv)
    if args.games < 1 or args.max_decisions < 1 or args.seed < 0:
        parser.error("games and max-decisions must be positive; seed must be non-negative")
    batch_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "_" + uuid.uuid4().hex[:8]
    output = args.output.resolve() / batch_id
    output.mkdir(parents=True, exist_ok=False)
    game_dir = ROOT / "game"
    files = sorted(p for p in game_dir.rglob("*") if p.is_file()
                   and (p.suffix.lower() in (".gd", ".tscn") or
                        (p.suffix.lower() == ".json" and p.parent.name == "JSON")))
    manifest = {"schema": SCHEMA, "batch_id": batch_id, "created_utc": datetime.now(timezone.utc).isoformat(),
                "floor": 20, "policy": args.policy, "seed": args.seed, "requested_games": args.games,
                "max_decisions": args.max_decisions,
                "game_files_sha256": {p.relative_to(game_dir).as_posix(): sha256(p) for p in files},
                "policy_sha256": sha256(Path(__file__).with_name("baseline_policy.py")),
                "association_rules_sha256": sha256(ROOT / "data" / "association_rules.json"),
                "baseline_priors_sha256": sha256(ROOT / "data" / "baseline_priors.json"),
                "symbol_values_sha256": sha256(ROOT / "data" / "symbol_values.json"),
                "symbol_value_code_sha256": sha256(Path(__file__).with_name("symbol_value.py")),
                "symbol_value_builder_sha256": sha256(Path(__file__).with_name("build_symbol_values.py")),
                "runtime_sha256": sha256(ROOT / "runtime" / "godot.exe")}
    write_json(output / "manifest.json", manifest)
    results = []
    started = time.perf_counter()
    try:
        with V3Client() as client, (output / "summary.jsonl").open("x", encoding="utf-8", newline="\n") as summary:
            if client.startup_warnings:
                write_json(output / "startup_warnings.json", client.startup_warnings)
            association_data = json.loads((ROOT / "data" / "association_rules.json").read_text(encoding="utf-8"))
            client.name_labels = {key: value["display"] for key, value in association_data["names"].items()}
            write_json(output / "protocol_hello.json", client.hello)
            for i in range(args.games):
                seed = f"B{args.seed:X}G{i:08X}"
                result = run_game(client, output / f"game_{i + 1:05d}.jsonl", seed,
                                  args.policy, args.seed * 1000003 + i, args.max_decisions)
                results.append(result)
                append_record(summary, result)
                print(f"{i+1}/{args.games}: {result['outcome']}, spins={result['spins']}, "
                      f"rent={result['rent_due']}, {result['seconds']:.2f}s", flush=True)
                if result["error"]:
                    print("Episode error; stopping batch:", result["error"], file=sys.stderr)
                    break
            client._log.flush()
            shutil.copyfile(client._root / "engine.log", output / "engine.log")
    except (EngineError, OSError) as exc:
        write_json(output / "batch_error.json", {"error": f"{type(exc).__name__}: {exc}"})
        print(f"Engine startup/batch error: {exc}", file=sys.stderr)
    counts = Counter(r["outcome"] for r in results)
    valid = counts["win"] + counts["loss"]
    summary_data = {"requested_games": args.games, "completed_games": len(results), "counts": dict(counts),
                    "valid_games": valid, "valid_win_rate": counts["win"] / valid if valid else None,
                    "excluded_games": len(results) - valid,
                    "elapsed_seconds_including_startup": round(time.perf_counter() - started, 3)}
    write_json(output / "batch_summary.json", summary_data)
    render_batch(output)
    print("Batch:", json.dumps(summary_data, ensure_ascii=False), "\nRecords:", output, flush=True)
    return 1 if len(results) != args.games or counts["invalid"] or counts["decision_limit"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
