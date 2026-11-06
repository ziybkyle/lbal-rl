"""Aggregate every mismatch across all three replay datasets (not just first diffs)."""
from __future__ import annotations

import collections
import glob
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LABELS = ["trace42", "trace43", "trace1"]


def eval_path(label):
    matches = glob.glob(os.path.join(ROOT, "evaluations", f"_verify_{label}_v*.json"))
    return max(matches, key=lambda p: int(os.path.basename(p).rsplit("_v", 1)[1].split(".")[0]))


def main():
    by_kind = collections.Counter()
    gaps = collections.Counter()
    deck_extra = collections.Counter()
    deck_missing = collections.Counter()
    board_move = collections.Counter()
    items_at = collections.Counter()
    spins = collections.Counter()
    lines = []
    for label in LABELS:
        recs = json.load(open(eval_path(label), encoding="utf-8"))
        for r in recs:
            if r.get("income_ok") is None:
                continue
            kinds = []
            if r.get("offer_checked", True) and not r.get("offer_ok"):
                kinds.append("offer")
            if not r.get("deck_ok"):
                kinds.append("deck")
                dm = r.get("deck_mismatch") or {}
                for n in dm.get("extra", []):
                    deck_extra[n] += 1
                for n in dm.get("missing", []):
                    deck_missing[n] += 1
            if not r.get("items_ok"):
                kinds.append("items")
            bm = r.get("board_mismatch") or []
            if bm:
                kinds.append("board")
                for e in bm:
                    board_move[(e["real"], e["rust"])] += 1
            if not r.get("income_ok"):
                kinds.append("income")
                gaps[(r.get("real_delta") or 0) - (r.get("rust_income") or 0)] += 1
            if kinds:
                key = "+".join(kinds)
                by_kind[key] += 1
                spins[(r.get("spin"), key)] += 1
                for i in r.get("items") or []:
                    items_at[(i, key)] += 1
                lines.append((label, r["file"], r.get("spin"), key,
                              (r.get("real_delta") or 0) - (r.get("rust_income") or 0),
                              r.get("deck_mismatch"), bm, r.get("items")))
    print("total mismatching decisions:", len(lines))
    print("by kind:", dict(by_kind))
    print("income gaps:", dict(sorted(gaps.items())))
    print("deck extra:", deck_extra.most_common(25))
    print("deck missing:", deck_missing.most_common(25))
    print("board moves:", board_move.most_common(15))
    print("spin x kind:", sorted(spins.items())[:40])
    print("\nitems present in mismatching decisions (top 40):")
    for (i, k), n in items_at.most_common(40):
        print(f"  {i:28s} {k:22s} {n}")
    print("\nall mismatching decisions:")
    for label, name, spin, key, gap, dm, bm, items in lines:
        print(f"{label:8s} {name} spin {spin:2d} {key:25s} gap={gap:+3d} deck={dm} board={bm} items={items}")


if __name__ == "__main__":
    main()
