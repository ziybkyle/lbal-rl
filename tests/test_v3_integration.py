"""Real-engine regression checks; runs only against a temporary game copy.

Run: python -m unittest discover -s tests -p test_v3_integration.py -v
The fixture provides coins and tokens so the same actions can be exercised on
every run. It does not touch Steam or the project's game/user-data directories.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))
from v3_client import Candidate, EngineError, Frame, V3Client  # noqa: E402
from baseline_policy import BaselinePolicy  # noqa: E402


class EngineRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="xyfd_regression_")
        cls.game = Path(cls.tmp.name) / "game"
        shutil.copytree(ROOT / "game", cls.game)
        path = cls.game / "rl_v3_driver.gd"
        source = path.read_text(encoding="utf-8")
        anchor = '\t\tmain.new_game()\n'
        if source.count(anchor) != 1:
            raise RuntimeError("Test fixture anchor no longer matches the driver")
        fixture = ('\t\tmain.new_game()\n'
                   '\t\tmain.get_node("Coins").coins = 100\n'
                   '\t\tmain.get_node("Pop-up Sprite/Pop-up").reroll_tokens = 3\n'
                   '\t\tmain.get_node("Pop-up Sprite/Pop-up").removal_tokens = 3\n')
        path.write_text(source.replace(anchor, fixture), encoding="utf-8")
        source = path.read_text(encoding="utf-8")
        anchor = '\tif kind == "probe_removal":'
        if source.count(anchor) != 1:
            raise RuntimeError("Terminal test fixture anchor no longer matches")
        terminal_fixture = '''\tif kind == "fixture_rich":
\t\tmain.get_node("Coins").coins = 100000
\t\t_emit(main)
\t\treturn
\tif kind == "fixture_symbol":
\t\tmain.get_node("Reels").add_tile([str(cmd.get("symbol"))])
\t\t_emit(main)
\t\treturn
\tif kind == "fixture_strike":
\t\tmain.get_node("Landlord").take_damage(1500)
\t\t_advance(main)
\t\t_emit(main)
\t\treturn
\tif kind == "fixture_terminal":
\t\tvar p = main.get_node("Pop-up Sprite/Pop-up")
\t\tvar stats = main.get_node("Stats Sprite/Stats")
\t\tvar landlord = main.get_node("Landlord")
\t\tp.emails.clear()
\t\tstats.killed = false
\t\tif str(cmd.get("result")) == "win":
\t\t\tp.times_rent_paid = 12
\t\t\tp.doing_boss_fight = true
\t\t\tlandlord.max_hp = 1500
\t\t\tlandlord.hp = 1
\t\t\tlandlord.die()
\t\telif str(cmd.get("result")) == "early_ending":
\t\t\tp.add_event("ending", null)
\t\telse:
\t\t\tp.add_event("game_over", null)
\t\tp.offset_y = p.offset_top
\t\tp.delay_timer = 0
\t\tcand_list = []
\t\t_advance(main)
\t\t_emit(main)
\t\treturn
'''
        path.write_text(source.replace(anchor, terminal_fixture + anchor), encoding="utf-8")
        cls.client = V3Client(game=cls.game, timeout=20, startup_timeout=40)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "client"):
            cls.client.close()
        if hasattr(cls, "tmp"):
            cls.tmp.cleanup()

    def reset(self, floor=1, seed="TEST000001"):
        frame = self.client.reset(seed, floor)
        self.assertEqual(frame.info["floor"], floor)
        self.assertEqual(frame.info["unresolved"], 0)
        return frame

    def find(self, frame: Frame, *, kind=None, type_id=None) -> Candidate:
        found = [c for c in frame.candidates
                 if (kind is None or c.kind == kind) and (type_id is None or c.type_id == type_id)]
        self.assertTrue(found, f"missing kind={kind} type={type_id}; "
                               f"email={frame.info.get('email')}, candidates={frame.candidates}")
        return found[0]

    def grant(self, name: str, *, saved=None) -> Frame:
        command = {"cmd": "probe_grant", "item": name}
        if saved is not None:
            command["saved"] = saved
        self.client._send(command)
        response = self.client._recv()
        self.assertTrue(response.get("ok"), response)
        frame = Frame.decode(self.client._recv(), self.client.hello["shapes"])
        self.client.frame = frame
        return frame

    def deck(self, frame: Frame) -> Counter:
        return Counter(t for t, visible in zip(frame.deck_types, frame.deck_mask) if visible)

    def test_01_floor_20_can_start_and_reset(self):
        for floor in (1, 20, 1, 20):
            frame = self.reset(floor)
            self.assertFalse(frame.terminated)
            self.assertFalse(frame.truncated)
            self.find(frame, type_id=430)
            if floor == 20:
                self.assertEqual(frame.info["actual_floor"], 20)
                self.assertTrue(frame.info["boss_unlocked"])
                self.assertTrue(frame.info["essences_unlocked"])
                self.assertFalse(frame.info["boss_defeated"])
                self.assertFalse(frame.info["boss"])

    def test_01b_boss_victory_loss_and_early_ending(self):
        for result, expected in (("win", "win"), ("loss", "loss"), ("early_ending", "invalid")):
            with self.subTest(result=result):
                self.reset(floor=20)
                frame = self.client.command({"cmd": "fixture_terminal", "result": result})
                self.assertEqual(frame.outcome, expected, frame.info)
                if result == "win":
                    self.assertTrue(frame.info["boss_defeated"])
                    self.assertEqual(frame.info["rents_paid"], 12)
                    self.assertLessEqual(frame.info["hp"], 0)
                else:
                    self.assertFalse(frame.info["boss_defeated"])
                self.assertFalse(frame.candidates)

    def test_01c_real_boss_transition_and_victory(self):
        self.reset(floor=20)
        frame = self.client.command({"cmd": "fixture_rich"})
        for _ in range(500):
            if frame.info["boss"]:
                break
            self.assertEqual(frame.outcome, "running", frame.info)
            priorities = (430, 413, 412, 415, 411)
            selected = next((c for t in priorities for c in frame.candidates if c.type_id == t), None)
            frame = self.client.step((selected or frame.candidates[0]).index)
        else:
            self.fail(f"Failed to reach the boss: {frame.info}")
        self.assertEqual(frame.info["rents_paid"], 12)
        self.assertEqual(frame.info["max_hp"], 1500)
        self.assertFalse(frame.info["boss_defeated"])
        frame = self.client.command({"cmd": "fixture_strike"})
        self.assertEqual(frame.outcome, "win", frame.info)
        self.assertTrue(frame.info["boss_defeated"])

    def test_02_seeded_reset_repeats_and_other_seed_differs(self):
        def signature(frame):
            return (frame.board, tuple((c.kind, c.type_id, c.reference) for c in frame.candidates),
                    frame.info["coins"], frame.info["spins"])

        def first_offer(seed):
            frame = self.reset(seed=seed)
            return self.client.step(self.find(frame, type_id=430).index)

        a = signature(first_offer("TEST000001"))
        b = signature(first_offer("TEST000001"))
        c = signature(first_offer("TEST000002"))
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_03_remove_symbol_spends_one_token(self):
        before = self.reset()
        n_before = sum(self.deck(before).values())
        tokens = before.info["removal_tokens"]
        frame = self.client.step(self.find(before, type_id=431).index)
        self.assertEqual(frame.kind, 1)
        target = self.find(frame, kind=2)
        count = self.deck(frame)[target.type_id]
        frame = self.client.step(target.index)
        self.assertEqual(frame.info["removal_tokens"], tokens - frame.info["removal_cost"])
        self.assertEqual(frame.info["removals_done"], 1)
        self.assertEqual(frame.info["removal_noop"], 0)
        self.assertEqual(sum(self.deck(frame).values()), n_before - 1)
        self.assertEqual(self.deck(frame)[target.type_id], count - 1)

    def test_03b_unremovable_hover_coin_is_not_offered(self):
        frame = self.reset()
        coin_id = self.client.hello["vocab"]["symbols"].index("coin")
        self.assertGreater(self.deck(frame)[coin_id], 0)
        frame = self.client.step(self.find(frame, type_id=431).index)
        offered = [c for c in frame.candidates if c.kind == 2 and c.type_id == coin_id]
        self.assertEqual(offered, [])
        self.assertEqual(frame.info["removal_noop"], 0)

    def test_03c_reset_clears_unfinished_removal(self):
        frame = self.reset()
        frame = self.client.step(self.find(frame, type_id=431).index)
        self.assertEqual(frame.kind, 1)
        frame = self.reset()
        frame = self.grant("credit_card", saved=6)
        before = self.deck(frame)
        frame = self.client.step(self.find(frame, type_id=430).index)
        choices = [c for c in frame.candidates if c.kind == 0]
        self.assertGreaterEqual(len(choices), 20)
        choice = choices[-1]
        frame = self.client.step(choice.index)
        self.assertEqual(self.deck(frame)[choice.type_id], before[choice.type_id] + 1)
        self.assertEqual(frame.info["unresolved"], 0)

    def test_04_reroll_changes_offer_and_spends_token(self):
        frame = self.reset()
        frame = self.client.step(self.find(frame, type_id=430).index)
        old = [c.type_id for c in frame.candidates if c.kind == 0]
        self.assertGreaterEqual(len(old), 3)
        tokens = round(frame.scalars[1] * 50)
        frame = self.client.step(self.find(frame, type_id=414).index)
        self.assertEqual(round(frame.scalars[1] * 50), tokens - 1)
        new = [c.type_id for c in frame.candidates if c.kind == 0]
        self.assertGreaterEqual(len(new), 3)
        self.assertNotEqual(old, new)

    def test_05_credit_card_offers_are_selectable(self):
        for name, saved, expected in (("credit_card", 6, 20), ("credit_card_essence", None, 30)):
            with self.subTest(item=name):
                self.reset()
                frame = self.grant(name, saved=saved)
                before = self.deck(frame)
                frame = self.client.step(self.find(frame, type_id=430).index)
                choices = [c for c in frame.candidates if c.kind == 0]
                self.assertGreaterEqual(len(choices), expected, (name, frame.info, choices))
                chosen = choices[-1]
                frame = self.client.step(chosen.index)
                self.assertGreaterEqual(
                    self.deck(frame)[chosen.type_id], before[chosen.type_id] + 1,
                    (name, self.client.type_name(chosen.type_id), frame.info,
                     self.deck(frame), before),
                )
                self.assertEqual(frame.info["unresolved"], 0)

    def test_06_oil_can_selects_column(self):
        for name, saved in (("oil_can", 4), ("oil_can_essence", None)):
            with self.subTest(item=name):
                self.reset()
                frame = self.grant(name, saved=saved)
                frame = self.client.step(self.find(frame, type_id=430).index)
                self.assertEqual(frame.kind, 2, frame.info)
                choice = self.find(frame, kind=5, type_id=424)
                frame = self.client.step(choice.index)
                self.assertEqual(frame.info["oil_can_picks"], 1)
                self.assertEqual(frame.info["oil_can_col"], 0)
                self.assertEqual(frame.info["oil_can_noop"], 0)

    def test_07_swap_accepts_two_distinct_positions(self):
        for name in ("swapping_device", "swapping_device_essence"):
            with self.subTest(item=name):
                self.reset()
                frame = self.grant(name)
                frame = self.client.step(self.find(frame, type_id=430).index)
                self.assertEqual(frame.kind, 3, frame.info)
                first = self.find(frame, kind=4)
                frame = self.client.step(first.index)
                self.assertEqual(frame.info["swap_picks"], 1)
                self.assertEqual(len(frame.info["swap_picked"]), 1)
                second = self.find(frame, kind=4)
                self.assertNotEqual(first.reference, second.reference)
                frame = self.client.step(second.index)
                self.assertEqual(frame.info["swap_picks"], 2)
                self.assertEqual(frame.info["swap_noop"], 0)

    def test_08_invalid_action_does_not_silently_advance(self):
        frame = self.reset()
        with self.assertRaises(EngineError):
            self.client.step(999)
        self.assertEqual(self.client.frame.frame_id, frame.frame_id)

    def test_08b_item_toggle_and_lunchbox_consumption(self):
        self.reset()
        frame = self.grant("lunchbox")
        frame = self.client.step(self.find(frame, type_id=430).index)
        frame = self.client.step(self.find(frame, kind=0).index)
        toggle = self.find(frame, kind=3, type_id=417)
        slot = toggle.reference - 116
        self.assertEqual(self.client.type_name(frame.item_types[slot]), "lunchbox")
        self.assertEqual(frame.item_features[slot][3], 0)
        frame = self.client.step(toggle.index)
        self.assertEqual(frame.item_features[slot][3], 1)
        self.assertFalse(any(c.kind == 3 and c.type_id == 418 for c in frame.candidates))
        frame = self.client.step(self.find(frame, kind=3, type_id=417).index)
        self.assertEqual(frame.item_features[slot][3], 0)
        frame = self.client.step(self.find(frame, kind=3, type_id=418).index)
        self.assertEqual(frame.info["unresolved"], 0)
        self.assertEqual(frame.info["email"], "add_tile")
        self.find(frame, kind=0)
        self.assertNotIn("lunchbox", {self.client.type_name(t) for t, present in
                                      zip(frame.item_types, frame.item_mask) if present})

    def test_08c_growth_core_uses_symbol_bomb(self):
        self.reset(floor=20)
        self.client.command({"cmd": "fixture_symbol", "symbol": "mrs_fruit"})
        frame = self.grant("symbol_bomb_small")
        frame = self.client.step(self.find(frame, type_id=430).index)
        for _ in range(20):
            if any(c.type_id == 430 for c in frame.candidates):
                break
            cards = [c for c in frame.candidates if c.kind == 0]
            frame = self.client.step((cards[0] if cards else frame.candidates[0]).index)
        self.assertIn("mrs_fruit", {self.client.type_name(t) for t, m in zip(frame.deck_types, frame.deck_mask) if m})
        policy = BaselinePolicy(self.client, 1)
        action, _ = policy.choose(frame)
        self.assertEqual((action.kind, action.type_id), (3, 418))
        self.assertEqual(self.client.type_name(frame.item_types[action.reference - 116]), "symbol_bomb_small")
        frame = self.client.step(action.index)
        self.assertEqual(frame.info["unresolved"], 0)
        self.assertTrue(any(c.kind == 0 and c.type_id < 168 for c in frame.candidates), frame.info)

    def test_09_comfy_pillow_and_essence(self):
        for name, before_left, rarity in (("comfy_pillow", 1, "rare"),
                                           ("comfy_pillow_essence", 2, "very_rare")):
            with self.subTest(item=name):
                self.reset()
                frame = self.grant(name)
                for _ in range(30):
                    if frame.info["email"] in ("comfy_pillow_prompt", "comfy_pillow_essence_prompt"):
                        break
                    self.assertEqual(frame.outcome, "running", frame.info)
                    cards = [c for c in frame.candidates if c.kind == 0]
                    choice = cards[0] if cards else self.find(frame, type_id=430)
                    frame = self.client.step(choice.index)
                else:
                    self.fail(f"{name} prompt did not appear")
                self.assertEqual(frame.info["spins_left"], before_left)
                self.find(frame, type_id=412)
                rents = frame.info["rents_paid"]
                frame = self.client.step(self.find(frame, type_id=411).index)
                self.assertEqual(frame.info["rents_paid"], rents + 1)
                held = {self.client.type_name(t) for t, present in zip(frame.item_types, frame.item_mask) if present}
                self.assertEqual(name in held, not name.endswith("_essence"))
                for _ in range(10):
                    if frame.info["email"] == "add_item":
                        break
                    self.assertEqual(frame.outcome, "running", frame.info)
                    frame = self.client.step(self.find(frame, kind=0).index)
                else:
                    self.fail(f"no item offer after using {name}")
                vocabulary = self.client.hello["vocab"]
                choices = [c for c in frame.candidates if c.kind == 0 and 168 <= c.type_id < 282]
                rarities = [vocabulary["rarity"]["items"][c.type_id - 168] for c in choices]
                self.assertIn(rarity, rarities)


if __name__ == "__main__":
    unittest.main(verbosity=2)
