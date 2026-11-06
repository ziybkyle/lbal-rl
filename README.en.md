# Luck be a Landlord — Rust Rule Simulator

Author: 群友镜中人

[中文](README.md) | [English](README.en.md)

A Rust rule simulator for *Luck be a Landlord*, built for high-throughput batch simulation, baseline policy research, and reinforcement-learning training. It reproduces the rules, numbers, and policy only; it does not rewrite the Godot UI, animations, or art. The original Godot project is used as a rules reference.

This is the first public version. The rules, numbers, policy, and APIs may still contain bugs. Issues, forks, and pull requests are welcome. When reporting a problem, please include the seed, the command you ran, the expected result, and the actual result.

## Features

- 168 symbols, 114 items (one placeholder), 113 essences, and 31 A20 fine prints.
- Complete spin, rent, reward, token, item, essence, and landlord-fight flow.
- A pure Rust core with a command-line batch simulator and PyO3 bindings.
- About 2,150 games/s on 20 threads over 20,000 games; about 13,800 steps/s for a 100,000-step Gym run.
- 160 Rust tests (136 unit + 24 integration) and 9 Python tests; 200 seeds, first 12 spins replay: 96.8% income, 97.6% board, 99.6% offers, 97.7% deck, 99.2% items.

## Seed system

Each run is driven by an integer or string seed, and the RNG is PCG32. A string seed is hashed with FNV-1a and derives independent `reel`, `effect`, `reel_shuffle`, and `effect_shuffle` streams per `spin_{n}`, matching the BetterLandlord community seed mod's derivation; the same seed, version, and policy reproduce the same sequence.

- CLI: `--seed N` for integer batch seeds; `--seed-text TEXT` to pass a string seed (used for same-seed cross-checks against the Godot game).
- Python: `lbal_py.Env(seed, data_dir)` and `lbal_py.BatchEnv(count, seed, data_dir)`.
- Running the same string seed as the Godot game allows spin-by-spin comparison of symbol placement, offers, and income; income matches on 96.8% of the first 12 spins across 200 seeds.

There is a public community seed system for the original game that is a useful reference:

- [Piraeus42/BetterLandlord](https://github.com/Piraeus42/BetterLandlord): a C# seed mod that hashes a string seed with FNV-1a and derives PCG random streams.
- It runs on the [Piraeus42/SlotWeave](https://github.com/Piraeus42/SlotWeave) modding framework.

The Rust core now reproduces this per-purpose stream derivation with the same PCG32 state progression. Remaining same-seed differences are tracked in [rust/FIDELITY.md](rust/FIDELITY.md).

## Baseline policy and test data

The built-in baseline, `heuristic-marginal-value-v13`, is a hand-written rule set, not a trained neural network. It always plays floor 20. In short: the filling phase ranks offers by rarity and a common-symbol priority table; after the deck reaches 20 symbols it switches to incremental valuation; removal cleans up low-value starting symbols while protecting growth cores; reroll tokens are reserved for rent rewards; one-shot symbols are discounted by remaining spins.

| Item | Result |
| --- | --- |
| 20,000-game benchmark, 20 threads | 3,871 wins, 19.36% win rate; average rents 7.23, spins 56.0, deck 25.5, final rent 603.5 |
| Real-game v13, 50 games | 18% win rate; average rents 7.34, spins 57.5 |
| Real-game trace replay, first 12 spins | 2,367 spins across 200 seeds: 96.8% income, 97.6% board, 99.7% offers, 97.7% deck, 99.2% items; 78.5% of games have no first difference |
| Gym random masked policy, 100,000 steps | 13,783 steps/s |
| Automated tests | 160 Rust tests (136 unit + 24 integration) + 9 Python tests |

19.36% is the win rate of the built-in hand-written policy, not an upper bound of the simulator. The real-game v13 record over 50 games is 18%; running the same v13 rules against this simulator scores in the same 10–13% range over 40 games (too small to estimate a win rate). Rule fidelity and policy strength should be evaluated separately.

Watch the sample size: 40 games has a standard deviation of about 5%, so older "ten-game smoke test" claims are noise and have been retired. Compare policies over at least 2,000 games, and compare rule changes with same-seed replays.

The full strategy rules are in [当前基线策略说明.md](当前基线策略说明.md), guide trade-offs in [攻略策略取舍.md](攻略策略取舍.md), and historical batches in [进阶20基线与记录.md](进阶20基线与记录.md). These detailed notes are currently in Chinese.

## Quick start

```bash
cd rust
cargo test --release
cargo run --release -p lbal_cli -- --games 2000 --seed 1 --threads 20 --hist
```

The original game JSON is not distributed with the repository. Put it in `game/JSON`, or point to it with `LBAL_DATA_DIR` / `--data-dir`. See [rust/README.en.md](rust/README.en.md#preparing-game-data).

## Repository layout

```text
godot_kernel/
├─ rust/                  Rust workspace (core, CLI, PyO3 bindings)
├─ python/                Python wrappers (RustEnv, Gym environment, v13 adapter)
├─ tests/                 Python tests
├─ data/                  policy valuations, priors, and statistics reports
└─ game/                  game JSON (not committed; prepare it yourself)
```

Documentation:

- [rust/README.md](rust/README.md): full Chinese documentation
- [rust/README.en.md](rust/README.en.md): full English documentation, build steps, APIs, and baseline policy
- [rust/FIDELITY.md](rust/FIDELITY.md): rule fidelity and known differences
- [rust/RUST_PORT_PLAN.md](rust/RUST_PORT_PLAN.md): implementation plan and acceptance notes

## Project status and feedback

This is the first public version. Many interactions are still approximated or missing. Issues, forks, and pull requests are welcome, especially rule fixes, seed cross-checks, and policy improvements. Please include the seed, command, expected result, and actual result so the problem can be reproduced.

## Copyright

The Rust code is by 群友镜中人 and licensed under the [MIT License](LICENSE).

*Luck be a Landlord*, its game data, text, and art remain copyright their original author. This repository does not include the game, the original data, or extracted scripts; the MIT License does not cover them.
