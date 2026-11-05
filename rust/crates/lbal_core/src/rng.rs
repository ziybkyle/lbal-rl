//! BetterLandlord-compatible random number streams.
//!
//! The reference implementation is the GDScript `PCGRng` class in
//! `patches/files/rng_infra.gd`. This module keeps the same 64-bit signed
//! state, 32-bit output function, seed derivation and shuffle order.

use std::collections::{BTreeMap, HashMap};

pub const MASK_63: i64 = 0x7FFF_FFFF_FFFF_FFFF;
pub const MASK_31: i64 = 0x7FFF_FFFF;
pub const MASK_32: i64 = 0xFFFF_FFFF;
pub const PCG_MULT: i64 = 6_364_136_223_846_793_005;
pub const PCG_INC: i64 = (1_442_695_040_888_963_407i64 << 1) | 1;

#[derive(Debug, Clone)]
pub struct BetterLandlordRng {
    state: i64,
    inc: i64,
    logging: bool,
    log: Vec<f64>,
}

impl BetterLandlordRng {
    pub fn new(seed: i64) -> Self {
        let mut rng = Self {
            state: seed,
            inc: PCG_INC,
            logging: false,
            log: Vec::new(),
        };
        rng.step();
        rng.state = rng.state.wrapping_add(PCG_MULT);
        rng.step();
        rng.step();
        rng
    }

    pub fn begin_log(&mut self) {
        self.log.clear();
        self.logging = true;
    }

    pub fn take_log(&mut self) -> Vec<f64> {
        self.logging = false;
        std::mem::take(&mut self.log)
    }

    fn step(&mut self) -> i64 {
        let old = self.state;
        self.state = old.wrapping_mul(PCG_MULT).wrapping_add(self.inc);
        old
    }

    pub fn randf(&mut self) -> f64 {
        let old = self.step();
        let lsr18 = (old >> 18) & 0x3FFF_FFFF_FFFF;
        let x = ((lsr18 ^ old) >> 27) & MASK_32;
        let rot = ((old >> 59) & 0x1F) as u32;
        let value = (x as u32).rotate_right(rot);
        let result = value as f64 / 4_294_967_296.0;
        if self.logging {
            self.log.push(result);
        }
        result
    }

    pub fn randi_max(&mut self, maximum: usize) -> usize {
        if maximum == 0 {
            return 0;
        }
        (self.randf() * maximum as f64) as usize
    }

    pub fn rand_range(&mut self, low: f64, high: f64) -> f64 {
        low + self.randf() * (high - low)
    }

    pub fn unit(&mut self) -> f64 {
        self.randf()
    }

    pub fn range(&mut self, low: usize, high: usize) -> usize {
        if high <= low {
            return low;
        }
        low + self.randi_max(high - low)
    }

    pub fn shuffle<T>(&mut self, values: &mut [T]) {
        for index in (1..values.len()).rev() {
            let swap = self.randi_max(index + 1);
            values.swap(index, swap);
        }
    }

    pub fn pick_index(&mut self, values: &[usize]) -> usize {
        values[self.randi_max(values.len())]
    }
}

pub fn fnv1a(text: &str) -> i64 {
    let mut value: i64 = 2_166_136_261;
    for byte in text.chars() {
        value = (value ^ byte as i64) & MASK_63;
        value = value.wrapping_mul(16_777_619) & MASK_63;
    }
    value & MASK_31
}

pub fn derive_seed(base: i64, name: &str) -> i64 {
    let mut value = base & MASK_63;
    for byte in name.chars() {
        value = (value.wrapping_shl(5).wrapping_add(value) ^ byte as i64) & MASK_63;
    }
    value & MASK_31
}

fn normalize_seed_text(text: &str) -> String {
    text.replace('O', "0").replace('I', "1")
}

pub struct RngStreams {
    pub seed_text: String,
    pub landlord_seed: i64,
    sym_rarity: BetterLandlordRng,
    sym_common: BetterLandlordRng,
    sym_uncommon: BetterLandlordRng,
    sym_rare: BetterLandlordRng,
    sym_vrare: BetterLandlordRng,
    fineprint: BetterLandlordRng,
    #[allow(dead_code)]
    cosmetic: BetterLandlordRng,
    forced_rarity: BetterLandlordRng,
    #[allow(dead_code)]
    scratch: BetterLandlordRng,
    reel: BetterLandlordRng,
    effect: BetterLandlordRng,
    reel_shuffle: BetterLandlordRng,
    effect_shuffle: BetterLandlordRng,
    oil_can: BetterLandlordRng,
    pub item_pick_event: u32,
    pub item_rarity_ctr: u32,
    pub essence_pick_event: u32,
    item_seq: HashMap<String, Vec<usize>>,
    item_cursor: HashMap<String, usize>,
    essence_seq: Vec<usize>,
    essence_cursor: usize,
}

