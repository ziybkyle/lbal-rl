"""Check that the local game data and mods match the alignment manifest.

Usage:
    python godot_kernel/python/check_alignment_versions.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MANIFEST = ROOT / "alignment" / "manifest.json"
DEFAULT_STEAM = Path(os.environ.get(
    "LBAL_STEAM_DIR",
    r"D:\game\steam\steamapps\common\Luck be a Landlord",
))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def check_data(manifest: dict, failures: list[str]) -> None:
    data_dir = ROOT / manifest["data_dir"]
    count_keys = {
        "Symbols - JSON.json": "symbols",
        "Items - JSON.json": "items",
        "Essences - JSON.json": "essences",
        "Fine Print - JSON.json": "fine_prints_total",
    }
    for name, expected_hash in manifest["data_files"].items():
        path = data_dir / name
        if not path.exists():
            failures.append(f"missing data file: {path}")
            continue
        actual = sha256(path)
        if actual != expected_hash:
            failures.append(f"hash mismatch: {name}\n  expected {expected_hash}\n  actual   {actual}")
        key = count_keys.get(name)
        if key:
            actual_count = len(read_json(path))
            expected_count = manifest["counts"][key]
            if actual_count != expected_count:
                failures.append(
                    f"count mismatch: {name} expected {expected_count}, actual {actual_count}"
                )


def check_steam(manifest: dict, steam_dir: Path, failures: list[str]) -> None:
    app_manifest = steam_dir.parent.parent / "appmanifest_1404850.acf"
    if app_manifest.exists():
        text = app_manifest.read_text(encoding="utf-8", errors="ignore")
        match = re.search(r'"buildid"\s+"(\d+)"', text)
        actual = match.group(1) if match else "unknown"
        if actual != manifest["steam_build"]:
            failures.append(
                f"Steam build mismatch: expected {manifest['steam_build']}, actual {actual}"
            )
    else:
        failures.append(f"missing Steam app manifest: {app_manifest}")

    pck = steam_dir / "Luck be a Landlord.pck"
    if pck.exists():
        actual = sha256(pck)
        if actual != manifest["pck_sha256"]:
            failures.append("Steam game package hash mismatch")
    else:
        failures.append(f"missing game package: {pck}")

    mods_dir = steam_dir / "SlotWeave" / "mods"
    if not mods_dir.exists():
        failures.append(f"missing mod directory: {mods_dir}")
        return
    for mod_id, expected_version in manifest["mods"].items():
        if mod_id == "SlotWeave":
            version_file = steam_dir / "SlotWeave" / "version.json"
            if not version_file.exists():
                failures.append("missing SlotWeave version file")
                continue
            actual = read_json(version_file).get("version", "unknown")
        else:
            mod_manifest = mods_dir / mod_id / "manifest.json"
            if not mod_manifest.exists():
                failures.append(f"missing mod: {mod_id}")
                continue
            actual = read_json(mod_manifest).get("Metadata", {}).get("Version", "unknown")
        if actual != expected_version:
            failures.append(
                f"mod version mismatch: {mod_id} expected {expected_version}, actual {actual}"
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default=str(MANIFEST))
    parser.add_argument("--steam-dir", default=str(DEFAULT_STEAM))
    parser.add_argument("--skip-steam", action="store_true")
    args = parser.parse_args()

    manifest = read_json(Path(args.manifest))
    failures: list[str] = []
    check_data(manifest, failures)
    if not args.skip_steam:
        check_steam(manifest, Path(args.steam_dir), failures)

    if failures:
        print("alignment version check failed:")
        for failure in failures:
            print(f"  - {failure}")
        raise SystemExit(1)
    print(
        f"alignment versions ok: build={manifest['steam_build']} "
        f"symbols={manifest['counts']['symbols']} items={manifest['counts']['items']} "
        f"essences={manifest['counts']['essences']}"
    )


if __name__ == "__main__":
    main()
