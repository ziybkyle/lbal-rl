# Luck be a Landlord — Rust Rule Simulator

Author: 群友镜中人

[中文](README.md) | [English](README.en.md)

This is a Rust rewrite of the rules of *Luck be a Landlord*, built for high-throughput batch simulation, strategy research, and reinforcement-learning training. It reproduces the rules, numbers, and policy only; it does not rewrite the Godot UI, animations, or art. The original Godot project is used as a rules reference.

This is the first public version. The rules, numbers, policy, and APIs may still contain bugs. Issues, forks, and pull requests are welcome. When reporting a problem, please include the seed, the command you ran, the expected result, the actual result, and a minimal reproduction if possible.

## Seed system

Every run is driven by a 64-bit seed, and the random number generator is PCG32. The same seed, version, and policy reproduce the same random sequence, which makes experiments repeatable and lets you compare policies fairly.

- CLI: `--seed N`; game *i* uses `seed + i`.
- Python: `lbal_py.Env(seed, data_dir)` and `lbal_py.BatchEnv(count, seed, data_dir)`.
- Bit-for-bit equality across platforms and versions is not guaranteed. Changing the rules or the RNG implementation can also change results for an old seed.

This project's seed system is part of the public Rust source. There is also a public community seed system for the original game that is a useful reference:

