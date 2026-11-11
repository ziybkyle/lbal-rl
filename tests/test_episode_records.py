"""Exercise real floor-20 episode records and the imported association hints."""
import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
from collections import Counter
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from baseline_policy import BaselinePolicy
from run_baseline import run_game
from report_batch import removal_choices, render_batch
from v3_client import Candidate, Frame, V3Client


class EpisodeRecordTests(unittest.TestCase):
    def test_community_priors_feeding_and_removal(self):
        names = {0: "dog", 1: "mouse", 2: "dwarf", 3: "coin", 4: "toddler",
                 5: "candy", 6: "cat", 7: "milk", 8: "bar_of_soap", 9: "coal", 10: "cow",
                 168: "quigley_the_wolf", 169: "ordinary_item",
                 170: "green_pepper", 171: "rare_item", 282: "green_pepper_essence", 283: "rare_item_essence",
                 284: "copycat_essence",
                 430: "spin", 431: "remove", 412: "cancel", 413: "skip", 414: "reroll"}
        class Client:
            game = ROOT / "game"
            hello = {"vocab": {"items": ["quigley_the_wolf", "ordinary_item", "green_pepper", "rare_item"],
                     "rarity": {"symbols": ["common"] * 168,
                                "items": ["common", "common", "common", "rare"],
                                "essences": ["essence", "essence", "essence"]}}}
            def type_name(self, value):
                return names[value]
        policy = BaselinePolicy(Client(), 2)
        def candidate(tid, kind=0, value=0):
            return Candidate(tid, kind, None, tid, (value, 0, 1, 0, 0, 0))
        def frame(options, deck=(), count=None, due=25):
            return SimpleNamespace(candidates=options, kind=0, deck_types=tuple(deck),
                deck_uids=tuple(range(1, len(deck) + 1)),
                deck_mask=(True,) * len(deck), deck_features=((1, 0, 1, 0),) * len(deck),
                item_types=(), item_features=(), item_mask=(),
                info={"deck_len": len(deck) if count is None else count, "spins": 1, "rent_due": due})
        selected, _ = policy.choose(frame([candidate(0), candidate(1), candidate(2)]))
        self.assertEqual(selected.type_id, 1)  # mouse's guide score beats dog and dwarf
        selected, _ = policy.choose(frame([candidate(168), candidate(169)]))
        self.assertEqual(selected.type_id, 168)  # early community item prior
        self.assertAlmostEqual(policy._feeding_estimate("candy", Counter(toddler=1), 19), 6 * 11 / 38)
        self.assertLess(policy._feeding_estimate("candy", Counter(toddler=1), 20), 6 * 11 / 38)
        selected, _ = policy.choose(frame([candidate(430, 1), candidate(431, 1)], [3, 4]))
        self.assertEqual(selected.type_id, 431)  # use removal even before 20 symbols
        selected, _ = policy.choose(frame([candidate(3, 2, 1), candidate(4, 2, 2), candidate(412, 1)], [3, 4]))
        self.assertEqual(selected.type_id, 3)
        self.assertIsNotNone(policy._removal_target_rank("coin", "common", 1, 0, 1,
                                                         {"coin"}, Counter(coin=1), set()))
        self.assertIsNone(policy._removal_target_rank("bronze_arrow", "uncommon", 0, 0, 1,
                                                      {"bronze_arrow"}, Counter(bronze_arrow=1), set()))
        self.assertLess(policy._removal_target_rank("flower", "common", 1, 0, 1,
                                                    {"flower"}, Counter(flower=1), set()),
                        policy._removal_target_rank("milk", "common", 1, 0, 1,
                                                    {"milk"}, Counter(milk=1), set()))
        self.assertEqual(policy._rarity_rank(candidate(282)), 0)
        self.assertEqual(policy._rarity_rank(candidate(284)), 0)
        self.assertEqual(policy._association_score("copycat_essence", {"cat"}, Counter(cat=1), set())[0], 0)
        self.assertGreater(policy._association_score("copycat_essence", {"cat"}, Counter(cat=5), set())[0], 0)
        policy.client.hello["vocab"]["rarity"]["symbols"][2] = "rare"
        selected, _ = policy.choose(frame([candidate(0), candidate(2)]))
        self.assertEqual(selected.type_id, 2)  # a rare symbol outranks a common symbol
        self.assertGreater(policy._filling_base_score("coal"), policy._filling_base_score("cheese"))
        self.assertTrue(policy._is_transient("milk", Counter(cat=1)))
        self.assertFalse(policy._is_transient("milk", Counter()))
        self.assertTrue(policy._is_transient("bubble", Counter()))
        self.assertEqual(policy._feeding_priority("milk", Counter(cat=1), 15, 300), 1)
        self.assertEqual(policy._feeding_priority("milk", Counter(cat=1), 15, 375), 0)
        self.assertEqual(policy._feeding_priority("bar_of_soap", Counter(toddler=1), 15, 150), 1)
        selected, _ = policy.choose(frame([candidate(9), candidate(0)], [9, 9]))
        self.assertEqual(selected.type_id, 9)  # a third coal is still allowed
        selected, _ = policy.choose(frame([candidate(9), candidate(3)], [9, 9, 9], count=20, due=300))
        self.assertEqual(selected.type_id, 3)  # a fourth coal is not allowed after the board is full
        selected, _ = policy.choose(frame([candidate(5), candidate(413, 1), candidate(414, 1)], count=22, due=375))
        self.assertEqual(selected.type_id, 413)  # keep rerolls for the rent reward
        selected, _ = policy.choose(frame([candidate(5), candidate(413, 1), candidate(414, 1)], count=22, due=150))
        self.assertEqual(selected.type_id, 413)  # preserve a reroll during the early game
        policy = BaselinePolicy(Client(), 3)
        selected, _ = policy.choose(frame([candidate(9), candidate(0)]))
        self.assertEqual(selected.type_id, 9)  # first card may be coal
        selected, _ = policy.choose(frame([candidate(9), candidate(0)], [9]))
        self.assertEqual(selected.type_id, 0)  # second card cannot be coal
        selected, _ = policy.choose(frame([candidate(9), candidate(0)], [9, 0]))
        self.assertEqual(selected.type_id, 9)  # no restriction from the third card
        policy.client.hello["vocab"]["rarity"]["symbols"][10] = "rare"
        selected, _ = policy.choose(frame([candidate(10), candidate(1)]))
        self.assertEqual(selected.type_id, 1)  # cow without cat is banned
        selected, _ = policy.choose(frame([candidate(10), candidate(1)], [6]))
        self.assertEqual(selected.type_id, 10)  # cow allowed once a cat is owned

    def test_association_colors_are_soft_hints(self):
        # Initialization reads the local exported data, not the user's desktop.
        class Client:
            game = ROOT / "game"
        policy = BaselinePolicy(Client(), 1)
        self.assertGreater(policy._association_score("rabbit", {"moon"})[0], 0)
        self.assertLess(policy._association_score("coal", {"hex_of_destruction"})[0], 0)
        self.assertEqual(policy._association_score("ninja", {"mouse"})[0], 0)
        self.assertLess(policy._association_score("general_zaroff", {"zaroffs_contract"})[0], 0)
        self.assertEqual(policy._association_score("banana", {"dog"})[0], 0)
        bonus, _ = policy._association_score("green_pepper", {"coin"}, {"coin": 3}, set())
        self.assertGreater(bonus, 0)

    def test_removal_choices_keep_order_and_ignore_automatic_events(self):
        def decision(step, kind, name):
            return {"record": "decision", "step": step, "chosen": {"kind": kind, "name": name},
                    "state": {"info": {"spins": step, "rent_due": 150}}}
        records = [decision(0, 2, "coin"), decision(1, 0, "milk"),
                   {"record": "automatic", "kind": "destroy"}, decision(2, 2, "cherry")]
        removals = removal_choices(records)
        self.assertEqual([r["name"] for r in removals], ["coin", "cherry"])
        self.assertEqual([r["step"] for r in removals], [1, 3])

    def test_real_floor20_episode_has_reconstructible_decisions(self):
        with tempfile.TemporaryDirectory(prefix="xyfd_record_test_") as tmp, V3Client(timeout=30) as client:
            path = Path(tmp) / "game.jsonl"
            result = run_game(client, path, "B2AG00000000", "heuristic", 42 * 1000003, 1000)
            self.assertIn(result["outcome"], ("win", "loss"), result)
            self.assertIsNone(result["error"])
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            decisions = [r for r in records if r["record"] == "decision"]
            self.assertEqual(len(decisions), result["decisions"])
            self.assertGreater(len(decisions), 10)
            self.assertEqual(records[0]["floor"], 20)
            self.assertEqual(records[-1]["summary"], result)
            self.assertEqual(records[-2]["record"], "terminal_frame")
            terminal = records[-2]["state"]
            self.assertEqual(terminal["outcome"], result["outcome"])
            self.assertTrue(terminal["info"]["boss_unlocked"])
            self.assertTrue(terminal["info"]["essences_unlocked"])
            self.assertEqual(terminal["info"]["actual_floor"], 20)
            self.assertEqual(len(result["rent_events"]), result["rents_paid"])
            for record in decisions:
                state = record["state"]
                frame = Frame.decode({"type": "frame", "blob": state["raw_frame_base64"],
                                      "info": state["info"]}, client.hello["shapes"])
                self.assertEqual(frame.frame_id, state["frame_id"])
                self.assertIn(record["chosen"]["index"], [c.index for c in frame.candidates])
                self.assertEqual(base64.b64encode(frame.raw).decode(), state["raw_frame_base64"])
            events = [event for r in records for event in r.get("state", {}).get("info", {}).get("events", [])]
            self.assertEqual(sum(e["kind"] == "spin" for e in events), result["spins"])
            self.assertTrue(any(e["kind"] == "coin_sum" for e in events))
            root = Path(tmp)
            (root / "manifest.json").write_text(json.dumps({"policy": "heuristic", "seed": 42}), encoding="utf-8")
            (root / "summary.jsonl").write_text(json.dumps(result) + "\n", encoding="utf-8")
            (root / "batch_summary.json").write_text(json.dumps({"valid_win_rate": 0, "valid_games": 1,
                                                                 "excluded_games": 0}), encoding="utf-8")
            render_batch(root)
            self.assertIn("game.html", (root / "report.html").read_text(encoding="utf-8"))
            self.assertIn("逐次决策", (root / "game.html").read_text(encoding="utf-8"))
            self.assertIn("删除符号顺序", (root / "game.html").read_text(encoding="utf-8"))
            self.assertIn("删除符号顺序", (root / "report.html").read_text(encoding="utf-8"))
            choice_report = (root / "choice_stats.html").read_text(encoding="utf-8")
            self.assertIn('<button type="button">25</button>', choice_report)
            self.assertIn("选择次数/出现次数", choice_report)
            self.assertIn("stage-sort-mode", choice_report)
            self.assertIn("data-offers=", choice_report)


if __name__ == "__main__":
    unittest.main()
