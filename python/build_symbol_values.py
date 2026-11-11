"""Build reusable approximate values from the local game's symbol definitions.

The output is a model for the policy, not a replacement for game mechanics.
The full list includes internal symbols and explicitly marks unsupported effects.
"""
import hashlib
import json
from math import comb
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "game" / "JSON" / "Symbols - JSON.json"

# Each feeder's actual game type/group checks were checked in Slot Icon.tscn.
FEEDERS = {
    "toddler": {"groups": ["toddlerlikes"], "bonus": 6},
    "cat": {"types": ["milk"], "bonus": 9},
    "mouse": {"types": ["cheese"], "bonus": 20},
    "goldfish": {"types": ["bubble"], "bonus": 15},
    "bear": {"types": ["honey"], "bonus": 40},
    "dwarf": {"groups": ["dwarflikes"], "value_multiple": 10},
    "monkey": {"groups": ["monkeylikes"], "value_multiple": 6},
    "miner": {"groups": ["minerlikes"], "bonus": 20},
    "hooligan": {"groups": ["spiritbox"], "bonus": 6},
    "bounty_hunter": {"types": ["thief"], "bonus": 20},
    "general_zaroff": {"groups": ["human"], "bonus": 25},
    "robin_hood": {"groups": ["robinhates"], "bonus": 15},
    "dame": {"types": ["martini"], "bonus": 40},
    "key": {"groups": ["chest"], "bonus": 0},
    "magic_key": {"groups": ["chest"], "bonus": 0, "death_multiplier": 3},
    "archaeologist": {"groups": ["archlikes"], "growth": 1},
    "mrs_fruit": {"groups": ["fruitlikes"], "growth": 1},
    "diver": {"groups": ["poslikes"], "growth": 1},
    "pirate": {"groups": ["piratelikes"], "growth": 1},
    "eldritch_beast": {"groups": ["fossillikes"], "growth": 1},
}

MULTIPLIERS = {
    "moon": {"groups": ["night"], "multiplier": 3},
    "beastmaster": {"groups": ["animal"], "multiplier": 2},
    "farmer": {"groups": ["farmerlikes"], "multiplier": 2},
    "chef": {"groups": ["food"], "multiplier": 2},
    "dame": {"groups": ["gem"], "multiplier": 2},
    "light_bulb": {"groups": ["gem"], "multiplier": 2},
    "witch": {"groups": ["witchlikes"], "multiplier": 2},
    "comedian": {"groups": ["funny"], "multiplier": 3},
    "bee": {"groups": ["beelikes"], "multiplier": 2},
    "joker": {"groups": ["suit"], "multiplier": 2},
    "sun": {"types": ["flower"], "multiplier": 5},
    "rain": {"types": ["flower"], "multiplier": 2},
    "king_midas": {"types": ["coin"], "multiplier": 3},
    "billionaire": {"groups": ["richlikes"], "multiplier": 2},
    "buffing_powder": {"all": True, "multiplier": 2},
}

# These are stable approximations, in coins/spin unless tagged as proxy.
# Boxes, tomb, urns and big ore use the user's fixed observed values instead of
# an invented destruction probability.  The remaining overrides cover effects
# whose generated symbols are now worth more than the printed face value.
BASE = {"anchor": 1.8, "d3": 2, "d5": 3, "robin_hood": 2.25,
        "crow": 1.25, "magpie": 1.25, "snail": 1.25, "turtle": 4/3,
        "sloth": 2, "owl": 4/3, "present": 10/12,
        "chemical_seven": 7, "lucky_capsule": 10, "tedium_capsule": 5,
        "void_stone": 8, "void_fruit": 8, "void_creature": 8,
        "lockbox": 1.0, "safe": 1.0, "treasure_chest": 2.0, "mega_chest": 3.0,
        "tomb": 4.0, "urn": 1.0, "big_urn": 2.0, "big_ore": 2.0,
        "bar_of_soap": 2.5, "golem": 0.5, "frozen_fossil": 3.0,
        "matryoshka_doll_1": 3.0, "mine": 2.0, "eldritch_beast": 4.0,
        "midas_bomb": 4.0, "card_shark": 3.0, "rabbit_fluff": 3.0,
        "diamond": 5.0, "watermelon": 4.0, "highlander": 6.0,
        "rabbit": 3.0, "wine": 3.0,
        "magic_key": 3.0, "key": 1.0}
