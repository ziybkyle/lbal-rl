"""Simulate the base initial reel layout and first shuffle_tiles call."""
from __future__ import annotations

import json
from pathlib import Path

from pcg_reference import Pcg, derive_seed, fnv1a

ROOT = Path(__file__).resolve().parents[2]
SYMBOLS = json.loads((ROOT / "game" / "JSON" / "Symbols - JSON.json").read_text(encoding="utf-8"))
SYMBOL_NAMES = list(SYMBOLS)


def base_icon_types(floor: int = 20):
    if floor >= 12:
        return [
            [None, "coin"],
            ["dud", None, "cherry"],
            [None, "pearl", None, "dud"],
            ["dud", None, "flower"],
            [None, "cat"],
        ]
    return [
        [None, "coin"],
        [None, None, "cherry"],
        [None, "pearl"],
        [None, None, "flower"],
        [None, "cat"],
    ]


def load_icons(types):
    types = list(types) + [None] * (5 - len(types))
    icons = []
    size = len(types)
    for index in range(size):
        value = types[(index - 1) % size]
        icons.append("empty" if value is None else value)
    return icons


def shuffle_tiles(reels, rng: Pcg):
    pool = [icon for reel in reels for icon in reel]
    empty_pool = [icon for icon in pool if icon == "empty"]
    total_empties = len(empty_pool)
    added_empties = 0
    passes = 0
    rng.custom_shuffle(pool)
    result = [[] for _ in range(5)]
    for index, icon in enumerate(pool):
        if icon == "empty" and len(pool) - total_empties + added_empties >= 25:
            passes += 1
        else:
            if icon == "empty":
                added_empties += 1
            result[(index - passes) % 5].append(icon)
    offscreen_non_empties = []
    visible_empties = []
    positions = []
    for reel_index, reel in enumerate(result):
        reel_positions = []
        for index, icon in enumerate(reel):
            row = (index + reel_index + 1) % len(reel) - 1
            reel_positions.append(row)
            if (row < 0 or row >= 4) and icon != "empty":
                offscreen_non_empties.append((reel, index))
            elif 0 <= row < 4 and icon == "empty":
                visible_empties.append((reel, index))
        positions.append(reel_positions)
    rng.custom_shuffle(visible_empties)
    if offscreen_non_empties:
        for index, empty in enumerate(visible_empties):
            if index >= len(offscreen_non_empties):
                break
            other = offscreen_non_empties[index]
            empty[0][empty[1]], other[0][other[1]] = other[0][other[1]], empty[0][empty[1]]
    board = [["empty"] * 5 for _ in range(4)]
    for reel_index, reel in enumerate(result):
        for index, icon in enumerate(reel):
            row = (index + reel_index + 1) % len(reel) - 1
            if 0 <= row < 4:
                board[row][reel_index] = icon
    return result, positions, board


def first_board(seed_text: str, floor: int = 20):
    landlord_seed = fnv1a(seed_text)
    spin_seed = derive_seed(landlord_seed, "spin_1")
    rng = Pcg(derive_seed(spin_seed, "reel_shuffle"))
    reels = [load_icons(types) for types in base_icon_types(floor)]
    result, positions, board = shuffle_tiles(reels, rng)
    return result, positions, board


if __name__ == "__main__":
    seed = "B2AG00000000"
    result, positions, board = first_board(seed)
    print("reels", result)
    print("positions", positions)
    print("board")
    for row in board:
        print(row)
