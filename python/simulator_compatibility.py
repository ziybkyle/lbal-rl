"""Allow only explicitly checked simulator updates for checkpoint continuation."""
import json
from pathlib import Path

COMPATIBILITY_FILE = Path(__file__).resolve().parents[1] / 'data/simulator_observation_compatibility.json'


def same_game_rules(previous_version, previous_hash, current_version, current_hash):
    if previous_version != current_version:
        return False
    if previous_hash == current_hash:
        return True
    try:
        data = json.loads(COMPATIBILITY_FILE.read_text(encoding='utf-8'))
        known = {previous_hash}
        for _ in range(len(data['observation_only_pairs']) + 1):
            for pair in data['observation_only_pairs']:
                if pair.get('game_traces_equal') is True and pair.get('rules_version',data['mechanics_version']) == current_version:
                    endpoints = {pair['before'],pair['after']}
                    if known & endpoints:known.update(endpoints)
            if current_hash in known:return True
        return False
    except (OSError, ValueError, TypeError, KeyError):
        return False


def checkpoint_compatible(previous_version, previous_hash, current_version, current_hash):
    if same_game_rules(previous_version, previous_hash, current_version, current_hash):
        return True
    try:
        data = json.loads(COMPATIBILITY_FILE.read_text(encoding='utf-8'))
        # A checkpoint may cross an explicitly validated rule repair without
        # claiming its old statistics were measured under the new rules.
        known={(previous_version,previous_hash)}
        for _ in range(len(data.get('checkpoint_upgrades',[]))+1):
            for pair in data.get('checkpoint_upgrades',[]):
                validated=(pair.get('legal_game_traces_equal') is True or
                           (pair.get('gameplay_change_validated') is True and
                            pair.get('legal_actions_compatible') is True and
                            pair.get('observation_layout_compatible') is True))
                if not validated or pair.get('checkpoint_upgrade_allowed') is not True:continue
                if any(same_game_rules(v,h,pair['before_version'],pair['before']) for v,h in known):
                    known.add((pair['after_version'],pair['after']))
            if any(same_game_rules(v,h,current_version,current_hash) for v,h in known):return True
        return False
    except (OSError, ValueError, TypeError, KeyError):
        return False