impl RngStreams {
    pub fn from_seed_u64(seed: u64) -> Self {
        let seed = (seed & MASK_31 as u64) as i64;
        Self::from_landlord_seed(seed.to_string(), seed)
    }

    pub fn from_seed_text(text: &str) -> Self {
        let normalized = normalize_seed_text(text);
        let landlord_seed = fnv1a(&normalized);
        Self::from_landlord_seed(normalized, landlord_seed)
    }

    fn from_landlord_seed(seed_text: String, landlord_seed: i64) -> Self {
        let sym_rarity = BetterLandlordRng::new(derive_seed(landlord_seed, "sym_rarity"));
        let sym_common = BetterLandlordRng::new(derive_seed(landlord_seed, "sym_common"));
        let sym_uncommon = BetterLandlordRng::new(derive_seed(landlord_seed, "sym_uncommon"));
        let sym_rare = BetterLandlordRng::new(derive_seed(landlord_seed, "sym_rare"));
        let sym_vrare = BetterLandlordRng::new(derive_seed(landlord_seed, "sym_vrare"));
        let fineprint = BetterLandlordRng::new(derive_seed(landlord_seed, "fineprint"));
        let cosmetic = BetterLandlordRng::new(derive_seed(landlord_seed, "cosmetic"));
        let forced_rarity = BetterLandlordRng::new(derive_seed(landlord_seed, "forced_rarity"));
        let scratch = BetterLandlordRng::new(derive_seed(landlord_seed, "scratch"));
        let reel_init = BetterLandlordRng::new(derive_seed(landlord_seed, "reel_init"));
        let effect_init = BetterLandlordRng::new(derive_seed(landlord_seed, "effect_init"));
        Self {
            seed_text,
            landlord_seed,
            sym_rarity,
            sym_common,
            sym_uncommon,
            sym_rare,
            sym_vrare,
            fineprint,
            cosmetic,
            forced_rarity,
            scratch,
            reel: reel_init,
            effect: effect_init,
            reel_shuffle: BetterLandlordRng::new(derive_seed(landlord_seed, "reel_init")),
            effect_shuffle: BetterLandlordRng::new(derive_seed(landlord_seed, "effect_init")),
            oil_can: BetterLandlordRng::new(derive_seed(landlord_seed, "reel_init")),
            item_pick_event: 0,
            item_rarity_ctr: 0,
            essence_pick_event: 0,
            item_seq: HashMap::new(),
            item_cursor: HashMap::new(),
            essence_seq: Vec::new(),
            essence_cursor: 0,
        }
    }

    pub fn begin_spin(&mut self, spin_num: u32) {
        let spin_seed = derive_seed(self.landlord_seed, &format!("spin_{spin_num}"));
        self.reel = BetterLandlordRng::new(derive_seed(spin_seed, "reel"));
        self.effect = BetterLandlordRng::new(derive_seed(spin_seed, "effect"));
        self.reel_shuffle = BetterLandlordRng::new(derive_seed(spin_seed, "reel_shuffle"));
        self.effect_shuffle = BetterLandlordRng::new(derive_seed(spin_seed, "effect_shuffle"));
        self.oil_can = BetterLandlordRng::new(derive_seed(spin_seed, "oil_can"));
        self.reel_shuffle.begin_log();
        self.effect_shuffle.begin_log();
    }

    pub fn reel(&mut self) -> &mut BetterLandlordRng {
        &mut self.reel
    }

    pub fn reel_shuffle(&mut self) -> &mut BetterLandlordRng {
        &mut self.reel_shuffle
    }

    pub fn effect(&mut self) -> &mut BetterLandlordRng {
        &mut self.effect
    }

    pub fn effect_shuffle(&mut self) -> &mut BetterLandlordRng {
        &mut self.effect_shuffle
    }

    pub fn oil_can(&mut self) -> &mut BetterLandlordRng {
        &mut self.oil_can
    }

    pub fn fineprint(&mut self) -> &mut BetterLandlordRng {
        &mut self.fineprint
    }

    pub fn forced_rarity(&mut self) -> &mut BetterLandlordRng {
        &mut self.forced_rarity
    }

    pub fn unit(&mut self) -> f64 {
        self.effect.randf()
    }

    pub fn range(&mut self, low: usize, high: usize) -> usize {
        self.effect.range(low, high)
    }

    pub fn shuffle<T>(&mut self, values: &mut [T]) {
        self.effect_shuffle.shuffle(values);
    }

