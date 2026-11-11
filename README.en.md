# lbal-rl · Luck be a Landlord simulator and RL toolkit

[中文](README.md) · [English](README.en.md) · [Model release](https://github.com/ziybkyle/lbal-rl/releases/tag/scratch-142k)

`lbal-rl` simulates Luck be a Landlord on Floor 20. Rust resolves spins, offers, rent, and the landlord fight. The Python panel lets a policy play repeated games, learn with PPO, and evaluate saved checkpoints on fixed seeds. Training and evaluation run locally on a CPU.

## Start here

| What you want to do | Where to start |
| --- | --- |
| Open the panel and evaluate a released model | [Installation and walkthrough](docs/TRAINING.en.md), then import the [142,000-game checkpoint](https://github.com/ziybkyle/lbal-rl/releases/tag/scratch-142k) |
| Continue the released model or start with random weights | [Train, save, and roll back](docs/TRAINING.en.md#train-save-and-roll-back) |
| Understand observations, configure progressive rent rewards, and use fixed seeds | [Training method](docs/METHOD.en.md) |
| Inspect individual games, pick rates, failures, or deck sizes | [Statistics and reports](docs/STATISTICS.en.md) |
| Simulate games in Rust or connect your own policy | [Build, CLI, and Python API](rust/README.en.md) |
| Reproduce a difference from the original game | [Fidelity and verification](rust/FIDELITY.md) |

The release includes a prebuilt Rust module for Windows x64 and CPython 3.12. Prepare the game data from your own Steam installation. The training guide walks through extraction, verification, and use of the Chinese-language panel.

## Published checkpoint

| Measure | Value |
| --- | --- |
| Starting point | Random weights, Floor 20, 142,000 completed training games |
| Model size | 36,599,710 bytes, about 34.9 MiB, including optimizer state |
| Fixed-seed evaluation | 1,000 games, 29 wins (**2.90%**), **7.912** successful rents on average |
| Evaluation conditions | Seeds `1900000000` through `1900000999`, normal starts, highest-scoring legal actions, zero truncations |
| Simulator rules | `2026-11-09-prespin-overflow-rent-offer-v15` |

The training history includes 2,000 games with an experimental exploration bias. The last 1,000 games used standard exploration and progressive rent rewards. The table gives a separate evaluation of the saved checkpoint. An import starts the counter at 142,000 and writes local per-game records once you resume. [Parameters, data hashes, and evaluation metadata](data/published_scratch_142k.json)

Download the [sample analysis](https://github.com/ziybkyle/lbal-rl/releases/download/scratch-142k/scratch-142k-analysis.html) and open it in a browser to explore pick rates from those 1,000 games. The [per-game CSV](https://github.com/ziybkyle/lbal-rl/releases/download/scratch-142k/scratch-142k-evaluation.csv) contains their outcomes and terminal data.

## Current Rust rules

- **Spin resolution and chains.** Destruction and removal have separate history pools. Corrections cover shared targets, placement of newly created symbols, ore, urn, soap, and coconut chains, and the one-time maturation of rabbits and wine.
- **Offers and tokens.** Credit Card effects can offer 20 or 30 symbols. Rerolls retain the current symbol offer's rarity guarantee. The action mask excludes rerolls for items and essences. Additional offers and rent rewards follow the corrected order.
- **Items and rent.** The engine handles pre-spin removal payouts, essence charge and consumption, item toggling, and renewed rent checks after cashing in a rescue item. Comfy Pillow and its essence can be used at their decision points.
- **Landlord and replay.** Fine-print draws, targets, and activation timing have been corrected. Observations expose clauses, item progress, separate history pools, and candidate pre-spin effects. Verification against the original game covers recorded seeds and action paths. Other paths can still differ. [Fidelity notes](rust/FIDELITY.md)

The built-in heuristic won 512 of 10,000 games (5.12%) on seeds `42` through `10041`, with a mean of 6.73 paid rents. The Rust release build took about 3.9 seconds on the test machine with 20 threads. PPO training also performs network inference, parameter updates, and logging. [Benchmark commands](rust/README.en.md#batch-simulation-and-replay)

## Project map

| Path | Contents |
| --- | --- |
| `rust/` | Rules engine, batch simulation CLI, and Python binding |
| `python/training_ui.py` | Training, evaluation, charts, and report controls |
| `python/train_fresh_rules.py` | Create or resume PPO runs and save checkpoints and games |
| `python/candidate_policy.py` | Candidate-scoring network and masked policy |
| `python/training_analytics.py` | Sortable offline HTML reports from per-game logs |
| `data/` | Project policy data, derived rule features, and checkpoint metadata |

For a rules issue, include the simulator version, seed type, action sequence, and the first spin with a discrepancy. For a training result, include the checkpoint, reward settings, and evaluation seed range and game count.

Created by 群友镜中人. Source code is available under the [MIT license](LICENSE). The original game, data, text, and artwork belong to their respective owners. Prepare the required data from your own copy.
