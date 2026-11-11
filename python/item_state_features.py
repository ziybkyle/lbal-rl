"""Versioned, append-only item observations; no strategy ratings or rewards."""
from __future__ import annotations

import hashlib
import json
import math

import numpy as np

_LOG101 = math.log(101)

VERSION = 'item_state_v1'
LATEST_VERSION = 'item_state_v2'
SLOTS = 256  # same slots as the managed action interface
FIELDS = ('saved_value', 'cashout_coins', 'progress', 'target', 'remaining', 'fraction',
          'age', 'stored_symbols', 'consumed_count', 'protection_limit', 'progress_known', 'cashout_known')
GLOBALS = ('rent_pending', 'rent_gap', 'destroyed_symbols', 'removed_symbols')
FEATURES = len(FIELDS)
DIM = SLOTS + SLOTS * FEATURES + len(GLOBALS)
SCHEMA_HASH = hashlib.sha256(json.dumps({'version': VERSION, 'slots': SLOTS, 'fields': FIELDS,
                                       'globals': GLOBALS, 'scaling': 'signed_log1p_log101'}, sort_keys=True).encode()).hexdigest()


def schema_hash(version):
    version = normalize_version(version)
    if version is None:
        return None
    if version == VERSION:
        return SCHEMA_HASH
    from rule_context_features import schema
    return hashlib.sha256((LATEST_VERSION + SCHEMA_HASH + schema()['hash']).encode()).hexdigest()


def dimension(version, capacity):
    version = normalize_version(version)
    if version is None:
        return 0
    if version == VERSION:
        return DIM
    from rule_context_features import dimension as context_dimension
    return DIM + context_dimension(capacity)


def normalize_version(version):
    if not version:
        return None
    if version is True or version == VERSION:
        return VERSION
    if version == LATEST_VERSION:
        return LATEST_VERSION
    raise ValueError(f'Unknown item-state observation version: {version}')


def scaled(value):
    value = float(value or 0)
    if not math.isfinite(value):
        raise ValueError('Item state must contain finite numbers')
    if value == 0:
        return value
    return math.copysign(math.log1p(abs(value)) / _LOG101, value)


def append(observation, legacy, rules, version=VERSION, capacity=None):
    if observation.get('item_state_version') != VERSION:
        raise ValueError('Rust 模块尚未提供完整物品状态，请更新模块后再使用此模型。')
    ids = np.zeros(SLOTS, dtype=np.float32)
    states = np.zeros((SLOTS, FEATURES), dtype=np.float32)
    managed = observation.get('managed_items', [])
    if len(managed) > SLOTS:
        raise ValueError('Owned item stacks exceed managed observation capacity')
    for slot, item in enumerate(managed):
        state = item.get('state')
        if not isinstance(state, dict):
            raise ValueError(f"Missing dynamic state for {item.get('name')}")
        ids[slot] = rules.identity(item['kind'], item['name'])
        progress = state.get('progress')
        current = progress['current'] if progress else 0
        target = progress['target'] if progress else 0
        fraction = min(1.0, max(0.0, current / target)) if target else 0.0
        states[slot] = [scaled(state.get('saved_value')), scaled(state.get('cashout_coins')),
                        scaled(current), scaled(target), scaled(progress['remaining'] if progress else 0),
                        fraction, scaled(state.get('age')), scaled(state.get('stored_symbols')),
                        scaled(state.get('consumed_count')), scaled(state.get('protection_limit')),
                        float(progress is not None), float(state.get('cashout_coins') is not None)]
    context = np.asarray([float(bool(observation.get('rent_pending'))),
                          scaled(float(observation.get('rent_due', 0))-float(observation.get('coins', 0))),
                          scaled(observation.get('destroyed_symbols')), scaled(observation.get('removed_symbols'))], dtype=np.float32)
    result = np.concatenate((legacy, ids, states.ravel(), context)).astype(np.float32, copy=False)
    if normalize_version(version) == LATEST_VERSION:
        from rule_context_features import encode
        result = np.concatenate((result, encode(observation, rules, capacity or rules.max_offers)))
    return result


LATEST_SCHEMA_HASH = schema_hash(LATEST_VERSION)
