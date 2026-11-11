"""Names and plain-language explanations for selectable scratch rewards."""

import json
import math

REWARD_NAMES = {
    'fixed': '方案 1：固定交租奖励',
    'progressive': '方案 2：递增交租与失败扣分',
    'win_potential': '方案 3：历史胜负对齐阶段分',
    'progress_potential': '方案 4：交租与房东进度预支',
}

# Keep saved UI preferences written by earlier versions readable.
REWARD_NAME_ALIASES = {
    '原方案：固定交租奖励': REWARD_NAMES['fixed'],
    '新方案：递增交租与失败扣分': REWARD_NAMES['progressive'],
    '历史方案：胜负对齐阶段分': REWARD_NAMES['win_potential'],
}

REWARD_EXPLANATIONS = {
    'fixed': (
        '方案 1：固定交租奖励\n\n'
        '前 12 次成功交租，每次 +1；打败房东 +20；失败没有额外扣分。'
        '之后的交租不额外加分。\n\n'
        '优点：交租进度容易获得奖励，和之前训练使用的方式相同。'
        '注意：分数同时重视活得更久和最终通关，不保证只追求最高胜率。'
        '每次开关物品或精华的奖赏按输入值直接计入。'
    ),
    'progressive': (
        '方案 2：递增交租与失败扣分\n\n'
        '成功交第 k 次租（k=1～12），得 0.5 + (k−1)/11 分：'
        '第一次 +0.5，最后一次 +1.5，12 次共 +12。'
        '正常失败 −3；打败房东 +20；第 12 次以后的交租没有额外奖励。\n\n'
        '以上是默认数值，可在面板调整每次交租、失败和通关分数。上述是原始分数。为了训练尺度，所有分数包括开关奖赏统一除以 20 后送给模型：'
        '例如失败实际 −0.15、通关实际 +1。开关框也填写原始分数，'
        '输入 −0.05 时实际每次 −0.0025。\n\n'
        '达到步数上限而中止不算失败，不额外扣失败分；正常失败则结束游戏。'
        '此方案鼓励更晚失败，也重视通关，但不能保证胜率最高。'
    ),
    'win_potential': (
        '方案 3：历史胜负对齐阶段分\n\n'
        '交租时按从零模型历史评估统计的阶段差值给分；游戏结束时再结算。'
        '开关奖赏为 0、且游戏正常结束时，整局总奖励只由胜负决定。\n\n'
        '参考数值来自不同训练阶段的历史数据，胜场较少，不能当作当前模型真实胜率。'
        '非零开关奖赏会改变这个目标。此前一次测试中平均交租明显下降，效果仍需评估。'
    ),
    'progress_potential': (
        '方案 4：交租与房东进度预支\n\n'
        '交完前 12 次租时按递增的进度差额预支奖励，共 0.25；房东战按已造成伤害继续预支，最多再预支 0.25。'
        '正常结束时收回已有进度分，通关额外 +1。成功交租或造成伤害的当步可能得到正分，'
        '但这不保证模型一定学得更快。\n\n'
        '本项目采用 γ=1，与现有训练一致。每局初始进度为 0，开关奖赏为 0 时，'
        '正常结束的整局总奖励为通关 1、失败 0；达到步数上限的截断不是正常结局。'
        '每次开关的自定义奖赏额外计入；非零时整局总奖励不再严格是 0 或 1。\n\n'
        '这是新的奖励方案，并非独立模型；选择已有模型并继续训练会沿用其权重和优化器状态。'
        '方案不使用额外的牌组试玩打分。'
    ),
}

PROGRESSIVE_SCALE = 20.0
DEFAULT_PROGRESSIVE_RENTS = tuple(0.5 + (k - 1) / 11 for k in range(1, 13))
DEFAULT_PROGRESSIVE_LOSS = -3.0
DEFAULT_PROGRESSIVE_WIN = 20.0


def validate_progressive_outcomes(loss, win):
    """Validate the unscaled failure penalty and victory reward."""
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (loss, win)):
        raise ValueError('失败和通关分数必须是有限数字')
    if not -20 <= loss <= 0 or not 0 <= win <= 50:
        raise ValueError('失败分数须在 −20 至 0，通关分数须在 0 至 50')
    return float(loss), float(win)


def validate_progressive_rents(values):
    """Validate the 12 unscaled rent rewards; return an immutable snapshot."""
    if not isinstance(values, (list, tuple)) or len(values) != 12:
        raise ValueError('需要 12 个交租奖励')
    result = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError('交租奖励必须是数字')
        value = float(value)
        if not math.isfinite(value) or not 0 <= value <= 5:
            raise ValueError('交租奖励必须在 0 到 5 之间')
        result.append(value)
    return tuple(result)


def parse_progressive_rents(text):
    try:
        return validate_progressive_rents(json.loads(text))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f'方案二奖励设置无效：{exc}') from exc
