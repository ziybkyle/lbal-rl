# Install and use the training panel

[Project overview](../README.en.md) · [中文](TRAINING.zh-CN.md) · [Training method](METHOD.en.md) · [Statistics guide](STATISTICS.en.md)

## 1. Download and install

These steps cover Windows x64 with 64-bit CPython 3.12. Training and evaluation use the CPU. To build the binding for other platforms, see the [Rust guide](../rust/README.en.md#python-integration).

1. Install [Python 3.12](https://www.python.org/downloads/windows/), including the Python Launcher and Tcl/Tk. Install Luck be a Landlord through Steam.
2. Open the [142,000-game release](https://github.com/ziybkyle/lbal-rl/releases/tag/scratch-142k). Download and extract **Source code (zip)**, then open the folder containing `Start-Training.bat`. Git users can check out the `scratch-142k` tag.
3. Download `lbal-py-windows-x86_64-cp312.zip`. Place its `lbal_py.pyd` in `rust/python_bindings/`, creating the directory if needed. Download `scratch-142k.zip` as well if you want to evaluate or resume the released model. Keep the model bundle zipped for import.
4. Open PowerShell in the project directory and run the following one-time setup.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --index-url https://download.pytorch.org/whl/cpu torch==2.15.0
.\.venv\Scripts\python.exe -m pip install -r requirements-training.txt
```

## 2. Prepare game data from Steam

Right-click Luck be a Landlord in your Steam library and choose **Manage → Browse local files**. Locate `Luck be a Landlord.pck`. Run the following from the project directory with the full path to your own PCK.

```powershell
.\.venv\Scripts\python.exe python/prepare_game_data.py "C:\Games\Steam\steamapps\common\Luck be a Landlord\Luck be a Landlord.pck"
```

Continue when the tool prints **Game data verified**. It reads four JSON files from your installation and verifies them before writing to `game/JSON/`. The published checkpoint matches Steam build `16940935`. Builds with identical data also pass. If verification reports a mismatch, check that the PCK comes from a matching game installation. [Expected file hashes](../data/published_scratch_142k.json)

You can also place the files below in `game/JSON/` and run `python/prepare_game_data.py` with the PCK argument omitted. The derived rule features bundled with the source reconstruct the checkpoint's inputs.

| File | Purpose |
| --- | --- |
| `Symbols - JSON.json` | Symbol definitions |
| `Items - JSON.json` | Item definitions |
| `Essences - JSON.json` | Essence definitions |
| `Fine Print - JSON.json` | Landlord fight clauses |

Run a quick check from the project directory to confirm that the Rust module can load and simulate games.

```powershell
.\.venv\Scripts\python.exe python/rust_env.py --games 2
```

## 3. Open the panel and evaluate

The panel currently uses Chinese labels. The labels below match the buttons on screen.

1. Double-click `Start-Training.bat`. You can also run `.\.venv\Scripts\python.exe python/training_ui.py` in PowerShell to see startup errors.
2. In **训练** (Training), click **导入模型包…** (Import model bundle) and choose `scratch-142k.zip`. The panel verifies the file hash and input versions, then selects the imported checkpoint under **模型起点** (Starting checkpoint). The counter starts at **142,000 games**.
3. Set **每次评估局数** (Games per evaluation). Try 50 for an installation check or **1,000** to reproduce the published evaluation. Choose **单独评估… → 开始评估** (Evaluate separately → Start).
4. The progress pane shows completed games, speed, mean rents, and win rate. After the run, open **结果对比** (Results) or **最新 1000 局评估** (Latest 1,000 evaluation games). Fixed evaluation seeds start at `1900000000`.

PyTorch/SB3 checkpoints can execute code when deserialized. Load files from sources you trust. The import button verifies the bundle against the release metadata in the source checkout.

## Train, save, and roll back

- **Choose a starting point.** Keep the imported checkpoint selected to resume it. **全新起点** (Fresh start) creates a separate experiment with random weights. The dropdown lists saved checkpoints.
- **Set a target.** For a first continuation, enable **本次持续训练达到** (Stop after this many new games) and enter `1000`. The target counts additional games in multiples of 1,000. Enable **训练结束后评估** (Evaluate at the end) to evaluate the checkpoint produced by this run.
- **Start and stop.** Click **开始持续训练** (Start continuous training). The panel normally saves every 1,000 games. **停止并保存** (Stop and save) finishes the current save stage. You can cancel the stop request before it takes effect. Every 10,000 cumulative games, the panel runs a fixed-seed evaluation with the configured number of games.
- **Compare results.** In **结果对比** (Results), `A / B` shows mean paid rents and win rate. **走势图** (Trends) shows four training and evaluation metrics. **死亡与牌组** (Failures and decks) lets you select training or evaluation data and a window. [How to read the reports](STATISTICS.en.md)
- **Roll back.** Select an earlier saved checkpoint and start another run. New saves use new directories and follow the selected checkpoint's history. The release's 142,000 games are the starting count. Charts and custom ranges use the per-game records available locally.

Training uses progressive rent rewards (scheme 2). **调整方案二** (Edit scheme 2) lets you change the points for each rent payment, defeat, and victory. **更多设置** (More settings) controls item toggles and their reward. **计算线程** (Compute threads) controls PyTorch CPU threads and takes effect at the next save stage. Each stage records its settings in `manifest.json`.

## Optional features and command line

The training page includes practice grants and inventory overflow penalties. [Optional training settings](METHOD.en.md#optional-training-settings)

You can use the same workflow from the project root. `$model` is the local directory printed by the importer. Use a new output directory and a training seed range disjoint from earlier training and evaluation.

```powershell
$py = ".\.venv\Scripts\python.exe"
$model = & $py python/published_checkpoint.py "C:\Downloads\scratch-142k.zip"
& $py python/manual_evaluate.py $model --out evaluations/my-first-eval --games 1000 --threads 2 --models after_continuation
& $py python/train_fresh_rules.py --previous $model --out runs/my-continuation --games 1000 --train-seed-start 1809000000 --limited-item-toggles --no-eval
& $py python/continuous_report.py runs/my-continuation
```

Use a nonexistent directory for `--out`. Run the evaluator separately after a command-line stage. Omit `--previous` to initialize random weights. For setup errors, check that you are using 64-bit Python 3.12 from `.venv`, that `lbal_py.pyd` is from the same release, and that the game data check passes. When backing up training, preserve the full branch of save directories. Each manifest links to its parent checkpoint.
