"""Run special-item regressions against an isolated copy of the v3 game.

Run with ``python -m unittest discover -s godot_kernel/tests -p test_special_regression.py``.
The debug hooks below are inserted only into the temporary game copy.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import unittest


KERNEL = Path(__file__).resolve().parents[1]
ENGINE = KERNEL / "runtime" / "godot.exe"
GAME = KERNEL / "game"

AUDIT_HOOK = '''
func _special_test_audit(main):
\tvar p = main.get_node("Pop-up Sprite/Pop-up")
\tvar item_data = []
\tfor i in main.get_node("Items").items:
\t\tif is_instance_valid(i):
\t\t\titem_data.append({"type": str(i.type), "saved": i.saved_value})
\tvar board = []
\tfor row in main.get_node("Reels").displayed_icons:
\t\tvar cells = []
\t\tfor ic in row:
\t\t\tcells.append(str(ic.type) if is_instance_valid(ic) else "null")
\t\tboard.append(cells)
\tvar candidates = []
\tfor c in cand_list:
\t\tcandidates.append({"reply": str(c["reply"]), "ck": int(c["ck"]), "ref": int(c["ref"])})
\treturn {"items": item_data, "board": board, "candidates": candidates,
\t\t"email_text": str(p.emails[0].text) if p.emails.size() > 0 else ""}

'''
RICH_HOOK = '''\tif kind == "special_test_rich":
\t\tmain.get_node("Coins").coins = 10000
\t\t_send({"type": "special_test_rich", "ok": true})
\t\t_send_frame()
\t\treturn
'''


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise RuntimeError(f"Expected exactly one test insertion point: {old!r}")
    return text.replace(old, new, 1)


class SpecialItemRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not ENGINE.is_file():
            raise FileNotFoundError(f"Godot runtime not found: {ENGINE}")
        cls.temp = tempfile.TemporaryDirectory(prefix="lbal-special-regression-")
        cls.root = Path(cls.temp.name)
        cls.game = cls.root / "game"
        shutil.copytree(GAME, cls.game)
        driver = cls.game / "rl_v3_driver.gd"
        text = driver.read_text(encoding="utf-8")
        text = replace_once(text, "func _emit(main):", AUDIT_HOOK + "func _emit(main):")
        text = replace_once(text, '\tif kind == "probe_grant":', RICH_HOOK + '\tif kind == "probe_grant":')
        text = replace_once(text, '"income_trace": trace,',
                            '"special_test": _special_test_audit(main), "income_trace": trace,')
        driver.write_text(text, encoding="utf-8", newline="")
        cls.items_db = json.loads((cls.game / "JSON" / "Items - JSON.json").read_text(encoding="utf-8"))
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        env = os.environ.copy()
        env.update(RL_PROTO="3", RL_PORT=str(port), RL_SEED="SPECIAL", RL_HEADLESS="1",
                   APPDATA=str(cls.root / "appdata"), LOCALAPPDATA=str(cls.root / "appdata"))
        cls.log_path = cls.root / "godot.log"
        cls.log = cls.log_path.open("w", encoding="utf-8")
        cls.proc = subprocess.Popen([str(ENGINE), "--path", str(cls.game), "--no-window",
                                     "--audio-driver", "Dummy"], env=env,
                                    stdout=cls.log, stderr=subprocess.STDOUT)
        cls.sock = None
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if cls.proc.poll() is not None:
                    raise RuntimeError(f"Godot exited during startup ({cls.proc.returncode})")
                try:
                    cls.sock = socket.create_connection(("127.0.0.1", port), timeout=1)
                    break
                except OSError:
                    time.sleep(.1)
            if cls.sock is None:
                raise TimeoutError("Godot did not open the test port")
            cls.sock.settimeout(30)
            cls.stream = cls.sock.makefile("rwb", buffering=0)
            hello = cls._receive()
            if hello.get("type") != "hello":
                raise RuntimeError(f"Unexpected handshake: {hello}")
        except Exception:
            cls._close()
            raise

    @classmethod
    def _close(cls):
        if getattr(cls, "sock", None):
            cls.sock.close()
        if getattr(cls, "proc", None) and cls.proc.poll() is None:
            cls.proc.terminate()
            cls.proc.wait(timeout=10)
        if getattr(cls, "log", None):
            cls.log.close()

    @classmethod
    def tearDownClass(cls):
        cls._close()
        log = cls.log_path.read_text(encoding="utf-8", errors="replace")
        cls.temp.cleanup()
        # Godot's recovered UI emits known Node lookup warnings. Treat actual
        # script errors, parsing failures, and the driver's fatal messages as failures.
        fatal_lines = [line for line in log.splitlines()
                       if any(tag in line for tag in ("SCRIPT ERROR", "Parse Error", "[RL] FATAL"))]
        if fatal_lines:
            raise AssertionError("Godot reported script errors:\n" + "\n".join(fatal_lines[:15]))

    @classmethod
    def _receive(cls):
        raw = cls.stream.readline()
        if not raw:
            raise RuntimeError(f"Godot disconnected (exit code {cls.proc.poll()})")
        msg = json.loads(raw)
        if msg.get("type") == "error":
            raise RuntimeError(f"Godot rejected the command: {msg}")
        return msg

    @classmethod
    def command(cls, **payload):
        cls.stream.write((json.dumps(payload) + "\n").encode())
        msg = cls._receive()
        if payload["cmd"] not in ("reset", "step"):
            frame = cls._receive()
            if frame.get("type") != "frame":
                raise RuntimeError(f"Expected frame after command: {frame}")
            return msg, frame
        if msg.get("type") != "frame":
            raise RuntimeError(f"Expected frame: {msg}")
        return msg

    def reset(self, grants):
        frame = self.command(cmd="reset", seed="SPECIAL", floor=1)
        for name, saved in grants:
            response, _ = self.command(cmd="probe_grant", item=name, saved=saved)
            self.assertTrue(response.get("ok"), response)
        return frame

    def step(self, index=0):
        return self.command(cmd="step", a=index)

    def until(self, frame, email):
        for _ in range(50):
            info = frame["info"]
            self.assertEqual(info["unresolved"], 0, info)
            if info["email"] == email:
                return frame
            self.assertNotIn(info["email"], ("game_over", "out_of_money"), info)
            frame = self.step()
        self.fail(f"Did not reach {email}")

    @staticmethod
    def items(frame):
        return {i["type"]: i["saved"] for i in frame["info"]["special_test"]["items"]}

    def take_prompt(self, accept, swap):
        frame = self.step(1 if accept else 0)
        if accept and swap:
            frame = self.step(1)
        return frame

    def test_oil_and_swap(self):
        for prefix, email, swap in (("oil_can", "oil_can_prompt", False),
                                    ("swapping_device", "swap_prompt_1", True)):
            essence = prefix + "_essence"
            saved = 0 if swap else 4
            for active in (True, False):
                for item in (prefix, essence):
                    with self.subTest(item=item, active=active):
                        frame = self.until(self.reset([(item, saved if item == prefix else 0)]), email)
                        info = frame["info"]
                        before = info["coins"]
                        board = info["special_test"]["board"]
                        frame = self.take_prompt(active, swap)
                        if item == essence:
                            self.assertEqual(self.items(frame).get(essence), int(active))
                        gain = 7 if active and swap and item == essence else 6 if active and not swap else 5
                        self.assertEqual(frame["info"]["coins"], before + gain)
                        if active and swap:
                            result = frame["info"]["special_test"]["board"]
                            self.assertEqual((result[0][2], result[0][3]), (board[0][3], board[0][2]))
            for initial in (0, 6):
                with self.subTest(item=prefix, both=True, initial=initial):
                    frame = self.until(self.reset([(prefix, saved), (essence, initial)]), email)
                    self.assertNotIn("_essence>", frame["info"]["special_test"]["email_text"])
                    frame = self.take_prompt(True, swap)
                    self.assertEqual(self.items(frame)[essence], initial)
                    self.assertIn("_essence>", frame["info"]["special_test"]["email_text"])
                    frame = self.take_prompt(True, swap)
                    if initial == 6:
                        self.assertNotIn(essence, self.items(frame))
                    else:
                        self.assertEqual(self.items(frame)[essence], 1)
            frame = self.until(self.reset([(prefix, saved), (essence, 0)]), email)
            frame = self.take_prompt(False, swap)
            frame = self.take_prompt(False, swap)
            self.assertEqual(self.items(frame)[essence], 0)

    def test_pillow(self):
        for item in ("comfy_pillow", "comfy_pillow_essence"):
            essence = item.endswith("_essence")
            for accept in (True, False):
                with self.subTest(item=item, accept=accept):
                    frame = self.reset([(item, 0)])
                    self.command(cmd="special_test_rich")
                    frame = self.until(frame, item + "_prompt")
                    info = frame["info"]
                    before = info["spins"]
                    before_rents = info["rents_paid"]
                    before_left = info["spins_left"]
                    self.assertEqual(info["spins_left"], 2 if essence else 1)
                    wanted = "<icon_confirm>" if accept else "<icon_deny>"
                    index = next(i for i, c in enumerate(info["special_test"]["candidates"])
                                 if c["reply"] == wanted)
                    frame = self.step(index)
                    self.assertEqual(frame["info"]["spins"], before)
                    self.assertEqual(item not in self.items(frame), essence and accept)
                    if accept:
                        frame = self.until(frame, "add_item")
                        offers = [c["reply"] for c in frame["info"]["special_test"]["candidates"]]
                        rarity = "very_rare" if essence else "rare"
                        self.assertTrue(any(self.items_db.get(o, {}).get("rarity") == rarity for o in offers), offers)
                    else:
                        self.assertEqual(frame["info"]["rents_paid"], before_rents)
                        self.assertEqual(frame["info"]["spins_left"], before_left)
                        candidates = frame["info"]["special_test"]["candidates"]
                        spin = next(i for i, c in enumerate(candidates) if c["reply"] == "spin")
                        frame = self.step(spin)
                        self.assertEqual(frame["info"]["spins"], before + 1)
                        self.assertIn(item, self.items(frame))


if __name__ == "__main__":
    unittest.main()
