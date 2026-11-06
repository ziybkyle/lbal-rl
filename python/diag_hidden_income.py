"""Find item payouts that are not visible in the per-icon income log.

For every spin, `real_delta` (the sum of coin_sum events) minus the sum of the
20 logged icon values is the income paid by items/effects that do not belong
to a board cell.  Grouping those residuals by held item shows which payouts
Rust must reproduce.
"""
from __future__ import annotations

import collections
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


def real_icons(path):
    out = {}
    seen = set()
    for line in open(path, encoding="utf-8"):
        r = json.loads(line)
        if r.get("record") != "decision":
            continue
        info = r["state"].get("info") or {}
        spin = info.get("spins")
        if not spin or spin in seen:
            continue
        seen.add(spin)
        entries = (info.get("income_icons") or [])[-20:]
        if entries:
            out[spin] = sum(e[3] for e in entries if len(e) >= 4)
    return out


def main():
    version = sys.argv[1] if len(sys.argv) > 1 else None
    by_item = collections.defaultdict(collections.Counter)
    residual = collections.Counter()
    examples = collections.defaultdict(list)
    for label, trace in TRACES.items():
        p = (os.path.join(ROOT, "evaluations", f"_verify_{label}_{version}.json")
             if version else None)
        if not p:
            matches = glob.glob(os.path.join(ROOT, "evaluations", f"_verify_{label}_v*.json"))
            p = max(matches, key=lambda x: int(os.path.basename(x).rsplit("_v", 1)[1].split(".")[0]))
        ev = json.load(open(p, encoding="utf-8"))
        rust = collections.defaultdict(dict)
        for r in ev:
            if r.get("income_ok") is not None:
                rust[r["file"]][r["spin"]] = r
        for path in sorted(glob.glob(os.path.join(ROOT, trace, "game_*.jsonl"))):
            name = os.path.basename(path)
            for spin, icon_sum in real_icons(path).items():
                rec = rust.get(name, {}).get(spin)
                if not rec:
                    continue
                delta = rec.get("real_delta") or 0
                hidden = delta - icon_sum
                residual[hidden] += 1
                items = rec.get("items") or []
                for item in items:
                    by_item[item][hidden] += 1
                if hidden != 0 and (rec.get("rust_income") or 0) != delta:
                    if len(examples[hidden]) < 6:
                        examples[hidden].append((label, name, spin, delta,
                                                 rec.get("rust_income"), items))
    print("=== residual (real_delta - icon_sum) distribution ===")
    for k, n in sorted(residual.items(), key=lambda x: -x[1])[:20]:
        print(f"  {k:+4d}: {n}")
    print("=== items whose nonzero residuals are most common ===")
    ranked = []
    for item, counts in by_item.items():
        nonzero = sum(n for k, n in counts.items() if k != 0)
        ranked.append((nonzero, item, dict(counts.most_common(5))))
    for n, item, top in sorted(ranked, reverse=True)[:25]:
        print(f"  {item:28s} nonzero={n:4d} top={top}")
    print("=== residual values on spins where Rust income is wrong ===")
    for hidden in sorted(examples):
        print(f"  hidden={hidden:+d}")
        for e in examples[hidden][:4]:
            print("   ", e)


if __name__ == "__main__":
    main()
