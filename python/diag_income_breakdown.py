"""Break down the income gap of every first-difference spin.

For the first-divergent decision of each game, compare:
  real_delta   = sum of coin_sum events in the real trace
  real_all     = sum of ALL trailing income_icons values (incl. empty cells)
  real_symbols = sum of non-empty trailing income_icons values
  rust_symbols = sum of rust symbol_income values
  extra        = rust extra_income (item/effect income not tied to a slot)
"""
from __future__ import annotations

import collections
import glob
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACES = [
    ("trace42", "runs/_trace42/20261104T231102157567Z_a20dbd50"),
    ("trace43", "runs/_trace43/20261104T233352939682Z_fc5dc00c"),
    ("trace1", "runs/_trace1/20261105T001628488524Z_da5a8c38"),
]


def eval_path(label):
    matches = glob.glob(os.path.join(ROOT, "evaluations", f"_verify_{label}_v*.json"))
    return max(matches, key=lambda p: int(os.path.basename(p).rsplit("_v", 1)[1].split(".")[0]))


def real_icons(path, spin):
    prev = None
    for line in open(path, encoding="utf-8"):
        rec = json.loads(line)
        if rec.get("record") != "decision":
            continue
        info = rec["state"].get("info") or {}
        cur = info.get("spins")
        if cur != spin or cur == prev:
            continue
        prev = cur
        entries = (info.get("income_icons") or [])[-20:]
        all_sum = sum(e[3] for e in entries if len(e) >= 4)
        sym_sum = sum(e[3] for e in entries if len(e) >= 4 and e[2] != "empty")
        empty = [(e[1] * 5 + e[0], e[3]) for e in entries if len(e) >= 4 and e[2] == "empty" and e[3]]
        return all_sum, sym_sum, empty
    return 0, 0, []


def first_diff(records):
    for r in records:
        if r.get("income_ok") is None:
            continue
        if r.get("offer_checked", True) and not r.get("offer_ok"):
            return "offer", r
        if not r.get("deck_ok"):
            return "deck", r
        if not r.get("items_ok"):
            return "items", r
        if r.get("board_mismatch"):
            return "board", r
        if not r.get("income_ok"):
            return "income", r
    return None, None


def main():
    totals = collections.Counter()
    for label, trace in TRACES:
        recs = json.load(open(eval_path(label), encoding="utf-8"))
        games = collections.OrderedDict()
        for r in recs:
            games.setdefault(r["file"], []).append(r)
        for name, records in games.items():
            kind, rec = first_diff(records)
            if kind is None:
                continue
            spin = rec["spin"]
            all_sum, sym_sum, empty = real_icons(os.path.join(ROOT, trace, name), spin)
            rust_sym = sum(e["value"] for e in (rec.get("symbol_income") or []))
            extra = rec.get("extra_income") or 0
            real = rec.get("real_delta") or 0
            rust = rec.get("rust_income") or 0
            totals[kind] += 1
            print(f"{label:8s} {name} spin {spin:2d} {kind:6s} real={real:4d} rust={rust:4d} "
                  f"gap={real-rust:+3d} | real_all={all_sum:4d} real_sym={sym_sum:4d} "
                  f"rust_sym={rust_sym:4d} extra={extra:4d} sym_gap={sym_sum-rust_sym:+3d} "
                  f"empty={empty} items={rec.get('items')}")
    print(dict(totals))


if __name__ == "__main__":
    main()
