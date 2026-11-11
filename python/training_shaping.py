"""Optional training-only rewards, frozen per save and never used by evaluation."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

VERSION = 'training_shaping_v1'
DEFAULTS = dict(deck_enabled=False, deck_threshold=22, deck_first=.5, deck_decay=.8,
                deck_cap=3.0, reference_enabled=False, reference_strength=.2,
                reference_deadband=.05, reference_min_offers=30, reference_smoothing=50,
                reference_choice_cap=.2, reference_cap=2.0, reference_eval_games=1000)


def validate(value=None):
    if not isinstance(value or {}, dict):
        raise ValueError('训练塑形设置必须是一个配置对象。')
    result = DEFAULTS | (value or {})
    result = {key: result[key] for key in DEFAULTS}
    for key in ('deck_enabled', 'reference_enabled'):
        if not isinstance(result[key], bool):
            raise ValueError('训练塑形开关必须为开启或关闭。')
    ranges = dict(deck_threshold=(0, 150), deck_first=(0, 5), deck_decay=(0, 1),
                  deck_cap=(0, 20), reference_strength=(0, 2), reference_deadband=(0, 1),
                  reference_min_offers=(1, 10000), reference_smoothing=(0, 10000),
                  reference_choice_cap=(0, 2), reference_cap=(0, 20), reference_eval_games=(1, 100000))
    integers = {'deck_threshold', 'reference_min_offers', 'reference_eval_games'}
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
    return c['deck_enabled'] or c['reference_enabled']


def label(config):
    c = validate(config)
    parts = []
    if c['deck_enabled']:
        parts.append(f'控牌：>{c["deck_threshold"]}张，首张−{c["deck_first"]:g}，后续×{c["deck_decay"]:g}，每局至多−{c["deck_cap"]:g}')
    if c['reference_enabled']:
        parts.append(f'评估差值引导：强度{c["reference_strength"]:g}，忽略{100*c["reference_deadband"]:g}个百分点内差距，每局绝对分≤{c["reference_cap"]:g}')
    return '；'.join(parts)


def suffix(config):
    c = validate(config)
    parts = []
    if c['deck_enabled']:
        parts.append(f'控牌{c["deck_threshold"]}')
    if c['reference_enabled']:
        parts.append(f'差值{c["reference_strength"]:g}')
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
    """Only use assessments on this checkpoint's ancestry, never another branch.

    ancestors is newest first, as returned by train_fresh_rules.load_ancestors.
    Raw legacy bucket semantics deliberately match the reference-comparison page;
    the reference report has no per-decision log to reconstruct player intervals.
    """
    c = validate(config)
    snapshot = dict(version=VERSION, config=c, status='disabled', rows=[],
                    units='unscaled reward points; divide with the selected base reward scheme',
                    bucket_basis='reference_original_source_ordinal',
                    repeat_rule='at most one reference score per kind/name/stage per game')
    if not enabled(c):
        return snapshot
    snapshot['status'] = 'deck_only' if not c['reference_enabled'] else 'waiting_for_evaluation'
    if c['reference_enabled']:
        from training_analytics import latest_eval_source, read_jsonl
        from prior_analysis import compare_records
        from training_options import freeze_reference
        from statistical_prior import digest
        history = [(path, summary) for path, summary, _ in reversed(ancestors)]
        try:
            source = latest_eval_source(history)
        except ValueError:
            source = None
        if source:
            games_path = Path(source['games_path'])
            records = list(read_jsonl(games_path))[-c['reference_eval_games']:]
            reference = freeze_reference(dict(use_prior=True), Path(directory).parent)
            prior = json.loads(Path(reference['prior_path']).read_text(encoding='utf8'))
            compared, _, missing = compare_records(records, prior)
            rows = []
            for row in compared:
                n, rn = row['legacy_model_n'], row['reference_n']
                if min(n, rn) < c['reference_min_offers']:
                    continue
                gap = row['reference_rate'] - row['legacy_model_rate']
                adjusted = math.copysign(max(0.0, abs(gap) - c['reference_deadband']), gap)
                reliability = n / (n + c['reference_smoothing'])
                rows.append(dict(kind=row['kind'], name=row['name'], ordinal=row['ordinal'],
                    rarity=row['rarity'], model_n=n, model_k=row['legacy_model_k'],
                    reference_n=rn, reference_k=row['reference_k'], gap=gap,
                    effective_gap=adjusted * reliability))
            snapshot.update(status='ready', rows=rows, games=len(records),
                valid_games=sum(not bool(row.get('truncated')) for row in records),
                evaluation_games_path=str(games_path.resolve()), evaluation_games_sha256=digest(games_path),
                evaluation_source=source.get('source'), evaluation_created_at=source.get('created_at'),
                evaluated_checkpoint=source.get('checkpoint') or source.get('result', {}).get('model_path'),
                evaluated_model_sha256=source.get('model_sha256'),
                reference_sha256=reference['prior_sha256'], reference_path=reference['prior_path'],
                missing_stage_decisions=missing)
    path = Path(directory)/'training_shaping_snapshot.json'
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf8')
    return snapshot


class TrainingShaper:
    def __init__(self, config, snapshot=None):
        self.config = validate(config)
        self.index = {(r['kind'], r['name'], r['ordinal']): r for r in (snapshot or {}).get('rows', [])}
        self.reset()

    def reset(self):
        self.deck_spent = self.reference_spent = 0.0
        self.scored = set()

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
        if c['reference_enabled']:
            parts['reference_gap'] = 0.0
            if chosen:
                from statistical_prior import source_ordinal
                kind, name = chosen.get('kind', 'symbol'), chosen.get('name')
                ordinal = source_ordinal(kind, previous.get('rents_paid', 0))
                key = (kind, name, ordinal)
                row = self.index.get(key)
                # Modified card rarities are not comparable with the reference.
                rarity_ok = row and (kind == 'essence' or not chosen.get('rarity') or chosen['rarity'] == row['rarity'])
                if rarity_ok and key not in self.scored:
                    self.scored.add(key)
                    amount = c['reference_strength'] * row['effective_gap']
                    limit = min(c['reference_choice_cap'], max(0.0, c['reference_cap'] - self.reference_spent))
                    amount = max(-limit, min(limit, amount))
                    self.reference_spent += abs(amount)
                    parts['reference_gap'] = amount
                    if amount:
                        detail['reference'] = dict(kind=kind, name=name, ordinal=ordinal,
                            reference_minus_model=row['gap'], model_n=row['model_n'])
        return parts, detail
