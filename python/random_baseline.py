"""Run a small repeatable random-policy smoke test, not a trained player."""
import argparse
from collections import Counter
import json
import random
import time

from v3_client import V3Client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games", type=int, default=3)
    parser.add_argument("--floor", type=int, default=1)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--max-decisions", type=int, default=3000)
    args = parser.parse_args()
    if args.games < 1 or args.max_decisions < 1 or not 1 <= args.floor <= 20:
        parser.error("games and max-decisions must be positive; floor must be 1..20")
    rng = random.Random(args.seed)
    results = []
    start_all = time.perf_counter()
    with V3Client() as game:
        for i in range(args.games):
            start = time.perf_counter()
            frame = game.reset(f"BASE{args.seed:04d}{i:04d}", args.floor)
            decisions = 0
            while frame.outcome == "running" and decisions < args.max_decisions:
                # Spin at the ready screen. Else choose uniformly among choices,
                # without wasting tokens or toggling items indefinitely.
                choices = [c for c in frame.candidates if c.type_id == 430]
                if not choices:
                    choices = [c for c in frame.candidates if c.type_id not in (414, 417, 418, 431)]
                if not choices:
                    choices = list(frame.candidates)
                if not choices:
                    break
                frame = game.step(rng.choice(choices).index)
                decisions += 1
            outcome = frame.outcome if frame.outcome != "running" else "decision_limit"
            result = {"game": i + 1, "outcome": outcome, "decisions": decisions,
                      "spins": frame.info.get("spins"),
                      "seconds": round(time.perf_counter() - start, 3),
                      "end_reason": frame.end_reason,
                      "unresolved": frame.info.get("unresolved", 0)}
            results.append(result)
            print(json.dumps(result), flush=True)
    print(json.dumps({"summary": dict(Counter(r["outcome"] for r in results)),
                      "mean_seconds_per_game": round(sum(r["seconds"] for r in results) / len(results), 3),
                      "total_seconds_including_startup": round(time.perf_counter() - start_all, 3)}))


if __name__ == "__main__":
    main()
