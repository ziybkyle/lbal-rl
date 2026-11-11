"""Configuration and explanation for the progressive rent reward."""

import json
import math

REWARD_MODE = 'progressive'
REWARD_NAMES = {REWARD_MODE: '方案 2：递增交租与失败扣分'}

REWARD_EXPLANATION = (
    '方案 2：递增交租与失败扣分\n\n'
    '成功交第 k 次租（k=1～12），得 0.5 + (k−1)/11 分：'
    '第一次 +0.5，最后一次 +1.5，12 次共 +12。'
    '正常失败 −3；打败房东 +20；第 12 次以后的交租没有额外奖励。\n\n'
    '以上是默认数值，可在面板调整每次交租、失败和通关分数。上述是原始分数。为了训练尺度，所有分数包括开关奖赏统一除以 20 后送给模型：'
    '例如失败实际 −0.15、通关实际 +1。开关框也填写原始分数，'
    '输入 −0.05 时实际每次 −0.0025。\n\n'
    '达到步数上限而中止不算失败，不额外扣失败分；正常失败则结束游戏。'
    '此方案鼓励更晚失败，也重视通关，但不能保证胜率最高。'
)

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
