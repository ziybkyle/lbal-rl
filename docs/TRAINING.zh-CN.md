# 安装和使用训练面板

[项目首页](../README.md) · [English](TRAINING.en.md) · [训练方法](METHOD.zh-CN.md) · [统计说明](STATISTICS.zh-CN.md)

## 1. 下载和安装

下面的步骤适用于 Windows x64 和 64 位 CPython 3.12。训练和评估使用 CPU。其他平台可从源代码构建绑定，见 [Rust 使用说明](../rust/README.md#python-接入)。

1. 安装 [Python 3.12](https://www.python.org/downloads/windows/)，安装时保留 Python Launcher 和 Tcl/Tk。先在 Steam 安装《幸运房东》。
2. 打开 [142,000 局 Release](https://github.com/ziybkyle/lbal-rl/releases/tag/scratch-142k)。下载并解压 **Source code (zip)**。打开解压后含 `Start-Training.bat` 的目录。习惯用 Git 的用户也可以检出 `scratch-142k` 标签。
3. 下载 `lbal-py-windows-x86_64-cp312.zip`，把里面的 `lbal_py.pyd` 放到项目的 `rust/python_bindings/`，目录不存在时先创建。需要续训或评估发布模型时，再下载 `scratch-142k.zip`，保存为 ZIP 供面板导入。
4. 在项目目录打开 PowerShell，依次运行以下命令。每次安装依赖只需做一次。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --index-url https://download.pytorch.org/whl/cpu torch==2.15.0
.\.venv\Scripts\python.exe -m pip install -r requirements-training.txt
```

## 2. 从 Steam 准备游戏数据

在 Steam 库右击《幸运房东》，打开 **管理 → 浏览本地文件**。找到 `Luck be a Landlord.pck`。在项目目录运行下面的命令，把示例路径替换成你电脑上的完整路径。

```powershell
.\.venv\Scripts\python.exe python/prepare_game_data.py "C:\Games\Steam\steamapps\common\Luck be a Landlord\Luck be a Landlord.pck"
```

看到 **游戏数据校验通过** 即可继续。工具从你自己的安装中读取并校验四个 JSON，保存到 `game/JSON/`。发布模型以 Steam 构建 `16940935` 的这四个文件为准，其他构建包含相同数据也可通过校验。报告游戏版本不匹配时，先核对你的 PCK 是否来自匹配的游戏安装。[逐文件校验值](../data/published_scratch_142k.json)

已经有四个文件时，可手动放入 `game/JSON/`，运行 `python/prepare_game_data.py` 省略 PCK 参数来校验。发布的规则特征来自本项目的编码器，足够重建这套模型的输入。

| 文件 | 用途 |
| --- | --- |
| `Symbols - JSON.json` | 符号定义 |
| `Items - JSON.json` | 物品定义 |
| `Essences - JSON.json` | 精华定义 |
| `Fine Print - JSON.json` | 房东战条款 |

安装后在同一个项目目录运行一次检查，确认 Rust 模块可加载并能启动模拟。

```powershell
.\.venv\Scripts\python.exe python/rust_env.py --games 2
```

## 3. 打开面板并评估

1. 双击 `Start-Training.bat`。也可以在 PowerShell 运行 `.\.venv\Scripts\python.exe python/training_ui.py`，以便看到启动错误。
2. 在 **训练** 页点 **导入模型包…**，选择 `scratch-142k.zip`。面板验证模型哈希、规则和输入版本，然后在 **模型起点** 选中导入的保存点。看到累计 **142,000 局** 即导入成功。
3. 设置 **每次评估局数**。第一次可以用 50 局验证安装。要复现发布成绩，设为 **1,000 局**。点 **单独评估… → 开始评估**。
4. 运行进度显示完成局数、速度、平均交租和通关率。评估结束后可从 **结果对比** 或 **最新 1000 局评估** 打开报告。固定评估默认从 `1900000000` 起顺序取种子。

发布包内含可执行反序列化的 PyTorch/SB3 检查点。只加载你信任的模型。面板导入按源码中的发布清单核验文件。

## 训练、保存和回退

- **选起点。** 保留导入的模型即可续训。点击 **全新起点** 会建立随机初始化的独立实验。下拉框可选择已有保存点。
- **定本次局数。** 第一次续训可勾选 **本次持续训练达到** 并填写 `1000`。目标局数是本次新增局数，要求为 1,000 的倍数。勾选 **训练结束后评估** 时，结束后只评估刚保存的模型。
- **开始和停止。** 点 **开始持续训练** 后通常每 1,000 局保存一次。点 **停止并保存** 会在当前保存阶段结束后停下。保存前还可撤销停止请求。每累计训练 10,000 局，按面板设置自动做一次固定种子评估。
- **比较结果。** **结果对比** 中 `A / B` 表示平均成功交租次数和通关率。**走势图** 有训练与评估的四条指标，**死亡与牌组** 可切换训练探索、历次评估和统计窗口。[读表方法](STATISTICS.zh-CN.md)
- **回退。** 从下拉框选回早先的保存点，再开始训练。新阶段写入新目录，沿所选起点关联历史。导入模型的 142,000 局是起点计数，本地训练图和自定义范围只使用实际保存的日志。

训练使用递增交租奖励（方案二）。**调整方案二** 可以分别修改每一次交租、失败和通关的分数。**更多设置** 可修改开关动作及其奖赏。**计算线程** 控制 PyTorch CPU 线程数，调整在下个保存阶段生效。每段保存、训练和评估的设置写进 `manifest.json`。

## 高级功能与命令行

训练页可配置指定内容练习和储物间超额扣分。[可选训练设置](METHOD.zh-CN.md#可选训练设置)

命令行也能复现同样的流程。在项目根目录运行，下面的 `$model` 是导入后打印的本地目录。选择新输出目录以及与历史训练、固定评估都不重叠的训练种子。

```powershell
$py = ".\.venv\Scripts\python.exe"
$model = & $py python/published_checkpoint.py "C:\Downloads\scratch-142k.zip"
& $py python/manual_evaluate.py $model --out evaluations/my-first-eval --games 1000 --threads 2 --models after_continuation
& $py python/train_fresh_rules.py --previous $model --out runs/my-continuation --games 1000 --train-seed-start 1809000000 --limited-item-toggles --no-eval
& $py python/continuous_report.py runs/my-continuation
```

命令行 `--out` 应指向尚不存在的目录。命令行阶段结束后可运行评估器。省略 `--previous` 会从随机权重开训。启动与规则版本不匹配时，检查运行的解释器是 `.venv` 中的 64 位 Python 3.12，`lbal_py.pyd` 来自同一 Release，并重新执行游戏数据校验。备份时保留整个训练分支的保存目录，阶段清单会指向父保存点。