# A fixed score gives each one-shot utility effect the same heuristic value.
UTILITY = dict.fromkeys(("item_capsule", "essence_capsule", "reroll_capsule",
                         "removal_capsule", "rarity_capsule", "time_capsule"), 2.0)
DEATH_BONUS = {"target": 10, "lockbox": 15, "safe": 30, "treasure_chest": 50,
               "mega_chest": 100, "billionaire": 39, "present": 10}
ONE_TURN = ["chemical_seven", "lucky_capsule", "tedium_capsule", "item_capsule",
            "essence_capsule", "reroll_capsule", "removal_capsule", "rarity_capsule",
            "time_capsule", "buffing_powder", "hustler"]
VOID = ["void_stone", "void_fruit", "void_creature"]
TIMED = ["bubble", "present", "spirit", "bar_of_soap", "light_bulb"]

# Spread one-time coin payouts over four spins for comparison with recurring
# income. The estimator applies the remaining-spin and inventory adjustments.
ONE_SHOT = {
    "lucky_capsule": {"value": BASE["lucky_capsule"] / 4, "skip_value": BASE["lucky_capsule"],
                      "kind": "cash", "capsule": True},
    "chemical_seven": {"value": BASE["chemical_seven"] / 4, "skip_value": BASE["chemical_seven"],
                       "kind": "cash", "lucky_seven": True},
    "tedium_capsule": {"value": BASE["tedium_capsule"] / 4, "kind": "cash", "capsule": True},
    **{name: {"value": score, "kind": "utility", "capsule": True} for name, score in UTILITY.items()},
    "buffing_powder": {"value": 0.0, "kind": "adjacent", "capsule": True},
    **{name: {"value": BASE[name] / 4, "skip_value": BASE[name], "kind": "void"} for name in VOID},
}
ONE_SHOT_HORIZON = {
    "reference": 30.0, "cash_floor": 0.5, "utility_floor": 0.4, "cap": 1.0,
    "void_empty_bonus": 0.4, "adjacent_factor": 0.35, "adjacent_cap": 6.0,
    "capsule_machine": 2.0, "capsule_machine_essence": 3.0,
    "capsule_full_board_bonus": 1.0,
    "lucky_seven_bonus": {"0": 0.5, "1": 1.5, "2": 8.0},
    # While filling, a free slot still has long-term value.  With at least
    # three empty slots a one-shot is discounted below a persistent 2-value
    # symbol; with fewer slots the normal late comparison applies.
    "filling_scale": 0.5, "filling_min_empties": 3,
}

# One-time cash-out rules taken from the local item/essence descriptions.
# The driver reports generated hover coins as ordinary coins, so hover value is
# estimated from the generators rather than from the visible coin count.
SPECIAL_COMBOS = {
    "suits": ["clubs", "diamonds", "hearts", "spades"],
    "flush": {"item": "flush", "essence": "flush_essence",
              "payout_item": 12, "payout_essence": 20, "essence_cap": 5,
              "premium": 0.5},
    "hover": {"item": "coin_on_a_string", "essence": "coin_on_a_string_essence",
              "payout_item": 3, "payout_essence": 10, "essence_cap": 10,
              "generators": {"king_midas": 1.0, "hex_of_midas": 0.3}},
}

# The user's observed pick-value curve.  It is deliberately a pick value, not
# the symbol's printed per-spin income, and is evaluated at the current rent.
RENT_CURVES = {
    "diver": [[25, 30.0], [650, 4.5]],
    "archaeologist": [[25, 30.0], [650, 4.5]],
    "pirate": [[25, 30.0], [650, 4.5]],
    "dove": [[25, 30.0], [650, 4.5]],
    "mrs_fruit": [[25, 10.0], [450, 4.5], [650, 2.0]],
    "amethyst": [[25, 9.0], [450, 4.05], [650, 1.8]],
    "pear": [[25, 8.0], [450, 3.6], [650, 1.6]],
}
RENT_CURVE_FLOOR = {"diver": 4.5, "archaeologist": 4.5, "pirate": 4.5,
                    "dove": 4.5, "mrs_fruit": 2.0, "amethyst": 1.8, "pear": 1.6}

