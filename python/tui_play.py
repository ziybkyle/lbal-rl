"""tui_play — human-playable TUI over the native Godot kernel (acceptance demo).

**LEGACY**: this drives the old v1/v2 six-command protocol (`GodotEnv`).  The
current env is protocol v3; play that with the graphical TUI instead:

    .venv/Scripts/python.exe -m env.native.tui [seed] [--px 8] [--no-color]

Kept because it exercises the `GodotEnv` spawn/act chain (see --demo), which is
still the regression path for the old client.

Run from Windows (spawns the engine in WSL automatically):
    python godot_kernel/python/tui_play.py [seed]

Keys:
    SPACE        spin, confirm an email, or pay rent
    1-9, 0       pick email card (when cards shown) or reply
    x            "skip" reply (add_tile / add_item prompts)
    r            reroll (if a reroll_pay reply is offered)
    n            new run, same seed     u    new run, new random seed
    d            toggle deck panel      q    quit
    DOWN         manage items between decisions
    arrows       select an item
    1            enable or disable the selected item
    SPACE        destroy the selected item; press SPACE again to confirm
"""
from __future__ import annotations

import random
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor"))
import curses

sys.path.insert(0, str(Path(__file__).resolve().parent))
from godot_env import GodotEnv, GodotError  # noqa: E402

SEED_CHARS = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"

REPLY_LABELS = {
    "pay_reply": "Pay Rent",
    "skip": "Skip",
    "reroll_pay": "Reroll",
    "retry": "Retry",
    "main_menu": "Main Menu",
    "<icon_confirm>": "Confirm",
    "<icon_deny>": "Deny",
}


def label_reply(r: str) -> str:
    if r in REPLY_LABELS:
        return REPLY_LABELS[r]
    if r.startswith("<icon_") and r.endswith(">"):
        return "Use " + r[6:-1].replace("_", " ")
    return r.replace("_", " ").title()


def pretty(type_name: str) -> str:
    return type_name.replace("_", " ")


def space_reply(email):
    """Confirm explicit prompts without silently choosing a card or declining."""
    if email.get("cards"):
        return None
    replies = email.get("replies", [])
    for reply in ("pay_reply", "<icon_confirm>"):
        if reply in replies:
            return reply
    return None


