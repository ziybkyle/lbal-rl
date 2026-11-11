# Luck be a Landlord — Rust Rule Simulator

Author: 群友镜中人

[中文](README.md) | [English](README.en.md)

A headless rule simulator for *Luck be a Landlord*, for batch simulation, policy evaluation, and reinforcement learning. The current rules identifier is `2026-11-09-prespin-overflow-rent-offer-v15`. It targets Floor 20 and covers the 5×4 board, rent, offers, items, essences, tokens, and the 1,500 HP landlord fight. It provides a Rust CLI, Python bindings, and an environment with action masks.

## Main fixes since the previous release

- **Spin resolution and chains:** destruction and removal now use separate symbol histories. Fixed shared targets, when generated symbols occupy vacancies or stay off-screen, and payout and RNG order for chains involving ore, urns, soap, and coconuts. Rabbit and Wine mature only once.
- **Offers and tokens:** Credit Card and its essence can produce 20 or 30 symbol offers. Only symbol offers can be rerolled; the current offer's rarity guarantees and restrictions persist. Rent reward offers now precede the extra offer from Frozen Pizza where applicable.
- **Items, essences, and rent:** corrected pre-spin removal payouts, essence counters and consumption, item toggles, and retrying rent after using Piggy Bank, Swear Jar, Coffee, or Devil's Deal. Comfy Pillow and its essence can be accepted when available.
- **Landlord fight and replay:** corrected fine-print selection, targets, and activation. Python exposes clauses, item progress, per-symbol destruction/removal counts, and pre-spin offer effects. Optional diagnostics record spin resolution details. Seeds and actions support reproducible comparisons; native-game differences remain.

See [fidelity notes](rust/FIDELITY.md) for the verified cases and their limits.

## Quick start

Supply the original symbol, item, essence, and fine-print JSON files in `game/JSON`, or specify their directory with `LBAL_DATA_DIR` or `--data-dir`. See the [Rust guide](rust/README.en.md) for builds and Python usage.

```bash
cd rust
cargo test --release --workspace
cargo run --release -p lbal_cli -- --games 10000 --seed 42 --threads 20 --hist
```

The current built-in heuristic won **645 of 10,000 games (6.45%)** on seeds 42–10041, averaging **6.51 rents**, **49.8 spins**, and **25.5 symbols** in the final inventory. On Windows with an i7-12700F, the release build took about **3.9 seconds** on 20 threads (about **2,567 games/s**). This is a heuristic benchmark; batch simulation throughput is separate from neural-network training speed.

For a bug report, include the rules version, seed, actions, and first divergence.

Rust source is licensed under [MIT](LICENSE). The game, its original data, text, and art remain the property of their owners and are not distributed here.
