# 幸运房东 Rust 规则模拟器

作者：群友镜中人

[中文](README.md) | [English](README.en.md)

这是一个用 Rust 重写的《Luck be a Landlord》（幸运房东）规则模拟器，面向高速批量对局、策略研究和强化学习训练。项目只复刻规则、数值和策略，不重写 Godot 的界面、动画与美术；原 Godot 工程只作为规则参考。

这是第一个公开版本，规则、数值、策略和接口都可能还有 bug。欢迎提交 Issue、Fork 或 Pull Request；反馈时请附上种子、运行命令、期望结果和实际结果，能给出最小复现最好。

## 种子系统

每一局由一个整数种子或字符串种子驱动，随机数使用 PCG32。字符串种子经 FNV-1a 哈希后，按 `spin_{n}` 派生 `reel`、`effect`、`reel_shuffle`、`effect_shuffle` 等独立随机流；相同种子、相同版本和相同策略会得到相同的随机序列，便于复现实验和对比不同策略。

- 命令行：`--seed N` 使用整数批次种子（第 i 局使用 `seed + i`）；`--seed-text TEXT` 直接指定字符串种子。
- Python：`lbal_py.Env(seed, data_dir)`、`lbal_py.BatchEnv(count, seed, data_dir)`。
- 修改规则或随机数实现后，旧种子的结果可能变化；跨平台逐位一致不保证。

本项目的种子系统随 Rust 源码一起公开。原版游戏的社区种子系统也有公开实现，可以作为对照和参考：