# Expected remaining spins by the rent that is currently due.  Fitted from the
# 100-game baseline record; used for permanent item/symbol income (pool ball,
# hustler) rather than pretending a one-time coin is permanent income.
REMAINING_SPINS = {"25": 52.0, "50": 47.0, "100": 43.0, "150": 39.0,
                   "225": 33.0, "300": 29.0, "375": 29.0, "450": 25.0,
                   "600": 22.0, "650": 20.0, "700": 20.0, "777": 18.0,
                   "1000": 12.0, "1500": 5.0}

# Curses are not all worth their printed three coins.  The user supplied the
# thievery net value and the general late-game policy: destruction/hoarding are
# acceptable early, but should be removed later; draining depends on the deck's
# average value.  Values below are pick scores, not printed coin values.
CURSE_VALUES = {
    "hex_of_thievery": {"kind": "thievery", "value": 1.2},
    "hex_of_destruction": {"kind": "destruction", "base": 2.5, "early_bonus": 0.04,
                            "early_rent": 150, "late_decay": 0.006},
    "hex_of_draining": {"kind": "draining", "base": 2.5, "early_bonus": 0.04,
                         "early_rent": 150, "late_decay": 0.004,
                         "average_penalty": 0.3, "average_free": 1.5},
    "hex_of_hoarding": {"kind": "hoarding", "base": 2.5, "early_bonus": 0.02,
                         "early_rent": 150, "late_decay": 0.005},
    "hex_of_tedium": {"kind": "flat", "base": 2.5, "late_decay": 0.0},
    "hex_of_emptiness": {"kind": "flat", "base": 2.5, "late_decay": 0.0},
}
NEGATIVE_CURSES = {"hex_of_destruction", "hex_of_draining", "hex_of_hoarding",
                   "hex_of_tedium", "hex_of_emptiness", "hex_of_thievery"}
CURSE_REMOVAL_RENT = {"hex_of_destruction": 300, "hex_of_hoarding": 300,
                      "hex_of_draining": 375, "hex_of_tedium": 375,
                      "hex_of_emptiness": 375}

# King Midas is primarily an engine for pirate (each hover coin feeds pirate
# for a permanent +1).  Coin multipliers make its generated hover coins much
# more valuable even without pirate.
KING_MIDAS = {"pirate_base": 12.0, "pirate_per_remaining": 0.4,
              "two_modifier_base": 8.0, "two_modifier_per_remaining": 0.15,
              "coin_modifiers": ["triple_coins", "triple_coins_essence", "rusty_gear"]}
HUSTLER = {"income_per_spin": 1.0, "upfront_cost": 7.0, "remaining_discount": 0.6}
WILDCARD = {"max_weight": 0.9, "floor": 8.0}
SPIRITBOX = {"symbols": ["urn", "big_urn", "tomb"], "grave_robber": "grave_robber",
             "grave_bonus": 4.0, "hooligan_bonus": 2.0}

# Indirect growth-food value.  The core does not have to eat the symbol itself:
# mine/golem generate ore, ore can become gems, and all of those can be eaten on
# later spins.  Values are pick scores when the core is already owned.  The user
# explicitly ranked archaeologist's food as mine > golem > big ore > ore > pearl.
GROWTH_ASSOCIATIONS = {
    "archaeologist": {"mine": 20.0, "golem": 16.0, "big_ore": 12.0, "ore": 10.0,
                       "pearl": 8.0, "sapphire": 7.0, "shiny_pebble": 6.0,
                       "oyster": 6.0},
    "pirate": {"mega_chest": 20.0, "treasure_chest": 16.0, "safe": 12.0,
               "lockbox": 10.0, "coin": 6.0, "beer": 5.0, "anchor": 5.0,
               "orange": 5.0},
    "diver": {"pearl": 10.0, "oyster": 9.0, "jellyfish": 8.0, "sand_dollar": 8.0,
              "pufferfish": 8.0, "goldfish": 6.0, "anchor": 5.0, "crab": 5.0,
              "snail": 5.0, "turtle": 5.0, "dog": 5.0},
    "dove": {"general_zaroff": 10.0, "matryoshka_doll_1": 8.0, "hooligan": 6.0},
    "mrs_fruit": {"coconut": 6.0, "peach": 5.0, "banana": 5.0, "orange": 5.0,
                  "cherry": 4.0, "coconut_half": 4.0},
    "eldritch_beast": {"frozen_fossil": 12.0, "witch": 6.0, "cultist": 5.0},
}
ITEM_PASSIVE = {"pool_ball": 1.0, "bowling_ball": 3.0, "four_leaf_clover": 4.0,
                "recycling": 1.0}
