"""Feature layout and numpy candidate-scoring model for behaviour cloning."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
ITEMS_PATH = ROOT / "game" / "JSON" / "Items - JSON.json"

RARITIES = ("none", "common", "uncommon", "rare", "very_rare", "essence")
KINDS = ("symbol", "item", "essence")
KIND_OFFSET = {"symbol": 0, "item": 256, "essence": 512}
VOCAB = 769  # index 0 reserved for padding/skip
EMBED_DIM = 16
BASE_GLOBAL_DIM = 17
GROUPS = (
    "fruit", "gem", "bird", "booze", "human", "rabbit",
    "cat", "wolf", "dwarf", "food", "suit", "plant",
)
SYMBOL_NAMES = (
    "coin", "dud", "pearl", "cat", "rabbit", "rabbit_fluff", "dwarf", "beer",
    "diamond", "sapphire", "orange", "shiny_pebble", "dog", "miner", "ore",
    "spirit", "toddler", "hooligan", "gambler", "wolf", "crow", "monkey",
    "goldfish", "bee", "flower", "anchor", "amethyst", "coal", "key",
    "banana_peel", "coconut", "peach",
)
ITEM_NAMES = tuple(json.loads(ITEMS_PATH.read_text(encoding="utf-8")).keys())
SYMBOLS_PATH = ROOT / "game" / "JSON" / "Symbols - JSON.json"
_SYMBOLS = json.loads(SYMBOLS_PATH.read_text(encoding="utf-8"))
SYMBOL_GROUPS = {name: definition.get("groups", []) for name, definition in _SYMBOLS.items()}
ITEM_INDEX = {name: index for index, name in enumerate(ITEM_NAMES)}
SYMBOL_INDEX = {name: index for index, name in enumerate(SYMBOL_NAMES)}
GLOBAL_DIM = BASE_GLOBAL_DIM + len(RARITIES) * 2 + len(GROUPS) + len(ITEM_NAMES) + len(SYMBOL_NAMES)
CAND_DIM = EMBED_DIM + len(RARITIES) + len(KINDS) + 3
MAX_CANDIDATES = 20
TOTAL_SLOTS = MAX_CANDIDATES + 1  # offers + skip
SKIP_SLOT = MAX_CANDIDATES
PHASES = ("spin_reward", "symbol_reward", "item_reward", "essence_reward")


def _global_features(record: dict) -> np.ndarray:
    phase = record["phase"]
    values = [
        record["coins"] / 50.0,
        record["rent_due"] / 50.0,
        record["rents_paid"] / 12.0,
        record["spins_left"] / 10.0,
        record["total_spins"] / 60.0,
        record["last_income"] / 50.0,
        record["removal_tokens"] / 5.0,
        record["reroll_tokens"] / 5.0,
        record["essence_tokens"] / 5.0,
        1.0 if record["can_remove"] else 0.0,
        1.0 if record["can_reroll"] else 0.0,
        record["deck_len"] / 30.0,
        record["item_count"] / 20.0,
    ]
    values.extend(1.0 if phase == name else 0.0 for name in PHASES)
    deck = record.get("deck", [])
    items = record.get("items", [])
    deck_rarities = [0] * len(RARITIES)
    for entry in deck:
        rarity = entry.get("rarity", "none")
        deck_rarities[RARITIES.index(rarity) if rarity in RARITIES else 0] += 1
    values.extend(count / 10.0 for count in deck_rarities)
    item_rarities = [0] * len(RARITIES)
    for entry in items:
        rarity = entry.get("rarity", "none")
        item_rarities[RARITIES.index(rarity) if rarity in RARITIES else 0] += 1
    values.extend(count / 5.0 for count in item_rarities)
    group_counts = {name: 0 for name in GROUPS}
    for entry in deck:
        for group in SYMBOL_GROUPS.get(entry["name"], ()):
            if group in group_counts:
                group_counts[group] += 1
    values.extend(group_counts[name] / 5.0 for name in GROUPS)
    item_counts = [0] * len(ITEM_NAMES)
    for entry in items:
        if entry.get("kind") == "item":
            index = ITEM_INDEX.get(entry["name"])
            if index is not None:
                item_counts[index] += 1
    values.extend(count / 3.0 for count in item_counts)
    symbol_counts = [0] * len(SYMBOL_NAMES)
    for entry in deck:
        index = SYMBOL_INDEX.get(entry["name"])
        if index is not None:
            symbol_counts[index] += 1
    values.extend(count / 3.0 for count in symbol_counts)
    return np.asarray(values, dtype=np.float32)


def featurize(record: dict):
    """Return (global, candidates, mask, embedding_indexes, chosen_slot)."""
    global_features = _global_features(record)
    candidates = np.zeros((TOTAL_SLOTS, CAND_DIM), dtype=np.float32)
    mask = np.zeros(TOTAL_SLOTS, dtype=bool)
    embed_idx = np.zeros(TOTAL_SLOTS, dtype=np.int32)
    offer = record["offer"][:MAX_CANDIDATES]
    for slot, entry in enumerate(offer):
        offset = KIND_OFFSET.get(entry["kind"], 0)
        type_id = int(entry["type_id"])
        embed_idx[slot] = 1 + offset + type_id
        candidates[slot, EMBED_DIM + RARITIES.index(entry["rarity"])] = 1.0
        candidates[slot, EMBED_DIM + len(RARITIES) + KINDS.index(entry["kind"])] = 1.0
        candidates[slot, EMBED_DIM + len(RARITIES) + len(KINDS)] = min(
            float(entry.get("base_value", 0)) / 20.0, 5.0
        )
        candidates[slot, EMBED_DIM + len(RARITIES) + len(KINDS) + 2] = slot / MAX_CANDIDATES
        mask[slot] = True
    skip = SKIP_SLOT
    mask[skip] = True
    candidates[skip, EMBED_DIM + len(RARITIES) + len(KINDS) + 1] = 1.0
    action = int(record["action"])
    chosen = action if action >= 0 else skip
    return global_features, candidates, mask, embed_idx, chosen


def build_dataset(records: list[dict]):
    size = len(records)
    globals_ = np.zeros((size, GLOBAL_DIM), dtype=np.float32)
    candidates = np.zeros((size, TOTAL_SLOTS, CAND_DIM), dtype=np.float32)
    masks = np.zeros((size, TOTAL_SLOTS), dtype=bool)
    embeds = np.zeros((size, TOTAL_SLOTS), dtype=np.int32)
    chosen = np.zeros(size, dtype=np.int64)
    for row, record in enumerate(records):
        globals_[row], candidates[row], masks[row], embeds[row], chosen[row] = featurize(record)
    return globals_, candidates, masks, embeds, chosen


class BCPolicyNet:
    def __init__(self, hidden: int = 96, seed: int = 0):
        rng = np.random.default_rng(seed)
        self.embedding = rng.normal(0.0, 0.05, (VOCAB, EMBED_DIM)).astype(np.float32)
        self.w1 = rng.normal(0.0, 0.05 ** 0.5, (GLOBAL_DIM + CAND_DIM, hidden)).astype(np.float32)
        self.b1 = np.zeros(hidden, dtype=np.float32)
        self.w2 = rng.normal(0.0, 0.05 ** 0.5, (hidden, 1)).astype(np.float32)
        self.b2 = np.zeros(1, dtype=np.float32)
        self.hidden = hidden

    def _input(self, globals_, candidates, embeds):
        embedded = self.embedding[embeds]  # (N, slots, EMBED_DIM)
        candidate = np.concatenate([embedded, candidates[..., EMBED_DIM:]], axis=-1)
        expanded = np.broadcast_to(globals_[:, None, :], (globals_.shape[0], TOTAL_SLOTS, GLOBAL_DIM))
        return np.concatenate([expanded, candidate], axis=-1)

    def forward(self, globals_, candidates, embeds, masks):
        inputs = self._input(globals_, candidates, embeds)
        flat = inputs.reshape(-1, GLOBAL_DIM + CAND_DIM)
        pre = flat @ self.w1 + self.b1
        hidden = np.maximum(pre, 0.0)
        scores = (hidden @ self.w2 + self.b2).reshape(masks.shape)
        scores = np.where(masks, scores, -1e9)
        scores -= scores.max(axis=1, keepdims=True)
        exp = np.exp(scores) * masks
        probabilities = exp / exp.sum(axis=1, keepdims=True)
        return probabilities, hidden, flat, inputs

    def predict(self, record: dict) -> tuple[int, float]:
        global_features, candidates, mask, embed_idx, _ = featurize(record)
        probabilities, _, _, _ = self.forward(
            global_features[None], candidates[None], embed_idx[None], mask[None]
        )
        slot = int(probabilities[0].argmax())
        return (-1 if slot == SKIP_SLOT else slot), float(probabilities[0, slot])

    def predict_action(self, record: dict) -> int:
        return self.predict(record)[0]

    def save(self, path: Path) -> None:
        np.savez(
            path,
            embedding=self.embedding,
            w1=self.w1,
            b1=self.b1,
            w2=self.w2,
            b2=self.b2,
        )

    @classmethod
    def load(cls, path: Path) -> "BCPolicyNet":
        data = np.load(path)
        model = cls(hidden=data["w1"].shape[1])
        model.embedding = data["embedding"]
        model.w1 = data["w1"]
        model.b1 = data["b1"]
        model.w2 = data["w2"]
        model.b2 = data["b2"]
        return model


def evaluate(model: BCPolicyNet, globals_, candidates, masks, embeds, chosen) -> tuple[float, float]:
    probabilities, _, _, _ = model.forward(globals_, candidates, embeds, masks)
    predictions = probabilities.argmax(axis=1)
    accuracy = float((predictions == chosen).mean())
    skip_mask = chosen == SKIP_SLOT
    skip_recall = float((predictions[skip_mask] == SKIP_SLOT).mean()) if skip_mask.any() else 0.0
    return accuracy, skip_recall
