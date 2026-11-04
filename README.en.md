# Luck be a Landlord — Rust Rule Simulator

Author: 群友镜中人

[中文](README.md) | [English](README.en.md)

A Rust rule simulator for *Luck be a Landlord*, built for high-throughput batch simulation, baseline policy research, and reinforcement-learning training. It reproduces the rules, numbers, and policy only; it does not rewrite the Godot UI, animations, or art. The original Godot project is used as a rules reference.

This is the first public version. The rules, numbers, policy, and APIs may still contain bugs. Issues, forks, and pull requests are welcome. When reporting a problem, please include the seed, the command you ran, the expected result, and the actual result.

## Features

- 168 symbols, 114 items (one placeholder), 113 essences, and 31 A20 fine prints.
- Complete spin, rent, reward, token, item, essence, and landlord-fight flow.
- A pure Rust core with a command-line batch simulator and PyO3 bindings.
- About 3,392 games/s on 20 threads for 2,000 games; about 16,203 steps/s for a 100,000-step Gym run.
- 152 Rust tests and 9 Python tests; 90.5% exact-income rate when replaying the first 5 spins of real-game traces.

## Seed system

Every run is driven by a 64-bit seed, and the RNG is PCG32. The same seed, version, and policy reproduce the same random sequence. The CLI uses `--seed N`; Python uses `lbal_py.Env(seed, data_dir)`.

There is a public community seed system for the original game that is a useful reference:

- [Piraeus42/BetterLandlord](https://github.com/Piraeus42/BetterLandlord): a C# seed mod that hashes a string seed with FNV-1a and derives PCG random streams.
- It runs on the [Piraeus42/SlotWeave](https://github.com/Piraeus42/SlotWeave) modding framework.

The Rust core currently uses its own PCG32 sequence and does not yet reproduce either mod's seed derivation, so its seeds cannot be compared spin-by-spin with theirs. Cross-checks and compatibility patches are welcome. See [rust/README.en.md](rust/README.en.md#seed-system) for details.

## Baseline policy and test data

The built-in baseline, `heuristic-marginal-value-v13`, is a hand-written rule set, not a trained neural network. It always plays floor 20. In short: the filling phase ranks offers by rarity and a common-symbol priority table; after the deck reaches 20 symbols it switches to incremental valuation; removal cleans up low-value starting symbols while protecting growth cores; reroll tokens are reserved for rent rewards; one-shot symbols are discounted by remaining spins.

| Item | Result |
| --- | --- |
| 2,000-game benchmark, 20 threads | 3,392 games/s; 261 wins, 13.05% win rate; average rents 6.43, spins 49.2, deck 24.5 |
| Real-game v13, 50 games | 18% win rate; average rents 7.34, spins 57.5 |
| Real-game trace replay, first 5 spins | 190 of 210 deterministic boards matched exactly (90.5%) |
| Gym random masked policy, 100,000 steps | 16,203 steps/s, about 34.3 MB resident memory |
| Automated tests | 152 Rust tests + 9 Python tests |

13.05% is the win rate of the built-in heuristic, not an upper bound of the simulator. A 30-game smoke test with the real v13 policy in this environment won about one third of its games (too small to estimate a win rate). Rule fidelity and policy strength should be evaluated separately.

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
