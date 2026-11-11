# Statistics and reports

[Project overview](../README.en.md) · [中文](STATISTICS.zh-CN.md) · [Training panel](TRAINING.en.md) · [Training method](METHOD.en.md)

## Find the right view

| Question | Where to look |
| --- | --- |
| How has the policy been doing recently? | **走势图** (Trends). Fixed-seed evaluation is on top and rolling training performance below. View all, the last 10,000, or the last 50,000 games. Choose a training window of 1,000, 2,000, 5,000, or 10,000 games. |
| Which continuation performed better? | **结果对比** (Results). `A / B` means `A` successful rents on average and a win rate of `B`. Choose visible columns in the header settings. Continuous training is grouped by 10,000 games in the table, with individual stage saves retained on disk. |
| Where do games end, and how large are decks? | **死亡与牌组** (Failures and decks). Switch between training and historical evaluations, and select a window. In evaluation mode select a saved batch and compare it with the previous one. |
| What does the policy pick? | **训练 → 浏览器分析** (Training → Browser analysis). Choose the latest evaluation, the latest training window, or a custom training range. Filter symbols, items, essences, and rarity. Click headers to sort or export CSV. |
| What happened in one game? | Open **逐局记录** (Per-game records) or scroll to the table below the rankings. Evaluation reports link to choices and actions. Logs store action IDs. |

Training exploration samples actions while updating the policy. Evaluation uses fixed weights and fixed seeds. Use matched seeds and game counts when comparing checkpoints. Use the rolling training curves and game logs to inspect the learning process.

## Wins, rents, and deck size

- **Win rate** divides wins by valid games. Step-limit truncations are tracked separately. Progress and per-game summaries average paid rents over the recorded games in the window.
- **Failed after paying 5 rents** means the game ended after the fifth successful payment and before completing the sixth. It can include a failed spin in that interval. Failures in the landlord fight are grouped under 12 paid rents.
- **Final deck size** counts symbols in the inventory at the last state. Wins and losses have separate averages and coverage counts. Missing values in older logs are unknown.

## Pick-rate denominators

Use coal as an example. The ranking distinguishes these measures.

| Column | Calculation |
| --- | --- |
| Overall pick rate | Games in which coal was picked at least once ÷ games in which it was offered at least once |
| First-offer pick rate | Games in which the policy picked coal on its first appearance ÷ games in which coal was offered |
| Pick rate for a rent interval | Times coal was picked during the interval ÷ times it appeared in that interval, with repeat offers counted |
| Win rate when owned | Wins among valid games that ever owned coal ÷ those games, including coal later consumed or removed |

A skipped or rerolled offer contributes to the appearance count. The replacement offer after a reroll is counted separately. The symbol, item, and essence rewards immediately after the 25-coin payment, including symbol rerolls, go in **Before rent 25**. Later spin offers go in **Rent 25–50**. An offer created by using an item in the middle of a period stays in the current interval.

Read the numerator, denominator, stage, and rarity together. Win rate when owned also reflects other cards, synergies, and survival long enough to see the symbol. Use the tables to find decisions worth investigating and a controlled comparison to estimate causal effects.

## Files saved locally

| File or directory | Contents |
| --- | --- |
| `runs/current_rules_training/<rules version>/` | Checkpoints and game records by save stage |
| `model_final.zip` | Stage weights and optimizer state for continuation |
| `manifest.json`, `summary.json` | Rules, rewards, options, parent checkpoint, seeds, and summaries |
| `training_games.jsonl` | Each game's outcome, seed, rents, spins, deck size, terminal tokens, and action counts |
| `training_decisions.jsonl.gz` | Offers, choices, ownership, and ordered actions joined to the summary by game number |
| `*_games.jsonl` and evaluation HTML | Fixed-seed per-game results. Separate manual evaluations are saved under `evaluations/`. |
| `analyses/` | Offline HTML and coverage metadata generated from existing logs |

The default compact logger flushes each completed game, so you can analyze finished games during training. To replay a game, use the matching simulator, game data, seed, and `action_ids`. Practice games also need the recorded grants. Add `--full-history` to a command-line training stage when you need per-action state snapshots. This increases runtime and storage. [Detailed log schema in Chinese](../EPISODE_HISTORY_SCHEMA.md)

The public 142,000-game checkpoint supplies the weights and settings. A custom training range starts with the first local continuation game, such as game 142,001. Evaluate the imported model to populate its per-game and candidate reports.

## Build a report from the command line

Run from the project directory and replace `runs/my-continuation` with your checkpoint directory. The report generator reads saved logs.

```powershell
$py = ".\.venv\Scripts\python.exe"
& $py python/training_analytics.py runs/my-continuation --mode evaluation
& $py python/training_analytics.py runs/my-continuation --mode training
& $py python/training_analytics.py runs/my-continuation --mode custom --first 142001 --last 143000
```

`evaluation` reads the most recent evaluation on the selected branch, up to 1,000 games. `training` reads the latest 1,000 training games. `custom` uses an inclusive range. Open the printed `分析.html` path in a browser to filter or sort.