class Tui:
    def __init__(self, stdscr, env: GodotEnv, seed: str):
        self.scr = stdscr
        self.env = env
        self.seed = seed
        self.msg = ""
        self.show_deck = True
        self.running = True
        self.item_menu = False
        self.item_index = 0
        self.confirm_destroy = False
        curses.curs_set(0)
        self.scr.nodelay(False)
        try:
            curses.start_color()
            curses.use_default_colors()
        except curses.error:
            pass
        curses.init_pair(1, curses.COLOR_GREEN, -1)     # uncommon
        curses.init_pair(2, curses.COLOR_YELLOW, -1)    # rare
        curses.init_pair(3, curses.COLOR_MAGENTA, -1)   # very rare
        curses.init_pair(4, curses.COLOR_CYAN, -1)      # header / accents
        curses.init_pair(5, curses.COLOR_RED, -1)       # danger

    # ------------------------------------------------------------ render

    def put(self, y, x, text, attr=0):
        h, w = self.scr.getmaxyx()
        if 0 <= y < h and x < w:
            try:
                self.scr.addnstr(y, x, text, w - x - 1, attr)
            except curses.error:
                pass

    def render(self, st):
        if getattr(self, "scr", None) is None:
            return  # headless (demo) mode
        if self.item_menu:
            self.render_items(st)
            return
        self.scr.erase()
        phase = st.get("phase", "?")
        hp = ""
        if st.get("max_hp", 0) and st.get("floor", 0) >= 12:
            hp = f"  HP {st['hp']}/{st['max_hp']}"
        self.put(0, 0, " LUCK BE A LANDLORD — native Godot kernel (TUI) ", curses.A_REVERSE)
        self.put(
            1, 0,
            f"seed {st.get('seed','?')}  floor {st.get('floor')}  "
            f"${st.get('coins', 0)}  rent {st.get('rent')} due in {st.get('spins_left')} spins  "
            f"spins {st.get('spins')}{hp}  [{phase}]",
            curses.color_pair(4) | curses.A_BOLD,
        )
        self.put(2, 0, "─" * 100)

        board = st.get("board", [])
        y = 4
        if board:
            self.put(y - 1, 0, "BOARD (4x5, col-major board[col][row 0..3]):", curses.A_BOLD)
            for row in range(4):
                line = []
                for col in range(min(5, len(board))):
                    cell = board[col][row] if row < len(board[col]) else "empty"
                    if cell == "empty":
                        line.append(" ·          ")
                    else:
                        line.append(f" {pretty(cell):<10} ")
                self.put(y + row, 0, "|".join(line))
            self.put(y + 4, 0, "─" * 100)

        items = st.get("items", [])
        iy = y - 1
        self.put(iy, 64, "ITEMS:", curses.A_BOLD)
        for i, it in enumerate(items[:14]):
            mark = "[x] " if it.get("disabled") else ""
            attr = curses.A_DIM if it.get("disabled") else 0
            self.put(iy + 1 + i, 64, f"{mark}{pretty(it['type'])}", attr)

        dy = y + 6
        if self.show_deck:
            deck = Counter(st.get("deck", []))
            txt = "  ".join(f"{pretty(k)}×{v}" for k, v in sorted(deck.items()))
            self.put(dy, 0, "DECK: " + txt, curses.A_DIM)
            dy += 1

        # email panel
        ey = dy + 1
        email = st.get("email") or {}
        if email:
            self.put(ey, 0, f"EMAIL: {pretty(email.get('type','?'))}", curses.A_BOLD | curses.color_pair(4))
            ey += 1
            cards = email.get("cards", [])
            for i, c in enumerate(cards[:10]):
                r = str(c.get("rarity", ""))
                attr = {"common": 0, "uncommon": 1, "rare": 2, "very_rare": 3}.get(r, 0)
                attr = curses.color_pair(attr) | curses.A_BOLD
                kind = "item" if c.get("item") else r or "symbol"
                self.put(ey + i, 2, f"[{(i+1)%10}] {pretty(c['type'])}  ({kind})", attr)
            if cards:
                ey += len(cards[:10])
            replies = email.get("replies", [])
            if replies:
                for i, r in enumerate(replies[:6]):
                    number = len(cards) + i
                    key = str((number + 1) % 10) if number < 10 else "-"
                    shortcut = " [x]" if r == "skip" else " [r]" if r == "reroll_pay" else ""
                    if r == space_reply(email):
                        shortcut += " [SPACE]"
                    self.put(ey, 2, f"({key}) {label_reply(r)}{shortcut}  ")
                    ey += 1
            hint = "number: choose   x: skip   r: reroll"
            if space_reply(email):
                hint += "   SPACE: confirm/pay"
            self.put(ey, 2, hint, curses.A_DIM)
            ey += 1
        elif phase == "title":
            self.put(ey, 0, "Title screen — press n to start a run (same seed) or u for a new seed.", curses.A_BOLD)
            ey += 1
        elif phase in ("game_over", "out_of_money"):
            self.put(ey, 0, f"*** RUN OVER ({phase}) at {st.get('spins')} spins — n: retry same seed   u: new seed   q: quit ***",
                     curses.color_pair(5) | curses.A_BOLD)
            ey += 1
        elif phase == "ending" or phase == "win":
            self.put(ey, 0, "*** YOU WIN — landlord defeated! n: play again   q: quit ***", curses.color_pair(1) | curses.A_BOLD)
            ey += 1
        else:
            self.put(ey, 0, "No email pending — SPACE to spin, n new run, d deck, q quit.", curses.A_DIM)
            ey += 1

        my = self.scr.getmaxyx()[0] - 3
        if self.msg:
            self.put(my, 0, self.msg[:96], curses.A_BOLD)
        self.put(my + 1, 0, "SPACE=spin/confirm/pay  1-9=pick  Down=items  x=skip  r=reroll", curses.A_DIM)
        self.put(my + 2, 0, "n=new(same seed)  u=new(random seed)  d=deck  q=quit", curses.A_DIM)
        self.scr.refresh()

    def render_items(self, st):
        self.scr.erase()
        items = st.get("items", [])
        height, _ = self.scr.getmaxyx()
        rows = max(1, height - 8)
        self.put(0, 0, " ITEM MENU ", curses.A_REVERSE)
        self.put(1, 0, "Arrows: select   1: enable/disable   SPACE: destroy   q: back")
        self.put(2, 0, "Destroy works only for eligible, enabled items; the game checks its rules.", curses.A_DIM)
        start = max(0, self.item_index - rows + 1)
        for n in range(start, min(len(items), start + rows)):
            item = items[n]
            status = "DISABLED" if item.get("disabled") else "enabled"
            can_destroy = "  destroyable" if item.get("destroyable") else ""
            attr = curses.A_REVERSE if n == self.item_index else 0
            self.put(4 + n - start, 0, f"{n + 1}. {pretty(item['type'])}  [{status}]{can_destroy}", attr)
        if not items:
            self.put(4, 0, "No items yet.")
        if self.confirm_destroy and items:
            self.put(height - 3, 0, f"Destroy {pretty(items[self.item_index]['type'])}? SPACE=confirm, other key=cancel", curses.A_BOLD)
        elif self.msg:
            self.put(height - 3, 0, self.msg, curses.A_BOLD)
        self.scr.refresh()

    def act_items(self, key, st):
        items = st.get("items", [])
        self.item_index = min(self.item_index, max(0, len(items) - 1))
        c = chr(key).lower() if 32 <= key < 127 else ""
        if key == curses.KEY_RESIZE:
            return st
        if self.confirm_destroy:
            self.confirm_destroy = False
            if c == " " and items:
                it = items[self.item_index]
                st = self.env.item_action(self.item_index, it["type"], "destroy")
                self.item_menu = False
                self.msg = f"Used {pretty(it['type'])}"
            else:
                self.msg = "Destroy cancelled."
            return st
        if c == "q" or key == 27:
            self.item_menu = False
            self.msg = ""
        elif key in (curses.KEY_UP, curses.KEY_LEFT):
            self.item_index = max(0, self.item_index - 1)
        elif key in (curses.KEY_DOWN, curses.KEY_RIGHT):
            self.item_index = min(max(0, len(items) - 1), self.item_index + 1)
        elif c == "1" and items:
            it = items[self.item_index]
            st = self.env.item_action(self.item_index, it["type"], "toggle")
            self.msg = "Item enabled" if it.get("disabled") else "Item disabled"
        elif c == " " and items:
            it = items[self.item_index]
            if it.get("disabled"):
                self.msg = "Enable this item before destroying it."
            elif not it.get("destroyable"):
                self.msg = "This item cannot be manually destroyed."
            else:
                self.confirm_destroy = True
        return st

    # ------------------------------------------------------------ actions

    def act(self, key, st):
        email = st.get("email") or {}
        cards = email.get("cards", [])
        replies = email.get("replies", [])
        try:
            if getattr(self, "item_menu", False):
                return self.act_items(key, st)
            if key in (curses.KEY_RESIZE,):
                return st
            c = chr(key) if 32 <= key < 127 else ""
            if c == " " and email:
                reply = space_reply(email)
                if reply is not None:
                    self.msg = f"reply {reply}"
                    self.render(st)
                    return self.env.choose(reply)
                self.msg = "Choose an option with a number."
                return st
            if c in (" ", "s"):
                if st.get("can_spin"):
                    self.msg = "spinning..."
                    self.render(st)
                    return self.env.spin()
                self.msg = "cannot spin now"
                return st
            if c == "q":
                self.running = False
                return st
            if c == "d":
                self.show_deck = not self.show_deck
                return st
            if key == curses.KEY_DOWN:
                if st.get("phase") != "idle":
                    self.msg = "Finish the current prompt before managing items."
                else:
                    self.item_menu = True
                    self.item_index = 0
                    self.confirm_destroy = False
                    self.msg = ""
                return st
            if c == "n":
                self.msg = "starting new run (same seed)..."
                self.render(st)
                return self.env.start(seed=self.seed, floor=st.get("floor", 1))
            if c == "u":
                self.seed = "".join(random.choice(SEED_CHARS) for _ in range(10))
                self.msg = f"new seed {self.seed}"
                self.render(st)
                return self.env.start(seed=self.seed, floor=1)
            if c.isdigit():
                i = int(c) - 1 if c != "0" else 9
                if cards and 0 <= i < len(cards):
                    self.msg = f"picked {cards[i]['type']}"
                    self.render(st)
                    return self.env.choose(cards[i]["type"])
                reply_index = i - len(cards)
                if replies and 0 <= reply_index < len(replies):
                    self.msg = f"reply {replies[reply_index]}"
                    self.render(st)
                    return self.env.choose(replies[reply_index])
                self.msg = "no such option"
                return st
            if c == "x":
                if "skip" in replies:
                    return self.env.choose("skip")
                self.msg = "no skip available"
                return st
            if c == "r":
                if "reroll_pay" in replies:
                    return self.env.choose("reroll_pay")
                self.msg = "no reroll available"
                return st
            if key == curses.KEY_UP or key == 10:
                return self.env.get_state()
        except GodotError as e:
            self.msg = f"error: {e}"
            try:
                return self.env.get_state()
            except Exception:
                return st
        return st

    def run(self):
        self.msg = "starting run..."
        self.scr.refresh()
        st = self.env.start(seed=self.seed)
        while self.running:
            self.render(st)
            key = self.scr.getch()
            if key == -1:
                continue
            st = self.act(key, st)


