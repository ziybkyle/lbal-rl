"""Game-rule observations, without rankings, expert actions, or deck penalties."""
from __future__ import annotations

from functools import lru_cache
import csv
import hashlib
import json
import math
from pathlib import Path
import re

import numpy as np

from gym_env import ACTION_SIZE, MAX_OFFERS, REMOVE_BASE

ROOT = Path(__file__).resolve().parent.parent
VERSION = 'candidate_rules_v1'
LATEST_VERSION = 'candidate_rules_v2'
KINDS = ('symbol', 'item', 'essence')
FILES = ('Symbols - JSON.json', 'Items - JSON.json', 'Essences - JSON.json')
FACTS_FILE = ROOT / 'data' / 'candidate_rule_facts.json'
DECK_SLOTS = ACTION_SIZE - REMOVE_BASE
DECK_FEATURES = 5  # base value, payout count, saved value, spins left, payouts left
_LOG101 = math.log(101)
ROLE_PATTERNS = {
    'adjacent': r'\badjacent\b', 'diagonal': r'\bdiagonal\b',
    'permanent': r'\bpermanent', 'multiplier': r'\b(?:times|multiply|multiplied)\b|<[^>]*>\s*x\b',
    'destroy': r'\bdestroy', 'self_destroy': r'destroy[^.]*\bitself\b',
    'on_destroy': r'when destroyed|when .*destroyed|if destroyed',
    'add': r'\badds?\b', 'transform': r'\btransform', 'conditional': r'\bif\b|at least|or more',
    'spins': r'\bspins?\b', 'payout_count': r'after giving',
    'chance': r'%|\bchance\b', 'choice': r'\bchoose|\bchoices?\b',
    'rarity': r'\b(?:rare|uncommon|common|rarity)\b', 'each': r'\beach\b|\bevery\b',
    'remove_token': r'removal_token', 'reroll_token': r'reroll_token',
    'essence_token': r'essence_token', 'empty_space': r'\bempty\b',
    'not_destroy': r'cannot be destroyed|can.t be destroyed|are not destroyed',
    'extra': r'\bextra\b|\badditional\b',
}


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def scaled(value):
    value = number(value)
    if value == 0:
        return value  # retain signed zero, including the legacy -0.0 case
    return math.copysign(math.log1p(abs(value)) / _LOG101, value)


def rule_facts(description):
    """Extract the catalog's structured features from one local description."""
    bare = re.sub(r'<[^>]*>', ' ', description).lower()
    plain = re.sub(r'<value_(\d+)>', r' V\1 ', description)
    plain = re.sub(r'\s+', ' ', re.sub(r'<[^>]*>', ' ', plain)).lower()
    transform = re.search(r'transforms? into\s+<icon_([^>]+)>', description, re.IGNORECASE)
    spins = re.search(r'transform.*?after\s+v(\d+)\s+spins?', plain)
    payouts = re.search(r'after giving.*?v(\d+)\s+times?', plain)
    return dict(groups=sorted(set(re.findall(r'<(?:group|last)_([^>]+)>', description))),
                icons=sorted(set(re.findall(r'<icon_([^>]+)>', description))),
                clean_icons=sorted({name for hover, name in re.findall(r'<icon_(hover_)?([^>]+)>', description)
                                    if name != 'coin' or hover}),
                mentions_removal=bool(re.search(r'\bremov(?:e|es|ed|ing)\b', bare)),
                mentions_pre_spin=bool(re.search(r'before (?:each|every|a) spin', bare)),
                roles=[role for role, pattern in ROLE_PATTERNS.items() if re.search(pattern, description.lower())],
                transform=transform[1] if transform else '',
                spins=int(spins[1]) if spins else 0, payouts=int(payouts[1]) if payouts else 0)