ITEM_ADDITIONS = {
    "nori_the_rabbit": {"types": ["rabbit", "rabbit_fluff"], "amount": 1},
    "pizza_the_cat": {"types": ["cat"], "amount": 1},
    "quigley_the_wolf": {"types": ["wolf"], "amount": 1},
    "fruit_basket": {"groups": ["fruit"], "amount": 1},
    "kyle_the_kernite": {"groups": ["kyle"], "amount": 1},
    "happy_hour": {"groups": ["booze"], "amount": 1},
    "birdhouse": {"groups": ["bird"], "amount": 1},
    "fish_bowl": {"types": ["goldfish"], "amount": 1},
    "anthropology_degree": {"groups": ["human"], "amount": 1},
}
ITEM_MULTIPLIERS = {
    "jackolantern": {"groups": ["halloween"], "multiplier": 2},
    "black_cat": {"types": ["cat"], "multiplier": 2},
    "dwarven_anvil": {"types": ["dwarf"], "multiplier": 2},
    "oswald_the_monkey": {"types": ["monkey"], "multiplier": 2},
    "maxwell_the_bear": {"types": ["bear"], "multiplier": 1.5},
    "golden_carrot": {"types": ["rabbit"], "multiplier": 5},
    "lucky_carrot": {"types": ["rabbit"], "multiplier": 3},
}


def matches(name, groups, rule):
    return bool(rule.get("all")) or name in rule.get("types", []) or bool(set(groups) & set(rule.get("groups", [])))


