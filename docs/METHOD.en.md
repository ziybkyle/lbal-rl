# Training method and experiment settings

[Project overview](../README.en.md) · [中文](METHOD.zh-CN.md) · [Training panel](TRAINING.en.md) · [Statistics](STATISTICS.en.md)

## Games and policy updates

The objective is to defeat the landlord on Floor 20. Spins earn coins. Offers provide symbols, items, and essences. The player pays twelve rents through the 777-coin payment. During the fight, earnings reduce the landlord's 1,500 HP and further rent payments are due. The board has 20 cells, so a larger inventory leaves some symbols off the board on each spin.

1. Rust returns the current state and legal actions. The policy scores them and samples from its distribution during training.
2. The simulator applies the action and returns the next state, reward, and episode status. Finished games are recorded before moving to the next training seed.
3. Each full rollout of 512 environment actions feeds a PPO update of the policy and value networks. A rollout can span games or end partway through one.
4. At a save boundary, the panel writes the weights, optimizer state, and statistics. The sampling count includes the final partial rollout. The next stage collects a new rollout.

The policy learns which actions to take. The value network estimates the reward that remains under the current reward scheme and supplies PPO's advantage estimate. Updated weights influence later training decisions. A fixed-seed evaluation uses one frozen checkpoint throughout.

## What the policy observes

| Input | Examples |
| --- | --- |
| Economy and phase | Balance, next rent, spins left, tokens, reward phase, landlord HP |
| Offers and rule features | Card identity, rarity, base value, rule references, growth delays, pre-spin cash-outs |
| Inventory | Symbol identities, values and individual progress, such as coal's growth countdown |
| Items and essences | Presence, enabled state, charge, stored value, available payout |
| Landlord clauses and history | Clause targets and destroyed and removed counts by symbol type |

Offers share a candidate scorer while preserving each candidate's position and identity. The simulator masks illegal actions. Actions include spinning, taking or skipping a choice, rerolling symbols, removing symbols, and toggling or using items when permitted. Item and essence offers have no reroll action. The policy receives features such as a coal's growth delay and learns how those facts affect game outcomes through experience.

## Progressive rent rewards (scheme 2)

| Event | Default points |
| --- | --- |
| First twelve rent payments | Rent `k` earns `0.5 + (k−1)/11`, from 0.5 to 1.5 per payment and 12 total |
| Defeat | −3 points |
| Defeating the landlord | +20 points |

Games that hit the step limit are tracked separately as truncations. You can configure points for individual rent payments, defeat, victory, and toggling items. The panel shows points before scaling. Rewards sent to PPO are divided by 20. [Reward implementation](../python/reward_schemes.py)

## Optional training settings

- **Practice grants** add a chosen symbol, item, or essence to a configured fraction of games at the start or after a rent payment.
- **Inventory overflow shaping** charges additional training points for further growth beyond a configurable threshold. The settings control decay and caps.

Evaluation uses normal starting states and the saved action permissions. The released checkpoint began with random weights and used progressive rent rewards across 142,000 games, including 2,000 games with an experimental exploration bias. Its final stage used normal starts, standard exploration, zero toggle reward, and an allowlist for active toggling. [Exact settings and allowlist](../data/published_scratch_142k.json)

## Evaluation and reproducibility

- Panel evaluation takes consecutive seeds starting at `1900000000`. Equal evaluation lengths use the same starting games across checkpoints. Training uses separate, nonoverlapping seed ranges.
- Evaluation freezes weights and chooses the highest-scoring legal action. Training samples actions while updating weights, so its win rate is reported separately. Fixed seeds reduce comparison noise. Fresh seeds are useful when checking whether improvements selected on the fixed set generalize.
- Win rate uses completed, nontruncated games. A game that hits the 2,000-action limit is marked as truncated. Reports retain the average rent count, wins, and truncation count.
- Save the game data version, simulator version, checkpoint hash, seed range, reward settings, action permissions, and practice settings when reproducing an experiment. The published checkpoint was saved with CPU PyTorch and SB3.

| PPO setting in the published checkpoint | Value |
| --- | --- |
| Algorithm | SB3-Contrib MaskablePPO, one sampling environment |
| Rollout and minibatch | 512 actions and 256 samples |
| Maximum optimization epochs and learning rate | 10 and 0.0003 |
| Discount `γ` and GAE `λ` | 1 and 0.95 |
| Entropy coefficient and target KL | 0.01 and 0.05 |

Compute per game depends on how many spins and decisions it reaches. The Rust heuristic benchmark measures simulator throughput. The panel's progress pane measures PPO training throughput. [Controls and settings](TRAINING.en.md)