class RuleCatalog:
    def __init__(self):
        self.sources = [ROOT / 'game' / 'JSON' / name for name in FILES]
        raw = [path.read_bytes() for path in self.sources]
        localization = ROOT / 'game' / 'Localization.csv'
        published = None
        if localization.is_file():
            self.sources.append(localization)
            with localization.open(encoding='utf-8-sig', newline='') as source:
                english = {row['key']: row.get('en', '') for row in csv.DictReader(source)}
        else:
            published = json.loads(FACTS_FILE.read_text(encoding='utf-8'))
            if published['catalog_version'] != VERSION or published['data_sha256'] != {
                    name: hashlib.sha256(data).hexdigest() for name, data in zip(FILES, raw)}:
                raise ValueError('游戏 JSON 与发布的规则特征不匹配，请使用对应版本的游戏数据。')
            self.sources.append(FACTS_FILE)
            english = {}
        digest = hashlib.sha256(VERSION.encode())
        for data in raw:
            digest.update(data)
        if published is None:
            digest.update(localization.read_bytes())
        self.cards = []
        for kind, data in zip(KINDS, raw):
            definitions = json.loads(data)
            for name, definition in sorted(definitions.items()):
                self.cards.append({'kind': kind, 'name': name, **definition,
                                   'description': english.get(name + '_desc', '')})
        self.identities = {(card['kind'], card['name']): index + 1 for index, card in enumerate(self.cards)}
        self.count = len(self.cards) + 1  # zero is padding, never a real identity
        self.groups = sorted({group for card in self.cards for group in card.get('groups', [])})
        self.group_index = {name: i for i, name in enumerate(self.groups)}
        self.roles = list(ROLE_PATTERNS)
        self.feature_names = ([f'kind:{k}' for k in KINDS] +
                              [f'rarity:{r}' for r in ('common', 'uncommon', 'rare', 'very_rare', 'essence')] +
                              ['base_value', 'has_base_value'] + [f'value:{i + 1}' for i in range(8)] +
                              [f'group:{g}' for g in self.groups] + [f'refers_group:{g}' for g in self.groups] +
                              [f'role:{r}' for r in self.roles] + ['growth_spins', 'growth_payouts',
                                                                 'has_transform_target', 'transform_target_base_value'])
        digest.update(json.dumps({'features': self.feature_names, 'roles': ROLE_PATTERNS,
                                  'deck_features': DECK_FEATURES}, sort_keys=True).encode())
        self.schema_hash = published['catalog_schema_hash'] if published else digest.hexdigest()
        self.table = np.zeros((self.count, len(self.feature_names)), dtype=np.float32)
        self.references = np.zeros((self.count, self.count), dtype=np.float32)
        self.transform_targets = np.zeros(self.count, dtype=np.int64)
        self.delays = {}
        self.facts = [published['cards'][card['kind']][card['name']] if published else rule_facts(card['description'])
                      for card in self.cards]
        by_name = {card['name']: index + 1 for index, card in enumerate(self.cards)}
        for index, (card, facts) in enumerate(zip(self.cards, self.facts), 1):
            targets, icons = set(facts['groups']), set(facts['icons'])
            target_id = by_name.get(facts['transform'], 0)
            self.transform_targets[index] = target_id
            target_base = self.cards[target_id - 1].get('value', 0) if target_id else 0
            parameters = card.get('values', [])
            spins, payouts = facts['spins'], facts['payouts']
            delay_spins = parameters[spins - 1] if 0 < spins <= len(parameters) else 0
            delay_payouts = parameters[payouts - 1] if 0 < payouts <= len(parameters) else 0
            self.delays[card['name']] = (number(delay_spins), number(delay_payouts))
            values = ([float(card['kind'] == kind) for kind in KINDS] +
                      [float(card.get('rarity') == rarity) for rarity in ('common', 'uncommon', 'rare', 'very_rare', 'essence')] +
                      [scaled(card.get('value')), float('value' in card)] +
                      [scaled(parameters[i]) if i < len(parameters) else 0 for i in range(8)] +
                      [float(g in card.get('groups', [])) for g in self.groups] +
                      [float(g in targets) for g in self.groups] +
                      [float(role in facts['roles']) for role in self.roles] +
                      [scaled(delay_spins), scaled(delay_payouts), float(bool(target_id)), scaled(target_base)])
            self.table[index] = values
            for other_index, other in enumerate(self.cards, 1):
                if other['name'] in icons or targets.intersection(other.get('groups', [])):
                    self.references[index, other_index] = 1
        # References describe rule text, not positive/negative ratings.
        self.extra_dim = MAX_OFFERS + self.count + DECK_SLOTS * DECK_FEATURES
        # Reward generation draws without replacement from one kind's pool.
        # Reserve that entire pool, including stacked credit-card effects.
        self.max_offers = max(sum(card['kind'] == kind for card in self.cards) for kind in KINDS)
        # Immutable catalog values do not need another logarithm every time
        # the same owned symbol is observed. These are derived lookups, not
        # additional inputs, and intentionally do not alter the schema.
        self.scaled_bases = (0.0,) + tuple(scaled(card.get('value')) for card in self.cards)
        time_machine = self.cards[self.identity('item', 'time_machine') - 1]
        self.time_machine_reduction = number(time_machine['values'][0])

    def observation_extra_dim(self, capacity=MAX_OFFERS):
        # Keep the complete v1 vector as a prefix. Each appended slot has an
        # independent identity and the same nine legacy candidate features.
        return self.extra_dim + (capacity - MAX_OFFERS) * 10

    def identity(self, kind, name):
        try:
            return self.identities[(kind, name)]
        except KeyError:
            raise ValueError(f'Unknown {VERSION} identity: {kind}/{name}') from None

    def append(self, observation, legacy, capacity=MAX_OFFERS):
        ids = np.zeros(MAX_OFFERS, dtype=np.float32)
        extra = np.zeros((capacity - MAX_OFFERS, 9), dtype=np.float32)
        extra_ids = np.zeros(capacity - MAX_OFFERS, dtype=np.float32)
        for entry in observation.get('offer', []):
            index = int(entry['index'])
            identity = self.identity(entry.get('kind', 'symbol'), entry['name'])
            if 0 <= index < MAX_OFFERS:
                ids[index] = identity
            elif MAX_OFFERS <= index < capacity:
                from train_ppo import candidate_features
                extra_ids[index - MAX_OFFERS] = identity
                extra[index - MAX_OFFERS] = candidate_features(entry, False)
            elif capacity > MAX_OFFERS:
                raise ValueError(f'offer index {index} exceeds the rule capacity {capacity}')
        owned = np.zeros(self.count, dtype=np.float32)
        deck = observation.get('deck', [])
        deck_ids = [self.identity('symbol', entry['name']) for entry in deck]
        for identity in deck_ids:
            owned[identity] += 0.1
        disabled = {(entry['kind'], entry['name']) for entry in observation.get('managed_items', []) if entry.get('disabled')}
        item_counts = {}
        for entry in observation.get('items', []):
            kind, name = entry.get('kind', 'item'), entry['name']
            if (kind, name) not in disabled:
                owned[self.identity(kind, name)] += 0.1
                item_counts[name] = item_counts.get(name, 0) + 1
        details = np.zeros((DECK_SLOTS, DECK_FEATURES), dtype=np.float32)
        for slot, entry in enumerate(deck[:DECK_SLOTS]):
            spins, payouts = self.delays.get(entry['name'], (0, 0))
            if entry['name'] == 'coal':
                if item_counts.get('time_machine_essence'):
                    spins = 0
                else:
                    spins = max(0, spins - item_counts.get('time_machine', 0) * self.time_machine_reduction)
            # Rabbit and wine mature once per instance, after actual payouts.
            # Screen appearances and later income do not restart that timer.
            payout_progress = number(entry.get('times_coins_given'))
            details[slot] = [self.scaled_bases[deck_ids[slot]], min(number(entry.get('times_coins_given')), 100) / 100,
                             scaled(entry.get('saved')),
                             max(0, spins - number(entry.get('times_displayed'))) / 20 if spins else 0,
                             max(0, payouts - payout_progress) / 20 if payouts else 0]
        return np.concatenate((legacy, ids, owned, details.ravel(), extra_ids, extra.ravel())).astype(np.float32, copy=False)


@lru_cache(maxsize=1)
def catalog():
    return RuleCatalog()


def capacity_for(version):
    if not version or version == VERSION:
        return MAX_OFFERS
    if version is True or version == LATEST_VERSION:
        return catalog().max_offers
    raise ValueError(f'Unknown candidate observation version: {version}')