- [Piraeus42/BetterLandlord](https://github.com/Piraeus42/BetterLandlord)：C# 写的幸运房东种子模组，包含种子系统、运行历史和胜率统计。它的种子流程是字符串种子经 FNV-1a 哈希，再按用途派生 PCG 随机流。
- 它运行在 [Piraeus42/SlotWeave](https://github.com/Piraeus42/SlotWeave) 模组框架上；SlotWeave 负责 Godot 运行时脚本注入和状态读取。

本项目的 Rust 核心已复刻 BetterLandlord 的按用途派生多路随机流，并使用相同的 PCG32 状态推进；`--seed-text` 可直接跑真机的 `B2A...` 字符串种子，符号落位、候选与收入可逐旋对照。当前 200 个种子、前 12 旋的收入一致率为 96.8%，剩余差异见 [FIDELITY.md](FIDELITY.md)。

## 项目组成

| crate | 作用 |
| --- | --- |
| `lbal_core` | 规则核心：牌组、棋盘、旋转、租金、奖励、物品、精华、小字、房东战与启发式策略 |
| `lbal_cli` | 命令行批量模拟、基准测试与真机轨迹收入重放 |
| `lbal_py` | PyO3 绑定，提供 `Env`、`BatchEnv` 和二进制紧凑观测 |

## 规则覆盖

- 168 个符号（包含少量未使用的占位条目）、114 个物品（其中 1 个为占位符）、113 种精华、31 条 A20 小字（第 31 条按无操作处理）。
- 5 列 × 4 行棋盘、按列抽牌、空位保留、旋转费用与 Dud 增长节奏。
- A20 租金曲线 25/50/100/150/225/300/375/450/600/650/700/777；第 12 次交租后进入 1500 点生命的房东战。
- 符号相邻、花色、百搭、摧毁、成长效果；物品的每旋收入与触发效果；精华的充能与一次性效果；交租奖励与代币结算。
- 估值和策略先验可由 `data/symbol_values.json`、`data/baseline_priors.json` 覆盖；文件缺失时使用内置默认值。

完整的规则清单、近似实现和已知差异见 [FIDELITY.md](FIDELITY.md)，实现过程与验收记录见 [RUST_PORT_PLAN.md](RUST_PORT_PLAN.md)。

## 性能与测试数据

以下数据在同一台 i7-12700F 上测得，换机器或编译选项后会变化：

| 项目 | 结果 |
| --- | --- |
| 2 万局基准，20 线程 | 3,871 胜，胜率 19.36%；平均交租 7.23、平均旋转 56.0、平均牌组 25.5、终局租金 603.5 |
| 吞吐 | 2,000 局 2,418 局/秒；2 万局长跑 731~2,152 局/秒（同一台机器不同时刻实测，受负载影响） |
| 单线程 | 约 265 局/秒 |
| Gym 随机掩码策略，100,000 步 | 13,783 步/秒 |
| 真机轨迹重放，前 12 旋 | 200 个种子的 2,367 旋：收入 96.8%、棋盘 97.6%、候选 99.7%、牌组 97.7%、物品 99.2%；78.5% 的局无首差异 |
| 自动化测试 | 160 项 Rust 测试（136 单元 + 24 集成）+ 9 项 Python 测试 |

19.36% 是内置手工策略的胜率，不代表规则模拟的上限。真机 v13 的 50 局记录为 18%、平均交租 7.34、平均旋转 57.5；把同一套 v13 规则接到这套环境上，实测同样在 10%~13% 区间（40 局，样本太小，只证明流程跑通）。规则保真度和策略强度要分开评估。

注意样本量：40 局的标准差约 5%，几种"十局冒烟赢几把"的旧结论都属于噪声，已经作废。比较策略至少要 2000 局，比较规则改动请用同种子重放。

## 基线策略

内置基线名为 `heuristic-marginal-value-v13`，是手工规则，不是训练好的神经网络。它固定运行进阶 20，重点规则如下：

- **填格阶段（牌组第一次达到 20 张之前）**：先比较品质；同品质再比较普通符号抓位、关联、喂食配合和特殊组合。普通符号抓位来自 `data/baseline_priors.json`。煤炭最多拿 3 个，拿过煤炭后下一手不拿；没有猫不拿牛；只有三个普通候选都排在抓位表末段时才跳过，正常最多跳 2 次，持有 2 个以上煤炭时最多跳 1 次。
- **满 20 张之后**：改用增量估值排序。长期符号按 1 个有效占位、可被现有消费者消除的食物按 0.5、一次自消除符号按 0.2、限时符号按 0.5 计算；超过 20 后每多 1 个有效占位扣 0.25 分，低于 20 时每个空位补 0.25 分。租金阶段门槛为 1/2/3/4；普通候选没有达到门槛就跳过，普通候选不重掷。
- **删除**：初始符号按 樱桃 → 花 → 香蕉皮 → 金币 → 猫咪 → 珍珠 的顺序清理；租金 300 起处理破坏、囤积等负面诅咒，375 起处理抽吸、沉闷、空虚；其余按当前增量估值删除低价值符号。成长核心、核心的食物、钥匙与箱子、箭头/罗宾汉/靶子、关键倍率核心会被保护。租金 300 起至少保留 1 个删除代币，600 起至少保留 2 个给房东战。
- **交租奖励重掷**：重掷代币集中用于交租奖励。租金 650 及以后，如果持有重掷且最高候选没有达到 4.5，会一直重掷到代币用完；600 奖励最多重掷 2 次并保留 1 个代币；300～450 奖励最多重掷 1 次并保留 1 个。牌组达到 24 张时，650 以下奖励不再重掷。
- **一次性符号和扭蛋**：财富扭蛋、虚空系列、七号化学物、各类扭蛋按一次性收益折现，再按剩余旋转数缩放；移除/重掷/物品/精华扭蛋在满格阶段优先级较高。与“跳过”比较时另用原始收益保底，避免把一次性符号错误跳过。
- **物品和精华**：先比较品质，同品质结合 `data/baseline_priors.json` 的首次选择率、现有正负关联和物品联动；精华在同一池内比较。油罐选择当前估值最高的一列；交换装置枚举相邻交换，只有正收益超过 0.2 才交换。
- **同花与系绳硬币**：花色在持有同花/同花精华时按旋转前收走的一次性金币估值；国王迈达斯和迈达斯魔盒生成的悬浮金币，在持有系绳硬币/精华时按生成速率计入。

这套基线仍不擅长物品开关、交换时机、胶囊内容期望、骰子期望和部分后期成长引擎。完整的 v13 规则见 [当前基线策略说明.md](../当前基线策略说明.md)，攻略取舍见 [攻略策略取舍.md](../攻略策略取舍.md)，历史批次和报告字段见 [进阶20基线与记录.md](../进阶20基线与记录.md)。

复现基准：

```bash
cargo run --release -p lbal_cli -- --games 2000 --seed 1 --threads 20 --hist
```

复现真机 v13 策略适配（需要完整仓库的 `python/` 目录）：

```bash
cd ..
python python/v13_rust.py --games 10 --seed 100
```

## 环境要求

- Windows：Rust stable + GNU 工具链。仓库的 `rust-toolchain.toml` 已锁定 `stable-x86_64-pc-windows-gnu`，需要可用的 MinGW-w64 链接器。
- Linux / macOS：代码本身不依赖 Windows，但作者只在 Windows 上验证过。删除 `rust-toolchain.toml`，或执行 `rustup override set stable`，使用本机默认工具链。
- Python 3.12（仅使用 PyO3 绑定时需要）。扩展需要使用与本机 Python 版本一致的 CPython 编译。
- 游戏 JSON 数据，见下一节。

## 准备游戏数据

模拟器默认从仓库根目录的 `game/JSON` 读取以下文件：

```text
game/JSON/
├─ Symbols - JSON.json
├─ Items - JSON.json
├─ Essences - JSON.json
└─ Fine Print - JSON.json
```

由于版权原因，仓库不附带游戏本体和原始 JSON。请从你合法拥有的游戏副本中导出，或向作者索取。

如果只分享 `rust` 目录，可以把数据放到 `rust/game/JSON`。也可以用环境变量或参数指定其他位置：

```bash
# 环境变量（CLI、测试和 PyO3 都生效）
export LBAL_DATA_DIR=/path/to/game/JSON
# Windows 命令提示符使用：
# set LBAL_DATA_DIR=D:\path\to\game\JSON

# 命令行参数（会覆盖自动查找的结果）
cargo run --release -p lbal_cli -- --data-dir /path/to/game/JSON
```

Python 里通过构造参数指定：

```python
env = lbal_py.Env(0, "/path/to/game/JSON")
```

可选的策略数据放在数据目录向上两级的 `data/` 下，例如仓库根目录的 `data/symbol_values.json` 和 `data/baseline_priors.json`。这两个文件缺失时仍可运行，但启发式策略的质量会下降。只分享 `rust` 目录时，把这两个文件复制到 `rust/data/` 即可。

## 构建

```bash
cd rust
cargo test --release
cargo build --release -p lbal_cli
```

### 构建 Python 扩展

```bash
cargo build --release -p lbal_py
```

Windows：

```bat
copy target\release\lbal_py.dll python_bindings\lbal_py.pyd
```

Linux / macOS：

```bash
# Linux
cp target/release/liblbal_py.so python_bindings/lbal_py.so
# macOS
cp target/release/liblbal_py.dylib python_bindings/lbal_py.so
```

仓库的 `.cargo/config.toml` 设置了 `PYO3_USE_RAW_DYLIB=0`，避免 Windows GNU 工具链调用 `dlltool`，改为链接 CPython 自带的导入库；python.org 的 Windows 安装包默认包含该库。Linux 和 macOS 会忽略这个变量。

## 命令行使用

```bash
cd rust
cargo run --release -p lbal_cli -- --games 2000 --seed 1 --threads 20 --hist
```

输出示例：

```text
games=20000 threads=20 elapsed=27.369s games_per_sec=730.8
wins=3871 losses=16129 win_rate=19.36% avg_rents=7.23 avg_spins=56.0 ...
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--games N` | 模拟局数，默认 10000 |
| `--seed N` | 起始种子，第 i 局使用 `seed + i` |
| `--seed-text TEXT` | 直接指定字符串种子（与真机/社区种子逐旋对拍） |
| `--threads N` | 线程数，0 表示使用全部逻辑线程 |
| `--max-spins N` | 单局最大旋转数，防止异常局无限进行 |
| `--detail` | 打印前 10 局的详细结果 |
| `--hist` | 打印交租次数分布 |
| `--replay DIR` | 重放 `game_*.jsonl` 真机轨迹并对比收入 |
| `--replay-actions DIR` | 重放真机动作序列，对比候选/牌组/物品/收入 |
| `--action-json FILE` | 将动作重放对比结果写入 JSON |
| `--replay-spins N` | 重放前 N 旋，默认 5 |
| `--data-dir DIR` | 指定游戏 JSON 目录 |

## Python 使用

在完整仓库中，`python/` 目录提供了更上层的封装：

```bash
cd ..   # 回到 godot_kernel
python python/rust_env.py --games 20
python python/gym_env.py --steps 2000
python -m unittest discover -s tests -p "test_gym_env.py"
```

直接使用 PyO3 绑定：

```python
import json
import sys

sys.path.insert(0, "python_bindings")
import lbal_py

env = lbal_py.Env(0, None)          # 种子, 数据目录；None 表示自动查找
obs = json.loads(env.observe())
print(obs["phase"], obs["coins"])

obs = json.loads(env.step(0))       # 准备阶段：旋转
```

动作空间为固定的 128 维布局，方便掩码策略使用同一个输出头：

```text
0          旋转（仅准备阶段）
1          跳过当前奖励
2          重掷当前奖励（需要重掷代币）
3..18      选择第 i-3 个奖励
19..127    删除牌组第 i-19 个符号
```

`BatchEnv(count, seed, data_dir)` 可以并行驱动多个环境，`observe_compact()` 返回小端 `i32` 二进制观测，适合训练循环。动作掩码和向量环境的实现见 `../python/gym_env.py`。

## 已知差异

- 同种子随机数已能逐旋对齐（PCG32 + BetterLandlord 派生），少量旋仍有 effect 流偏移；当前前 12 旋收入一致率 96.8%。
- A20 小字第 31 条暂按无操作处理；部分精华和物品是近似实现，个别触发顺序与真机不同。
- 房东战实现了血量、租金循环和胜负判定，但没有模拟阶段技能与战斗演出。
- 商店、物品启用开关和部分实例状态尚未实现。
- 内置策略在 900 租金之后的成长引擎仍弱于真机 v13，这是胜率差距的主要来源。

更多细节见 [FIDELITY.md](FIDELITY.md)。

## 仓库结构

```text
rust/
├─ Cargo.toml
├─ crates/
│  ├─ lbal_core/          规则核心
│  ├─ lbal_cli/           批量模拟与基准
│  └─ lbal_py/            PyO3 绑定
├─ python_bindings/       编译后的扩展（不提交，需本地构建）
├─ FIDELITY.md            规则保真度与已知差异
├─ RUST_PORT_PLAN.md      实现计划与验收记录
└─ .cargo/config.toml     PyO3 链接配置
```

## 项目状态与反馈

这是第一个公开版本，仍有许多近似实现和未覆盖的交互，出现 bug 是正常的。欢迎：

- 提交 Issue，附上种子、命令、期望结果和实际结果；
- Fork 后提交 Pull Request，尤其是规则修正、种子对拍和策略改进；
- 分享你的种子、胜率记录或更好的基线，方便其他人复现和比较。

## 常见问题

**提示 `failed to load game data`**：没有找到 `game/JSON`，按上面的说明放置数据，或设置 `LBAL_DATA_DIR` / `--data-dir`。

**提示 `No module named 'lbal_py'`**：先构建 `lbal_py`，再把生成的动态库复制到 `rust/python_bindings/` 并改成 `lbal_py.pyd`（Windows）或 `lbal_py.so`（Linux / macOS）。

**Windows GNU 提示 `dlltool.exe not found`**：确认使用的是仓库内的 `.cargo/config.toml`（其中设置了 `PYO3_USE_RAW_DYLIB=0`），并确认 Python 安装目录的 `libs/python3xx.lib` 存在。

**在 Git Bash 里用 MSVC 构建时报 `link: extra operand`**：Git 自带的 `link.exe` 抢占了 MSVC 链接器。请改用仓库锁定的 GNU 工具链，或在 Visual Studio 开发者命令行中构建。

**Python 版本不匹配**：`.pyd` 与构建时使用的 CPython 版本绑定；换 Python 版本后需要重新构建。

## 版权与许可

Rust 代码作者：群友镜中人，使用 [MIT 许可证](../LICENSE)。

《Luck be a Landlord》及其游戏数据、文本和美术版权归游戏原作者所有。本仓库不包含游戏本体、原始数据或从游戏中提取的脚本；MIT 许可证不覆盖这些内容。
