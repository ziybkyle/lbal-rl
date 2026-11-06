"""Full first-difference report for real-game vs Rust action replays.

For each game in the three trace sets this finds the first decision where any
checked field diverges, then prints the complete per-slot income table for that
spin (real `income_icons` vs Rust `symbol_income`), the deck/item mismatches and
the held items.  A summary clusters the per-slot value gaps by symbol name.

Usage:
    python python/diag_all.py                 # all games, compact
    python python/diag_all.py --kind income  # only one first-diff kind
    python python/diag_all.py --game game_00002.jsonl --full
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TRACE_DIRS = [
    ("trace42", "runs/_trace42/20261104T231102157567Z_a20dbd50"),
    ("trace43", "runs/_trace43/20261104T233352939682Z_fc5dc00c"),
    ("trace1", "runs/_trace1/20261105T001628488524Z_da5a8c38"),
]


def load_datasets(version=None):
    """Pick the newest `_verify_<label>_vNN.json` unless `version` is given."""
    out = []
    for label, trace in TRACE_DIRS:
        if version:
            ev = f"evaluations/_verify_{label}_{version}.json"
        else:
            import glob
            matches = glob.glob(os.path.join(ROOT, "evaluations", f"_verify_{label}_v*.json"))
            ev = max(matches, key=lambda p: int(os.path.basename(p).rsplit("_v", 1)[1].split(".")[0]))
            ev = os.path.relpath(ev, ROOT)
        recs = json.load(open(os.path.join(ROOT, ev), encoding="utf-8"))
        games = collections.OrderedDict()
        for r in recs:
            games.setdefault(r["file"], []).append(r)
        out.append((label, os.path.join(ROOT, trace), games))
    return out


def load_real_icons(path: str, spin: int):
    """Return {slot: (name, value)} for the first decision of `spin`."""
    previous_spin = None
    for line in open(path, encoding="utf-8"):
        record = json.loads(line)
        if record.get("record") != "decision":
            continue
        info = record["state"].get("info") or {}
        cur = info.get("spins")
        if cur != spin or cur == previous_spin:
            continue
        previous_spin = cur
        slots = {}
        for entry in (info.get("income_icons") or [])[-20:]:
            if len(entry) < 4:
                continue
            x, y, name, value = entry[0], entry[1], entry[2], entry[3]
            slots[y * 5 + x] = (name, value)
        return slots, record["state"], info
    return {}, None, {}


def load_rust_icons(record):
    return {e["slot"]: (e.get("name") or "", e["value"]) for e in (record.get("symbol_income") or [])}


def first_diff(records):
    for r in records:
        if r.get("income_ok") is None:
            st = r.get("action_status")
            if st not in ("ok", "auto", "swallowed", "ui", "done"):
                return "status", r
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind")
    ap.add_argument("--game")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--version", help="replay version tag, e.g. v43")
    args = ap.parse_args()

    totals = collections.Counter()
    symbol_gaps = collections.Counter()
    cases = []
    for label, trace_dir, games in load_datasets(args.version):
        for name, records in games.items():
            kind, rec = first_diff(records)
            if kind is None or (args.kind and kind != args.kind) or (args.game and name != args.game):
                continue
            totals[kind] += 1
            cases.append((label, name, kind, rec, trace_dir))

    print(f"first differences: {len(cases)}  {dict(totals)}")
    for label, name, kind, rec, trace_dir in cases:
        spin = rec.get("spin")
        real_slots, real_state, real_info = load_real_icons(os.path.join(trace_dir, name), spin)
        rust_slots = load_rust_icons(rec)
        gap = (rec.get("real_delta") or 0) - (rec.get("rust_income") or 0)
        print(f"\n=== {label} {name} spin {spin} kind={kind} email={rec.get('email')} "
              f"real={rec.get('real_delta')} rust={rec.get('rust_income')} gap={gap:+d} "
              f"extra={rec.get('extra_income')} gains={rec.get('item_gains')}")
        print(f"    items: {rec.get('items')}")
        if kind == "deck":
            print(f"    deck: {rec.get('deck_mismatch')}  order={rec.get('deck_order_mismatch')}")
        if kind == "items":
            print(f"    items_mismatch: {rec.get('items_mismatch')}")
        if kind == "board":
            print(f"    board: {rec.get('board_mismatch')}")
        if kind == "offer":
            print(f"    offers real={rec.get('real_offer')} rust={rec.get('rust_offer')}")
        # The real game records item payouts on "empty" board cells; Rust
        # keeps the same money in `extra_income`.  Comparing those cells
        # directly is meaningless, so only compare real symbols.
        rows = []
        for slot in sorted(set(real_slots) | set(rust_slots)):
            rn, rv = real_slots.get(slot, ("-", 0))
            un, uv = rust_slots.get(slot, ("-", 0))
            if rn == "empty" and un in ("", "-"):
                continue
            if rv != uv or (rn != "-" and un not in ("", "-") and rn != un and not args.full):
                rows.append((slot, rn, rv, un, uv))
        if rows:
            print("    slot real(name,value)                 rust(name,value)          diff")
            for slot, rn, rv, un, uv in rows:
                symbol_gaps[rn if rn != "-" else un] += rv - uv
                print(f"    {slot:2d}   {rn:20s} {rv:4d}        {un:20s} {uv:4d}      {rv - uv:+d}")
        elif not real_slots:
            print("    (no real income_icons for this spin)")
        if args.full and real_state is not None:
            print(f"    real board: {real_state.get('board')}")

    print("\n=== per-symbol total gaps (real - rust) ===")
    for name, delta in symbol_gaps.most_common():
        print(f"  {name:24s} {delta:+d}")


if __name__ == "__main__":
    main()
