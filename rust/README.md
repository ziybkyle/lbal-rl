# Rust 工作区使用说明

[项目首页](../README.md) | [English](README.en.md) | [规则保真度](FIDELITY.md)

当前规则：`2026-11-09-prespin-overflow-rent-offer-v15`。

| 组件 | 用途 |
| --- | --- |
| `lbal_core` | 进阶 20 规则、种子、结算和内置手工策略 |
| `lbal_cli` | 多线程批量模拟、导出逐局结果、重放原版轨迹 |
| `lbal_py` | Python 单局及批量绑定，提供 JSON 和紧凑观测 |

## 准备数据和环境

在仓库根目录的 `game/JSON` 放入合法取得的四份游戏数据：

- `Symbols - JSON.json`
- `Items - JSON.json`
- `Essences - JSON.json`
- `Fine Print - JSON.json`

也可用 `LBAL_DATA_DIR` 指定目录，或在命令行加 `--data-dir DIR`。Python 构造函数接受 `data_dir`。仓库中的策略数据是可选估值资料，游戏 JSON 需要自行准备。

如果已有 Steam 安装，可以使用项目自带工具提取这四个文件。需要训练面板时也可直接下载 Windows x64 / Python 3.12 的预编译绑定。[准备数据和安装面板](../docs/TRAINING.zh-CN.md)

Windows 使用仓库指定的 Rust GNU 工具链及 MinGW-w64 链接器。Linux / macOS 可运行 `rustup override set stable` 选择本机工具链。使用 Python 绑定时，需要安装与扩展目标一致的 CPython；本版在 Windows、CPython 3.12 上验证。

## 批量模拟与复现

```bash
cd rust
cargo test --release --workspace --locked
cargo run --release -p lbal_cli -- --games 10000 --seed 42 --threads 20 --hist
cargo run --release -p lbal_cli -- --games 1 --seed-text 1234567 --detail
```

- `--seed N`：批量整数种子；`--seed-text TEXT`：按原样传入社区种子模组显示的文本。两者走不同的入口，即使看起来是相同数字也不可混用。
- `--threads N`：并行局数使用的线程数，`0` 使用可用逻辑线程。
- `--hist`：输出终局交租次数分布；`--results-json FILE`：导出逐局结果。
- `--replay-actions DIR`、`--replay-spins N`：按原版轨迹重放动作并限制对比范围。具体参数用 `--help` 查看。

字符串种子使用 FNV-1a 派生房东种子，并按用途与旋转次数分开使用 PCG32 随机流，方便对照 [BetterLandlord](https://github.com/Piraeus42/BetterLandlord) 的种子。复现时保持游戏数据、规则版本和操作顺序一致。内置策略是 [手工基线](../当前基线策略说明.md)。

## Python 接入

在 `rust` 目录运行 `cargo build --release --locked -p lbal_py`。将构建产物复制到 `rust/python_bindings/`：Windows 将 `target/release/lbal_py.dll` 命名为 `lbal_py.pyd`；Linux / macOS 将对应共享库命名为 `lbal_py.so`。目录不存在时先创建。若本机有多个 Python，构建前用 `PYO3_PYTHON` 指定目标解释器。

回到仓库根目录：

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

环境的 `action_mask()` 标出当前可用动作。已有动作编号保持不变：`0` 旋转或确认交租，`1` 跳过，`2` 重掷符号，`3..18` 选前 16 个候选，`19..127` 移除符号。管理模式增加 `128..383` 开关物品/精华、`384..639` 主动使用；扩大候选容量时，第 17 个起从 `640` 编号。选择任意候选时可用 `offer_action(index)` 计算动作编号。

底层 `lbal_py.Env` 另有自己的动作接口：`step(0)` 在准备阶段旋转，候选阶段使用从零开始的候选索引，负数跳过；重掷、移除和物品管理使用单独方法。批量调用使用 `lbal_py.BatchEnv`。

JSON 观测含 `rules_version`、`managed_items`、`boss_context`、`symbol_history_counts` 和候选的 `pre_spin_effect`。默认观测不会收集逐符号结算轨迹；核查局面时可通过 `set_diagnostics(True)` 打开，再调用 `diagnostics()` 查看上一次旋转明细和两个符号池。
