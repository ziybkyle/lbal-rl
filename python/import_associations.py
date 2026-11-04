"""Export the user's editable helper workbook and color rules to JSON.

Read-only on both inputs. Requires openpyxl only for this optional import;
the normal baseline uses the resulting JSON and the Python standard library.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import unicodedata

from openpyxl import load_workbook


def normalize(value):
    return unicodedata.normalize("NFKD", str(value).strip()).encode("ascii", "ignore").decode("ascii").lower().replace(" ", "_")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.output.resolve() in (args.workbook.resolve(), args.source.resolve()):
        parser.error("Output must differ from the inputs")
    wb = load_workbook(args.workbook, read_only=True, data_only=True)
    try:
        rows = list(wb.active.values)
    finally:
        wb.close()
    if not rows or tuple(rows[0][:2]) != ("Internal ID", "中文名"):
        raise ValueError("Unexpected association workbook header")
    names = {}
    edges = set()
    for row in rows[1:]:
        if not row or not row[0]:
            continue
        key = normalize(row[0])
        if key in names:
            raise ValueError(f"Duplicate normalized ID: {key}")
        names[key] = {"display": str(row[1] or ""), "category": str(row[5] or "")}
        for name in str(row[3] or "").split(", "):
            if name:
                edges.add(tuple(sorted((key, normalize(name)))))
    missing = sorted({x for edge in edges for x in edge if x not in names})
    if missing:
        raise ValueError(f"Unknown association references: {missing}")
    tree = ast.parse(args.source.read_text(encoding="utf-8"))
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in ("RED_PAIRS", "GRAY_PAIRS"):
                found[name] = [tuple(sorted(map(normalize, pair)))
                               for pair in ast.literal_eval(node.value.generators[0].iter)]
            elif name == "SELF_POSITIVE":
                found[name] = ast.literal_eval(node.value.args[0].func.value).split()
            elif name in ("SPECIAL_TRIGGER_ITEMS", "STILL_OWNED_EVEN_IF_DESTROYED"):
                found[name] = sorted(ast.literal_eval(node.value))
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
            if (isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "RED_PAIRS" and call.func.attr == "add"):
                pair = ast.literal_eval(call.args[0].args[0])
                found.setdefault("RED_ADDITIONS", []).append(tuple(sorted(map(normalize, pair))))
    if not all(k in found for k in ("RED_PAIRS", "GRAY_PAIRS", "SELF_POSITIVE",
                                     "SPECIAL_TRIGGER_ITEMS", "STILL_OWNED_EVEN_IF_DESTROYED")):
        raise ValueError("Could not read the helper's color rules")
    result = {"schema": 1, "source_workbook": args.workbook.name,
              "workbook_sha256": hashlib.sha256(args.workbook.read_bytes()).hexdigest(),
              "source_code": args.source.name,
              "source_code_sha256": hashlib.sha256(args.source.read_bytes()).hexdigest(),
              "names": names, "pairs": sorted(edges),
              "red_pairs": sorted(set(found["RED_PAIRS"] + found.get("RED_ADDITIONS", []))),
              "gray_pairs": sorted(set(found["GRAY_PAIRS"])),
              "self_positive": sorted(found["SELF_POSITIVE"]),
              "special_trigger_items": found["SPECIAL_TRIGGER_ITEMS"],
              "still_owned_even_if_destroyed": found["STILL_OWNED_EVEN_IF_DESTROYED"]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Exported {len(names)} entries and {len(edges)} association pairs to {args.output}")


if __name__ == "__main__":
    main()
