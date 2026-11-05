# 幸运房东 Rust 规则模拟器

作者：群友镜中人

[中文](README.md) | [English](README.en.md)

这是《Luck be a Landlord》（幸运房东）的 Rust 规则模拟器，用于高速批量对局、基线策略研究和强化学习训练。它只复刻规则、数值与策略，不重写 Godot 的界面和动画；原 Godot 工程只作为参考。

这是第一个公开版本，规则、数值、策略和接口都可能还有 bug。欢迎提交 Issue、Fork 或 Pull Request；反馈时请附上种子、运行命令、期望结果和实际结果。

## 主要特性

- 168 个符号、114 个物品（其中 1 个为占位符）、113 种精华、31 条 A20 小字。
- 完整的旋转、租金、奖励、代币、物品、精华和房东战流程。
- 纯 Rust 核心，提供命令行批量模拟与 PyO3 绑定。
- 2,000 局 20 线程约 3,392 局/秒；100,000 步 Gym 环境约 16,203 步/秒。
- 160 项 Rust 测试（136 单元 + 24 集成）和 9 项 Python 测试；200 个种子、前 12 旋重放：收入 96.8%、棋盘 97.6%、候选 99.6%、牌组 97.7%、物品 99.2%。

## 种子系统

每一局由一个整数种子或字符串种子驱动，随机数使用 PCG32。字符串种子经 FNV-1a 哈希后，按 `spin_{n}` 派生 `reel`、`effect`、`reel_shuffle`、`effect_shuffle` 等独立随机流，与社区种子模组 BetterLandlord 的派生方式一致；相同种子、版本和策略会得到相同的随机序列。

- 命令行：`--seed N` 使用整数批次种子；`--seed-text TEXT` 直接指定字符串种子（用于与原版 Godot 工程同一局对拍）。
- Python：`lbal_py.Env(seed, data_dir)`、`lbal_py.BatchEnv(count, seed, data_dir)`。
- 与原版 Godot 工程使用同一字符串种子运行时，符号落位、候选和收入可逐旋对照；当前 200 个种子的前 12 旋收入一致率为 96.8%。

原版游戏的社区种子系统有公开实现，可以对照：

- [Piraeus42/BetterLandlord](https://github.com/Piraeus42/BetterLandlord)：C# 种子模组，字符串种子经 FNV-1a 哈希后派生 PCG 随机流。
- 它运行在 [Piraeus42/SlotWeave](https://github.com/Piraeus42/SlotWeave) 模组框架上。

本项目的 Rust 核心已复刻这套按用途派生的多路随机流，并使用一致的 PCG32 状态推进；同种子回放的差异见 [rust/FIDELITY.md](rust/FIDELITY.md)。

## 基线策略与测试数据

内置基线 `heuristic-marginal-value-v13` 是手工规则，不是训练好的神经网络。固定进阶 20，主要思路是：填格阶段按品质和抓位排序；满 20 张后改用增量估值；删除初始低值符号并保护成长核心；重掷代币集中用于交租奖励；一次性符号按剩余旋转折现。

| 项目 | 结果 |
| --- | --- |
| 2,000 局基准，20 线程 | 3,392 局/秒；261 胜，胜率 13.05%；平均交租 6.43、平均旋转 49.2、平均牌组 24.5 |
| 真机 v13 对照，50 局 | 胜率 18%，平均交租 7.34、平均旋转 57.5 |
| 真机轨迹前 12 旋重放 | 200 个种子的 2,367 旋：收入 96.8%、棋盘 97.6%、候选 99.6%、牌组 97.7%、物品 99.2%；78% 的局无首差异 |
| Gym 随机掩码策略，100,000 步 | 16,203 步/秒，常驻内存约 34.3 MB |
| 自动化测试 | 160 项 Rust 测试（136 单元 + 24 集成）+ 9 项 Python 测试 |

13.05% 是内置启发式策略的胜率，不代表规则模拟的上限；真机 v13 策略在同一环境中做 30 局冒烟约三成胜率（样本太小，只作参考）。规则保真度和策略强度需要分开评估。

完整的策略规则见 [当前基线策略说明.md](当前基线策略说明.md)，攻略取舍见 [攻略策略取舍.md](攻略策略取舍.md)，历史批次见 [进阶20基线与记录.md](进阶20基线与记录.md)。

## 快速开始

```bash
cd rust
cargo test --release
cargo run --release -p lbal_cli -- --games 2000 --seed 1 --threads 20 --hist
```

游戏原始 JSON 不随仓库分发，请放到 `game/JSON`，或用 `LBAL_DATA_DIR` / `--data-dir` 指定目录。详见 [rust/README.md](rust/README.md#准备游戏数据)。

## 目录说明

```text
godot_kernel/
├─ rust/                  Rust 工作区（规则核心、命令行、PyO3 绑定）
├─ python/                Python 封装（RustEnv、Gym 环境、v13 策略适配）
├─ tests/                 Python 测试
├─ data/                  策略估值、先验和统计报告
└─ game/                  游戏 JSON（不提交，需自行准备）
```

详细文档：

- [rust/README.md](rust/README.md)：完整中文说明、构建、接口和基线策略
- [rust/README.en.md](rust/README.en.md)：English documentation
- [rust/FIDELITY.md](rust/FIDELITY.md)：规则保真度与已知差异
- [rust/RUST_PORT_PLAN.md](rust/RUST_PORT_PLAN.md)：实现计划与验收记录

## 项目状态与反馈

这是第一个公开版本，仍有许多近似实现和未覆盖的交互。欢迎提交 Issue、Fork 或 Pull Request，尤其是规则修正、种子对拍和策略改进。反馈时请附上种子、命令、期望结果和实际结果，方便复现。

## 版权说明

Rust 代码作者为群友镜中人，使用 [MIT 许可证](LICENSE)。

《Luck be a Landlord》及其游戏数据、文本和美术版权归游戏原作者所有。本仓库不包含游戏本体、原始数据或提取脚本；MIT 许可证不覆盖这些内容。
