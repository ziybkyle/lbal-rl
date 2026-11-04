"""Regression checks for removal targets, using a temporary game copy."""
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))
from v3_client import Frame, V3Client

class RemovalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="xyfd_removal_")
        game = Path(cls.tmp.name) / "game"
        shutil.copytree(ROOT / "game", game)
        path = game / "rl_v3_driver.gd"
        s = path.read_text(encoding="utf8")
        s = s.replace('var active = false', '''func _fixture_hovers(p):
	var a = []
	for h in _deck_hover_icons(p):
		a.append([str(h.type), str(h.value_text), str(h.permanent_bonus), str(h.permanent_multiplier)])
	return a

func _fixture_reals(main):
	var a = []
	for r in main.get_node("Reels").reels:
		for ic in r.icons:
			if str(ic.type) == "coin":
				a.append([ic.permanent_bonus, ic.get_child(1).raw_string, ic.get_child(3).raw_string, ic.get_child(2).raw_string])
	return a

var active = false''')
        anchor = '\tif kind == "probe_removal":'
        fixture = '''	if kind == "fixture_removal":
		var p = main.get_node("Pop-up Sprite/Pop-up")
		var reels = main.get_node("Reels")
		p.removal_tokens = 3
		p.removal_cost = 1
		if p.emails.size() > 0:
			p.resolve_event("skip")
			_advance(main)
		if str(cmd.get("mode")) == "bonus":
			var chosen = []
			for r in reels.reels:
				for ic in r.icons:
					if ic.type != "empty" and ic.type != "dud":
						chosen.append(ic)
			var first = chosen[0]
			var second = chosen[1]
			first.change_type("cherry", false)
			first.permanent_bonus = 0
			first.update_value_text()
			second.change_type("cherry", false)
			second.permanent_bonus = 5
			second.update_value_text()
			reels.update_icon_types()
		elif str(cmd.get("mode")) == "pending":
			var r = reels.reels[0]
			var bonus = "<color_" + main.get_node("Options Sprite/Options").colors3["symbol_bonus_text"] + ">+3<end>"
			r.icon_types_to_be_added = ["goldfish", "cherry", "jellyfish"]
			r.icon_types_tba_bonus_texts = [["", "", ""], [bonus, "", ""], ["", "", ""]]
		elif str(cmd.get("mode")) == "fineprint":
			main.get_node("Landlord").fine_print = [{"num": 20}]
		p.saved_symbol_order.clear()
		p.draw_removal_prompt(true)
		cand_list = _build_candidates(main, p)
		_emit(main)
		return
	if kind == "fixture_inspect":
		var p = main.get_node("Pop-up Sprite/Pop-up")
		var r = main.get_node("Reels").reels[0]
		var vals = []
		for rr in main.get_node("Reels").reels:
			for ic in rr.icons:
				if ic.type == "coin":
					vals.append(ic.permanent_bonus)
		_send({"type": "fixture_inspect", "queue": r.icon_types_to_be_added,
			"metadata": r.icon_types_tba_bonus_texts, "coins": vals,
			"removed": p.removed_symbol_types, "tokens": p.removal_tokens, "saved": p.saved_symbol_data, "hovers": _fixture_hovers(p), "real": _fixture_reals(main)})
		_send_frame()
		return
'''
        assert s.count(anchor) == 1
        s = s.replace(anchor, fixture + anchor)
        path.write_text(s, encoding="utf8")
        cls.client = V3Client(game=game, timeout=50)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "client"):
            cls.client.close()
        if hasattr(cls, "tmp"):
            cls.tmp.cleanup()

    def setup_case(self, mode):
        self.client.reset("REMOVALTEST01", 1)
        return self.client.command({"cmd": "fixture_removal", "mode": mode})

    def inspect(self):
        c = self.client
        c._send({"cmd": "fixture_inspect"})
        info = c._recv()
        c.frame = Frame.decode(c._recv(), c.hello["shapes"])
        return info

    def candidates(self, f, name):
        return [a for a in f.candidates if a.kind == 2 and self.client.type_name(a.type_id) == name]

    def test_symbol_bonus_variants_are_distinct_and_exact(self):
        for bonus in (0, 5):
            with self.subTest(bonus=bonus):
                f = self.setup_case("bonus")
                opts = self.candidates(f, "cherry")
                self.assertEqual(len(opts), 2, (f.info, opts, self.inspect()))
                self.assertEqual({a.aux[1] for a in opts}, {0.0, 5.0})
                target = next(a for a in opts if a.aux[1] == bonus)
                self.assertIsNotNone(target.reference)
                f = self.client.step(target.index)
                self.assertEqual(f.info["removal_noop"], 0)
                self.assertEqual(f.info["removals_done"], 1)

    def test_pending_metadata_and_jellyfish_token_refund(self):
        f = self.setup_case("pending")
        goldfish = self.candidates(f, "goldfish")
        self.assertEqual(len(goldfish), 1, f.info)
        self.assertEqual(goldfish[0].aux[3], 1)
        f = self.client.step(goldfish[0].index)
        info = self.inspect()
        self.assertEqual(info["queue"], ["cherry", "jellyfish"])
        self.assertEqual(len(info["metadata"]), 2)
        self.assertIn("+3", info["metadata"][0][0])
        self.assertEqual(info["tokens"], 2)
        jelly = self.candidates(f, "jellyfish")
        self.assertEqual(len(jelly), 1, f.info)
        f = self.client.step(jelly[0].index)
        info = self.inspect()
        self.assertEqual(info["queue"], ["cherry"])
        self.assertEqual(len(info["metadata"]), 1)
        self.assertIn("+3", info["metadata"][0][0])
        self.assertEqual(info["tokens"], 2)  # spent one and jellyfish gave one back
        self.assertEqual(f.info["removal_noop"], 0)
        cherry = self.candidates(f, "cherry")
        self.assertTrue(any(abs(a.aux[1] - 3) < 0.01 for a in cherry), cherry)
        chosen = next(a for a in cherry if abs(a.aux[1] - 3) < 0.01)
        self.client.step(chosen.index)
        info = self.inspect()
        self.assertEqual(info["queue"], [])
        self.assertEqual(info["metadata"], [])

    def test_hover_coin_is_not_offered(self):
        f = self.setup_case("pending")
        self.assertIn("coin", f.info.get("unremovable_types", []))
        self.assertEqual(self.candidates(f, "coin"), [])

    def test_fine_print_exposes_unremovable_coin(self):
        f = self.setup_case("fineprint")
        self.assertIn("coin", f.info.get("unremovable_types", []))
        self.assertEqual(self.candidates(f, "coin"), [])

if __name__ == "__main__":
    unittest.main()
