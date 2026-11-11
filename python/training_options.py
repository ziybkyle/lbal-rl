"""Explicit per-save training options; no game or neural-network imports."""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = 'simple_training_options_v2'


@lru_cache(maxsize=1)
def cards():
    names = json.loads((ROOT/'data/association_rules.json').read_text(encoding='utf8'))['names']
    result = []
    for kind, file in [('symbol', 'Symbols'), ('item', 'Items'), ('essence', 'Essences')]:
        definitions = json.loads((ROOT/'game/JSON'/f'{file} - JSON.json').read_text(encoding='utf8'))
        for name in definitions:
            if name == 'empty':
                continue
            result.append(dict(kind=kind, name=name, display=names.get(name, {}).get('display', name)))
    return result


def validate_options(value=None):
    if isinstance(value, str):
        value = json.loads(value)
    value = value or {}
    result = dict(version=VERSION, practice=None)
    from training_shaping import validate as validate_shaping
    result['shaping'] = validate_shaping(value.get('shaping'))
    if value.get('practice'):
        p = value['practice']
        kind, name = p.get('kind'), p.get('name')
        if (kind, name) not in {(c['kind'], c['name']) for c in cards()}:
            raise ValueError('请选择图鉴中的符号、物品或精华。')
        def integer(key, default, low, high):
            raw = p.get(key, default)
            number = float(raw)
            if isinstance(raw, bool) or not math.isfinite(number) or not number.is_integer() or not low <= number <= high:
                raise ValueError(f'{key} 应为 {low}～{high} 的整数。')
            return int(number)
        count = integer('count', 1, 1, 1 if kind == 'essence' else 10)
        percent = float(p.get('percent', 25))
        if not math.isfinite(percent) or not 0 < percent <= 100:
            raise ValueError('指定内容练习的比例请输入大于 0、至多 100 的百分数。')
        trigger = p.get('trigger', 'start')
        if trigger not in ('start', 'rent'):
            raise ValueError('请选择开局添加，或交租后添加。')
        result['practice'] = dict(kind=kind, name=name, count=count, percent=percent,
                                  trigger=trigger, rent=integer('rent', 6, 1, 12))
    return result


def options_label(options):
    o = validate_options(options)
    label = '正常开局'
    if o['practice']:
        p = o['practice']
        name = next(c['display'] for c in cards() if (c['kind'], c['name']) == (p['kind'], p['name']))
        when = '开局' if p['trigger'] == 'start' else f'交租{p["rent"]}次后'
        label = f'{p["percent"]:g}%对局{when}加{name}×{p["count"]}'
    from training_shaping import label as shaping_label
    shaping = shaping_label(o['shaping'])
    if shaping:
        label += '｜' + shaping
    return label


def model_label(meta, total):
    options = meta.get('training_options')
    source = meta.get('previous_run') or meta.get('origin_run')
    uid = meta.get('source_checkpoint_sha256', '')[:6] or (Path(source).name[-6:] if source else '全新')
    base = int(meta.get('previous_training_games', 0))
    own = '｜保存 #' + str(meta['model_id']) if meta.get('model_id') else ''
    return f'累计 {total:,} 局｜来源 {base:,} 局 #{uid}｜{options_label(options)}｜奖励方案二{own}'


def save_id(path):
    name = Path(path).name
    match = re.search(r'ui_training_\d{8}_\d{6}_([a-f0-9]{6})', name)
    return match[1] if match else name[-12:]


def save_suffix(options, source_total=0):
    o = validate_options(options)
    label = '正常开局'
    if o['practice']:
        p = o['practice']
        name = next(c['display'] for c in cards() if (c['kind'], c['name']) == (p['kind'], p['name']))
        label = f'{"开局" if p["trigger"] == "start" else "租"+str(p["rent"])}{name[:8]}x{p["count"]}_{p["percent"]:g}pct'
    from training_shaping import suffix as shaping_suffix
    return f'_源{source_total}_{label}' + shaping_suffix(o['shaping'])


class PracticeSchedule:
    """One training-only grant per eligible episode; never consume the game's RNG."""
    def __init__(self, practice):
        self.practice = validate_options(dict(practice=practice))['practice']
        self.events = []
        self.applied = False
        self.selected = False

    def reset(self, seed):
        self.events = []
        self.applied = False
        p = self.practice
        digest = hashlib.sha256(f'practice-v1:{int(seed)}'.encode()).digest()
        fraction = int.from_bytes(digest[:8], 'little') / 2**64
        self.selected = bool(p and fraction < p['percent'] / 100)

    def apply(self, env):
        p, obs = self.practice, env.observation
        if not p or not self.selected or self.applied or obs.get('done') or obs.get('phase') != 'ready':
            return
        if p['trigger'] == 'rent' and int(obs.get('rents_paid', 0)) < p['rent']:
            return
        env.training_grant(p['kind'], p['name'], p['count'])
        self.applied = True
        self.events.append(dict(**p, spin=obs.get('total_spins', 0), rents_paid=obs.get('rents_paid', 0)))

    def drain_events(self):
        events, self.events = self.events, []
        return events
