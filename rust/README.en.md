# Rust workspace guide

[Project overview](../README.en.md) | [中文](README.md) | [Fidelity notes](FIDELITY.md)

Current rules: `2026-11-09-prespin-overflow-rent-offer-v15`.

| Component | Purpose |
| --- | --- |
| `lbal_core` | Floor 20 rules, seeded simulation, resolution, and built-in heuristic |
| `lbal_cli` | Parallel simulation, per-game export, and native trace replay |
| `lbal_py` | Single-game and batch Python bindings with JSON and compact observations |

## Data and tools

Supply the following four files from a legally obtained game copy in the repository's `game/JSON` directory:

- `Symbols - JSON.json`
- `Items - JSON.json`
- `Essences - JSON.json`
- `Fine Print - JSON.json`

You can also use `LBAL_DATA_DIR`, the CLI's `--data-dir DIR`, or the Python constructor's `data_dir` argument. The repository's policy data is optional; the original game JSON is required.

On Windows, use the pinned GNU Rust toolchain and a MinGW-w64 linker. On Linux or macOS, select the native toolchain with `rustup override set stable`. The Python extension must match the target CPython installation; this release was verified on Windows with CPython 3.12.

## Batch simulation and replay

```bash
cd rust
cargo test --release --workspace --locked
cargo run --release -p lbal_cli -- --games 10000 --seed 42 --threads 20 --hist
cargo run --release -p lbal_cli -- --games 1 --seed-text 1234567 --detail
```

- `--seed N` selects integer batch seeds. `--seed-text TEXT` uses the literal seed shown by the community mod; a numeric string is distinct from an integer seed.
- `--threads N` sets the simulation threads; `0` uses the available logical threads.
- `--hist` prints the final rent histogram; `--results-json FILE` exports per-game results.
- `--replay-actions DIR` replays a native action trace; `--replay-spins N` limits comparison length. Run `--help` for the complete options.

Text seeds use FNV-1a and separate PCG32 streams by purpose and spin, to support comparison with [BetterLandlord](https://github.com/Piraeus42/BetterLandlord). Reproduction requires the same data, rules, and action sequence. The default CLI policy is a [hand-written heuristic](../当前基线策略说明.md).

## Python

From `rust`, run `cargo build --release --locked -p lbal_py`. Create `rust/python_bindings/` if needed and copy the extension there. Rename the Windows `target/release/lbal_py.dll` to `lbal_py.pyd`, or the Linux/macOS shared library to `lbal_py.so`. Set `PYO3_PYTHON` before building if you need to select a particular Python installation.

From the repository root:

```bash
python python/rust_env.py --games 20
python -m unittest discover -s tests -p "test_gym_env.py"
```

```python
import sys
sys.path.insert(0, "python")

from gym_env import RustGymEnv

env = RustGymEnv(seed=42, enable_management=True, offer_capacity=168)
obs, info = env.reset()
obs, reward, terminated, truncated, info = env.step(0)
```

Use `action_mask()` to check what can be done. The existing action numbers remain: `0` spins or confirms rent; `1` skips; `2` rerolls a symbol offer; `3..18` select the first 16 offers; `19..127` remove a deck symbol. Management adds `128..383` for toggles and `384..639` for item/essence use. With expanded offer capacity, the 17th and later offers start at action `640`. Use `offer_action(index)` for either range.

The lower-level `lbal_py.Env` has a separate interface: `step(0)` spins in the ready phase; during an offer, pass a zero-based index or a negative number to skip. Rerolls, removals, and item management use separate methods. `lbal_py.BatchEnv` runs independent games.

JSON observations include `rules_version`, `managed_items`, `boss_context`, `symbol_history_counts`, and each symbol offer's `pre_spin_effect`. Detailed spin accounting is opt-in: call `set_diagnostics(True)` and then read `diagnostics()` after a spin for payout details and both symbol histories.