def main():
    seed = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "".join(random.choice(SEED_CHARS) for _ in range(10))
    if "--demo" in sys.argv:
        return demo(seed)
    env = GodotEnv(seed=seed)
    try:
        curses.wrapper(lambda scr: Tui(scr, env, seed).run())
    finally:
        env.close()


def demo(seed):
    """Headless replay of the same TUI action logic (no curses): proves the
    Windows spawn/TCP/act chain end-to-end. Keys: 1=pick/reply, space=spin."""
    env = GodotEnv(seed=seed)
    tui = Tui.__new__(Tui)  # act() only needs .env/.msg
    tui.env = env
    tui.scr = None
    tui.msg = ""
    tui.show_deck = True
    tui.running = True
    try:
        st = env.start()
        print(f"start ok: phase={st['phase']} coins={st['coins']} rent={st['rent']}")
        keys = ["1"] * 8 + [" ", "1", " ", "1", " ", "1", " ", "1", "1", " ", "1"]
        for k in keys:
            if st["phase"] in ("game_over", "out_of_money"):
                st = env.choose("retry")
                print("  -> retried")
                continue
            st = tui.act(ord(k), st)
            e = st.get("email") or {}
            desc = e.get("type") or st["phase"]
            cards = [c["type"] for c in e.get("cards", [])]
            print(f"key {k!r}: phase={st['phase']} email={desc} cards={cards[:3]} "
                  f"coins={st['coins']} spins={st['spins']}")
        print("DEMO OK: full turn flow played on Windows")
    finally:
        env.close()


if __name__ == "__main__":
    main()