- [Piraeus42/BetterLandlord](https://github.com/Piraeus42/BetterLandlord): a C# seed mod with a seed system, run history, and win-rate statistics. Its seed flow hashes a string seed with FNV-1a and derives PCG streams for different purposes.
- It runs on the [Piraeus42/SlotWeave](https://github.com/Piraeus42/SlotWeave) modding framework, which handles Godot runtime script injection and state reads.

Important: the Rust core currently uses its own PCG32 seed sequence and **does not yet reproduce BetterLandlord's per-purpose derived random streams**, so its seeds cannot be compared spin-by-spin with that mod. The core contains an FNV-1a string-to-seed helper (`seed_from_text`), but the CLI and Python APIs currently accept only integer seeds. Cross-checks, compatibility patches, and algorithm discussions are welcome.

## Crates

| Crate | Purpose |
| --- | --- |
| `lbal_core` | Rules core: deck, board, spins, rent, rewards, items, essences, fine print, landlord fight, and heuristic policy |
| `lbal_cli` | Command-line batch simulation, benchmarks, and income replay of real-game traces |
| `lbal_py` | PyO3 bindings: `Env`, `BatchEnv`, and compact binary observations |

## Rule coverage

- 168 symbols (including a few unused placeholders), 114 items (one placeholder), 113 essences, and 31 A20 fine prints (fine print 31 is treated as a no-op).
- 5 columns × 4 rows, per-column draws, empty slots preserved, spin cost, and Dud growth.
- A20 rent curve 25/50/100/150/225/300/375/450/600/650/700/777; after the 12th rent payment, the 1500-HP landlord fight begins.
- Adjacency, suits, wildcards, destruction, and growth for symbols; per-spin and triggered item effects; essence charging and one-shot effects; rent rewards and token settlement.
- Valuation and policy priors can be overridden by `data/symbol_values.json` and `data/baseline_priors.json`; built-in defaults are used when those files are absent.

The full rule list, approximations, and known differences are in [FIDELITY.md](FIDELITY.md). The implementation plan and acceptance notes are in [RUST_PORT_PLAN.md](RUST_PORT_PLAN.md).

## Performance and test data

Measured on one i7-12700F; numbers will change with hardware and compiler options:

| Item | Result |
| --- | --- |
| 2,000-game benchmark, 20 threads | 3,392 games/s; 261 wins, 13.05% win rate; average rents 6.43, spins 49.2, deck 24.5, final rent 503.1 |
| Single thread | About 390 games/s |
| Gym random masked policy, 100,000 steps | 16,203 steps/s, about 34.3 MB resident memory |
| Real-game trace replay, first 5 spins | 190 of 210 deterministic boards matched income exactly (90.5%), mean absolute error 6.10 |
| Automated tests | 152 Rust tests + 9 Python tests |

13.05% is the win rate of the built-in heuristic, not an upper bound of the simulator. A 30-game smoke test with the real v13 policy adapted to this environment won about one third of its games (too small to estimate a win rate). The real-game v13 record over 50 games is 18%, with average rents 7.34 and average spins 57.5. Rule fidelity and policy strength should be evaluated separately.

## Baseline policy

The built-in baseline is called `heuristic-marginal-value-v13`. It is a hand-written rule set, not a trained neural network. It always plays floor 20. The main rules are:

- **Filling phase (before the deck first reaches 20 symbols)**: compare rarity first; within the same rarity, compare the common-symbol priority table, associations, feeding synergies, and special combos. The priority table comes from `data/baseline_priors.json`. Take at most 3 coal symbols, and do not take coal on the hand immediately after taking one; do not take a cow without a cat; skip only when all three offers are common and near the bottom of the priority table. Normally at most 2 skips; with 2 or more coal, at most 1.
- **After 20 symbols**: rank by incremental value. Long-lived symbols count as 1 effective slot, food with an existing consumer as 0.5, self-destroying one-shot symbols as 0.2, and timed symbols as 0.5. Each effective slot above 20 costs 0.25 points; each empty slot below 20 adds 0.25. Rent-phase thresholds are 1/2/3/4. Skip normal offers that do not meet the threshold, and do not reroll normal offers.
- **Removal**: clean up the starting symbols in the order cherry → flower → banana peel → coin → cat → pearl. From rent 300, remove negative curses such as destruction and hoarding; from 375, remove drain, dullness, and emptiness. Other low-value symbols are removed by current incremental value. Growth cores, their food, key/chest pairs, arrow/Robin Hood/target pairs, and key multiplier cores are protected. Keep at least 1 removal token from rent 300 and at least 2 for the landlord fight from rent 600.
- **Rent-reward rerolls**: reroll tokens are reserved for rent rewards. At rent 650 and above, keep rerolling while the best offer is below 4.5 and tokens remain. At 600, reroll at most twice and keep one token; at 300–450, reroll at most once and keep one. Once the deck reaches 24 symbols, do not reroll rewards below 650.
- **One-shot symbols and gacha**: wealth gacha, void symbols, chemical seven, and the various gacha symbols are discounted as one-time income and scaled by remaining spins. Removal, reroll, item, and essence gacha are prioritized after the deck is full. A raw-value floor is used when comparing against “skip”, so one-shot symbols are not skipped by mistake.
- **Items and essences**: compare rarity first; within the same rarity, combine the first-pick rates in `data/baseline_priors.json`, existing positive/negative associations, and item synergies. Essences are compared within the essence pool. The oil can picks the highest-value column; the swapping device enumerates adjacent swaps and only swaps when the gain exceeds 0.2.
- **Flush and coin on a string**: cards are valued as one-time cash collected before the spin when the flush item or its essence is held. Coins generated by King Midas and the Midas box are valued by generation rate when coin-on-a-string or its essence is held.

The baseline is still weak at item toggles, swap timing, capsule-content expectations, dice expectations, and parts of the late-game growth engine. The full v13 rules are in [当前基线策略说明.md](../当前基线策略说明.md), the guide trade-offs are in [攻略策略取舍.md](../攻略策略取舍.md), and historical batches and report fields are in [进阶20基线与记录.md](../进阶20基线与记录.md). These detailed notes are currently in Chinese.

Reproduce the benchmark:

```bash
cargo run --release -p lbal_cli -- --games 2000 --seed 1 --threads 20 --hist
```

Reproduce the real v13 policy adapter (requires the `python/` directory from the full repository):

```bash
cd ..
python python/v13_rust.py --games 10 --seed 100
```

## Requirements

- Windows: Rust stable with the GNU toolchain. The repository's `rust-toolchain.toml` pins `stable-x86_64-pc-windows-gnu`, which requires a working MinGW-w64 linker.
- Linux / macOS: the code itself is not Windows-specific, but the author has only verified it on Windows. Delete `rust-toolchain.toml` or run `rustup override set stable` to use the host's default toolchain.
- Python 3.12 (only needed for the PyO3 bindings). Build the extension with the same CPython version you will use to run it.
- Game JSON data; see the next section.

## Preparing game data

By default the simulator reads these files from `game/JSON` at the repository root:

```text
game/JSON/
├─ Symbols - JSON.json
├─ Items - JSON.json
├─ Essences - JSON.json
└─ Fine Print - JSON.json
```

Because of copyright, the repository does not include the game or the original JSON. Export them from a copy of the game you legally own, or ask the author.

If you only share the `rust` directory, put the data in `rust/game/JSON`. You can also point to another location with an environment variable or a command-line argument:

```bash
# Environment variable (used by the CLI, tests, and PyO3)
export LBAL_DATA_DIR=/path/to/game/JSON
# Windows command prompt:
# set LBAL_DATA_DIR=D:\path\to\game\JSON

# Command-line argument (overrides automatic discovery)
cargo run --release -p lbal_cli -- --data-dir /path/to/game/JSON
```

In Python, pass the directory to the constructor:

```python
env = lbal_py.Env(0, "/path/to/game/JSON")
```

Optional policy data lives in `data/` two levels above the data directory, for example `data/symbol_values.json` and `data/baseline_priors.json` at the repository root. The simulator still runs without them, but the heuristic policy becomes weaker. If you share only the `rust` directory, copy these two files into `rust/data/`.

## Build

```bash
cd rust
cargo test --release
cargo build --release -p lbal_cli
```

### Build the Python extension

```bash
cargo build --release -p lbal_py
```

Windows:

```bat
copy target\release\lbal_py.dll python_bindings\lbal_py.pyd
```

Linux / macOS:

```bash
# Linux
cp target/release/liblbal_py.so python_bindings/lbal_py.so
# macOS
cp target/release/liblbal_py.dylib python_bindings/lbal_py.so
```

The repository's `.cargo/config.toml` sets `PYO3_USE_RAW_DYLIB=0` so the Windows GNU toolchain does not need `dlltool` and links against CPython's import library instead. The python.org Windows installer includes that library by default. Linux and macOS ignore this variable.

## Command line

```bash
cd rust
cargo run --release -p lbal_cli -- --games 2000 --seed 1 --threads 20 --hist
```

Example output:

```text
games=2000 threads=20 elapsed=0.590s games_per_sec=3392.0
wins=261 losses=1739 win_rate=13.05% avg_rents=6.43 avg_spins=49.2 ...
```

Common options:

| Option | Meaning |
| --- | --- |
| `--games N` | Number of games; default 10000 |
| `--seed N` | Starting seed; game *i* uses `seed + i` |
| `--threads N` | Number of threads; 0 means all logical CPUs |
| `--max-spins N` | Maximum spins per game, to stop abnormal runs |
| `--detail` | Print details for the first 10 games |
| `--hist` | Print the rent-payment distribution |
| `--replay DIR` | Replay `game_*.jsonl` real-game traces and compare income |
| `--replay-spins N` | Replay the first N spins; default 5 |
| `--data-dir DIR` | Game JSON directory |

## Python usage

In the full repository, the `python/` directory provides higher-level wrappers:

```bash
cd ..   # back to godot_kernel
python python/rust_env.py --games 20
python python/gym_env.py --steps 2000
python -m unittest discover -s tests -p "test_gym_env.py"
```

Using the PyO3 binding directly:

```python
import json
import sys

sys.path.insert(0, "python_bindings")
import lbal_py

env = lbal_py.Env(0, None)          # seed, data directory; None means auto-discover
obs = json.loads(env.observe())
print(obs["phase"], obs["coins"])

obs = json.loads(env.step(0))       # ready phase: spin
```

The action space is a fixed 128-wide layout so a masked policy can use one output head:

```text
0          spin (ready phase only)
1          skip the current reward
2          reroll the current reward (needs a reroll token)
3..18      take offer i - 3
19..127    remove deck symbol i - 19
```

`BatchEnv(count, seed, data_dir)` drives multiple environments in parallel, and `observe_compact()` returns a little-endian `i32` binary observation suitable for training loops. See `../python/gym_env.py` for the action mask and vectorized environment.

## Known differences

- The RNG is PCG32, not Godot's random sequence. Distribution-level comparisons are possible, but spin-by-spin alignment is not.
- A20 fine print 31 is currently treated as a no-op. Some essences and items are approximations, and a few trigger orders differ from the real game.
- The landlord fight models HP, the rent loop, and win/loss, but not phase skills or combat presentation.
- The shop, item enable/disable toggles, and some per-instance state are not implemented.
- The built-in policy is weaker than real v13 after rent 900; this is the main source of the win-rate gap.

See [FIDELITY.md](FIDELITY.md) for details.

## Repository layout

```text
rust/
├─ Cargo.toml
├─ crates/
│  ├─ lbal_core/          rules core
│  ├─ lbal_cli/           batch simulation and benchmarks
│  └─ lbal_py/            PyO3 bindings
├─ python_bindings/       built extension (not committed; build locally)
├─ FIDELITY.md            rule fidelity and known differences
├─ RUST_PORT_PLAN.md      implementation plan and acceptance notes
└─ .cargo/config.toml     PyO3 linking configuration
```

## Project status and feedback

This is the first public version. Many interactions are still approximated or missing, so bugs are expected. Contributions are welcome:

- Open an issue with the seed, command, expected result, and actual result.
- Fork the repository and send a pull request, especially for rule fixes, seed cross-checks, and policy improvements.
- Share your seeds, win-rate records, or a better baseline so others can reproduce and compare.

## FAQ

**`failed to load game data`**: `game/JSON` was not found. Place the data as described above, or set `LBAL_DATA_DIR` / `--data-dir`.

**`No module named 'lbal_py'`**: build `lbal_py`, copy the dynamic library into `rust/python_bindings/`, and rename it to `lbal_py.pyd` on Windows or `lbal_py.so` on Linux / macOS.

**`dlltool.exe not found` on Windows GNU**: make sure the repository's `.cargo/config.toml` is in effect (it sets `PYO3_USE_RAW_DYLIB=0`) and that `libs/python3xx.lib` exists in your Python installation.

**`link: extra operand` when building with MSVC in Git Bash**: Git's own `link.exe` is shadowing the MSVC linker. Use the GNU toolchain pinned by the repository, or build from a Visual Studio developer command prompt.

**Python version mismatch**: the `.pyd` is tied to the CPython version it was built against. Rebuild after changing Python.

## Copyright and license

Rust code author: 群友镜中人, licensed under the [MIT License](../LICENSE).

*Luck be a Landlord*, its game data, text, and art are copyright their original author. This repository does not include the game, the original data, or scripts extracted from the game; the MIT License does not cover them.
