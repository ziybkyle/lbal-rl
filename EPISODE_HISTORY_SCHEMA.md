# 训练和评估逐局记录（精简默认，完整诊断可选）

为了减少训练开销，训练和评估默认使用精简记录，不再对每个动作复制完整状态、比较递归差分和序列化状态历史。记录方式不改变随机数、动作选择、奖励或游戏规则。以前已经生成的完整历史文件保持原样。

## 默认精简记录

- 训练继续写 `training_games.jsonl` 和 `training_decisions.jsonl.gz`。压缩明细使用兼容旧统计的 `schema = training_decisions_v1`，包含 `decisions`、`owned`。
- 评估继续写 `<label>_games.jsonl`，包含原 `decisions`、`owned`，默认不创建 `<label>_history.jsonl.gz`。
- 训练压缩明细和评估逐局记录新增 `action_schema = gym_action_ids_v1` 与 `action_ids`。这是实际执行的所有 Gym 动作编号，包含旋转、支付租金和选择，严格按执行顺序排列。`decisions.step` 可定位非旋转操作。
- 终局三类代币以及消除/移除总量、失败原因保留，训练汇总和压缩明细均可直接读取。它们是终局标量，不需要重新运行游戏。
- 精简记录不包含每步完整牌组、物品进度或历史池构成。有需要时，使用同一版本的模拟器、数据、种子和 `action_ids` 重新运行；指定内容练习还须沿用运行配置及已记录的赠送事件。不同游戏规则版本不能保证重放一致。
- 每完成一局仍立即写入并 flush。旧统计页面和历次评估统计继续使用原候选、选择和汇总字段。

## 可选完整诊断

仅需要逐步状态诊断时设置 `RustGymEnv(..., full_history=True)`，或在开始一局前设置 `LbalGym._env.full_history = True`。共享评估入口使用 `evaluate(..., full_history=True)`；旧独立评估命令可传入 `--full-history`。请勿在一局中途开关记录方式。

完整模式的格式及读取接口如下，默认训练不会支付这部分计算和存储成本。

## 文件与旧数据

- 完整诊断下，训练完整过程放在现有压缩文件 `training_decisions.jsonl.gz`。
- 完整诊断下，评估新增 `<label>_history.jsonl.gz`，保存完整过程。自动评估和单独评估共用同一记录方法。
- 每完成一局即写入并 flush 汇总及压缩过程文件。尚未完成的局只在内存中，不进行昂贵的逐动作磁盘同步。
- 旧的 `decisions`、`owned`、候选顺序、选择、动作统计等字段保留。旧记录缺少的终局资源是**未知**，不能当成 0；不自动重跑或修改旧数据。

## 汇总新增列

`end_reroll_tokens`、`end_removal_tokens`、`end_essence_tokens` 是终局剩余的重掷、移除、精华代币，包含失败局和截断局。在向量环境自动重置之前，从最后一个动作的实际结果取得。

另有 `end_destroyed_symbols`、`end_removed_symbols`、`failure_reason`。完整诊断还写入 `history_schema` 和 `history_ref`：`history_ref.file` 指向同目录的过程文件，`history_ref.game` 是文件内从 1 开始的局号。终局金币、房东血量、牌组张数沿用原列。

## 完整过程

`history_schema = episode_history_v2`，训练的兼容标签 `schema = training_decisions_v2`。

每行有 `game`、`seed`、`events`、`end_state`、`history_complete`。训练还保留原 `decisions`、`owned`。

- `events` 包含所有动作，包括旋转、支付租金、重掷、移除符号、拿取、跳过、开关和主动使用物品。
- 每个事件包含严格递增的 `step`、`action_id`、`action`、`reward`、`reward_parts` 和 `delta`。选择及管理动作另有 `choice` / `managed_target`。
- 第一条事件的 `initial_state` 是开局实际观察，包括指定内容练习的开局赠送。
- 初始状态与变化保留符号逐个状态、物品/精华启用状态及进度、候选及其品质、上场位置、金币和代币、租金和候选阶段。新增规则上下文 `boss_context`、`symbol_history_counts`、候选 `pre_spin_effect` 等也随观察自动保留。
- 不反复保存描述文本或专家便利分数 `expert_score`，不在每一步重复整个牌组和物品表。
- `end_state` 是最后动作后的完整观察；即使环境随后自动重开下一局，也不会串局。
- `decisions` 的 `step` 可以关联对应事件；`context` / `after` 可直接查看金币、资源、阶段、牌组数量、相关兑现物品状态。需要其他任意物品和池构成时，用事件重建，不必重新运行游戏。

## 变化格式与读取接口

变化是针对前一状态的递归补丁：`v` 替换值，字典 `s` 新增字段、`d` 删除字段、`p` 修改字段；数组 `n` 指定新长度，`p` 按索引修改。`delta = null` 表示状态没有变化。

建议使用 `python/episode_history.py` 的公共函数，无需自行解释补丁：

```python
from episode_history import read_history_records, reconstruct_episode, recorded_terminal_metrics

for game in read_history_records('training_decisions.jsonl.gz'):
    for event, before, after in reconstruct_episode(game):
        # before 是操作前；after 是操作后。
        pass
    remaining = recorded_terminal_metrics(game)
```

查看仍在写入的文件时设置 `allow_incomplete=True`，只读取已经完整写入的行。`recorded_terminal_metrics` 也能读取旧汇总，缺失值返回 `None`。`reconstruct_episode` 对缺少完整过程的旧记录明确报错，不制造伪造历史。
