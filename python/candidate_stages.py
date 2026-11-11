"""Player-facing rent intervals, independent of frozen exploration priors."""
RENT_SCHEDULE = (25, 50, 100, 150, 225, 300, 375, 450, 600, 650, 700, 777)
STAGE_BASIS = 'rent_rewards_to_previous_interval_v1'


def decision_ordinal(decision, rent_reward=None):
    paid = max(0, int(decision.get('rents_paid') or 0))
    # A reroll stays in symbol_reward, so the improved offer stays in the same interval.
    phase = decision.get('phase')
    if phase in ('symbol_reward', 'essence_reward') or (phase == 'item_reward' and rent_reward is not False):
        return max(1, paid)
    if rent_reward:
        return max(1, paid)
    if not decision.get('phase') and (decision.get('candidates') or [{}])[0].get('kind') == 'item':
        return max(1, paid)  # Older item-only logs already used the earned-rent bucket.
    return paid + 1


def staged_decisions(record):
    """Keep rent-generated bonus offers together; don't shift mid-period item capsules."""
    rent_context = None
    for decision in record.get('decisions') or []:
        key = (decision.get('rents_paid'), decision.get('spin'))
        phase = decision.get('phase')
        if phase in ('symbol_reward', 'essence_reward'):
            rent_context = key
        elif phase == 'ready':
            # A manually used box/bomb after reward selection starts a separate offer.
            rent_context = None
        in_reward = (rent_context == key) if decision.get('spin') is not None else None
        yield decision, decision_ordinal(decision, in_reward)


def rent_amount(ordinal):
    return RENT_SCHEDULE[ordinal - 1] if ordinal <= 12 else 1000 + (ordinal - 13) * 500


def stage_label(ordinal):
    if ordinal == 1:
        return '交租 25 前'
    return f'交租 {rent_amount(ordinal - 1)}～{rent_amount(ordinal)}'
