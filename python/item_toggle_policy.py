"""Player-selected toggle permissions, shared by training, evaluation and UI."""
from __future__ import annotations

ALLOWLIST_VERSION = 'player_selection_20261108_v1'

ITEM_TOGGLES = (
    ('egg_carton', '蛋盒'), ('fish_bowl', '鱼缸'), ('frying_pan', '煎锅'),
    ('grave_robber', '盗墓人'), ('guillotine', '断头台'), ('lucky_seven', '幸运七'),
    ('quigley_the_wolf', '野狼奎格利'), ('rain_cloud', '雨云'), ('reroll', '重掷'),
    ('shedding_season', '脱毛季节'), ('swear_jar', '脏话罐'), ('watering_can', '浇水壶'),
    ('coin_on_a_string', '系绳硬币'), ('compost_heap', '堆肥堆'),
    ('conveyor_belt', '传送带'), ('dark_humor', '黑色幽默'), ('fertilizer', '肥料'),
    ('flush', '同花'), ('lockpick', '开锁器'), ('lefty_the_rabbit', '兔子莱福特'),
    ('lint_roller', '粘毛滚筒'), ('piggy_bank', '存钱罐'),
    ('shattered_mirror', '碎镜子'), ('x_ray_machine', 'X 光机'),
    ('zaroffs_contract', '扎罗夫合约'), ('capsule_machine', '扭蛋机'),
)
ESSENCE_TOGGLES = (
    ('checkered_flag_essence', '格子旗精华'), ('egg_carton_essence', '蛋盒精华'),
    ('fish_bowl_essence', '鱼缸精华'), ('lockpick_essence', '开锁器精华'),
    ('quigley_the_wolf_essence', '野狼奎格利精华'), ('reroll_essence', '重掷精华'),
    ('shedding_season_essence', '脱毛季节精华'), ('swear_jar_essence', '脏话罐精华'),
    ('tax_evasion_essence', '逃税精华'), ('treasure_map_essence', '藏宝图精华'),
    ('coin_on_a_string_essence', '系绳硬币精华'), ('lemon_essence', '柠檬精华'),
    ('lint_roller_essence', '粘毛滚筒精华'), ('piggy_bank_essence', '存钱罐精华'),
    ('looting_glove_essence', '抢劫手套精华'), ('rusty_gear_essence', '生锈齿轮精华'),
    ('shattered_mirror_essence', '碎镜子精华'), ('time_machine_essence', '时光机精华'),
    ('capsule_machine_essence', '扭蛋机精华'), ('coffee_essence', '咖啡精华'),
    ('golden_carrot_essence', '金胡萝卜精华'), ('lucky_carrot_essence', '幸运胡萝卜精华'),
    ('lucky_dice_essence', '幸运骰子精华'), ('protractor_essence', '量角器精华'),
    ('quiver_essence', '箭袋精华'), ('void_portal_essence', '虚空传送门精华'),
    ('ancient_lizard_blade_essence', '古老蜥蜴之刃精华'), ('telescope_essence', '望远镜精华'),
)
DEFAULT_ALLOWLIST = frozenset(
    [('item', name) for name, _ in ITEM_TOGGLES]
    + [('essence', name) for name, _ in ESSENCE_TOGGLES]
)


def normalize_allowlist(entries=None):
    """Restore a saved snapshot, so later list edits cannot change old evaluations."""
    if entries is None:
        return DEFAULT_ALLOWLIST
    if not isinstance(entries, (list, tuple, set, frozenset)):
        raise ValueError('开关清单必须是物品类别与内部名称的列表。')
    result = set()
    for entry in entries:
        if (not isinstance(entry, (list, tuple)) or len(entry) != 2
                or entry[0] not in ('item', 'essence')
                or not isinstance(entry[1], str) or not entry[1]):
            raise ValueError('开关清单含有无效的类别或内部名称。')
        result.add(tuple(entry))
    return frozenset(result)


def toggle_policy_metadata(limited_item_toggles=False, item_toggle_allowlist=None):
    limited = bool(limited_item_toggles)
    return {
        'limited_item_toggles': limited,
        'item_toggle_allowlist_version': ALLOWLIST_VERSION if limited else None,
        'item_toggle_allowlist': [list(pair) for pair in sorted(normalize_allowlist(item_toggle_allowlist))] if limited else None,
    }


def saved_toggle_policy(meta):
    """Old records retain their original unrestricted behavior."""
    return {'limited_item_toggles': bool(meta.get('limited_item_toggles', False)),
            'item_toggle_allowlist': meta.get('item_toggle_allowlist')}


def toggle_scope_label(allow_item_toggle=True, limited_item_toggles=False):
    if not allow_item_toggle:
        return '禁止'
    return '指定清单' if limited_item_toggles else '全部允许'


def toggle_scope_description(meta):
    scope = toggle_scope_label(meta.get('allow_item_toggle', True), meta.get('limited_item_toggles', False))
    return (f'本轮训练和所有评估的主动开关权限：{scope}。'
            '物品自动效果、使用或消除物品、交租救急与枕头选择不受清单限制；'
            '同一物品或精华每次旋转前仍最多开关一次。')
