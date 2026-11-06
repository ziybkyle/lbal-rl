"""Dump a single game's per-spin detail (real trace + Rust replay) for one spin.

Usage: python python/_tmp_case.py <label> <game_000NN.jsonl> <spin> [more spins...]
"""
from __future__ import annotations

import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACES = {
    "trace42": "runs/_trace42/20261104T231102157567Z_a20dbd50",
    "trace43": "runs/_trace43/20261104T233352939682Z_fc5dc00c",
    "trace1": "runs/_trace1/20261105T001628488524Z_da5a8c38",
}


def eval_path(label):
    matches = glob.glob(os.path.join(ROOT, "evaluations", f"_verify_{label}_v*.json"))
    return max(matches, key=lambda p: int(os.path.basename(p).rsplit("_v", 1)[1].split(".")[0]))


def main():
    label, name = sys.argv[1:3]
    spins = set(int(s) for s in sys.argv[3:])
    recs = [r for r in json.load(open(eval_path(label), encoding="utf-8")) if r["file"] == name]
    seen = set()
    for line in open(os.path.join(ROOT, TRACES[label], name), encoding="utf-8"):
        rec = json.loads(line)
        if rec.get("record") != "decision":
            continue
        info = rec["state"].get("info") or {}
        spin = info.get("spins")
        if spin not in spins or spin in seen:
            continue
        seen.add(spin)
        print(f"===== spin {spin} email={rec['state'].get('email')} =====")
        print("board:", rec["state"].get("board"))
        print("deck:", [d.get("name") for d in (rec["state"].get("deck") or [])])
        print("items:", [i.get("name") for i in (rec["state"].get("items") or [])])
        print("events:", json.dumps(info.get("events"), ensure_ascii=False)[:1800])
        print("income_icons:", (info.get("income_icons") or [])[-20:])
        print("income_trace(last 3):", (info.get("income_trace") or [])[-3:])
        print("item_log:", json.dumps(info.get("item_log"), ensure_ascii=False)[:800])
        for r in recs:
            if r.get("spin") == spin and r.get("income_ok") is not None:
                print("--- rust replay ---")
                for k in ("income_ok", "real_delta", "rust_income", "extra_income", "item_gains",
                          "deck_ok", "deck_mismatch", "items_ok", "board_mismatch", "symbol_income",
                          "real_offer", "rust_offer", "chosen", "items"):
                    print(f"  {k}: {json.dumps(r.get(k), ensure_ascii=False)[:900]}")
                break


if __name__ == "__main__":
    main()
