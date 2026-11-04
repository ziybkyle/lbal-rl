"""Fast, transparent approximations for a full-board symbol choice."""
from collections import Counter
import json
from pathlib import Path


class SymbolValues:
    def __init__(self, path: Path):
        self.data = json.loads(path.read_text(encoding="utf-8"))
        self.symbols = self.data["symbols"]
        self.pairs = self.data["pairs"]
        self.feeders = self.data["feeders"]
        self.boosters = self.data["multipliers"]
        self.combos = self.data["special_combos"]
        self.p = self.data["adjacency_probability"]
        self.rent_curves = self.data.get("rent_curves", {})
        self.rent_curve_floor = self.data.get("rent_curve_floor", {})
        self.remaining_spins_table = {int(k): v for k, v in self.data.get("remaining_spins", {}).items()}
        self.curse_values = self.data.get("curse_values", {})
        self.negative_curses = set(self.data.get("negative_curses", []))
        self.king_midas_rules = self.data.get("king_midas", {})
        self.hustler_rules = self.data.get("hustler", {})
        self.wildcard_rules = self.data.get("wildcard", {})
        self.spiritbox_rules = self.data.get("spiritbox", {})
        self.growth_associations = self.data.get("growth_associations", {})
        self.item_passive = self.data.get("item_passive", {})
        self.one_shot = self.data.get("one_shot", {})
        self.one_shot_horizon = self.data.get("one_shot_horizon", {})
        self.chest_death = {"lockbox": 15, "safe": 30, "treasure_chest": 50, "mega_chest": 100}

    def matches(self, name, rule):
        groups = self.symbols.get(name, {}).get("groups", [])
        return bool(rule.get("all")) or name in rule.get("types", []) or any(g in groups for g in rule.get("groups", []))

    def prob(self, count):
        return self.p[str(max(0, min(int(count), 3)))]

    def rent_value(self, name, rent):
        points = self.rent_curves.get(name)
        if not points:
            return None
        rent = float(rent or 0)
        if rent <= points[0][0]:
            value = points[0][1]
        elif rent >= points[-1][0]:
            value = points[-1][1]
        else:
            value = points[-1][1]
            for (left_rent, left_value), (right_rent, right_value) in zip(points, points[1:]):
                if left_rent <= rent <= right_rent:
                    share = (rent - left_rent) / (right_rent - left_rent)
                    value = left_value + (right_value - left_value) * share
                    break
        return max(self.rent_curve_floor.get(name, 0.0), value)

    def remaining_spins(self, rent):
        if not self.remaining_spins_table:
            return 20.0
        rent = int(rent or 0)
        keys = sorted(self.remaining_spins_table)
        if rent <= keys[0]:
            return float(self.remaining_spins_table[keys[0]])
        for key in reversed(keys):
            if rent >= key:
                return float(self.remaining_spins_table[key])
        return float(self.remaining_spins_table[keys[-1]])

    def one_shot_horizon_for(self, kind, rent):
        rule = self.one_shot_horizon
        remaining = self.remaining_spins(rent)
        reference = max(1.0, float(rule.get("reference", 30.0)))
        floor_key = "utility_floor" if kind == "utility" else "cash_floor"
        floor = float(rule.get(floor_key, 0.5))
        return min(float(rule.get("cap", 1.0)), max(floor, remaining / reference))

    def capsule_multiplier(self, items):
        rule = self.one_shot_horizon
        # The essence and item are alternative upgrades of the same effect;
        # taking the maximum avoids double-counting an uncertain stacking rule.
        return max(1.0, float(rule.get("capsule_machine", 2.0)) if items.get("capsule_machine") else 1.0,
                   float(rule.get("capsule_machine_essence", 3.0))
                   if items.get("capsule_machine_essence") else 1.0)

    def adjacent_one_turn_value(self, frame, client, deck, items):
        rule = self.one_shot_horizon
        board = list(getattr(frame, "board", ()) or ())
        names = []
        values = []
        for type_id in board:
            name = client.type_name(type_id)
            if name in ("empty", "missing", None):
                continue
            names.append(name)
            values.append(max(0.0, self.base(name, items)))
        deck_len = int(frame.info.get("deck_len", sum(deck.values())) or 0)
        if not values:
            names = [name for name, count in deck.items() if name != "empty" for _ in range(count)]
            values = [max(0.0, self.base(name, items)) for name in names]
        average = sum(values) / len(values) if values else 0.0
        occupied = len(values)
        neighbors = getattr(frame, "board_neighbors", ()) or ()
        if board and neighbors and occupied < len(board):
            empty_indices = [index for index, name in enumerate(board)
                             if client.type_name(name) in ("empty", "missing", None)]
            counts = [sum(1 for other in neighbors[index] if other < len(board) and
                          client.type_name(board[other]) not in ("empty", "missing", None))
                      for index in empty_indices if index < len(neighbors)]
            expected = sum(counts) / len(counts) if counts else 0.25 * occupied
        elif board and neighbors and occupied:
            counts = [len(neighbor) for neighbor in neighbors[:len(board)]]
            expected = sum(counts) / len(counts) if counts else 5.0
        else:
            expected = 0.25 * min(20, deck_len or occupied)
        return min(float(rule.get("adjacent_cap", 6.0)),
                   float(rule.get("adjacent_factor", 0.35)) * expected * average)

    def one_shot_value(self, name, frame, client, deck, items):
        rule = self.one_shot.get(name)
        if not rule:
            return None
        rent = frame.info.get("rent_due", 1)
        deck_len = int(frame.info.get("deck_len", sum(deck.values())) or 0)
        kind = rule.get("kind", "cash")
        horizon = self.one_shot_horizon_for(kind, rent)
        multiplier = self.capsule_multiplier(items) if rule.get("capsule") else 1.0
        if kind == "adjacent":
            value = self.adjacent_one_turn_value(frame, client, deck, items)
        else:
            value = float(rule.get("value", 0.0))
        if kind in ("cash", "void"):
            value *= self.expected_boost(name, deck, items)
        value *= multiplier
        if kind == "void":
            if deck_len < 20:
                value += float(self.one_shot_horizon.get("void_empty_bonus", 0.4))
        if rule.get("lucky_seven"):
            count = int(items.get("lucky_seven", 0))
            bonus = self.one_shot_horizon.get("lucky_seven_bonus", {})
            value += float(bonus.get(str(min(2, count)), 0.0))
        value *= horizon
        if rule.get("capsule") and deck_len >= 20:
            value += float(self.one_shot_horizon.get("capsule_full_board_bonus", 0.0))
        return value

    def one_shot_skip_value(self, name):
        return float(self.one_shot.get(name, {}).get("skip_value", 0.0))

    def filling_one_shot_score(self, name, frame, client, deck, items):
        value = self.one_shot_value(name, frame, client, deck, items)
        if value is None:
            return None
        deck_len = int(frame.info.get("deck_len", sum(deck.values())) or 0)
        empties = max(0, 20 - deck_len)
        if empties >= int(self.one_shot_horizon.get("filling_min_empties", 3)):
            value *= float(self.one_shot_horizon.get("filling_scale", 0.54))
        return value

    def current_value(self, name, features, items, deck):
        return self.base(name, items, features) * self.expected_boost(name, deck, items)

    def deck_average_value(self, frame, client, deck, items):
        values = []
        for tid, features, present in zip(frame.deck_types, frame.deck_features, frame.deck_mask):
            if not present:
                continue
            other = client.type_name(tid)
            values.append(max(0.0, self.current_value(other, features, items, deck)))
        return sum(values) / len(values) if values else 0.0

    def wildcard_value(self, frame, client, deck, items):
        values = []
        for tid, features, present in zip(frame.deck_types, frame.deck_features, frame.deck_mask):
            if present:
                other = client.type_name(tid)
                values.append(max(0.0, self.current_value(other, features, items, deck)))
        if not values:
            return float(self.wildcard_rules.get("floor", 8.0))
        values.sort(reverse=True)
        top = values[:3]
        expected = self.wildcard_rules.get("max_weight", 0.9) * top[0]
        if len(top) > 1:
            expected += (1 - self.wildcard_rules.get("max_weight", 0.9)) * sum(top[1:]) / len(top[1:])
        return max(self.wildcard_rules.get("floor", 8.0), expected)

    def curse_score(self, name, frame, deck, items, client):
        rule = self.curse_values.get(name)
        if not rule:
            return None
        rent = float(frame.info.get("rent_due", 1) or 1)
        kind = rule.get("kind")
        if kind == "thievery":
            return float(rule.get("value", 1.2))
        base = float(rule.get("base", 2.5))
        early_rent = float(rule.get("early_rent", 150))
        value = base + max(0.0, early_rent - rent) * float(rule.get("early_bonus", 0.0))
        value -= max(0.0, rent - early_rent) * float(rule.get("late_decay", 0.0))
        if kind == "draining":
            average = self.deck_average_value(frame, client, deck, items)
            value -= float(rule.get("average_penalty", 0.3)) * max(
                0.0, average - float(rule.get("average_free", 1.5)))
        return value

    def spiritbox_bonus(self, name, deck, items):
        rule = self.spiritbox_rules
        if name not in rule.get("symbols", []):
            return 0.0
        bonus = 0.0
        if items.get(rule.get("grave_robber", "grave_robber"), 0):
            bonus += float(rule.get("grave_bonus", 4.0))
        if deck.get("hooligan", 0):
            bonus += float(rule.get("hooligan_bonus", 2.0))
        return bonus

    def growth_food_bonus(self, name, deck):
        bonus = 0.0
        for core, foods in self.growth_associations.items():
            if deck.get(core, 0) and name in foods:
                bonus = max(bonus, foods[name])
        return bonus

    def key_value(self, name, deck):
        chests = [count for chest, count in deck.items() if chest in self.chest_death and count]
        if not chests:
            return 0.0
        total = sum(count for chest, count in deck.items()
                    if chest in self.chest_death for _ in range(count))
        average = sum(self.chest_death[chest] * count for chest, count in deck.items()
                      if chest in self.chest_death) / total
        if name == "key":
            return 5.0 + 0.5 * average
        return 0.0

    def item_effect_value(self, name, deck, items, frame=None, client=None):
        rent = frame.info.get("rent_due", 1) if frame is not None else 375
        remaining = self.remaining_spins(rent) * 0.6
        value = self.item_passive.get(name, 0.0) * remaining
        if name in self.data["item_additions"]:
            rule = self.data["item_additions"][name]
            value += sum(count for other, count in deck.items()
                         if self.matches(other, rule)) * rule["amount"]
        if name in self.data["item_multipliers"]:
            rule = self.data["item_multipliers"][name]
            if frame is not None and client is not None:
                for tid, features, present in zip(frame.deck_types, frame.deck_features, frame.deck_mask):
                    if present and self.matches(client.type_name(tid), rule):
                        value += (rule["multiplier"] - 1) * max(
                            0.0, self.current_value(client.type_name(tid), features, items, deck))
            else:
                value += (rule["multiplier"] - 1) * sum(
                    count for other, count in deck.items() if self.matches(other, rule))
        if name == self.spiritbox_rules.get("grave_robber", "grave_robber"):
            boxes = sum(deck.get(box, 0) for box in self.spiritbox_rules.get("symbols", []))
            if boxes:
                value += 6.0 + 4.0 * boxes
        if name == "copycat" and deck.get("cat", 0):
            value += 4.0 * deck["cat"]
        if name == "ancient_lizard_blade":
            duplicates = sum(1 for other, count in deck.items() if count >= 2 and other != "empty")
            value += max(0.0, 9.0 - 2.0 * duplicates) * remaining
        return value

    def item_adjusted(self, name, value, items):
        # Game effects generally add bonuses before multiplying.
        value += self.item_addition(name, items)
        return value * self.item_multiplier(name, items)

    def item_addition(self, name, items):
        value = 0.0
        for item, rule in self.data["item_additions"].items():
            if items.get(item, 0) and self.matches(name, rule):
                value += rule["amount"] * items[item]
        return value

    def item_multiplier(self, name, items):
        factor = 1.0
        for item, rule in self.data["item_multipliers"].items():
            if items.get(item, 0) and self.matches(name, rule):
                factor *= rule["multiplier"] ** items[item]
        return factor

    def expected_boost(self, name, deck, items=None):
        factor = 1.0
        for booster, rule in self.boosters.items():
            if booster == "buffing_powder":
                continue  # lasts one spin; handled by one_shot_value
            n = min(deck.get(booster, 0), 3)
            if n and self.matches(name, rule):
                factor *= self.data["multiplier_factors"][f"{rule['multiplier']}:{n}"]
        # Keep the user's explicitly requested Robin Hood + arrow example as
        # base + adjacent +3; otherwise approximate arrows' eight random rays.
        if name != "robin_hood":
            directions = 2 if items and (items.get("quiver", 0) or items.get("quiver_essence", 0)) else 1
            for arrow, mult in (("bronze_arrow", 2), ("silver_arrow", 3), ("golden_arrow", 4)):
                n = min(deck.get(arrow, 0), 3)
                if n:
                    factor *= self.data["arrow_factors"][f"{directions}:{mult}:{n}"]
        return factor

    def base(self, name, items, current=None):
        entry = self.symbols.get(name, {})
        value = entry.get("base", 0)
        if current is not None:
            # Preserve fixed expected values such as d3/Robin Hood but include
            # actual accumulated permanent bonus and multiplier on the instance.
            value += current[0] - entry.get("raw_base", current[0]) + current[1]
            return (value + self.item_addition(name, items)) * current[2] * self.item_multiplier(name, items)
        return self.item_adjusted(name, value, items)

    def growth_food(self, name, deck):
        return any(rule.get("growth") and deck.get(eater, 0) and name + "/" + eater in self.pairs
                   for eater, rule in self.feeders.items())

    def slots(self, name, deck, physical_count, items=None):
        items = items or {}
        rule = self.combos["flush"]
        if (name in self.combos["suits"] and
                (items.get(rule["item"], 0) or items.get(rule["essence"], 0))):
            return 0.0  # paid out and removed before the spin, so no slot is used
        if name in self.data["one_turn"] or (name in self.data["void"] and physical_count >= 20):
            return 0.2
        if name in self.data["timed"]:
            return 0.5
        if name in ("key", "magic_key") and any(self.matches(other, {"groups": ["chest"]}) for other in deck):
            return 0.5
        if self.matches(name, {"groups": ["chest"]}) and (items.get("lockpick", 0) or items.get("lockpick_essence", 0)):
            return 0.5
        if any(deck.get(eater, 0) and name + "/" + eater in self.pairs for eater in self.feeders):
            return 0.5
        return 1.0

    def suit_payout(self, deck, items):
        rule = self.combos["flush"]
        suits = sum(deck.get(name, 0) for name in self.combos["suits"])
        payout, sources = 0.0, []
        if items.get(rule["essence"], 0) and suits < rule["essence_cap"]:
            payout += rule["payout_essence"]
            sources.append("同花精华")
        if items.get(rule["item"], 0):
            payout += rule["payout_item"]
            sources.append("同花")
        return payout, "+".join(sources)

    def hover_payout(self, items):
        rule = self.combos["hover"]
        payout = 0.0
        if items.get(rule["item"], 0):
            payout += rule["payout_item"]
        if items.get(rule["essence"], 0):
            payout += rule["payout_essence"]
        return payout

    def item_combo(self, name, deck, items, frame=None, client=None):
        flush = self.combos["flush"]
        hover = self.combos["hover"]
        suits = sum(deck.get(s, 0) for s in self.combos["suits"])
        if name == flush["item"]:
            return flush["payout_item"] * suits + flush["premium"]
        if name == flush["essence"]:
            return flush["payout_essence"] * min(flush["essence_cap"], suits) + flush["premium"]
        rate = sum(deck.get(generator, 0) * weight for generator, weight in hover["generators"].items())
        if name == hover["item"]:
            return 10 * (1 + hover["payout_item"] * rate)
        if name == hover["essence"]:
            return hover["payout_essence"] * min(hover["essence_cap"], rate * hover["essence_cap"])
        return self.item_effect_value(name, deck, items, frame, client)

    def effective_count(self, deck, physical_count, items=None):
        return sum(count * self.slots(name, deck, physical_count, items) for name, count in deck.items())

    def _result(self, name, value, frame, deck, items, empty_compensation, notes, growth=False, status=None):
        physical = frame.info.get("deck_len", sum(deck.values())) + 1
        prospective = deck.copy()
        prospective[name] += 1
        effective = sum(count * self.slots(other, prospective, physical, items)
                        for other, count in deck.items())
        slots = self.slots(name, deck, physical, items)
        penalty = 0.25 * max(0.0, effective + slots - 20)
        empty_bonus = 0.0
        if empty_compensation and value > 0 and name not in self.negative_curses:
            empty_bonus = 0.25 * max(0.0, 20 - (effective + slots))
        return {"score": value - penalty + empty_bonus, "gross": value, "penalty": penalty,
                "empty_bonus": empty_bonus, "slots": slots, "effective": effective,
                "growth": growth, "notes": notes,
                "status": status or self.symbols.get(name, {}).get("status", "unknown")}

    def estimate(self, name, frame, client, deck, items, empty_compensation=False):
        info = self.symbols.get(name)
        if info is None:
            return {"score": 0, "gross": 0, "penalty": 0, "empty_bonus": 0, "slots": 1,
                    "effective": 0, "growth": False, "notes": ["未知符号"], "status": "unknown"}
        physical = frame.info.get("deck_len", sum(deck.values())) + 1
        payout, source = self.suit_payout(deck, items)
        if name in self.combos["suits"] and payout:
            effective = sum(count * self.slots(other, deck, physical, items)
                            for other, count in deck.items())
            slots = 0.0
            penalty = 0.25 * max(0.0, effective + slots - 20)
            empty_bonus = 0.25 * max(0.0, 20 - (effective + slots)) if empty_compensation else 0.0
            value = payout + self.combos["flush"]["premium"]
            notes = [f"{source}会一次性收走该花色，按 {payout:.0f} 金币计",
                     "收走时不占用出场格"]
            return {"score": value - penalty + empty_bonus, "gross": value, "penalty": penalty,
                    "empty_bonus": empty_bonus, "slots": slots, "effective": effective,
                    "growth": False, "notes": notes, "status": "special_combo"}
        rent = frame.info.get("rent_due", 1)
        remaining = self.remaining_spins(rent)
        notes = []

        if name in self.curse_values:
            value = self.curse_score(name, frame, deck, items, client)
            notes.append(f"诅咒按净期望估值，当前租金 {rent:g} 时为 {value:.2f}")
            return self._result(name, value, frame, deck, items, empty_compensation,
                                notes, status="curse")

        if name == "wildcard":
            value = self.wildcard_value(frame, client, deck, items)
            notes.append("按相邻符号最高当前价值的 90% 估算，并设 8 分下限")
            return self._result(name, value, frame, deck, items, empty_compensation,
                                notes, status="partially_modeled")

        if name == "hustler":
            rule = self.hustler_rules
            value = max(0.0, float(rule.get("income_per_spin", 1.0)) *
                        self.capsule_multiplier(items) * remaining *
                        float(rule.get("remaining_discount", 0.6)) -
                        float(rule.get("upfront_cost", 7.0)))
            notes.append(f"台球每回合 +{rule.get('income_per_spin', 1):.0f}，"
                         f"扣除 {rule.get('upfront_cost', 7):.0f} 金币后按剩余回合折现")
            if self.capsule_multiplier(items) > 1:
                notes.append(f"扭蛋机/扭蛋机精华使台球数量约 ×{self.capsule_multiplier(items):g}")
            return self._result(name, value, frame, deck, items, empty_compensation,
                                notes, status="partially_modeled")

        if name == "king_midas":
            rule = self.king_midas_rules
            value = 1.0 + self.hover_payout(items)
            pirate = deck.get("pirate", 0)
            modifiers = sum(1 for mod in rule.get("coin_modifiers", []) if items.get(mod, 0))
            if pirate:
                value = max(value, float(rule.get("pirate_base", 12.0)) +
                            float(rule.get("pirate_per_remaining", 0.4)) * remaining)
                notes.append("有海盗：悬浮金币持续喂养海盗，按最高优先级估值")
            elif modifiers >= 2:
                value = max(value, float(rule.get("two_modifier_base", 8.0)) +
                            float(rule.get("two_modifier_per_remaining", 0.15)) * remaining)
                notes.append("两种金币倍率同时存在，按高收益组合估值")
            elif modifiers == 1:
                value = max(value, value + 1.0)
                notes.append("单种金币倍率只给少量加分")
            if self.hover_payout(items):
                notes.append(f"系绳硬币/精华每次收走约加 {self.hover_payout(items):.0f}")
            return self._result(name, value, frame, deck, items, empty_compensation,
                                notes, status="partially_modeled")

        if name == "key":
            value = self.key_value(name, deck)
            notes.append("场上有箱子时钥匙价值很高" if value else "满格且无箱子，不拿普通钥匙")
            return self._result(name, value, frame, deck, items, empty_compensation,
                                notes, status="partially_modeled")

        if name == "magic_key":
            value = 3.0
            notes.append("魔法钥匙按用户给定值 3 估分")
            return self._result(name, value, frame, deck, items, empty_compensation,
                                notes, status="partially_modeled")

        if name == "midas_bomb":
            physical = frame.info.get("deck_len", sum(deck.values()))
            low_quality = 0
            for tid, features, present in zip(frame.deck_types, frame.deck_features, frame.deck_mask):
                if not present:
                    continue
                other = client.type_name(tid)
                if self.current_value(other, features, items, deck) <= 1.5:
                    low_quality += 1
            if physical >= 24 and low_quality >= 6:
                value = 8.0 + 0.3 * low_quality
                notes.append("牌组明显超过 20 且低质量符号很多，迈达斯炸弹作为清洁手段")
            else:
                value = 0.5
                notes.append("牌组没有明显溢出，默认不抓迈达斯炸弹")
            return self._result(name, value, frame, deck, items, empty_compensation,
                                notes, status="partially_modeled")

        if name in self.one_shot:
            rule = self.one_shot[name]
            kind = rule.get("kind", "cash")
            value = self.one_shot_value(name, frame, client, deck, items)
            horizon = self.one_shot_horizon_for(kind, rent)
            notes.append(f"一次性符号：按一次性收益折算，剩余约 {remaining:.0f} 回合，"
                         f"系数 {horizon:.2f}")
            if kind == "adjacent":
                notes.append("增益扭蛋只放大相邻符号一回合；按当前场面周围符号的平均价值估算")
            elif kind == "void":
                notes.append("虚空符号按消除时的 8 金币估值；空格相邻加成只计少量")
            elif rule.get("lucky_seven"):
                notes.append("七号化学物附赠幸运七物品，按已有幸运七数量加成分")
            multiplier = self.capsule_multiplier(items)
            if multiplier > 1:
                notes.append(f"扭蛋机/扭蛋机精华使扭蛋效果约 ×{multiplier:g}")
            if name == "tedium_capsule":
                notes.append("已扣除把后续符号降为普通品质的负面影响")
            return self._result(name, value, frame, deck, items, empty_compensation,
                                notes, status="one_shot")

        curve = self.rent_value(name, rent)
        curve_growth = curve is not None
        if curve_growth:
            base = (curve + self.item_addition(name, items)) * self.item_multiplier(name, items)
        else:
            base = self.base(name, items)
        if name in self.data["void"] and physical < 20:
            base = 0  # not guaranteed to self-destruct with empty cells
        value = base * self.expected_boost(name, deck, items) if base > 0 else base
        if name in self.data["utility_proxies"]:
            notes.append("功能性代币等价分，非金币")
        if name in self.data["void"] and physical < 20:
            notes.append("未满格，虚空符号不保证消除")
        if curve_growth:
            notes.append(f"租金 {rent:g} 时成长符号按 {curve:.2f} 估分")

        if name in self.chest_death and (deck.get("key") or deck.get("magic_key")):
            if deck.get("key"):
                chest_bonus = 5.0 + 0.5 * self.chest_death[name]
            else:
                chest_bonus = 3.0 + 0.3 * self.chest_death[name]
            value = max(value, chest_bonus)
            notes.append("场上有钥匙，箱子会被优先消除换钱并腾出格子")

        if name == "bounty_hunter" and items.get("zaroffs_contract"):
            humans = sum(count for other, count in deck.items()
                         if "human" in self.symbols.get(other, {}).get("groups", []))
            value = max(value, 3.0 + 25.0 * self.prob(humans))
            notes.append("扎罗夫合约：赏金按将军的人类喂食估值")
        if name == "coal":
            # Coal has no immediate income; its pick value is the future diamond,
            # and the guide's timing says it must be taken early.
            future = 4.0 if rent <= 150 else 3.0 if rent <= 300 else 2.0 if rent <= 450 else 1.0
            value = max(value, future)
            notes.append(f"煤炭按未来钻石的 {future:.1f} 分估值，越早越强")
        if name == "matryoshka_doll_1":
            value = max(2.0, min(4.0, 1.0 + remaining / 20.0))
            notes.append(f"套娃按剩余 {remaining:.0f} 回合的平均成长估分")
        if name == "cow" and deck.get("cat"):
            value = max(value, 4.0)
            notes.append("有猫时奶牛按约 4 分估值")
        if name == "gambler":
            value = max(value, 3.0 if (deck.get("d3") or deck.get("d5") or
                                       deck.get("midas_bomb")) else 2.5)
            notes.append("赌徒按隐性白板估值，有骰子/炸弹时更高")
        if name == "golem" and (deck.get("miner") or deck.get("dwarf") or
                                items.get("mining_pick") or items.get("borax_kyle") or
                                items.get("borax_kyle_essence")):
            value = max(value, 3.0)
            notes.append("有矿工/矮人/矿镐/硼砂凯尔，石巨人的矿石收益变得可用")
        if name == "beehive" and deck.get("bear"):
            value = max(value, 7.0)
            notes.append("有熊时蜂巢按攻略的 7 分等价估值")
        if name == "mine" and not deck.get("archaeologist"):
            if items.get("mining_pick"):
                value += 2.0
                notes.append("有矿镐，矿井的矿石销毁收益提高")
            if deck.get("golem"):
                value += 1.0
                notes.append("有石巨人，矿井多一层矿石来源")
            notes.append("没有地质学家时矿井按低值裸抓")
        if name == "chicken":
            chicken_synergy = (items.get("chicken_coop") or items.get("egg_carton") or
                               deck.get("egg") or deck.get("golden_egg") or deck.get("chick"))
            if chicken_synergy:
                value = max(value, 4.0)
                notes.append("有鸡舍/蛋盒/蛋类配合，大鸡价值提高")
            else:
                value = min(value, 1.2)
                notes.append("没有配合，大鸡不建议裸抓")
        if name == "bartender":
            if deck.get("dwarf"):
                value += 4.0
                notes.append("有矮人消费酒，酒保价值提高")
            elif rent >= 225:
                value = min(value, 1.8)
                notes.append("225 租金后没有矮人，酒保产出的酒会挤占格子")
        if name == "dwarf" and deck.get("bartender"):
            value += 5.0
            notes.append("有酒保提供酒，矮人优先级很高")

        # A food can be consumed once. With multiple eater types use the best
        # estimated route, avoiding duplicate payout for the same food.
        food_extra = 0.0
        for eater, rule in self.feeders.items():
            count = deck.get(eater, 0)
            pair = self.pairs.get(name + "/" + eater)
            if not count or not pair:
                continue
            n = min(count, 3)
            extra = pair["expected_eater"][str(n)]
            if rule.get("value_multiple"):
                extra += self.prob(n) * (base - info["raw_base"]) * rule["value_multiple"]
            eater_mult = self.item_multiplier(eater, items)
            food_extra = max(food_extra, max(0.0, extra * eater_mult +
                                            pair["expected_death"][str(n)] * self.item_multiplier(name, items)))
        value += food_extra
        growth = curve_growth or self.growth_food(name, deck)

        # Adding an eater can exploit existing food. Count only the incremental
        # chance relative to already-owned eaters of that same type.
        if name in self.feeders:
            old = deck.get(name, 0)
            delta = self.prob(old + 1) - self.prob(old)
            eater_factor = self.item_multiplier(name, items)
            matched = 0
            for food, count in deck.items():
                pair = self.pairs.get(food + "/" + name)
                if pair and count:
                    value += delta * (pair["eater_bonus"] * eater_factor +
                                      pair["death_bonus"] * self.item_multiplier(food, items)) * count
                    matched += count
            if matched and self.feeders[name].get("growth"):
                growth = True

        # The candidate may also multiply existing symbols. Instance bonuses
        # come from the observation, so grown rabbits and gems matter.
        if name in self.boosters and name != "buffing_powder":
            rule = self.boosters[name]
            for tid, features, present in zip(frame.deck_types, frame.deck_features, frame.deck_mask):
                if not present:
                    continue
                other = client.type_name(tid)
                if self.matches(other, rule):
                    existing = self.base(other, items, features) * self.expected_boost(other, deck, items)
                    value += self.prob(1) * (rule["multiplier"] - 1) * existing
        if name in ("bronze_arrow", "silver_arrow", "golden_arrow"):
            mult = {"bronze_arrow": 2, "silver_arrow": 3, "golden_arrow": 4}[name]
            directions = 2 if items.get("quiver", 0) or items.get("quiver_essence", 0) else 1
            ray = directions * self.data["arrow_hit_probability"]
            for tid, features, present in zip(frame.deck_types, frame.deck_features, frame.deck_mask):
                if present:
                    other = client.type_name(tid)
                    existing = self.base(other, items, features) * self.expected_boost(other, deck, items)
                    value += ray * (mult - 1) * max(0, existing)
                    if other == "target":
                        value += ray * 10  # target's one-off destruction payment
            notes.append("箭头射线增益为简化估计")

        # Robin Hood adds +3 to adjacent arrows. Separate from arrow's own
        # difficult random-direction value, which remains an approximation.
        if name == "robin_hood":
            value += 3 * self.prob(1) * sum(deck.get(x, 0) for x in ("bronze_arrow", "silver_arrow", "golden_arrow"))
        elif name in ("bronze_arrow", "silver_arrow", "golden_arrow"):
            value += 3 * self.prob(1) * deck.get("robin_hood", 0)

        # Dog and omelette trigger at most once per spin. Bee's own +1 is
        # per adjacent matching symbol, separate from its doubling effect.
        for main, group, amount in (("dog", "doglikes", 2), ("omelette", "omelettestuff", 2)):
            targets = sum(n for target, n in deck.items() if group in self.symbols.get(target, {}).get("groups", []))
            if name == main:
                value += amount * self.prob(targets)
            elif group in info["groups"]:
                value += deck.get(main, 0) * amount * (self.prob(targets + 1) - self.prob(targets))
        if name == "bee":
            value += self.prob(1) * sum(n for target, n in deck.items() if "beelikes" in self.symbols.get(target, {}).get("groups", []))
        elif "beelikes" in info["groups"]:
            value += self.prob(1) * deck.get("bee", 0)

        # Simple symmetric effects from other copies; account for the new copy
        # and the extra income it confers on existing copies.
        same = deck.get(name, 0)
        if name in ("diamond", "watermelon", "ruby", "emerald", "strawberry"):
            value += 2 * same
        elif name == "cultist":
            value += 2 * same + (3 if same == 2 else 1 if same >= 3 else 0)
        elif name == "ninja" and not items.get("cursed_katana", 0):
            value -= 2 * same
        if name == "ninja":
            value += 1.5 * min(3, deck.get("mouse", 0))
        elif name == "mouse":
            value += 1.5 * min(2, deck.get("ninja", 0))
        elif name == "crab":
            value += 6 * (4 / 19) * same

        hover = self.hover_payout(items)
        generator_rate = self.combos["hover"]["generators"].get(name, 0.0)
        if hover and generator_rate:
            value += hover * generator_rate
            notes.append(f"系绳硬币收走该符号生成的硬币，每回合约加 {hover * generator_rate:.2f}")

        if name == "golden_arrow":
            value = max(value, 12.0)
            notes.append("金箭头按 4.125X 的攻略公式估值，设置 12 分下限")
        spiritbox = self.spiritbox_bonus(name, deck, items)
        if spiritbox:
            value += spiritbox
            notes.append(f"流氓/盗墓人使该幽魂箱额外 +{spiritbox:.2f}")
        growth_association = self.growth_food_bonus(name, deck)
        if growth_association:
            value = max(value, growth_association)
            notes.append(f"成长核心间接关联，按 {growth_association:.2f} 估分")
        if growth and not curve_growth:
            value = max(value, 4.5)
            notes.append("永久成长食物/核心按至少4.5估分")
        if food_extra:
            notes.append(f"单次喂食贡献约 {food_extra:.2f}")
        return self._result(name, value, frame, deck, items, empty_compensation,
                            notes, growth=growth, status=info["status"])
