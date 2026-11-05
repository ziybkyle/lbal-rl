"""Reference implementation of BetterLandlord PCGRng for test fixtures.

This mirrors the GDScript implementation in patches/files/rng_infra.gd,
including signed 64-bit arithmetic and 31-bit seed masks.
"""
from __future__ import annotations

MASK_63 = 0x7FFFFFFFFFFFFFFF
MASK_31 = 0x7FFFFFFF
MASK_32 = 0xFFFFFFFF
PCG_MULT = 6364136223846793005
PCG_INC = (1442695040888963407 << 1) | 1


def wrap64(value: int) -> int:
    value &= (1 << 64) - 1
    return value - (1 << 64) if value >= (1 << 63) else value


class Pcg:
    def __init__(self, seed: int):
        self.state = wrap64(seed)
        self.inc = wrap64(PCG_INC)
        self.step()
        self.state = wrap64(self.state + PCG_MULT)
        self.step()
        self.step()

    def step(self) -> int:
        old = self.state
        self.state = wrap64(old * PCG_MULT + self.inc)
        return old

    def randf(self) -> float:
        old = self.step()
        lsr18 = (old >> 18) & 0x3FFFFFFFFFFF
        x = (lsr18 ^ old) >> 27
        x &= MASK_32
        rot = (old >> 59) & 0x1F
        result = ((x >> rot) | (x << ((-rot) & 31))) & MASK_32
        return result / 4294967296.0

    def randi_max(self, maximum: int) -> int:
        return int(self.randf() * maximum)

    def rand_range(self, low: float, high: float) -> float:
        return low + self.randf() * (high - low)

    def custom_shuffle(self, values):
        for index in range(len(values) - 1, 0, -1):
            swap = int(self.randf() * (index + 1))
            values[index], values[swap] = values[swap], values[index]


def fnv1a(text: str) -> int:
    value = 2166136261
    for char in text:
        value = (value ^ ord(char)) & MASK_63
        value = (value * 16777619) & MASK_63
    return value & MASK_31


def derive_seed(base: int, name: str) -> int:
    value = base & MASK_63
    for char in name:
        value = (((value << 5) + value) ^ ord(char)) & MASK_63
    return value & MASK_31


if __name__ == "__main__":
    for seed in (1, 42, 12345):
        rng = Pcg(seed)
        print(seed, [round(rng.randf(), 12) for _ in range(10)])
    values = list(range(20))
    Pcg(12345).custom_shuffle(values)
    print("shuffle20", values)
    print("fnv", fnv1a("B2AG00000000"))
    print("spin1", derive_seed(fnv1a("B2AG00000000"), "spin_1"))
