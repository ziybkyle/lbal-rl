"""Compare real run income against the Rust replay.

The default source is the BetterHistory directory written by the seed mod.
Each real spin is converted to the small JSONL format understood by
`lbal_cli --replay`, then the CLI exports one record per spin.

Examples:
    python godot_kernel/python/align_report.py --max-spins 50
    python godot_kernel/python/align_report.py --source runs --run-dir runs/20261103T205924413682Z_19a9aa81
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULT_HISTORY = Path(os.environ.get(
    "LBAL_BETTER_HISTORY",
    Path.home() / "AppData" / "Roaming" / "Godot" / "app_userdata"
    / "Luck be a Landlord" / "betterHistory",
))
CLI = ROOT / "rust" / "target" / "release" / "lbal_cli.exe"
if not CLI.exists():
    CLI = ROOT / "rust" / "target" / "release" / "lbal_cli"
DATA_DIR = ROOT / "game" / "JSON"
ITEM_NAMES = set(json.loads((DATA_DIR / "Items - JSON.json").read_text(encoding="utf-8")))


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def convert_better_history(history: Path, output_dir: Path, max_spins: int) -> dict:
    runs_dir = history / "runs"
    files = sorted(runs_dir.glob("*.json"))
    meta_by_file: dict[str, dict] = {}
    for path in files:
        run = read_json(path)
        meta = run.get("meta", {})
        items: list[str] = []
        lines: list[dict] = []
        cumulative_income = 0
        spin_count = 0
        for cycle in run.get("rent_cycles", []):
            rent_due = int(cycle.get("rent_required", 0))
            rents_before = int(cycle.get("cycle_index", 1)) - 1
            for spin in cycle.get("spins", []):
                board = [entry.get("id") for entry in spin.get("board_symbols", [])]
                if not board:
                    continue
                spin_count += 1
                if spin_count > max_spins:
                    break
                income = int(spin.get("coin_change", 0)) + 1
                cumulative_income += income
                lines.append({
                    "state": {
                        "info": {
                            "spins": int(spin.get("spin_num", spin_count)),
                            "earned": cumulative_income,
                            "main_symbol": spin.get("main_symbol"),
                            "rent_due": rent_due,
                            "rents_paid": rents_before,
                        },
                        "deck": [{"name": entry.get("id")} for entry in spin.get("deck_symbols", [])],
                        "items": [{"type": name} for name in items],
                        "board": board,
                    }
                })
                for action in spin.get("extra_actions", []):
                    kind = action.get("action")
                    if action.get("type") != "item":
                        continue
                    name = action.get("id")
                    if name not in ITEM_NAMES:
                        continue
                    if kind == "added":
                        items.append(name)
                    elif kind in ("destroyed", "used") and name in items:
                        items.remove(name)
            if spin_count > max_spins:
                break
        if not lines:
            continue
        target = output_dir / f"game_{len(meta_by_file) + 1:05d}.jsonl"
        target.write_text(
            "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
            encoding="utf-8",
        )
        meta_by_file[target.name] = meta
    return meta_by_file


def collect_run_files(run_dir: Path) -> dict:
    meta = {}
    for path in sorted(run_dir.glob("game_*.jsonl")):
        meta[path.name] = {"run_id": run_dir.name}
    return meta


def run_replay(source_dir: Path, max_spins: int, meta_by_file: dict, data_dir: Path) -> list[dict]:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
        export_path = Path(handle.name)
    try:
        command = [
            str(CLI),
            "--replay", str(source_dir),
            "--replay-spins", str(max_spins),
            "--replay-json", str(export_path),
            "--data-dir", str(data_dir),
        ]
        result = subprocess.run(command, check=True, text=True, capture_output=True)
        print(result.stdout.strip())
        if result.stderr.strip():
            print(result.stderr.strip())
        records = json.loads(export_path.read_text(encoding="utf-8"))
    finally:
        export_path.unlink(missing_ok=True)
    for record in records:
        record["run"] = meta_by_file.get(record.get("file"), {}).get("run_id", "")
    return records


def summarize(records: list[dict]) -> dict:
    compared = [record for record in records if record.get("status") == "compared"]
    random = [record for record in records if record.get("status") == "random"]
    missing = [record for record in records if record.get("status") == "missing"]
    exact = [record for record in compared if record.get("exact")]
    diffs = [abs(int(record["game_delta"]) - int(record["rust"])) for record in compared]

    def bucket(spin: int) -> str:
        if spin <= 5:
            return "1-5"
        if spin <= 12:
            return "6-12"
        if spin <= 30:
            return "13-30"
        return "31+"

    by_bucket = defaultdict(lambda: {"total": 0, "exact": 0, "abs_diff": 0})
    by_symbol = defaultdict(lambda: {"total": 0, "exact": 0, "abs_diff": 0})
    by_item = defaultdict(lambda: {"total": 0, "exact": 0, "abs_diff": 0})
    for record in compared:
        diff = abs(int(record["game_delta"]) - int(record["rust"]))
        is_exact = bool(record.get("exact"))
        for table, key in (
            (by_bucket, bucket(int(record.get("spin", 0)))),
            (by_symbol, record.get("main_symbol") or "unknown"),
            (by_item, "has_item" if record.get("items") else "no_item"),
        ):
            table[key]["total"] += 1
            table[key]["exact"] += int(is_exact)
            table[key]["abs_diff"] += diff

    def rows(table):
        return [
            {
                "key": key,
                "total": value["total"],
                "exact": value["exact"],
                "exact_rate": value["exact"] / value["total"],
                "mean_abs_diff": value["abs_diff"] / value["total"],
            }
            for key, value in table.items()
        ]

    mismatches = [
        record for record in compared if not record.get("exact")
    ]
    mismatches.sort(
        key=lambda record: abs(int(record["game_delta"]) - int(record["rust"]))
        , reverse=True
    )
    return {
        "records": len(records),
        "compared": len(compared),
        "exact": len(exact),
        "exact_rate": len(exact) / max(1, len(compared)),
        "mean_abs_diff": sum(diffs) / max(1, len(compared)),
        "random_boards": len(random),
        "missing": len(missing),
        "by_spin": sorted(rows(by_bucket), key=lambda row: ["1-5", "6-12", "13-30", "31+"].index(row["key"])),
        "by_symbol": sorted(rows(by_symbol), key=lambda row: row["total"], reverse=True)[:20],
        "by_item": rows(by_item),
        "top_mismatches": mismatches[:20],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=("betterhistory", "runs"), default="betterhistory")
    parser.add_argument("--history", type=str, default=str(DEFAULT_HISTORY))
    parser.add_argument("--run-dir", type=str, default="")
    parser.add_argument("--max-spins", type=int, default=50)
    parser.add_argument("--data-dir", type=str, default=str(DATA_DIR))
    parser.add_argument("--output", type=str, default="")
    args = parser.parse_args()

    history = Path(args.history)
    with tempfile.TemporaryDirectory(prefix="lbal_align_") as temporary:
        source_dir = Path(temporary)
        if args.source == "runs":
            run_dir = Path(args.run_dir) if args.run_dir else ROOT / "runs" / "20261103T205924413682Z_19a9aa81"
            meta_by_file = collect_run_files(run_dir)
            source_dir = run_dir
        else:
            meta_by_file = convert_better_history(history, source_dir, args.max_spins)
        records = run_replay(source_dir, args.max_spins, meta_by_file, Path(args.data_dir))

    report = summarize(records)
    report["source"] = args.source
    report["history"] = str(history)
    report["max_spins"] = args.max_spins
    report["records_raw"] = records
    if args.source == "betterhistory":
        report["note"] = (
            "BetterHistory 只记录紧凑棋盘和部分实例状态；"
            "后段差异主要来自缺失状态，不能直接当作规则错误。"
            "完整规则对比请用 runs 模式。"
        )

    output = Path(args.output) if args.output else (
        ROOT / "evaluations" / f"align_{args.source}_{args.max_spins}spins.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(
        f"compared={report['compared']} exact={report['exact']} "
        f"exact_rate={report['exact_rate']:.1%} "
        f"mean_abs_diff={report['mean_abs_diff']:.2f} "
        f"random={report['random_boards']} missing={report['missing']}"
    )
    print("by spin:", " | ".join(
        f"{row['key']} {row['exact']}/{row['total']}={row['exact_rate']:.0%}"
        for row in report["by_spin"]
    ))
    print("by item:", " | ".join(
        f"{row['key']} {row['exact']}/{row['total']}={row['exact_rate']:.0%}"
        for row in report["by_item"]
    ))
    print("top symbols:")
    for row in report["by_symbol"][:8]:
        print(
            f"  {row['key']}: {row['exact']}/{row['total']}={row['exact_rate']:.0%} "
            f"mean_diff={row['mean_abs_diff']:.2f}"
        )
    print("worst spins:")
    for record in report["top_mismatches"][:5]:
        print(
            f"  {record.get('run')} spin={record.get('spin')} "
            f"real={record.get('game_delta')} rust={record.get('rust')} "
            f"main={record.get('main_symbol')} items={record.get('items')}"
        )
    if report.get("note"):
        print(f"note: {report['note']}")
    print(f"saved {output}")


if __name__ == "__main__":
    main()
