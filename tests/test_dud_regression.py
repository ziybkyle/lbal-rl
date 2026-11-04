"""Verify floor-20 dud observations and actions in an isolated game copy."""
from collections import Counter
import json
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from v3_client import Frame, V3Client  # noqa: E402


class DudRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="xyfd_dud_test_")
        cls.game = Path(cls.tmp.name) / "game"
        shutil.copytree(ROOT / "game", cls.game)
        driver = cls.game / "rl_v3_driver.gd"
        source = driver.read_text(encoding="utf-8")
        reset_anchor = '\t\tmain.new_game()\n'
        hook_anchor = '\tif kind == "probe_grant":'
        if source.count(reset_anchor) != 1 or source.count(hook_anchor) != 1:
            raise RuntimeError("Dud test fixture no longer matches the driver")
        source = source.replace(reset_anchor, reset_anchor +
            '\t\tmain.get_node("Coins").coins = 10000\n'
            '\t\tmain.get_node("Pop-up Sprite/Pop-up").removal_tokens = 3\n', 1)
        hooks = '''\tif kind == "test_dud_history":
\t\tvar p = main.get_node("Pop-up Sprite/Pop-up")
\t\tp.destroyed_symbol_types.append("dud")
\t\tp.removed_symbol_types.append("dud")
\t\t_emit(main)
\t\treturn
\tif kind == "test_unknown_deck":
\t\tmain.get_node("Reels").reels[0].icon_types_to_be_added.append("unknown_test_type")
\t\t_emit(main)
\t\treturn
'''
        driver.write_text(source.replace(hook_anchor, hooks + hook_anchor, 1), encoding="utf-8")
        cls.client = V3Client(game=cls.game, timeout=25)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "client"):
            cls.client.close()
        cls.tmp.cleanup()

    def reset(self):
        frame = self.client.reset("SPECIAL20", 20)
        self.assertEqual(frame.info["floor"], 20)
        self.assertEqual(frame.info["unresolved"], 0)
        self.assertFalse(frame.truncated, frame.info)
        return frame

    def select(self, frame, type_id):
        choice = next((c for c in frame.candidates if c.type_id == type_id), None)
        self.assertIsNotNone(choice, frame.candidates)
        return self.client.step(choice.index)

    def test_vocab_board_deck_neighbours_and_history(self):
        vocabulary = self.client.hello["vocab"]
        source = json.loads((self.game / "JSON" / "Symbols - JSON.json").read_text(encoding="utf-8"))
        old_types = sorted(k for k in source if k not in ("empty", "dud"))
        self.assertEqual(len(old_types), 166)
        self.assertEqual(vocabulary["symbols"][:166], old_types)
        self.assertEqual(vocabulary["symbols"][166], "dud")
        self.assertEqual(len(vocabulary["symbols"]), 168)
        self.assertEqual(len(vocabulary["rarity"]["symbols"]), 168)
        self.assertEqual(self.client.type_name(166), "dud")
        frame = self.reset()
        self.assertEqual(frame.vocab_version, 2)
        deck = Counter(t for t, valid in zip(frame.deck_types, frame.deck_mask) if valid)
        self.assertEqual(deck[166], 3)
        self.assertEqual(frame.board.count(166), 3)
        shapes = self.client.hello["shapes"]
        start = struct.calcsize("<IIBHHHffBBBHB") + 4 * shapes["N_SCALARS"]
        history = struct.unpack_from(f'<{2 * shapes["N_SYM"]}f', frame.raw, start)
        board_offset = start + 8 * shapes["N_SYM"]
        board = struct.unpack_from("<20H", frame.raw, board_offset)
        adjacent = struct.unpack_from("<160H", frame.raw, board_offset + 40)
        self.assertEqual(board, frame.board)
        for r in range(4):
            for c in range(5):
                for k, (dr, dc) in enumerate(((-1, -1), (0, -1), (1, -1), (-1, 0),
                                               (1, 0), (-1, 1), (0, 1), (1, 1))):
                    rr, cc = r + dr, c + dc
                    expected = board[rr * 5 + cc] if 0 <= rr < 4 and 0 <= cc < 5 else 410
                    self.assertEqual(adjacent[(r * 5 + c) * 8 + k], expected)
        self.assertIn(166, adjacent)
        frame = self.client.command({"cmd": "test_dud_history"})
        after = struct.unpack_from(f'<{2 * shapes["N_SYM"]}f', frame.raw, start)
        self.assertGreater(after[166], history[166])
        self.assertGreater(after[shapes["N_SYM"] + 166], history[shapes["N_SYM"] + 166])

    def test_dud_cannot_be_removed_but_can_be_swapped(self):
        frame = self.reset()
        frame = self.select(frame, 431)
        self.assertEqual(frame.kind, 1)
        self.assertFalse(any(c.kind == 2 and c.type_id == 166 for c in frame.candidates))
        self.assertTrue(any(c.kind == 2 for c in frame.candidates))
        frame = self.reset()
        self.client._send({"cmd": "probe_grant", "item": "swapping_device"})
        response = self.client._recv()
        self.assertTrue(response.get("ok"), response)
        self.client.frame = Frame.decode(self.client._recv(), self.client.hello["shapes"])
        frame = self.select(self.client.frame, 430)
        self.assertEqual(frame.kind, 3, frame.info)
        dud = next(c for c in frame.candidates if c.kind == 4 and c.type_id == 166)
        other = next(c for c in frame.candidates if c.kind == 4 and c.type_id != 166)
        board = frame.board
        frame = self.client.step(dud.index)
        other = next(c for c in frame.candidates if c.kind == 4 and c.reference == other.reference)
        frame = self.client.step(other.index)
        self.assertEqual(frame.info["swap_picks"], 2)
        self.assertEqual(frame.info["swap_noop"], 0)
        self.assertEqual(frame.board[dud.reference], board[other.reference])
        self.assertEqual(frame.board[other.reference], 166)

    def test_unknown_deck_type_is_invalid(self):
        self.reset()
        frame = self.client.command({"cmd": "test_unknown_deck"})
        self.assertTrue(frame.truncated)
        self.assertEqual(frame.outcome, "invalid")
        self.assertIn("unknown_test_type", frame.info["unknown_deck_types"])


if __name__ == "__main__":
    unittest.main()
