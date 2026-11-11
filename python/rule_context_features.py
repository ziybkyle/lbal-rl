"""Append-only rule facts: public history, boss clauses and pre-spin consumers.

These are observations of existing mechanics, never rankings, rewards or advice.
The legacy catalog is deliberately untouched so old checkpoints keep their inputs.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json

import numpy as np

from candidate_rules import catalog, scaled

VERSION = 'rule_context_v1'
CLAUSES = 37
CANDIDATE_FIELDS = ('removes_before_spin', 'cash_per_symbol', 'stored_as_egg',
                    'stored_as_goldfish', 'consumer_count', 'rule_mentions_remove',
                    'rule_mentions_before_spin', 'clean_forward_owned', 'clean_reverse_owned',
                    'destroyed_referenced', 'removed_referenced', 'self_destroyed', 'self_removed')


@lru_cache(maxsize=1)
def schema():
    rules = catalog()
    symbols = tuple(card['name'] for card in rules.cards if card['kind'] == 'symbol')
    # <icon_coin> denotes currency; <icon_hover_coin> denotes the coin symbol.
    # The old reference matrix remains embedded in saved models, unchanged.
    clean = np.zeros_like(rules.references)
    roles = np.zeros((rules.count, 2), dtype=np.float32)
    for index, facts in enumerate(rules.facts, 1):
        targets, icons = set(facts['groups']), set(facts['clean_icons'])
        roles[index] = [facts['mentions_removal'], facts['mentions_pre_spin']]
        for target, other in enumerate(rules.cards, 1):
            clean[index, target] = float(other['name'] in icons or bool(targets.intersection(other.get('groups', []))))
    # A separate identity for every clause preserves which target belongs to
    # which restriction (a bag of all targets would lose this information).
    context_dim = CLAUSES * 4 + len(symbols) * 2 + 1
    digest = hashlib.sha256(json.dumps(dict(version=VERSION, symbols=symbols,
        clauses=CLAUSES, candidates=CANDIDATE_FIELDS, context_dim=context_dim,
        catalog=rules.schema_hash, reference_matrix=hashlib.sha256(clean.tobytes()).hexdigest(),
        role_table=hashlib.sha256(roles.tobytes()).hexdigest(),
        scaling='signed_log1p_log101', target_encoding='separate_categorical_identity_per_clause'),
        sort_keys=True).encode()).hexdigest()
    return dict(symbols=symbols, symbol_positions={name: i for i, name in enumerate(symbols)},
                references=clean, roles=roles, context_dim=context_dim,
                candidate_dim=len(CANDIDATE_FIELDS), hash=digest)


def dimension(capacity):
    info = schema()
    return info['context_dim'] + capacity * info['candidate_dim']


def encode(observation, rules, capacity):
    if observation.get('rule_context_version') != VERSION:
        raise ValueError('Rust 模块尚未提供房东条款和分类历史池，请更新模块后继续训练。')
    info = schema()
    # Allocate padding once; only real clauses/history/candidates are filled.
    # Every slice retains the original float32 ordering and scaling.
    result = np.zeros(info['context_dim'] + capacity * info['candidate_dim'], dtype=np.float32)
    active, pending, active_targets, pending_targets = result[:4 * CLAUSES].reshape(4, CLAUSES)
    boss = observation['boss_context']
    for clause in boss.get('clauses', []):
        number = int(clause['number'])
        if not 1 <= number <= CLAUSES:
            raise ValueError(f'Unknown landlord clause {number}')
        is_pending = bool(clause.get('pending'))
        (pending if is_pending else active)[number - 1] = 1
        target = clause.get('target')
        if target:
            identities = [rules.identities.get((kind, target)) for kind in ('symbol', 'item', 'essence')]
            identities = [identity for identity in identities if identity is not None]
            if not identities:
                raise ValueError(f'Unknown landlord target {target}')
            if len(identities) != 1:
                raise ValueError(f'Ambiguous landlord target {target}')
            (pending_targets if is_pending else active_targets)[number - 1] = identities[0]
    history = observation['symbol_history_counts']
    destroyed, removed = np.zeros((2, rules.count), dtype=np.float32)
    symbols = len(info['symbols'])
    history_values = result[4 * CLAUSES:4 * CLAUSES + 2 * symbols].reshape(2, symbols)
    for row, (key, output) in enumerate((('destroyed', destroyed), ('removed', removed))):
        counts = history[key]
        for name, count in counts.items():
            output[rules.identity('symbol', name)] = float(count)
            history_values[row, info['symbol_positions'][name]] = scaled(count)
    result[info['context_dim'] - 1] = scaled(boss.get('damage_to_next', 0)) if observation.get('boss_fight') else 0
    offer = observation.get('offer', [])
    if not offer:
        return result
    owned = np.zeros(rules.count, dtype=np.float32)
    for entry in observation.get('deck', []):
        owned[rules.identity('symbol', entry['name'])] += 1
    for entry in observation.get('items', []):
        owned[rules.identity(entry.get('kind', 'item'), entry['name'])] += 1
    facts = result[info['context_dim']:].reshape(capacity, info['candidate_dim'])
    references = info['references']
    has_destroyed = bool(history['destroyed'])
    has_removed = bool(history['removed'])
    for entry in offer:
        slot = int(entry['index'])
        if not 0 <= slot < capacity:
            raise ValueError('Candidate exceeds rule-context capacity')
        identity = rules.identity(entry.get('kind', 'symbol'), entry['name'])
        effect = entry.get('pre_spin_effect')
        if entry.get('kind', 'symbol') == 'symbol' and not isinstance(effect, dict):
            raise ValueError(f"Missing pre-spin rule facts for {entry['name']}")
        effect = effect or {}
        facts[slot] = [float(bool(effect.get('removed'))), scaled(effect.get('cash_per_symbol')),
            float(bool(effect.get('stored_as_egg'))), float(bool(effect.get('stored_as_goldfish'))),
            scaled(effect.get('consumer_count')), *info['roles'][identity],
            scaled(references[identity] @ owned), scaled(references[:, identity] @ owned),
            scaled(references[identity] @ destroyed) if has_destroyed else 0,
            scaled(references[identity] @ removed) if has_removed else 0,
            scaled(destroyed[identity]), scaled(removed[identity])]
    return result
