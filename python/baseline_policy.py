"""Small, transparent heuristic for floor 20; it is not a trained model."""
from collections import Counter
import json
from pathlib import Path
import random

from v3_client import Candidate, Frame, V3Client
from symbol_value import SymbolValues


class BaselinePolicy:
    VERSION = "heuristic-marginal-value-v14"
    INITIAL_REMOVAL_ORDER = ("cherry", "flower", "banana_peel", "coin", "cat", "pearl")
    REMOVAL_PROTECTED_EARLY = {"coal", "gambler"}
    REMOVAL_UNREMOVABLE = {"dud", "empty", "missing", "hover_coin"}
    REMOVAL_CORE_TARGETS = {
        "sun": {"flower"},
        "moon": {"rabbit", "wolf", "owl"},
        "comedian": {"dog", "banana", "banana_peel", "monkey", "toddler", "joker"},
        "farmer": {"banana", "peach", "orange", "cherry", "coconut", "coconut_half",
                   "watermelon", "strawberry", "apple", "cheese", "egg", "golden_egg",
                   "milk", "seed"},
        "chef": {"cheese", "milk", "wine", "beer", "martini", "candy", "omelette",
                 "honey", "chemical_seven"},
        "dame": {"diamond", "ruby", "emerald", "sapphire", "amethyst", "pearl",
                 "shiny_pebble", "watermelon"},
        "light_bulb": {"diamond", "ruby", "emerald", "sapphire", "amethyst", "pearl",
                       "shiny_pebble"},
        "witch": {"owl", "crow", "cat", "apple"},
        "king_midas": {"coin"},
    }
    CHESTS = {"lockbox", "safe", "treasure_chest", "mega_chest"}
    CURSE_REMOVAL_ORDER = ("hex_of_destruction", "hex_of_hoarding", "hex_of_draining",
                           "hex_of_tedium", "hex_of_emptiness")

    def __init__(self, client: V3Client, seed: int):
        self.client = client
        self.rng = random.Random(seed)
        path = client.game / "JSON" / "Symbols - JSON.json"
        self.symbols = json.loads(path.read_text(encoding="utf-8"))
        path = Path(__file__).resolve().parent.parent / "data" / "association_rules.json"
        rules = json.loads(path.read_text(encoding="utf-8"))
        self.priors = json.loads((path.parent / "baseline_priors.json").read_text(encoding="utf-8"))
        self.values = SymbolValues(path.parent / "symbol_values.json")
        self.pairs = {tuple(p) for p in rules["pairs"]}
        self.red = {tuple(p) for p in rules["red_pairs"]}
        self.gray = {tuple(p) for p in rules["gray_pairs"]}
        self.self_positive = set(rules["self_positive"])
        self.trigger_items = set(rules["special_trigger_items"])
        self.persistent_essences = set(rules["still_owned_even_if_destroyed"])
        self.categories = {key: value["category"] for key, value in rules["names"].items()}
        self.filling_order = sorted(
            (name for name, card in self.symbols.items() if card.get("rarity") == "common"),
            key=lambda name: (-self._filling_base_score(name), name),
        )
        self.filling_position = {name: index for index, name in enumerate(self.filling_order)}
        self.growth_cores = {"diver", "archaeologist", "pirate", "dove", "mrs_fruit",
                             "amethyst", "pear", "eldritch_beast"}
        self.growth_block = set(self.growth_cores)
        for pair in self.values.pairs:
            food, eater = pair.split("/", 1)
            if eater in self.growth_cores:
                self.growth_block.add(food)
        for core, foods in self.values.growth_associations.items():
            self.growth_block.update(foods)
            self.growth_block.add(core)
        self.current_spins_left = 99
        self.last_removal_spin = None
        self.filling_skips = 0
        self.filling_picks = 0
        self.last_symbol_pick = None
        self.ever_full = False
        self.current_due = 0
        self.last_rents_paid = 0
        self.rent_reward_active = False
        self.reward_rerolls = 0
        self.unremovable_types = set()

    def choose(self, frame: Frame) -> tuple[Candidate, str]:
        options = frame.candidates
        if not options:
            raise ValueError("No legal candidates in a running frame")
        self.current_due = frame.info.get("rent_due", 1)
        rents_paid = int(frame.info.get("rents_paid", 0) or 0)
        if rents_paid > self.last_rents_paid:
            self.rent_reward_active = True
            self.reward_rerolls = 0
        self.last_rents_paid = rents_paid
        email = str(frame.info.get("email", ""))
        if email not in ("add_tile", "add_item", "add_tile_prompt", "add_item_prompt"):
            self.rent_reward_active = False
        self.unremovable_types = set(frame.info.get("unremovable_types", []) or [])
        self.current_spins_left = frame.info.get("spins_left", 99)
        deck_len = frame.info.get("deck_len", 0)
        if deck_len >= 20:
            self.ever_full = True
        spin = next((c for c in options if c.type_id == 430), None)
        deck = Counter(self.client.type_name(t) for t, m in zip(frame.deck_types, frame.deck_mask) if m)
        # Disabled items cannot currently confer an effect. The original helper
        # includes some consumed-essence effects, which this frame cannot yet
        # represent completely; do not invent those states here.
        held = {self.client.type_name(t) for t, f, m in
                zip(frame.item_types, frame.item_features, frame.item_mask) if m and not f[3]}
        item_counts = Counter()
        for t, f, m in zip(frame.item_types, frame.item_features, frame.item_mask):
            if m and not f[3]:
                item_counts[self.client.type_name(t)] += max(1, int(f[0]))
        held.update(set(frame.info.get("destroyed_item_types", [])) & self.persistent_essences)
        owned = set(deck) | held
        if spin:
            growth_cores = {"mrs_fruit", "archaeologist", "diver", "dove", "pirate", "eldritch_beast"}
            if any(deck[core] for core in growth_cores):
                bombs = {"symbol_bomb_small", "symbol_bomb_big", "symbol_bomb_very_big", "symbol_bomb_quantum",
                         "symbol_bomb_small_essence", "symbol_bomb_big_essence", "symbol_bomb_very_big_essence",
                         "symbol_bomb_quantum_essence"}
                for candidate in options:
                    if candidate.kind != 3 or candidate.type_id != 418 or candidate.reference is None:
                        continue
                    index = candidate.reference - 116
                    if 0 <= index < len(frame.item_types) and frame.item_mask[index]:
                        item_name = self.client.type_name(frame.item_types[index])
                        if item_name in bombs:
                            return candidate, f"已有永久成长核心，使用符号炸弹：{item_name}"
            removal = next((c for c in options if c.type_id == 431), None)
            spin_count = frame.info.get("spins")
            if removal and self._has_removal_target(frame, deck, held, owned, item_counts) and self.last_removal_spin != spin_count:
                self.last_removal_spin = spin_count
                return removal, "有删除机会，尝试删除低价值符号"
            return spin, "继续转动"
        removals = [c for c in options if c.kind == 2]
        if removals:
            ranked = []
            for candidate in removals:
                name = self.client.type_name(candidate.type_id)
                score = self._removal_score(candidate.type_id, candidate.aux, frame, deck, item_counts)
                rank = self._removal_target_rank(name, self._rarity_label(candidate),
                                                 candidate.aux[0], candidate.aux[1], candidate.aux[2],
                                                 owned, deck, held, self.current_due,
                                                 score=score, deck_len=frame.info.get("deck_len", 0),
                                                 tokens=frame.info.get("removal_tokens", 0),
                                                 cost=frame.info.get("removal_cost", 1))
                if rank is not None:
                    ranked.append((rank, candidate))
            if ranked:
                rank, candidate = min(ranked, key=lambda pair: pair[0])
                return candidate, f"删除低价值符号（优先级 {rank[0]}/{rank[1]}，估值 {rank[2]:.2f}）"
            return self._fallback(options, (412,)), "没有合适的低价值目标，取消删除"
        if frame.kind == 2:
            return self._choose_oil_column(frame, deck, item_counts)
        if frame.kind == 3:
            return self._choose_swap(frame, deck, item_counts)

        cards = [c for c in options if c.kind == 0]
        if cards:
            symbols = [c for c in cards if 0 <= c.type_id < 168]
            if symbols:
                due = frame.info.get("rent_due", 1)
                skip = next((c for c in options if c.type_id in (412, 413)), None)
                reroll = next((c for c in options if c.type_id == 414), None)
                if self.rent_reward_active:
                    reward_choice = self._choose_rent_reward_symbols(
                        frame, symbols, deck, item_counts, owned, held, deck_len, due, skip, reroll)
                    if reward_choice is not None:
                        return reward_choice
                if self.ever_full:
                    scores = self._score_symbol_candidates(frame, symbols, deck, item_counts,
                                                          owned, held, deck_len)
                    target = 1 if due <= 50 else 2 if due <= 150 else 3 if due <= 375 else 4
                    allowed = self._allowed_symbol_choices(scores, frame, deck)
                    eligible = [(estimate, c) for estimate, c in allowed if estimate["score"] >= target]
                    one_shot_available = any(estimate.get("one_shot_skip", 0.0) >= target
                                             for estimate, _ in allowed)
                    if skip and not eligible and not one_shot_available:
                        return skip, f"增量收益未达到阶段门槛 {target}，保留重掷等待交租奖励"
                    choices = eligible or allowed or scores
                    def late_rank(entry):
                        estimate, c = entry
                        return (estimate["score"], self._rarity_rank(c))
                    best_rank = max(late_rank(entry) for entry in choices)
                    estimate, best = self.rng.choice([entry for entry in choices if late_rank(entry) == best_rank])
                    details = list(estimate["notes"])
                    if estimate.get("empty_bonus"):
                        details.append(f"空位补偿 +{estimate['empty_bonus']:.2f}")
                    if estimate.get("one_shot_skip", 0.0) >= target and estimate["score"] < target:
                        details.append(f"一次性符号对跳过使用原值 {estimate['one_shot_skip']:g}，因此不跳过")
                    text = "；".join(details) or "基础及已建模配合"
                    return best, (f"满格后模型增量估分 {estimate['score']:.2f}（毛值 {estimate['gross']:.2f}，"
                                  f"溢出扣 {estimate['penalty']:.2f}）；有效符号数 {estimate['effective']:.1f}+"
                                  f"{estimate['slots']:.1f}；阶段门槛 {target}；{text}；覆盖 {estimate['status']}")
                count = deck_len
                def filling_rank(c):
                    name = self.client.type_name(c.type_id)
                    base_score = self._filling_base_score(name)
                    association = self._association_score(name, owned, deck, held)[0]
                    feed = 0.1 * self._feeding_priority(name, deck, count, due)
                    combo = self._filling_combo_bonus(name, deck, item_counts)
                    value_bonus = self._filling_value_bonus(name, frame, deck, item_counts)
                    return (self._rarity_rank(c), base_score + association + feed + combo + value_bonus)
                ranked = [(filling_rank(c), c) for c in symbols]
                allowed = [(score, c) for score, c in ranked
                           if not self._filling_banned(self.client.type_name(c.type_id), deck)]
                limit = 1 if deck["coal"] >= 2 else 2
                if ((not allowed or self._filling_skip_quality(symbols, frame, deck, held, owned, item_counts))
                        and skip and self.filling_skips < limit):
                    self.filling_skips += 1
                    return skip, (f"填格阶段候选都处于低位（跳牌上限 {limit}，本次为第 {self.filling_skips} 次），"
                                  "保留资源等待更好的符号")
                choices = allowed or ranked
                top = max(score for score, _ in choices)
                best_score, best = self.rng.choice([(score, c) for score, c in choices if score == top])
                name = self.client.type_name(best.type_id)
                self.filling_picks += 1
                self.last_symbol_pick = name
                position = self.filling_position.get(name)
                place = (f"抓位 {position + 1}/{len(self.filling_order)}" if position is not None
                         else "未列入普通抓位")
                associations = self._association_score(name, owned, deck, held)[1]
                feed = self._feeding_estimate(name, deck, count)
                forced = "跳过上限已用尽，选择相对最好的候选；" if not allowed else ""
                return best, (f"{forced}填格{place}；品质 {self._rarity_label(best)}；综合 {best_score[1]:.2f}；"
                              f"关联 {associations or '无'}；喂食增量估计 {feed:.2f}")
            def item_rank(c):
                name = self.client.type_name(c.type_id)
                association = self._association_score(name, owned, deck, held)[0]
                guide = float(self.priors["early_item_scores"].get(name, 0)) / 6 if not self.ever_full else 0.0
                combo = min(0.6, self.values.item_combo(name, deck, item_counts, frame, self.client) / 30)
                return (self._rarity_rank(c), guide + association + combo)
            high = max(item_rank(c) for c in cards)
            best = self.rng.choice([c for c in cards if item_rank(c) == high])
            name = self.client.type_name(best.type_id)
            kind = "item" if best.type_id < 282 else "essence"
            _, associations = self._association_score(name, owned, deck, held)
            combo = min(0.6, self.values.item_combo(name, deck, item_counts, frame, self.client) / 30)
            combo_text = f"组合加成 {combo:.2f}；" if combo else ""
            return best, f"{('精华统一比较' if kind == 'essence' else '物品品质 ' + self._rarity_label(best))}；{combo_text}综合 {high[1]:.2f}；关联 {associations or '无'}"

        # Pay rent when possible; otherwise use a game-provided alternative.
        if frame.info.get("email") == "rent_due":
            pay = next((c for c in options if c.type_id == 415), None)
            return (pay or options[0]), "支付租金或使用可用的替代方式"
        return self._fallback(options, (412, 411, 413, 415)), "选择可用的确认或跳过"

    def _score_symbol_candidates(self, frame, symbols, deck, item_counts, owned, held, deck_len):
        scores = []
        for c in symbols:
            name = self.client.type_name(c.type_id)
            estimate = self.values.estimate(name, frame, self.client, deck, item_counts,
                                            empty_compensation=deck_len < 20)
            estimate["one_shot_skip"] = self.values.one_shot_skip_value(name)
            association, labels = self._association_score(name, owned, deck, held)
            info = self.values.symbols.get(name, {})
            if info.get("rarity") == "rare" and info.get("base", 0) < 3:
                positive = sum(1 for token in labels.split(", ") if token.startswith("+"))
                bonus = min(1.5, 0.35 * positive)
            else:
                bonus = 0.5 * association
            if bonus:
                estimate["score"] += bonus
                estimate["notes"].append(f"关联加成 {bonus:+.2f}")
            scores.append((estimate, c))
        return scores

    def _allowed_symbol_choices(self, scores, frame, deck):
        late_no_growth = self.current_due >= 777 and self.current_spins_left <= 5
        allowed = []
        for estimate, c in scores:
            name = self.client.type_name(c.type_id)
            if name == "cow" and not deck["cat"]:
                continue
            if name == "key" and not any(deck.get(chest) for chest in self.CHESTS):
                continue
            if name == "coal" and deck["coal"] >= 3:
                continue
            if late_no_growth and name in self.growth_block:
                continue
            allowed.append((estimate, c))
        return allowed

    def _reward_target(self, due):
        if due <= 50:
            return 1.0
        if due <= 150:
            return 2.0
        if due <= 225:
            return 2.5
        if due <= 375:
            return 3.0
        if due <= 450:
            return 3.3
        if due < 600:
            return 3.9
        return 4.5

    def _choose_rent_reward_symbols(self, frame, symbols, deck, item_counts, owned, held,
                                    deck_len, due, skip, reroll):
        scores = self._score_symbol_candidates(frame, symbols, deck, item_counts,
                                               owned, held, deck_len)
        allowed = self._allowed_symbol_choices(scores, frame, deck)
        if deck_len < 20:
            allowed = [(estimate, c) for estimate, c in allowed
                       if not self._filling_banned(self.client.type_name(c.type_id), deck)]
        choices = allowed or scores
        if not choices:
            return None

        def rank(entry):
            estimate, c = entry
            return (estimate["score"], self._rarity_rank(c))

        def best_of(entries):
            best_rank = max(rank(entry) for entry in entries)
            return self.rng.choice([entry for entry in entries if rank(entry) == best_rank])

        target = self._reward_target(due)
        eligible = [(estimate, c) for estimate, c in allowed if estimate["gross"] >= target]
        one_shot_available = any(estimate.get("one_shot_skip", 0.0) >= target
                                 for estimate, _ in allowed)
        best_estimate, best = best_of(choices)
        best_name = self.client.type_name(best.type_id)

        if eligible:
            estimate, candidate = best_of(eligible)
            self.reward_rerolls = 0
            if deck_len < 20:
                self.filling_picks += 1
                self.last_symbol_pick = self.client.type_name(candidate.type_id)
            return candidate, (f"交租奖励选择 {self.client.type_name(candidate.type_id)}："
                               f"估分 {estimate['score']:.2f} 达到奖励门槛 {target:g}")
        if one_shot_available:
            self.reward_rerolls = 0
            return best, (f"交租奖励保留一次性符号 {best_name}："
                          f"原始跳过价值达到奖励门槛 {target:g}")

        tokens = int(frame.info.get("reroll_tokens", 0) or 0)
        cost = max(1, int(frame.info.get("reroll_cost", 1) or 1))
        reserve = 0 if due >= 650 else 1
        max_rerolls = 99 if due >= 650 else 2 if due >= 600 else 1
        if due < 650:
            if deck_len >= 24:
                max_rerolls = 0
            elif deck_len >= 22 and max_rerolls > 1:
                max_rerolls -= 1
        if (reroll is not None and tokens - cost >= reserve and
                self.reward_rerolls < max_rerolls):
            self.reward_rerolls += 1
            return reroll, (f"交租奖励最高估分 {best_estimate['score']:.2f} 未达到门槛 {target:g}，"
                            f"使用重掷（本次第 {self.reward_rerolls} 次，保留 {reserve} 个）")

        self.reward_rerolls = 0
        if due >= 650:
            if deck_len < 20:
                self.filling_picks += 1
                self.last_symbol_pick = best_name
            return best, (f"650+ 交租奖励已无重掷可用，选择最高分 {best_name}"
                          f"（{best_estimate['score']:.2f}），不跳过")
        if skip is not None and deck_len >= 20 and best_estimate["score"] < target:
            return skip, (f"交租奖励最高估分 {best_estimate['score']:.2f} 未达到门槛 {target:g}，"
                          "牌组已满，跳过")
        if deck_len < 20:
            self.filling_picks += 1
            self.last_symbol_pick = best_name
        return best, (f"交租奖励未达门槛但仍有空位，选择最高分 {best_name}"
                      f"（{best_estimate['score']:.2f}）")

    def _board_names(self, frame):
        names = []
        for type_id in frame.board:
            name = self.client.type_name(type_id)
            names.append(None if name in ("empty", "missing") else name)
        while len(names) < 20:
            names.append(None)
        return names

    def _spin_value(self, name, items, doubled=False):
        if not name:
            return 0.0
        value = max(0.0, self.values.base(name, items))
        return value * (2.0 if doubled else 1.0)

    def _adjacency_bonus(self, left, right, items, left_doubled=False, right_doubled=False):
        if not left or not right:
            return 0.0
        bonus = 0.0
        for eater, food, food_doubled in ((left, right, right_doubled), (right, left, left_doubled)):
            rule = self.values.feeders.get(eater)
            if rule and self.values.matches(food, rule):
                pair = self.values.pairs.get(food + "/" + eater)
                if pair:
                    extra = float(pair.get("extra", 0.0))
                    if rule.get("growth"):
                        extra = max(extra, 5.0)
                    if food_doubled:
                        extra *= 2.0
                    bonus += extra
                growth = self.values.growth_associations.get(eater, {}).get(food, 0.0)
                if growth:
                    bonus += 0.5 * growth * (2.0 if food_doubled else 1.0)
            booster = self.values.boosters.get(eater)
            if booster and self.values.matches(food, booster):
                bonus += 0.5 * (booster["multiplier"] - 1) * self._spin_value(
                    food, items, food_doubled)
        left_groups = self.values.symbols.get(left, {}).get("groups", [])
        right_groups = self.values.symbols.get(right, {}).get("groups", [])
        if left == "dog" and "doglikes" in right_groups:
            bonus += 2.0
        if right == "dog" and "doglikes" in left_groups:
            bonus += 2.0
        if left == "bee" and "beelikes" in right_groups:
            bonus += 1.0
        if right == "bee" and "beelikes" in left_groups:
            bonus += 1.0
        if left == "omelette" and "omelettestuff" in right_groups:
            bonus += 2.0
        if right == "omelette" and "omelettestuff" in left_groups:
            bonus += 2.0
        return bonus

    def _board_score(self, names, neighbors, items, doubled=None):
        doubled = doubled or set()
        total = sum(self._spin_value(name, items, index in doubled)
                    for index, name in enumerate(names))
        for index, adjacent in enumerate(neighbors):
            for other in adjacent:
                if other <= index or other >= len(names):
                    continue
                total += self._adjacency_bonus(
                    names[index], names[other], items,
                    index in doubled, other in doubled)
        return total

    def _swap_delta(self, names, neighbors, items, first, second, essence):
        if first == second or first >= len(names) or second >= len(names):
            return -999.0
        swapped = list(names)
        swapped[first], swapped[second] = swapped[second], swapped[first]
        before = self._board_score(names, neighbors, items)
        doubled = {first, second} if essence else set()
        after = self._board_score(swapped, neighbors, items, doubled)
        return after - before

    def _choose_oil_column(self, frame, deck, items):
        names = self._board_names(frame)
        best_col, best_score = 0, -1.0
        for col in range(5):
            score = sum(self._spin_value(names[row * 5 + col], items)
                        for row in range(4) if row * 5 + col < len(names))
            if score > best_score:
                best_col, best_score = col, score
        target = 424 + best_col
        choice = next((c for c in frame.candidates if c.type_id == target), None)
        if choice is not None:
            return choice, f"油罐选择当前收益最高的第 {best_col + 1} 列（估值 {best_score:.2f}）"
        return self._fallback(frame.candidates, (424, 425, 426, 427, 428, 412)), "油罐没有可用列，回退"

    def _choose_swap(self, frame, deck, items):
        candidates = [c for c in frame.candidates if c.kind == 4 and c.reference is not None]
        if not candidates:
            return self._fallback(frame.candidates, (412,)), "没有可交换的格子"
        names = self._board_names(frame)
        essence = bool(items.get("swapping_device_essence", 0))
        picked = frame.info.get("swap_picked", []) or []
        if picked:
            first = int(picked[0])
            best = None
            for candidate in candidates:
                second = int(candidate.reference)
                delta = self._swap_delta(names, frame.board_neighbors, items, first, second, essence)
                if best is None or delta > best[0]:
                    best = (delta, candidate)
            if best is not None and best[0] > 0.2:
                return best[1], f"交换第二格，预估位置收益 +{best[0]:.2f}"
            return self._fallback(frame.candidates, (412,)), "没有正的交换收益，取消"
        best = None
        for index, first in enumerate(candidates):
            for second in candidates[index + 1:]:
                delta = self._swap_delta(names, frame.board_neighbors, items,
                                         int(first.reference), int(second.reference), essence)
                if best is None or delta > best[0]:
                    best = (delta, first, second)
        if best is not None and best[0] > 0.2:
            return best[1], f"交换第一格，预估位置收益 +{best[0]:.2f}"
        return self._fallback(frame.candidates, (412,)), "没有正的交换收益，取消"

    def _fallback(self, options, preferences):
        for type_id in preferences:
            found = next((c for c in options if c.type_id == type_id), None)
            if found:
                return found
        return options[0]

    def _filling_base_score(self, name):
        printed = self.symbols.get(name, {}).get("value") or 0
        return float(self.priors["filling_symbol_scores"].get(name, printed)) / 6

    def _filling_banned(self, name, deck):
        if self.current_due >= 777 and self.current_spins_left <= 5 and name in self.growth_block:
            return True
        if name == "cow" and not deck["cat"]:
            return True
        if name == "coal" and (deck["coal"] >= 3 or
                               (self.filling_picks == 1 and self.last_symbol_pick == "coal")):
            return True
        return False

    def _filling_combo_bonus(self, name, deck, items):
        flush = self.values.combos["flush"]
        if name in self.values.combos["suits"] and (items.get(flush["item"]) or items.get(flush["essence"])):
            return min(0.6, self.values.suit_payout(deck, items)[0] / 20)
        hover = self.values.combos["hover"]
        if name in hover["generators"] and (items.get(hover["item"]) or items.get(hover["essence"])):
            return min(0.6, self.values.hover_payout(items) * hover["generators"][name] / 30)
        return 0.0

    def _filling_value_bonus(self, name, frame, deck, item_counts):
        if name == "key":
            return 0.8 if any(deck.get(chest) for chest in self.CHESTS) else 0.0
        if name in self.CHESTS and deck.get("key"):
            return 0.8
        if name == "golden_arrow":
            return 0.8
        if name in self.values.one_shot:
            value = self.values.filling_one_shot_score(name, frame, self.client, deck, item_counts)
            # A free slot still has long-term value while filling.  Keep this
            # as a small rank offset around a persistent 2-value symbol; the
            # rarity and the guide score remain part of the ranking.
            return max(-0.6, min(0.6, ((value or 0.0) - 2.0) * 0.3))
        if name not in self.values.rent_curves and name not in {"hustler", "wildcard", "king_midas"}:
            return 0.0
        estimate = self.values.estimate(name, frame, self.client, deck, item_counts)
        return min(0.8, max(0.0, estimate["gross"]) / 40.0)

    def _filling_skip_quality(self, symbols, frame, deck, held, owned, item_counts):
        cutoff = len(self.filling_order) - (7 if deck["coal"] >= 2 else 10)
        count = frame.info.get("deck_len", 0)
        due = frame.info.get("rent_due", 1)
        for candidate in symbols:
            name = self.client.type_name(candidate.type_id)
            if self._rarity_label(candidate) != "common" or name not in self.filling_position:
                return False
            if self.filling_position[name] < cutoff:
                return False
            if self._association_score(name, owned, deck, held)[0] > 0:
                return False
            if self._feeding_priority(name, deck, count, due) > 0:
                return False
            if self._filling_combo_bonus(name, deck, item_counts) > 0:
                return False
        return True

    def _removal_score(self, type_id, features, frame, deck, item_counts):
        name = self.client.type_name(type_id)
        estimate = self.values.estimate(name, frame, self.client, deck, item_counts)
        raw = float(self.values.symbols.get(name, {}).get("raw_base", 0.0))
        try:
            effective = (float(features[0]) + float(features[1])) * float(features[2])
        except (TypeError, ValueError):
            effective = 0.0
        return estimate.get("score", 0.0) + max(0.0, effective - raw)

    def _has_removal_target(self, frame, deck, held, owned, item_counts):
        tokens = frame.info.get("removal_tokens")
        tokens = 1 if tokens is None else int(tokens)
        cost = max(1, int(frame.info.get("removal_cost", 1) or 1))
        if tokens < cost:
            return False
        # Keep two tokens for the landlord battle; spend the rest before 600.
        reserve = 2 if self.current_due >= 600 else 1 if self.current_due >= 300 else 0
        if tokens - cost < reserve:
            return False
        rarity = self.client.hello["vocab"]["rarity"]["symbols"]
        deck_len = frame.info.get("deck_len", 0)
        for type_id, features, present in zip(frame.deck_types, frame.deck_features, frame.deck_mask):
            if not present:
                continue
            label = rarity[type_id] if 0 <= type_id < len(rarity) else "none"
            name = self.client.type_name(type_id)
            if name in self.unremovable_types:
                continue
            score = self._removal_score(type_id, features, frame, deck, item_counts)
            if self._removal_target_rank(name, label, features[0], features[1], features[2],
                                         owned, deck, held, self.current_due, score=score,
                                         deck_len=deck_len, tokens=tokens, cost=cost) is not None:
                return True
        return False

    def _removal_target_rank(self, name, rarity, value, bonus, multiplier, owned, deck, held, due=0,
                             score=None, deck_len=20, tokens=0, cost=1):
        if name in self.REMOVAL_UNREMOVABLE or name in self.unremovable_types:
            return None
        if self.categories.get(name) != "符号":
            return None
        try:
            effective = (float(value) + float(bonus)) * float(multiplier)
        except (TypeError, ValueError):
            return None
        due = due or 0
        if name in self.CURSE_REMOVAL_ORDER:
            threshold = self.values.data.get("curse_removal_rent", {}).get(name, 999)
            if due < threshold:
                return None
            return (1, self.CURSE_REMOVAL_ORDER.index(name), effective)
        if self._removal_protected(name, deck, held, due):
            return None
        if score is None:
            score = effective
        # A card that no longer earns its slot is removable at any rarity.
        limit = 1.2 if due <= 150 else 1.8 if due <= 300 else 2.2 if due <= 450 else 2.8
        if deck_len < 20 and due < 600:
            limit = min(limit, 1.0)
        if rarity != "common":
            limit = min(limit, 2.2)
        if tokens - cost >= 3:
            limit += 0.3  # do not hoard tokens once the board is full
        if score > limit:
            return None
        if name in self.INITIAL_REMOVAL_ORDER:
            return (0, self.INITIAL_REMOVAL_ORDER.index(name), score)
        return (2, self._rarity_rank_for_removal(rarity), score)

    def _rarity_rank_for_removal(self, rarity):
        return {"common": 0, "uncommon": 1, "rare": 2, "very_rare": 3}.get(rarity, 4)

    def _removal_protected(self, name, deck, held, due):
        if name in self.growth_cores or self.values.growth_food(name, deck):
            return True
        if name == "flower" and deck.get("sun"):
            return True
        if name == "bee" and (deck.get("beehive") or deck.get("flower", 0) >= 3):
            return True
        if name in self.REMOVAL_PROTECTED_EARLY and due < 450:
            return True
        if name == "general_zaroff":
            return True
        if name == "bounty_hunter" and "zaroffs_contract" in held:
            return True
        if name == "key" and any(deck.get(chest) for chest in self.CHESTS):
            return True
        if name in self.CHESTS and deck.get("key"):
            return True
        if name in ("bronze_arrow", "silver_arrow", "golden_arrow"):
            return True
        if name == "banana_peel" and deck.get("thief"):
            return True
        if name == "thief" and deck.get("banana_peel"):
            return True
        if name == "robin_hood" and (deck.get("target") or
                                     any(deck.get(arrow) for arrow in
                                         ("bronze_arrow", "silver_arrow", "golden_arrow"))):
            return True
        if name == "target" and deck.get("robin_hood"):
            return True
        for core, targets in self.REMOVAL_CORE_TARGETS.items():
            if deck.get(core) and name in targets:
                return True
            if name == core and any(deck.get(target) for target in targets):
                return True
        if name == "billionaire" and (deck.get("wine") or deck.get("robin_hood") or
                                      deck.get("general_zaroff")):
            return True
        # Item support can carry a symbol that looks weak from its face value.
        for item in held:
            for table in ("item_additions", "item_multipliers"):
                rule = self.values.data.get(table, {}).get(item)
                if rule and self.values.matches(name, rule):
                    return True
        # A feeder and its matching food are worth more together than either
        # half alone; the same is true for the food half.
        if name in self.values.feeders:
            for food, count in deck.items():
                if count and food + "/" + name in self.values.pairs:
                    return True
        for eater in self.values.feeders:
            if deck.get(eater) and name + "/" + eater in self.values.pairs:
                return True
        return False

    def _rarity_label(self, candidate):
        vocab = self.client.hello["vocab"]
        tid = candidate.type_id
        if tid < 168:
            category, idx = "symbols", tid
        elif tid < 282:
            category, idx = "items", tid - 168
        else:
            category, idx = "essences", tid - 282
        rarity = vocab["rarity"][category]
        label = rarity[idx] if 0 <= idx < len(rarity) else "none"
        return label

    def _rarity_rank(self, candidate):
        return {"none": 0, "common": 1, "uncommon": 2, "rare": 3, "very_rare": 4,
                "essence": 0}.get(self._rarity_label(candidate), 0)

    def _feeding_estimate(self, name, deck, count):
        # A single food can be consumed once. The board has 20 positions and
        # a random pair is adjacent with probability 11/38. With multiple
        # consumers, use an approximate conditional independence calculation.
        n = count + 1
        selected = min(1.0, 20 / n)
        pair_given_selected = min(1.0, 19 / max(1, n - 1)) * (11 / 38)
        estimates = [selected * (1 - (1 - pair_given_selected) ** deck[consumer]) * payout
                     for consumer, payout in self.priors["feed_pairs"].get(name, []) if deck[consumer]]
        return max(estimates, default=0.0)

    def _is_transient(self, name, deck):
        # Food is only an expected removal when a matching eater is present.
        if name == "bubble":
            return True  # it also expires on its own
        pairs = self.priors["feed_pairs"].get(name)
        if pairs is not None:
            return any(deck[consumer] for consumer, _ in pairs)
        if name == "pinata":
            return bool(deck["toddler"])
        if name == "bar_of_soap":
            return bool(deck["toddler"] or deck["goldfish"])
        return name in self.priors["transient_symbols"]

    def _feeding_priority(self, name, deck, count, due):
        # Simple survival bias before the difficult 300-rent payment. Piñata
        # generates seven candies, so only take the shortcut before filling.
        if due > 300:
            return 0
        if name == "pinata" and deck["toddler"] and count < 20:
            return 1
        if name == "bar_of_soap" and (deck["goldfish"] or deck["toddler"]) and count < 20:
            return 1
        return int(self._feeding_estimate(name, deck, count) >= 1.5)

    def _association_score(self, candidate, owned, deck=None, held=None):
        deck = deck or {}
        held = held or set()
        extra = set()
        category = self.categories.get(candidate, "")
        if candidate in self.trigger_items:
            extra.update(name for name, count in deck.items() if count >= 3)
        elif category == "符号" and deck.get(candidate, 0) >= 2:
            extra.update(held & self.trigger_items)
        if category == "符号" and "frozen_pizza_essence" in held:
            extra.add("frozen_pizza_essence")
        if category == "精华":
            extra.update(held & {"popsicle", "popsicle_essence"})
        if category in ("物品", "精华") and "bag_of_holding_essence" in held:
            extra.add("bag_of_holding_essence")
        positive, negative = [], []
        for other in sorted(owned | extra):
            pair = tuple(sorted((candidate, other)))
            if pair not in self.pairs and other not in extra:
                continue
            if candidate == "copycat_essence" and other == "cat" and deck.get("cat", 0) < 5:
                continue  # a single cat does not meet its five-cat trigger
            # Match the helper's directional dog display rule and color order.
            if other == "dog" and candidate not in {"beastmaster", "comedian", "quigley_the_wolf", "quigley_the_wolf_essence"}:
                continue
            if pair in self.red:
                negative.append(other)
            elif (pair in self.gray or "lunchbox" in pair or
                  (candidate == other and candidate not in self.self_positive)):
                continue
            else:
                positive.append(other)
        # These are small, explicitly provisional ranking hints, not inferred
        # profits, prohibitions, or ground truth about winning.
        adjustment = min(0.6, 0.2 * len(positive)) - min(0.9, 0.3 * len(negative))
        labels = ["+" + s for s in positive] + ["-" + s for s in negative]
        return adjustment, ", ".join(labels[:6])