def build():
    raw = SOURCE.read_bytes()
    symbols = json.loads(raw)
    probabilities = {}
    factors = {}
    # Uniform arrangement in 4x5. A random cell has 3, 5 or 8 neighbors;
    # sample n other positions without replacement, then average over cells.
    for count in range(4):
        denominator = comb(19, count)
        probability = 0.0
        for neighbors, cells in ((3, 4), (5, 10), (8, 6)):
            probability += cells / 20 * (1 - comb(19 - neighbors, count) / denominator)
        probabilities[str(count)] = probability
        for mult in (2, 3, 5):
            expected = 0.0
            for neighbors, cells in ((3, 4), (5, 10), (8, 6)):
                for hits in range(count + 1):
                    if hits <= neighbors and count - hits <= 19 - neighbors:
                        expected += cells / 20 * comb(neighbors, hits) * comb(19-neighbors, count-hits) / denominator * mult ** hits
            factors[f"{mult}:{count}"] = expected
    # An arrow points along one of eight rays; the target need not be adjacent.
    ray_pairs = sum(1 for y in range(4) for x in range(5)
                    for yy in range(4) for xx in range(5)
                    if (x != xx or y != yy) and (x == xx or y == yy or abs(x-xx) == abs(y-yy)))
    arrow_probability = ray_pairs / (20 * 19 * 8)
    arrow_factors = {f"{directions}:{mult}:{n}": (1 + directions * arrow_probability * (mult - 1)) ** n
                     for directions in (1, 2) for mult in (2, 3, 4) for n in range(4)}
    pairs = {}
    catalog = {}
    descriptions = json.loads((ROOT / "game" / "game_data_structured.json").read_text(encoding="utf8"))
    descriptions = {x["id"]: x for x in descriptions["symbols"]}
    for name, entry in symbols.items():
        groups = entry.get("groups", [])
        raw_value = float(entry.get("value") or 0)
        base = BASE.get(name, UTILITY.get(name, raw_value))
        matched = []
        for eater, rule in FEEDERS.items():
            if not matches(name, groups, rule):
                continue
            matched.append(eater)
            eater_bonus = rule.get("bonus", 0) + rule.get("value_multiple", 0) * raw_value
            death_bonus = DEATH_BONUS.get(name, 0) * rule.get("death_multiplier", 1)
            extra = eater_bonus + death_bonus
            pairs[name + "/" + eater] = {"extra": extra, "eater_bonus": eater_bonus,
                "death_bonus": death_bonus, "growth": rule.get("growth", 0),
                "expected_extra": {str(n): extra * probabilities[str(n)] for n in range(1, 4)},
                "expected_eater": {str(n): eater_bonus * probabilities[str(n)] for n in range(1, 4)},
                "expected_death": {str(n): death_bonus * probabilities[str(n)] for n in range(1, 4)}}
        effects = [m for m, rule in MULTIPLIERS.items() if matches(name, groups, rule)]
        desc = descriptions.get(name, {})
        # A non-empty description is not presumed fully modeled just because
        # its face value is known; spawned symbols, transformations, etc. vary.
        internal = entry.get("rarity") is None
        if internal:
            status = "internal"
        elif name in ONE_SHOT:
            status = "one_shot"
        elif name in UTILITY:
            status = "utility_proxy"
        elif not desc.get("desc_zh"):
            status = "base_and_supported_synergies"
        elif (name in BASE or name in RENT_CURVES or name in CURSE_VALUES or
              name in {"hustler", "wildcard", "king_midas"} or matched or
              any(m != "buffing_powder" for m in effects) or
              name in MULTIPLIERS or name in FEEDERS or
              name in {"bronze_arrow", "silver_arrow", "golden_arrow", "crab", "ninja", "cultist", "diamond", "watermelon",
                       "coal", "ruby", "emerald", "strawberry", "highlander", "card_shark", "rabbit", "wine",
                       "matryoshka_doll_1", "cow", "gambler", "beehive", "bounty_hunter"}):
            status = "partially_modeled"
        else:
            status = "base_only"
        catalog[name] = {"name_zh": desc.get("name_zh", name), "rarity": entry.get("rarity"),
                         "groups": groups, "base": base, "raw_base": raw_value,
                         "status": status, "feeders": matched, "boosters": effects,
                         "description": desc.get("desc_zh", "")}
    result = {"schema": 1, "symbol_source_sha256": hashlib.sha256(raw).hexdigest(),
              "notes": "4x5 无放回相邻模型；食物的当回合单次收入并非永久收入；数量超过3按3估算。",
              "adjacency_probability": probabilities, "multiplier_factors": factors,
              "arrow_hit_probability": arrow_probability, "arrow_factors": arrow_factors,
              "feeders": FEEDERS, "multipliers": MULTIPLIERS, "pairs": pairs,
              "item_additions": ITEM_ADDITIONS, "item_multipliers": ITEM_MULTIPLIERS,
              "one_turn": ONE_TURN, "void": VOID, "timed": TIMED,
              "one_shot": ONE_SHOT, "one_shot_horizon": ONE_SHOT_HORIZON,
              "utility_proxies": UTILITY, "special_combos": SPECIAL_COMBOS,
              "rent_curves": RENT_CURVES, "rent_curve_floor": RENT_CURVE_FLOOR,
              "remaining_spins": REMAINING_SPINS, "curse_values": CURSE_VALUES,
              "negative_curses": sorted(NEGATIVE_CURSES),
              "curse_removal_rent": CURSE_REMOVAL_RENT,
              "king_midas": KING_MIDAS, "hustler": HUSTLER, "wildcard": WILDCARD,
              "spiritbox": SPIRITBOX, "item_passive": ITEM_PASSIVE,
              "growth_associations": GROWTH_ASSOCIATIONS,
              "symbols": catalog}
    target = ROOT / "data" / "symbol_values.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    print(f"Wrote {len(catalog)} symbol entries and {len(pairs)} food/feeder tables to {target}")


if __name__ == "__main__":
    build()
