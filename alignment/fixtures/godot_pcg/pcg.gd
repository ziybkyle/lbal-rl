extends SceneTree

const PCG_DEFAULT_INC = 1442695040888963407
const PCG_MULT = 6364136223846793005
const MASK_63 = 0x7FFFFFFFFFFFFFFF
const MASK_31 = 0x7FFFFFFF

class PCGRng:
	var state: int
	var inc: int

	func _init(seed_val: int):
		state = seed_val
		inc = (PCG_DEFAULT_INC << 1) | 1
		_step()
		state = state + PCG_MULT
		_step()
		_step()

	func _step():
		var old: int = state
		state = old * PCG_MULT + inc
		return old

	func randf() -> float:
		var old: int = _step()
		var lsr18: int = (old >> 18) & 0x3FFFFFFFFFFF
		var x: int = lsr18 ^ old
		x = (x >> 27) & 0xFFFFFFFF
		var rot: int = (old >> 59) & 0x1F
		var result: int = ((x >> rot) | (x << ((-rot) & 31))) & 0xFFFFFFFF
		return float(result) / 4294967296.0

	func randi_max(max_val: int) -> int:
		return int(floor(self.randf() * float(max_val)))

	func custom_shuffle(arr: Array):
		var n: int = arr.size()
		for i in range(n - 1, 0, -1):
			var j: int = int(floor(self.randf() * float(i + 1)))
			var tmp = arr[i]
			arr[i] = arr[j]
			arr[j] = tmp

func _bh_fnv1a(text: String) -> int:
	var h: int = 2166136261
	for c in text:
		h = (h ^ ord(c)) & MASK_63
		h = (h * 16777619) & MASK_63
	return h & MASK_31

func _bh_derive_seed(base: int, name: String) -> int:
	var h: int = base & MASK_63
	for c in name:
		h = (((h << 5) + h) ^ ord(c)) & MASK_63
	return h & MASK_31

func _init():
	for s in [1, 42, 12345]:
		var rng = PCGRng.new(s)
		var values = []
		for i in range(10):
			values.push_back(rng.randf())
		print("seed ", s, " ", values)
	var arr = []
	for i in range(20):
		arr.push_back(i)
	PCGRng.new(12345).custom_shuffle(arr)
	print("shuffle20 ", arr)
	print("fnv ", _bh_fnv1a("B2AG00000000"))
	print("spin1 ", _bh_derive_seed(_bh_fnv1a("B2AG00000000"), "spin_1"))
	var base = _bh_fnv1a("B2AG00000000")
	for name in ["sym_rarity", "sym_common", "forced_rarity"]:
		var r = PCGRng.new(_bh_derive_seed(base, name))
		var vals = []
		for i in range(6):
			vals.push_back(r.randf())
		print(name, " ", vals)
	quit()
