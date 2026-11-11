"""Compact replay actions by default; opt-in lossless diagnostic histories.

An episode has an initial observation and one recursive delta for *every*
action, including spins.  Reconstructing it is a JSON operation, not a game
replay: it neither needs the simulator nor makes new random draws.  Observed
symbol/item state, enabled flags, progress, pools and boss rules are retained.
Only display-only expert scores are omitted. Missing old fields stay unknown.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

HISTORY_SCHEMA = 'episode_history_v2'
DETAILS_SCHEMA = 'training_decisions_v2'
COMPACT_DETAILS_SCHEMA = 'training_decisions_v1'
ACTION_SCHEMA = 'gym_action_ids_v1'
RESOURCE_KEYS = ('reroll_tokens', 'removal_tokens', 'essence_tokens')
CONTEXT_KEYS = ('phase', 'coins', 'rent_due', 'rents_paid', 'spins_left',
                'total_spins', *RESOURCE_KEYS, 'can_reroll', 'can_remove',
                'rent_pending', 'must_take_symbol', 'boss_fight', 'boss_hp')
SYNERGY_ITEMS = frozenset(('coin_on_a_string', 'coin_on_a_string_essence',
                          'flush', 'flush_essence', 'pink_pepper', 'pink_pepper_essence'))


def deepcopy(value):
    """Copy JSON containers without copy.deepcopy's object-dispatch overhead."""
    if isinstance(value, dict):
        return {key: deepcopy(child) for key, child in value.items()}
    if isinstance(value, list):
        return [deepcopy(child) for child in value]
    return value


def snapshot(observation: dict) -> dict:
    state = deepcopy(observation)
    # Expert scores are convenience output, not part of simulator state.
    for entry in state.get('offer', ()):
        entry.pop('expert_score', None)
    return state


def difference(before, after):
    """A small JSON-safe patch; ``None`` means no change."""
    if before == after:
        return None
    if isinstance(before, dict) and isinstance(after, dict):
        changed = {key: patch for key in before.keys() & after.keys()
                   if (patch := difference(before[key], after[key])) is not None}
        result = {}
        added = {key: deepcopy(after[key]) for key in after.keys() - before.keys()}
        deleted = sorted(before.keys() - after.keys())
        if changed:
            result['p'] = changed
        if added:
            result['s'] = added
        if deleted:
            result['d'] = deleted
        return result
    if isinstance(before, list) and isinstance(after, list):
        changed = {}
        for index, value in enumerate(after):
            patch = difference(before[index], value) if index < len(before) else {'v': deepcopy(value)}
            if patch is not None:
                changed[str(index)] = patch
        result = {'p': changed}
        if len(before) != len(after):
            result['n'] = len(after)
        return result
    return {'v': deepcopy(after)}


def apply_difference(before, patch):
    if patch is None:
        return deepcopy(before)
    if 'v' in patch:
        return deepcopy(patch['v'])
    result = deepcopy(before)
    if isinstance(result, list):
        size = patch.get('n', len(result))
        result = result[:size] + [None] * max(0, size - len(result))
        for index, delta in patch.get('p', {}).items():
            result[int(index)] = apply_difference(result[int(index)], delta)
    else:
        for key in patch.get('d', ()):
            result.pop(key, None)
        result.update(deepcopy(patch.get('s', {})))
        for key, delta in patch.get('p', {}).items():
            result[key] = apply_difference(result[key], delta)
    return result


def decision_context(obs: dict) -> dict:
    context = {key: obs.get(key) for key in CONTEXT_KEYS}
    context['deck_len'] = len(obs['deck']) if 'deck' in obs else None
    context['post_rent_offer'] = obs.get('phase') == 'symbol_reward'
    context['resource_items'] = [deepcopy(item) for item in obs.get('managed_items', ())
                                 if item.get('name') in SYNERGY_ITEMS]
    return context


def terminal_metrics(obs: dict | None) -> dict:
    """Flat terminal columns for summaries; never pretend missing means zero."""
    obs = obs or {}
    return {**{f'end_{key}': obs.get(key) for key in RESOURCE_KEYS},
            'end_destroyed_symbols': obs.get('destroyed_symbols'),
            'end_removed_symbols': obs.get('removed_symbols'),
            'failure_reason': obs.get('failure_reason')}


def recorded_terminal_metrics(record: dict) -> dict:
    """Read new summaries/details and old logs with honest missing values."""
    metrics = terminal_metrics(record.get('end_state'))
    for key in metrics:
        if key in record:
            metrics[key] = record[key]
    return metrics


def compact_record(game, seed, actions, end_state, **extra) -> dict:
    """Keep exact gym action order and terminal resources, without state copies.

    Replaying requires the same simulator/rules and practice configuration as
    the source run.  This is intentionally not a reconstructible state journal.
    """
    return dict(game=game, seed=seed, action_schema=ACTION_SCHEMA,
                action_ids=actions, **terminal_metrics(end_state), **extra)


def attach_episode_history(env, previous: dict, current: dict, record: dict) -> dict:
    """Logging-only hook called after the action/reward have been resolved."""
    step = record['step']
    initial = step == 1 or not hasattr(env, '_episode_history_state')
    before = snapshot(previous) if initial else env._episode_history_state
    after = snapshot(current)
    event = {'step': step, 'action_id': record['action'],
             'action': record['action_kind'], 'reward': record.get('reward'),
             'reward_parts': record.get('reward_parts'),
             'delta': difference(before, after)}
    if initial:
        event['initial_state'] = before
    if record.get('managed_target') is not None:
        event['managed_target'] = deepcopy(record['managed_target'])
    decision = record.get('analysis_decision')
    if decision is not None:
        decision.update(step=step, context=decision_context(previous),
                        after=decision_context(current))
        if record.get('managed_target') is not None:
            decision['managed_target'] = deepcopy(record['managed_target'])
        event['choice'] = decision.get('choice')
    env._episode_history_state = after
    record['history_event'] = event
    # This is the observation immediately following the action, not an
    # auto-reset observation provided by the vector environment.
    if current.get('done') or record.get('truncated'):
        record['episode_end_state'] = after
    return record


def history_record(game, seed, events, end_state, **extra) -> dict:
    complete = bool(events and 'initial_state' in events[0]
                    and [e['step'] for e in events] == list(range(1, len(events) + 1)))
    return dict(game=game, seed=seed, history_schema=HISTORY_SCHEMA,
                history_complete=complete, events=events,
                end_state=snapshot(end_state) if end_state is not None else None, **extra)


def reconstruct_episode(record: dict):
    """Yield (event, before, after), raising for missing/invalid old history."""
    if record.get('history_schema') != HISTORY_SCHEMA:
        raise ValueError('This older log has no complete recorded state history; missing fields are unknown.')
    state = None
    for expected_step, event in enumerate(record.get('events', ()), 1):
        if event.get('step') != expected_step:
            raise ValueError('History contains a missing or out-of-order step')
        if expected_step == 1:
            if 'initial_state' not in event:
                raise ValueError('History is missing its initial state')
            state = deepcopy(event['initial_state'])
        before = state
        state = apply_difference(state, event.get('delta'))
        yield event, before, state


def read_history_records(path, *, allow_incomplete=False):
    path = Path(path)
    opener = gzip.open if path.suffix == '.gz' else open
    try:
        with opener(path, 'rt', encoding='utf-8') as source:
            for line in source:
                if allow_incomplete and not line.endswith('\n'):
                    break
                if line.strip():
                    yield json.loads(line)
    except EOFError:
        if not allow_incomplete:
            raise
