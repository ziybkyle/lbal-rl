"""Check published examples, overflow weights and conditional item use."""
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from baseline_policy import BaselinePolicy
from symbol_value import SymbolValues
from v3_client import Candidate


class SymbolValueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.values = SymbolValues(ROOT / "data" / "symbol_values.json")
        cls.names = ["coin", "toddler", "candy", "robin_hood", "target", "apple", "rabbit", "moon",
                     "bronze_arrow", "silver_arrow", "golden_arrow", "mrs_fruit", "cherry", "cat", "milk",
                     "archaeologist", "shiny_pebble", "lucky_capsule", "void_stone", "clubs", "king_midas",
                     "diver", "pirate", "dove", "amethyst", "pear", "wildcard", "hustler",
                     "hex_of_thievery", "hex_of_destruction", "hex_of_draining", "hex_of_hoarding",
                     "hex_of_tedium", "hex_of_emptiness", "key", "magic_key", "safe", "mega_chest",
                     "treasure_chest", "lockbox", "tomb", "urn", "big_urn", "big_ore", "mine", "golem",
                     "ore", "pearl", "pirate", "chicken", "egg", "bartender", "dwarf", "midas_bomb",
                     "chemical_seven", "tedium_capsule", "item_capsule", "essence_capsule",
                     "reroll_capsule", "removal_capsule", "rarity_capsule", "time_capsule",
                     "buffing_powder", "diamond", "watermelon", "ruby", "beehive", "bear", "d5",
                     "general_zaroff", "card_shark", "highlander", "wine", "bounty_hunter",
                     "thief", "sun", "flower", "bee", "anchor", "ninja", "mouse", "rabbit_fluff"]
        class Client:
            game = ROOT / "game"
            hello = {"vocab": {"rarity": {"symbols": ["common"] * 168, "items": ["common"] * 114,
                                         "essences": ["essence"] * 113}, "items": []}}
            def type_name(self, tid):
                if tid == 168:
                    return "symbol_bomb_small"
                return SymbolValueTests.names[tid]
        cls.client = Client()

    def frame(self, names, candidates=(), items=(), rent=375, spins_left=99, email="",
              rents_paid=0, reroll_tokens=0, reroll_cost=1, removal_tokens=None,
              unremovable_types=None):
        counts = Counter(names)
        tids = tuple(self.names.index(name) for name in names)
        features = tuple((self.values.symbols[name]["raw_base"], 0.0, 1.0, 0.0) for name in names)
        info = {"deck_len": len(names), "spins": 25, "rent_due": rent,
                "spins_left": spins_left, "email": email, "rents_paid": rents_paid,
                "reroll_tokens": reroll_tokens, "reroll_cost": reroll_cost}
        if removal_tokens is not None:
            info["removal_tokens"] = removal_tokens
        if unremovable_types is not None:
            info["unremovable_types"] = unremovable_types
        return SimpleNamespace(deck_types=tids, deck_features=features, deck_mask=(True,) * len(names),
                               deck_uids=tuple(range(1, len(names)+1)),
                               item_types=tuple(168 for _ in items),
                               item_features=tuple((1, 0, 0, 0, 1) for _ in items),
                               item_mask=(True,) * len(items), candidates=candidates, kind=0,
                               info=info), counts

    def estimate(self, name, deck, items=None, rent=375):
        frame, counts = self.frame(deck, rent=rent)
        return self.values.estimate(name, frame, self.client, counts, Counter(items or {}))

    def test_precomputed_one_to_three_feeders(self):
        probability = [0.2894736842105263, 0.4970760233918129, 0.6455108359133127]
        for n, p in enumerate(probability, 1):
            self.assertAlmostEqual(self.values.prob(n), p)
            self.assertAlmostEqual(self.estimate("candy", ["toddler"] * n)["score"], 1 + 6*p)
            self.assertAlmostEqual(self.estimate("target", ["robin_hood"] * n)["score"], 2 + 25*p)
        self.assertAlmostEqual(self.estimate("apple", ["robin_hood"])["score"], 3 + 15*probability[0])
        self.assertAlmostEqual(self.estimate("robin_hood", ["bronze_arrow"])["score"], 2.25 + 3*probability[0])

    def test_additions_multipliers_growth_and_arrows(self):
        expected_factor = self.values.data["multiplier_factors"]["3:2"]
        self.assertAlmostEqual(self.estimate("rabbit", ["moon", "moon"], {"nori_the_rabbit": 1})["score"],
                               4 * expected_factor)
        self.assertAlmostEqual(self.estimate("rabbit", [], {"nori_the_rabbit": 1, "golden_carrot": 1})["score"], 20)
        self.assertGreaterEqual(self.estimate("cherry", ["mrs_fruit"])["score"], 4.5)
        self.assertGreaterEqual(self.estimate("shiny_pebble", ["archaeologist"])["score"], 4.5)
        self.assertAlmostEqual(self.estimate("milk", ["cat"], {"pizza_the_cat": 1})["score"],
                               1 + 9 * self.values.prob(1))
        frame, deck = self.frame(["coin"] * 19)
        for name, expected in (("bronze_arrow", 1.375), ("silver_arrow", 2.75)):
            self.assertAlmostEqual(self.values.estimate(name, frame, self.client, deck, Counter())["score"], expected)
            self.assertAlmostEqual(self.values.estimate(name, frame, self.client, deck, Counter(quiver=1))["score"], 2*expected)
            self.assertAlmostEqual(self.values.estimate(name, frame, self.client, deck,
                                    Counter(quiver=1, quiver_essence=1))["score"], 2*expected)
        self.assertGreaterEqual(self.values.estimate("golden_arrow", frame, self.client, deck,
                                                      Counter())["score"], 12)

    def test_overflow_weights(self):
        deck = Counter(coin=19, toddler=1)
        self.assertEqual(self.values.effective_count(deck, 21), 20)
        self.assertEqual(self.values.slots("candy", deck, 21), 0.5)
        self.assertEqual(self.values.slots("lucky_capsule", deck, 21), 0.2)
        self.assertEqual(self.values.slots("void_stone", deck, 21), 0.2)
        self.assertEqual(self.values.slots("coin", deck, 21), 1)
        self.assertEqual(self.values.slots("void_stone", deck, 19), 1)
        self.assertEqual(self.estimate("coin", ["coin"]*20)["penalty"], 0.25)
        self.assertAlmostEqual(self.estimate("candy", ["coin"]*19 + ["toddler"])["penalty"], 0.125)
        self.assertAlmostEqual(self.estimate("lucky_capsule", ["coin"]*20)["penalty"], 0.05)
        self.assertAlmostEqual(self.estimate("toddler", ["coin"]*19 + ["candy"])["penalty"], 0.125)

    def test_one_shot_values_use_discounted_scores(self):
        deck = ["coin"] * 19
        at300 = {name: self.estimate(name, deck, rent=300)["gross"]
                 for name in ("lucky_capsule", "chemical_seven", "tedium_capsule",
                              "item_capsule", "essence_capsule", "reroll_capsule",
                              "removal_capsule", "rarity_capsule", "time_capsule", "void_stone")}
        at1000 = {name: self.estimate(name, deck, rent=1000)["gross"] for name in at300}
        self.assertLess(at300["lucky_capsule"], 5)
        self.assertLess(at300["void_stone"], 5)
        cash_horizon = self.values.one_shot_horizon_for("cash", 300)
        self.assertAlmostEqual(at300["tedium_capsule"], 5 / 4 * cash_horizon)
        self.assertAlmostEqual(at300["lucky_capsule"], 10 / 4 * cash_horizon)
        self.assertEqual(at300["removal_capsule"], at300["rarity_capsule"])
        self.assertEqual(at300["rarity_capsule"], at300["reroll_capsule"])
        self.assertLess(at1000["lucky_capsule"], at300["lucky_capsule"])
        self.assertLess(at1000["rarity_capsule"], at300["rarity_capsule"])
        machine = self.estimate("lucky_capsule", deck, {"capsule_machine": 1}, rent=300)
        plain = self.estimate("lucky_capsule", deck, rent=300)
        self.assertAlmostEqual(machine["gross"], 2 * plain["gross"])
        lucky_seven = self.estimate("chemical_seven", deck, {"lucky_seven": 2}, rent=300)
        self.assertGreater(lucky_seven["gross"], at300["chemical_seven"] + 5)

    def test_one_turn_adjacency_uses_current_board_values(self):
        low = self.estimate("buffing_powder", ["coin"] * 20, rent=300)
        high = self.estimate("buffing_powder", ["diamond"] * 20, rent=300)
        self.assertGreater(high["gross"], low["gross"])
        # A one-turn multiplier must not become a permanent boost on the deck.
        plain = self.estimate("coin", ["coin"] * 19 + ["buffing_powder"], rent=300)
        self.assertAlmostEqual(plain["gross"], 1.0)

    def test_one_shot_filling_and_skip_values(self):
        frame, deck = self.frame(["coin"] * 8, rent=100)
        filling = self.values.filling_one_shot_score("lucky_capsule", frame, self.client, deck, Counter())
        self.assertAlmostEqual(filling, 10 / 4 * 0.5)
        frame, deck = self.frame(["coin"] * 19, rent=100)
        near_full = self.values.filling_one_shot_score("lucky_capsule", frame, self.client, deck, Counter())
        self.assertAlmostEqual(near_full, 10 / 4)
        self.assertEqual(self.values.one_shot_skip_value("lucky_capsule"), 10.0)
        self.assertEqual(self.values.one_shot_skip_value("chemical_seven"), 7.0)
        self.assertEqual(self.values.one_shot_skip_value("void_stone"), 8.0)
        self.assertEqual(self.values.one_shot_skip_value("tedium_capsule"), 0.0)
        policy = BaselinePolicy(self.client, 1)
        policy.ever_full = True
        lucky = Candidate(0, 0, None, 17, (0,) * 6)
        coin = Candidate(1, 0, None, 0, (0,) * 6)
        strong = Candidate(3, 0, None, self.names.index("diamond"), (0,) * 6)
        skip = Candidate(2, 0, None, 413, (0,) * 6)
        frame, _ = self.frame(["coin"] * 20, (lucky, coin, skip), rent=777)
        self.assertEqual(policy.choose(frame)[0], lucky)
        # Raw skip value only prevents skipping; it must not beat a stronger
        # persistent candidate on the discounted comparison.
        frame, _ = self.frame(["coin"] * 20, (lucky, strong, skip), rent=777)
        self.assertEqual(policy.choose(frame)[0], strong)
        frame, _ = self.frame(["coin"] * 20, (coin, skip), rent=777)
        self.assertEqual(policy.choose(frame)[0], skip)

    def test_rent_reward_reroll_policy(self):
        coin = Candidate(0, 0, None, self.names.index("coin"), (0,) * 6)
        diamond = Candidate(1, 0, None, self.names.index("diamond"), (0,) * 6)
        skip = Candidate(2, 1, None, 413, (0,) * 6)
        reroll = Candidate(3, 1, None, 414, (0,) * 6)

        policy = BaselinePolicy(self.client, 1)
        frame, _ = self.frame(["coin"] * 22, (coin, skip, reroll), rent=300,
                              email="add_tile", rents_paid=1, reroll_tokens=2)
        self.assertEqual(policy.choose(frame)[0].type_id, 414)
        frame, _ = self.frame(["coin"] * 22, (coin, skip, reroll), rent=300,
                              email="add_tile", rents_paid=1, reroll_tokens=1)
        self.assertEqual(policy.choose(frame)[0].type_id, 413)

        policy = BaselinePolicy(self.client, 1)
        for remaining in (3, 2, 1):
            frame, _ = self.frame(["coin"] * 22, (coin, skip, reroll), rent=650,
                                  email="add_tile", rents_paid=1, reroll_tokens=remaining)
            self.assertEqual(policy.choose(frame)[0].type_id, 414)
        frame, _ = self.frame(["coin"] * 22, (coin, skip, reroll), rent=650,
                              email="add_tile", rents_paid=1, reroll_tokens=0)
        self.assertEqual(policy.choose(frame)[0].type_id, coin.type_id)

        policy = BaselinePolicy(self.client, 1)
        frame, _ = self.frame(["coin"] * 22, (diamond, skip, reroll), rent=650,
                              email="add_tile", rents_paid=1, reroll_tokens=2)
        self.assertEqual(policy.choose(frame)[0].type_id, diamond.type_id)

        policy = BaselinePolicy(self.client, 1)
        frame, _ = self.frame(["coin"] * 22, (coin, skip, reroll), rent=375,
                              reroll_tokens=2)
        self.assertEqual(policy.choose(frame)[0].type_id, 413)
        frame, _ = self.frame(["coin"] * 22, (coin, skip, reroll), rent=650,
                              reroll_tokens=2)
        self.assertEqual(policy.choose(frame)[0].type_id, 413)

        policy = BaselinePolicy(self.client, 1)
        frame, _ = self.frame(["coin"] * 24, (coin, skip, reroll), rent=450,
                              email="add_tile", rents_paid=1, reroll_tokens=2)
        self.assertEqual(policy.choose(frame)[0].type_id, 413)

        policy = BaselinePolicy(self.client, 1)
        frame, _ = self.frame(["coin"] * 22, (coin, skip, reroll), rent=450,
                              email="add_tile", rents_paid=1, reroll_tokens=2)
        self.assertEqual(policy.choose(frame)[0].type_id, 414)


        policy = BaselinePolicy(self.client, 1)
        rank = policy._removal_target_rank
        self.assertIsNone(rank("toddler", "common", 1, 0, 1, {"toddler", "candy"},
                               Counter(toddler=1, candy=1), set()))
        self.assertIsNone(rank("robin_hood", "common", -4, 0, 1, {"robin_hood", "target"},
                               Counter(robin_hood=1, target=1), set()))
        self.assertIsNone(rank("mrs_fruit", "uncommon", 2, 0, 1, {"mrs_fruit"},
                               Counter(mrs_fruit=1), set()))
        self.assertIsNone(rank("bronze_arrow", "uncommon", 0, 0, 1, {"bronze_arrow"},
                               Counter(bronze_arrow=1), set()))
        self.assertIsNone(rank("dud", "common", -1, 0, 1, {"dud"}, Counter(dud=1), set()))
        self.assertIsNone(rank("cat", "common", 1, 0, 1, {"cat", "black_cat"},
                               Counter(cat=1), {"black_cat"}))
        self.assertIsNotNone(rank("flower", "common", 1, 0, 1, {"flower"}, Counter(flower=1), set()))
        self.assertIsNone(rank("flower", "common", 1, 1, 1, {"flower"}, Counter(flower=1), set()))
        self.assertLess(rank("flower", "common", 1, 0, 1, {"flower"}, Counter(flower=1), set()),
                        rank("mouse", "common", 1, 0, 1, {"mouse"}, Counter(mouse=1), set()))

    def test_empty_compensation_and_special_combos(self):
        frame, deck = self.frame(["coin"] * 5)
        plain = self.values.estimate("coin", frame, self.client, deck, Counter())
        paid = self.values.estimate("coin", frame, self.client, deck, Counter(), empty_compensation=True)
        self.assertEqual(plain["empty_bonus"], 0)
        self.assertAlmostEqual(paid["empty_bonus"], 0.25 * 14)
        frame, deck = self.frame(["coin"] * 5 + ["clubs"])
        flush = self.values.estimate("clubs", frame, self.client, deck, Counter(flush=1))
        self.assertEqual(flush["status"], "special_combo")
        self.assertAlmostEqual(flush["gross"], 12.5)
        self.assertGreater(flush["score"], 10)
        self.assertEqual(self.values.slots("clubs", Counter(clubs=1), 2, Counter(flush=1)), 0)
        self.assertEqual(self.values.suit_payout(Counter(clubs=2, hearts=1), Counter(flush_essence=1))[0], 20)
        self.assertAlmostEqual(self.values.item_combo("flush", Counter(clubs=3), Counter(flush=1)), 36.5)
        self.assertAlmostEqual(self.values.item_combo("coin_on_a_string", Counter(king_midas=1),
                                                      Counter(coin_on_a_string=1)), 40)
        frame, deck = self.frame(["coin"] * 5 + ["king_midas"])
        without = self.values.estimate("king_midas", frame, self.client, deck, Counter())
        with_string = self.values.estimate("king_midas", frame, self.client, deck,
                                           Counter(coin_on_a_string=1))
        self.assertAlmostEqual(with_string["score"] - without["score"], 3)

    def test_rent_curves_and_very_rare_overrides(self):
        for name in ("diver", "archaeologist", "pirate", "dove"):
            self.assertAlmostEqual(self.estimate(name, ["coin"] * 5, rent=25)["gross"], 30.0)
            self.assertAlmostEqual(self.estimate(name, ["coin"] * 5, rent=650)["gross"], 4.5)
        self.assertAlmostEqual(self.estimate("mrs_fruit", ["coin"] * 5, rent=25)["gross"], 10.0)
        self.assertAlmostEqual(self.estimate("mrs_fruit", ["coin"] * 5, rent=450)["gross"], 4.5)
        self.assertAlmostEqual(self.estimate("amethyst", ["coin"] * 5, rent=25)["gross"], 9.0)
        self.assertAlmostEqual(self.estimate("pear", ["coin"] * 5, rent=25)["gross"], 8.0)
        self.assertAlmostEqual(self.estimate("wildcard", ["coin"] * 5, rent=300)["gross"], 8.0)
        self.assertGreaterEqual(self.estimate("wildcard", ["highlander"] * 5, rent=300)["gross"], 5.0)
        self.assertAlmostEqual(self.estimate("hustler", ["coin"] * 5, rent=25)["gross"], 24.2)
        self.assertAlmostEqual(self.estimate("hustler", ["coin"] * 5, rent=650)["gross"], 5.0)
        self.assertAlmostEqual(self.estimate("king_midas", ["coin"] * 5,
                                             {"coin_on_a_string": 1}, rent=25)["gross"], 4.0)
        self.assertAlmostEqual(self.estimate("king_midas", ["coin"] * 5,
                                             {"coin_on_a_string_essence": 1}, rent=25)["gross"], 11.0)
        self.assertGreater(self.estimate("king_midas", ["pirate"] + ["coin"] * 4, rent=25)["gross"], 30)
        self.assertGreater(self.estimate("king_midas", ["coin"] * 5,
                                         {"triple_coins": 1, "rusty_gear": 1}, rent=25)["gross"], 10)

    def test_curses_keys_and_fixed_box_values(self):
        self.assertAlmostEqual(self.estimate("hex_of_thievery", ["coin"] * 10, rent=25)["gross"], 1.2)
        self.assertAlmostEqual(self.estimate("hex_of_destruction", ["coin"] * 10, rent=150)["gross"], 2.5)
        self.assertGreater(self.estimate("hex_of_destruction", ["coin"] * 10, rent=25)["gross"], 2.5)
        self.assertLess(self.estimate("hex_of_destruction", ["coin"] * 10, rent=650)["gross"], 0)
        self.assertAlmostEqual(self.estimate("key", ["safe"] + ["coin"] * 4, rent=650)["gross"], 20.0)
        self.assertAlmostEqual(self.estimate("key", ["coin"] * 5, rent=650)["gross"], 0.0)
        self.assertAlmostEqual(self.estimate("magic_key", ["safe"] + ["coin"] * 4, rent=650)["gross"], 3.0)
        expected = {"lockbox": 1.0, "safe": 1.0, "treasure_chest": 2.0, "mega_chest": 3.0,
                    "tomb": 4.0, "urn": 1.0, "big_urn": 2.0, "big_ore": 2.0}
        for name, value in expected.items():
            self.assertAlmostEqual(self.estimate(name, ["coin"] * 5, rent=300)["gross"], value)
        self.assertGreaterEqual(self.estimate("safe", ["key"] + ["coin"] * 4, rent=300)["gross"], 20)
        self.assertGreaterEqual(self.estimate("mega_chest", ["key"] + ["coin"] * 4, rent=300)["gross"], 50)
        self.assertLess(self.estimate("midas_bomb", ["coin"] * 19, rent=300)["gross"], 1)
        self.assertGreater(self.estimate("midas_bomb", ["coin"] * 25, rent=300)["gross"], 8)

    def test_growth_association_order(self):
        for name, value in (("mine", 20.0), ("golem", 16.0), ("big_ore", 12.0),
                            ("ore", 10.0), ("pearl", 8.0)):
            self.assertAlmostEqual(self.estimate(name, ["archaeologist"] + ["coin"] * 9,
                                                 rent=300)["gross"], value)
        self.assertGreater(self.estimate("mine", ["archaeologist"] + ["coin"] * 9, rent=300)["gross"],
                           self.estimate("golem", ["archaeologist"] + ["coin"] * 9, rent=300)["gross"])

    def test_naked_and_synergy_rare_rules(self):
        self.assertAlmostEqual(self.estimate("mine", ["coin"] * 5, rent=300)["gross"], 2.0)
        self.assertAlmostEqual(self.estimate("chicken", ["coin"] * 5, rent=300)["gross"], 1.2)
        self.assertGreaterEqual(self.estimate("chicken", ["egg"] + ["coin"] * 4, rent=300)["gross"], 4.0)
        self.assertAlmostEqual(self.estimate("bartender", ["coin"] * 5, rent=150)["gross"], 3.0)
        self.assertLess(self.estimate("bartender", ["coin"] * 5, rent=300)["gross"], 2.0)
        self.assertGreater(self.estimate("bartender", ["dwarf"] + ["coin"] * 4, rent=300)["gross"], 6.0)
        self.assertGreater(self.estimate("dwarf", ["bartender"] + ["coin"] * 4, rent=300)["gross"], 5.0)
        self.assertGreater(self.estimate("mine", ["archaeologist"] + ["coin"] * 9, rent=300)["gross"], 10.0)

    def test_guide_symbol_values_and_removal(self):
        self.assertAlmostEqual(self.estimate("rabbit_fluff", ["coin"] * 5, rent=300)["gross"], 3.0)
        self.assertAlmostEqual(self.estimate("rabbit", ["coin"] * 5, rent=300)["gross"], 3.0)
        self.assertAlmostEqual(self.estimate("wine", ["coin"] * 5, rent=300)["gross"], 3.0)
        self.assertAlmostEqual(self.estimate("card_shark", ["coin"] * 5, rent=300)["gross"], 3.0)
        self.assertAlmostEqual(self.estimate("highlander", ["coin"] * 5, rent=300)["gross"], 6.0)
        self.assertAlmostEqual(self.estimate("diamond", ["coin"] * 5, rent=300)["gross"], 5.0)
        self.assertAlmostEqual(self.estimate("diamond", ["diamond"] + ["coin"] * 4, rent=300)["gross"], 7.0)
        self.assertAlmostEqual(self.estimate("watermelon", ["watermelon"] + ["coin"] * 4, rent=300)["gross"], 6.0)
        self.assertAlmostEqual(self.estimate("ruby", ["ruby"] + ["coin"] * 4, rent=300)["gross"], 5.0)
        self.assertAlmostEqual(self.estimate("golem", ["coin"] * 5, rent=300)["gross"], 0.5)
        self.assertGreaterEqual(self.estimate("beehive", ["bear"] + ["coin"] * 4, rent=300)["gross"], 7.0)
        self.assertEqual(self.estimate("golden_arrow", ["coin"] * 19, rent=300)["gross"], 12.0)
        contract = self.estimate("bounty_hunter", ["thief"] + ["coin"] * 4,
                                 {"zaroffs_contract": 1}, rent=300)["gross"]
        self.assertGreater(contract, 10.0)
        self.assertGreater(self.estimate("ninja", ["mouse", "mouse"] + ["coin"] * 3, rent=300)["gross"], 4.0)
        self.assertGreater(self.estimate("mouse", ["ninja"] + ["coin"] * 4, rent=300)["gross"], 2.0)

        policy = BaselinePolicy(self.client, 1)
        rank = policy._removal_target_rank
        self.assertIsNotNone(rank("anchor", "common", 1, 0, 1, {"anchor"},
                                  Counter(anchor=1), set(), due=650, score=1.8,
                                  deck_len=22, tokens=5, cost=1))
        self.assertIsNotNone(rank("bee", "common", 1, 0, 1, {"bee", "flower"},
                                  Counter(bee=1, flower=1), set(), due=650, score=1.6,
                                  deck_len=22, tokens=5, cost=1))
        self.assertIsNone(rank("flower", "common", 1, 0, 1, {"flower", "sun"},
                               Counter(flower=1, sun=1), set(), due=650, score=2.2,
                               deck_len=22, tokens=5, cost=1))
        self.assertIsNone(rank("d5", "uncommon", 0, 0, 1, {"d5"},
                               Counter(d5=1), set(), due=650, score=3.0,
                               deck_len=22, tokens=5, cost=1))
        self.assertIsNone(rank("general_zaroff", "rare", 1, 0, 1, {"general_zaroff"},
                               Counter(general_zaroff=1), set(), due=650, score=8.0,
                               deck_len=22, tokens=5, cost=1))
        self.assertIsNone(rank("banana_peel", "common", 1, 0, 1, {"banana_peel", "thief"},
                               Counter(banana_peel=1, thief=1), set(), due=650, score=1.0,
                               deck_len=22, tokens=5, cost=1))

        policy.current_due = 650
        frame, deck = self.frame(["anchor", "bee", "coin"], rent=650)
        frame.info.update({"removal_tokens": 5, "removal_cost": 1})
        self.assertTrue(policy._has_removal_target(frame, deck, set(), set(deck), Counter()))
        frame.info["removal_tokens"] = 2
        self.assertFalse(policy._has_removal_target(frame, deck, set(), set(deck), Counter()))

        policy.unremovable_types = {"coin"}
        coin_frame, coin_deck = self.frame(["coin"], rent=650, removal_tokens=5)
        self.assertFalse(policy._has_removal_target(coin_frame, coin_deck, set(), {"coin"}, Counter()))
        self.assertIsNone(policy._removal_target_rank(
            "coin", "common", 1, 0, 1, {"coin"}, Counter(coin=1), set(), due=650,
            score=1.0, deck_len=22, tokens=5, cost=1))
        policy.unremovable_types = set()

    def test_late_growth_ban_and_curse_removal(self):
        policy = BaselinePolicy(self.client, 1)
        policy.current_due = 777
        policy.current_spins_left = 5
        self.assertTrue(policy._filling_banned("ore", Counter(ore=1)))
        self.assertTrue(policy._filling_banned("archaeologist", Counter(archaeologist=1)))
        policy.current_spins_left = 6
        self.assertFalse(policy._filling_banned("ore", Counter(ore=1)))
        self.assertIsNotNone(policy._removal_target_rank(
            "hex_of_destruction", "uncommon", 3, 0, 1, {"hex_of_destruction"},
            Counter(hex_of_destruction=1), set(), due=300))
        self.assertIsNone(policy._removal_target_rank(
            "hex_of_destruction", "uncommon", 3, 0, 1, {"hex_of_destruction"},
            Counter(hex_of_destruction=1), set(), due=100))

    def test_bomb_waits_for_growth_core(self):
        policy = BaselinePolicy(self.client, 1)
        spin = Candidate(0, 1, None, 430, (0,)*6)
        bomb = Candidate(1, 3, 116, 418, (0,)*6)
        frame, _ = self.frame(["coin"], (spin, bomb), ["symbol_bomb_small"])
        self.assertEqual(policy.choose(frame)[0], spin)
        frame, _ = self.frame(["mrs_fruit"], (spin, bomb), ["symbol_bomb_small"])
        self.assertEqual(policy.choose(frame)[0], bomb)


if __name__ == "__main__":
    unittest.main()
