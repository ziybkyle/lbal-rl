use rand::seq::SliceRandom;
use rand::{Rng, SeedableRng};
use rand_pcg::Pcg32;

pub struct GameRng {
    inner: Pcg32,
}

impl GameRng {
    pub fn from_seed(seed: u64) -> Self {
        Self {
            inner: Pcg32::seed_from_u64(seed),
        }
    }

    pub fn from_text(seed: &str) -> Self {
        Self::from_seed(seed_from_text(seed))
    }

    pub fn range(&mut self, low: usize, high: usize) -> usize {
        self.inner.gen_range(low..high)
    }

    pub fn unit(&mut self) -> f64 {
        self.inner.gen::<f64>()
    }

    pub fn shuffle<T>(&mut self, values: &mut [T]) {
        values.shuffle(&mut self.inner);
    }
}

/// FNV-1a, used only to turn a human-readable seed into a stable integer.
pub fn seed_from_text(text: &str) -> u64 {
    let mut hash = 0xcbf29ce484222325u64;
    for byte in text.as_bytes() {
        hash ^= *byte as u64;
        hash = hash.wrapping_mul(0x100000001b3);
    }
    hash
}
