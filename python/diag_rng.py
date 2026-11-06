"""Compare the real game's effect-stream rolls with the Rust replay, spin by spin.

The real trace logs every tagged random call in `info.rng_log` / `info.rng_tags`;
the replay logs the effect stream in `rng_log`.  This reports where the two
sequences first diverge and how many extra rolls the real game made.
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


def eval_path(label, version=None):
    if version:
        return os.path.join(ROOT, "evaluations", f"_verify_{label}_{version}.json")
    matches = glob.glob(os.path.join(ROOT, "evaluations", f"_verify_{label}_v*.json"))
    return max(matches, key=lambda p: int(os.path.basename(p).rsplit("_v", 1)[1].split(".")[0]))


def real_rolls(path):
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
        log = info.get("rng_log") or []
        tags = info.get("rng_tags") or []
        if log:
            out[spin] = (log, tags)
    return out


def main():
    version = sys.argv[1] if len(sys.argv) > 1 else None
    only = sys.argv[2] if len(sys.argv) > 2 else None
    summary = collections.Counter()
    extra_tags = collections.Counter()
    rows = []
    for label, trace in TRACES.items():
        if only and label != only:
            continue
        ev = json.load(open(eval_path(label, version), encoding="utf-8"))
        rust = collections.defaultdict(dict)
        for r in ev:
            if r.get("income_ok") is not None and r.get("rng_log"):
                rust[r["file"]][r["spin"]] = r["rng_log"]
        for path in sorted(glob.glob(os.path.join(ROOT, trace, "game_*.jsonl"))):
            name = os.path.basename(path)
            for spin, (real, tags) in real_rolls(path).items():
                ours = rust.get(name, {}).get(spin)
                if ours is None:
                    continue
                prefix = 0
                for a, b in zip(real, ours):
                    if abs(a - b) > 1e-4:
                        break
                    prefix += 1
                if prefix == min(len(real), len(ours)):
                    kind = "prefix-match"
                else:
                    kind = "diverged"
                delta = len(real) - len(ours)
                summary[(kind, delta)] += 1
                for t in tags[prefix:]:
                    extra_tags[t] += 1
                rows.append((label, name, spin, kind, delta, prefix, len(real), len(ours),
                             tags[prefix:][:12], real[prefix:][:4], ours[prefix:][:4]))
    print("=== (kind, real_len - rust_len) counts ===")
    for k, n in sorted(summary.items(), key=lambda x: (-x[1], str(x[0]))):
        print(" ", k, n)
    print("=== tags of extra real rolls ===")
    for t, n in extra_tags.most_common(20):
        print(" ", t, n)
    print("=== per-spin detail (only diverged or non-zero delta) ===")
    for row in rows:
        if row[3] == "diverged" or row[4] != 0:
            print(f"{row[0]:8s} {row[1]} spin {row[2]:2d} {row[3]:13s} delta={row[4]:+3d} "
                  f"prefix={row[5]} real={row[6]} rust={row[7]} tags={row[8]} "
                  f"real_next={[round(x,4) for x in row[9]]} rust_next={[round(x,4) for x in row[10]]}")


if __name__ == "__main__":
    main()
