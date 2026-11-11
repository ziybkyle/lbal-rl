"""Optional training-only rewards, frozen per save and never used by evaluation."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

VERSION = 'training_shaping_v1'
DEFAULTS = dict(deck_enabled=False, deck_threshold=22, deck_first=.5, deck_decay=.8, deck_cap=3.0)


def validate(value=None):
    if not isinstance(value or {}, dict):
        raise ValueError('训练塑形设置必须是一个配置对象。')
    result = DEFAULTS | (value or {})
    result = {key: result[key] for key in DEFAULTS}
    if not isinstance(result['deck_enabled'], bool):
        raise ValueError('储物间扣分开关必须为开启或关闭。')
    ranges = dict(deck_threshold=(0, 150), deck_first=(0, 5), deck_decay=(0, 1), deck_cap=(0, 20))
    integers = {'deck_threshold'}
    for key, (low, high) in ranges.items():
        raw = result[key]
        try:
            number = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f'{key} 必须为数字。') from None
        if isinstance(raw, bool) or not math.isfinite(number) or not low <= number <= high:
            raise ValueError(f'{key} 应在 {low}～{high} 之间。')
        if key in integers and not number.is_integer():
            raise ValueError(f'{key} 必须为整数。')
        result[key] = int(number) if key in integers else number
    return result


def enabled(config):
    c = validate(config)
    return c['deck_enabled']


def label(config):
    c = validate(config)
    parts = []
    if c['deck_enabled']:
        parts.append(f'控牌：>{c["deck_threshold"]}张，首张−{c["deck_first"]:g}，后续×{c["deck_decay"]:g}，每局至多−{c["deck_cap"]:g}')
    return '；'.join(parts)


def suffix(config):
    c = validate(config)
    parts = []
    if c['deck_enabled']:
        parts.append(f'控牌{c["deck_threshold"]}')
    if not parts:
        return ''
    fingerprint = hashlib.sha256(json.dumps(c, sort_keys=True).encode()).hexdigest()[:6]
    return '_' + '_'.join(parts) + '_' + fingerprint


def overflow_cost(count, config):
    """Concave cumulative cost; consecutive marginal costs are first * decay**i."""
    c = config
    n = max(0, int(count) - c['deck_threshold'])
    q = c['deck_decay']
    if not n:
        return 0.0
    return c['deck_first'] * (n if q == 1 else (1 - q ** n) / (1 - q))


def freeze_snapshot(directory, ancestors, config):
    c = validate(config)
    snapshot = dict(version=VERSION, config=c, status='deck_only' if c['deck_enabled'] else 'disabled')
    if c['deck_enabled']:
        path = Path(directory)/'training_shaping_snapshot.json'
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf8')
    return snapshot


class TrainingShaper:
    def __init__(self, config, snapshot=None):
        self.config = validate(config)
        self.reset()

    def reset(self):
        self.deck_spent = 0.0

    def step(self, previous, current, chosen=None):
        c = self.config
        parts, detail = {}, {}
        before, after = len(previous.get('deck') or []), len(current.get('deck') or [])
        if c['deck_enabled']:
            growth = max(0.0, overflow_cost(after, c) - overflow_cost(before, c))
            cost = min(growth, max(0.0, c['deck_cap'] - self.deck_spent))
            self.deck_spent += cost
            parts['deck_overflow'] = -cost
            if cost:
                detail.update(deck_before=before, deck_after=after)
        return parts, detail