    pub fn symbol_rarity_roll(&mut self) -> f64 {
        self.sym_rarity.randf()
    }

    pub fn pick_symbol(&mut self, rarity: &str, pool_len: usize) -> usize {
        let stream = match rarity {
            "common" => &mut self.sym_common,
            "uncommon" => &mut self.sym_uncommon,
            "rare" => &mut self.sym_rare,
            "very_rare" => &mut self.sym_vrare,
            _ => &mut self.sym_common,
        };
        stream.randi_max(pool_len)
    }

    pub fn begin_item_pick(
        &mut self,
        round: u32,
        domains: &BTreeMap<String, Vec<usize>>,
    ) {
        self.item_pick_event += 1;
        self.item_seq.clear();
        self.item_cursor.clear();
        for (rarity, domain) in domains {
            let mut sorted = domain.clone();
            let name = format!(
                "itemseq_{}_{}_{}",
                rarity, round, self.item_pick_event
            );
            let seed = derive_seed(self.landlord_seed, &name);
            let mut rng = BetterLandlordRng::new(seed);
            rng.shuffle(&mut sorted);
            self.item_seq.insert(rarity.clone(), sorted);
            self.item_cursor.insert(rarity.clone(), 0);
        }
    }

    pub fn begin_essence_pick(&mut self, round: u32, domain: &[usize]) {
        self.essence_pick_event += 1;
        self.essence_seq = domain.to_vec();
        let name = format!(
            "essenceseq_{}_{}",
            round, self.essence_pick_event
        );
        let seed = derive_seed(self.landlord_seed, &name);
        BetterLandlordRng::new(seed).shuffle(&mut self.essence_seq);
        self.essence_cursor = 0;
    }

    pub fn item_rarity_roll(&mut self, round: u32) -> f64 {
        let name = format!("itmrarity_{}_{}", round, self.item_rarity_ctr);
        let mut rng = BetterLandlordRng::new(derive_seed(self.landlord_seed, &name));
        self.item_rarity_ctr += 1;
        rng.randf()
    }

    pub fn pick_item(&mut self, rarity: &str, filtered: &[usize]) -> Option<usize> {
        let key = rarity;
        if let Some(sequence) = self.item_seq.get(key) {
            let mut cursor = *self.item_cursor.get(key).unwrap_or(&0);
            while cursor < sequence.len() {
                let candidate = sequence[cursor];
                cursor += 1;
                if filtered.contains(&candidate) {
                    self.item_cursor.insert(key.to_string(), cursor);
                    return Some(candidate);
                }
            }
            self.item_cursor.insert(key.to_string(), sequence.len());
        }
        if filtered.is_empty() {
            return None;
        }
        let name = format!(
            "itemfb_{}_{}_{}",
            key, self.item_pick_event, self.item_cursor.get(key).copied().unwrap_or(0)
        );
        let seed = derive_seed(self.landlord_seed, &name);
        Some(filtered[(seed.rem_euclid(filtered.len() as i64)) as usize])
    }

    pub fn pick_essence(&mut self, filtered: &[usize]) -> Option<usize> {
        while self.essence_cursor < self.essence_seq.len() {
            let candidate = self.essence_seq[self.essence_cursor];
            self.essence_cursor += 1;
            if filtered.contains(&candidate) {
                return Some(candidate);
            }
        }
        if filtered.is_empty() {
            return None;
        }
        let name = format!(
            "essfb_{}_{}",
            self.essence_pick_event, self.essence_cursor
        );
        let seed = derive_seed(self.landlord_seed, &name);
        Some(filtered[(seed.rem_euclid(filtered.len() as i64)) as usize])
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rng_matches_reference_values() {
        let mut rng = BetterLandlordRng::new(12345);
        let expected = [
            0.269871462835,
            0.459075860446,
            0.016083686613,
            0.574243370909,
            0.904548750492,
            0.969666796504,
            0.908215647331,
            0.406757036224,
            0.126436642138,
            0.851257446688,
        ];
        for value in expected {
            assert!((rng.randf() - value).abs() < 1e-12);
        }
    }

    #[test]
    fn shuffle_matches_reference() {
        let mut values: Vec<usize> = (0..20).collect();
        BetterLandlordRng::new(12345).shuffle(&mut values);
        assert_eq!(
            values,
            vec![18, 17, 4, 10, 2, 3, 13, 6, 7, 11, 16, 1, 19, 12, 15, 14, 9, 0, 8, 5]
        );
    }

    #[test]
    fn seed_derivation_matches_reference() {
        assert_eq!(fnv1a("B2AG00000000"), 1_087_346_433);
        assert_eq!(derive_seed(fnv1a("B2AG00000000"), "spin_1"), 1_055_549_259);
    }
}
