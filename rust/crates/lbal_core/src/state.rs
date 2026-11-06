use crate::data::{GameData, RENT_SCHEDULE_FLOOR_20};
use crate::rng::{BetterLandlordRng, RngStreams};
use std::collections::{BTreeMap, HashMap, HashSet};

pub const BOARD_COLS: usize = 5;
pub const BOARD_ROWS: usize = 4;
pub const BOARD_SLOTS: usize = BOARD_COLS * BOARD_ROWS;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Rarity {
    Common,
    Uncommon,
    Rare,
    VeryRare,
}

impl Rarity {
    pub fn parse(name: &str) -> Option<Self> {
        match name {
            "common" => Some(Self::Common),
            "uncommon" => Some(Self::Uncommon),
            "rare" => Some(Self::Rare),
            "very_rare" => Some(Self::VeryRare),
            _ => None,
        }
    }
}

/// Value of a symbol that was spawned during the current spin. Godot
/// re-checks newly added icons, so they can still receive adjacency bonuses,
/// be destroyed, or trigger their own effects before `check_values`.
#[derive(Debug, Clone, Copy)]
struct SpawnedValue {
    sum: i32,
    mult: f64,
    saved: i32,
    destroyed: bool,
    /// Off-screen icons never appear in `check_values`.
    visible: bool,
}

fn spawned_value(sum: i32, mult: f64) -> i32 {
    let product = sum as f64 * mult;
    if product < 0.0 {
        sum
    } else {
        product.round() as i32
    }
}

#[derive(Debug, Clone, Copy)]
pub struct Symbol {
    pub type_id: u16,
    pub bonus: i32,
    pub multiplier: i32,
    pub times_displayed: u32,
    /// Number of spins where this instance gave at least one coin.
    pub times_coins_given: u32,
    /// Per-instance `saved_value` used by symbols such as the light bulb.
    pub saved: i32,
}

impl Symbol {
    pub fn new(type_id: usize, _value: i32) -> Self {
        Self {
            type_id: type_id as u16,
            bonus: 0,
            multiplier: 1,
            times_displayed: 0,
            times_coins_given: 0,
            saved: 0,
        }
    }

    /// Rebuild a symbol whose displayed value already includes permanent gains.
    pub fn with_current_value(type_id: usize, value: i32, data: &GameData) -> Self {
        let base = data.symbols[type_id].base_value;
        Self {
            type_id: type_id as u16,
            bonus: value - base,
            multiplier: 1,
            times_displayed: 0,
            times_coins_given: 0,
            saved: 0,
        }
    }

    pub fn current_value(&self, data: &GameData) -> i32 {
        let base = data.symbols[self.type_id as usize].base_value;
        (base + self.bonus) * self.multiplier
    }
}

/// Values that Godot decides while *registering* conditional effects,
/// before any effect is resolved.  The registration pass consumes the
/// shared `effect` stream, so keeping the same draws is required even when
/// the stored value is later overwritten by a check-time roll.
#[derive(Debug, Clone, Default)]
struct SpinPreRoll {
    /// d3/d5 value chosen during registration.
    dice: Option<i32>,
    /// Arrow directions chosen during registration.
    arrow_directions: Vec<usize>,
    /// Neighbour destroyed by hex_of_destruction (target is picked at registration).
    destruction_target: Option<usize>,
    /// Neighbour drained by hex_of_draining (shuffled and picked at registration).
    draining_target: Option<usize>,
}

pub const REEL_ICONS: usize = 5;

#[derive(Debug, Clone)]
pub struct Board {
    /// Deck indices, row-major: slot = y * 5 + x.
    pub slots: [Option<usize>; BOARD_SLOTS],
    /// Godot reels keep five icons each; one is off-screen.
    pub reels: [Vec<Option<usize>>; BOARD_COLS],
    /// Screen row for each icon: -1 means off-screen.
    pub positions: [Vec<i32>; BOARD_COLS],
    initialized: bool,
}

impl Board {
    pub fn empty() -> Self {
        Self {
            slots: [None; BOARD_SLOTS],
            reels: Default::default(),
            positions: Default::default(),
            initialized: false,
        }
    }

    pub fn occupied(&self) -> usize {
        self.slots.iter().filter(|slot| slot.is_some()).count()
    }

    fn base_layout(data: &GameData, deck: &[Symbol]) -> Option<[Vec<Option<usize>>; BOARD_COLS]> {
        let names: Vec<&str> = deck
            .iter()
            .map(|symbol| data.symbols[symbol.type_id as usize].name.as_str())
            .collect();
        if names.len() != 8
            || !names.starts_with(&["coin", "cherry", "pearl", "flower", "cat"])
            || names[5..] != ["dud", "dud", "dud"]
        {
            return None;
        }
        let mut used = [false; 8];
        let take = |name: &str, used: &mut [bool; 8]| -> Option<usize> {
            for (index, current) in names.iter().enumerate() {
                if !used[index] && current == &name {
                    used[index] = true;
                    return Some(index);
                }
            }
            None
        };
        let mut reels: [Vec<Option<usize>>; BOARD_COLS] = Default::default();
        let layout = [
            ["empty", "empty", "coin", "empty", "empty"],
            ["empty", "dud", "empty", "cherry", "empty"],
            ["empty", "empty", "pearl", "empty", "dud"],
            ["empty", "dud", "empty", "flower", "empty"],
            ["empty", "empty", "cat", "empty", "empty"],
        ];
        for (reel, icons) in layout.iter().enumerate() {
            reels[reel] = icons
                .iter()
                .map(|name| {
                    if *name == "empty" {
                        None
                    } else {
                        take(*name, &mut used)
                    }
                })
                .collect();
        }
        Some(reels)
    }

    fn rebuild_positions(&mut self) {
        for reel in 0..BOARD_COLS {
            let count = self.reels[reel].len();
            self.positions[reel] = (0..count)
                .map(|index| {
                    if count == 0 {
                        -1
                    } else {
                        ((index + reel + 1) % count) as i32 - 1
                    }
                })
                .collect();
        }
    }

    fn sync_slots(&mut self) {
        self.slots = [None; BOARD_SLOTS];
        for reel in 0..BOARD_COLS {
            for (index, entry) in self.reels[reel].iter().enumerate() {
                let row = self.positions[reel].get(index).copied().unwrap_or(-1);
                if (0..BOARD_ROWS as i32).contains(&row) {
                    self.slots[row as usize * BOARD_COLS + reel] = *entry;
                }
            }
        }
    }

    /// Exact Godot `shuffle_tiles()` for the base layout.
    pub fn deal(&mut self, deck: &[Symbol], rng: &mut BetterLandlordRng, data: &GameData) {
        if !self.initialized || self.reels.iter().all(|reel| reel.is_empty()) {
            if let Some(reels) = Self::base_layout(data, deck) {
                self.reels = reels;
                self.initialized = true;
                self.rebuild_positions();
            } else {
                self.reels = Default::default();
                for reel in 0..BOARD_COLS {
                    self.reels[reel] = vec![None; REEL_ICONS];
                }
                for (index, symbol) in deck.iter().enumerate() {
                    let reel = index % BOARD_COLS;
                    let slot = self.reels[reel]
                        .iter()
                        .position(|entry| entry.is_none())
                        .unwrap_or(0);
                    self.reels[reel][slot] = Some(index);
                    let _ = symbol;
                }
                self.initialized = true;
                self.rebuild_positions();
            }
        }
        let mut pool: Vec<Option<usize>> = self
            .reels
            .iter()
            .flat_map(|reel| reel.iter().copied())
            .collect();
        let mut total_empties = pool.iter().filter(|entry| entry.is_none()).count();
        // Godot keeps the pool at most 25 icons: when the deck grows past
        // 20 symbols it erases spare empties (first empty in reel order)
        // *before* shuffling.  Removing them also changes how many draws
        // `_rrr_shuffle` consumes, so this has to happen first.
        while pool.len() > 25 && total_empties > 0 {
            if let Some(position) = pool.iter().position(|entry| entry.is_none()) {
                pool.remove(position);
                total_empties -= 1;
            } else {
                break;
            }
        }
        let mut added_empties = 0usize;
        let mut passes = 0usize;
        rng.shuffle(&mut pool);
        let mut distributed: [Vec<Option<usize>>; BOARD_COLS] = Default::default();
        for (index, entry) in pool.iter().enumerate() {
            if entry.is_none()
                && pool.len().saturating_sub(total_empties) + added_empties >= 25
            {
                passes += 1;
            } else {
                if entry.is_none() {
                    added_empties += 1;
                }
                distributed[(index - passes) % BOARD_COLS].push(*entry);
            }
        }
        self.reels = distributed;
        self.rebuild_positions();

        let mut offscreen_non_empties: Vec<(usize, usize)> = Vec::new();
        let mut visible_empties: Vec<(usize, usize)> = Vec::new();
        for reel in 0..BOARD_COLS {
            for index in 0..self.reels[reel].len() {
                let row = self.positions[reel][index];
                let entry = self.reels[reel][index];
                if !(0..BOARD_ROWS as i32).contains(&row) && entry.is_some() {
                    offscreen_non_empties.push((reel, index));
                } else if (0..BOARD_ROWS as i32).contains(&row) && entry.is_none() {
                    visible_empties.push((reel, index));
                }
            }
        }
        rng.shuffle(&mut visible_empties);
        for (index, empty) in visible_empties.iter().enumerate() {
            if index >= offscreen_non_empties.len() {
                break;
            }
            let other = offscreen_non_empties[index];
            let value = self.reels[other.0][other.1];
            self.reels[empty.0][empty.1] = value;
            self.reels[other.0][other.1] = None;
        }
        self.sync_slots();
    }

    /// Place a newly acquired symbol into an empty slot.
    ///
    /// Mirrors `Main.add_tile` + `Reel.add_tile`: prefer a reel with a
    /// *visible* empty, then any reel with an empty (possibly off-screen),
    /// and only append a sixth icon when the reels are completely full.
    pub fn add_symbol(&mut self, deck_index: usize, rng: &mut BetterLandlordRng) {
        let mut visible_reels: Vec<usize> = Vec::new();
        let mut any_reels: Vec<usize> = Vec::new();
        for reel in 0..BOARD_COLS {
            for index in 0..self.reels[reel].len() {
                if self.reels[reel][index].is_none() {
                    any_reels.push(reel);
                    let row = self.positions[reel].get(index).copied().unwrap_or(-1);
                    if (0..BOARD_ROWS as i32).contains(&row) {
                        visible_reels.push(reel);
                        break;
                    }
                }
            }
        }
        let reel = if !visible_reels.is_empty() {
            visible_reels[rng.randi_max(visible_reels.len())]
        } else if !any_reels.is_empty() {
            any_reels[rng.randi_max(any_reels.len())]
        } else {
            // Full reels: Godot appends to the reel with the most icons.
            let mut best = 0usize;
            let mut best_count = 0usize;
            for reel in 0..BOARD_COLS {
                if self.reels[reel].len() > best_count {
                    best_count = self.reels[reel].len();
                    best = reel;
                }
            }
            self.reels[best].push(Some(deck_index));
            self.rebuild_positions();
            self.sync_slots();
            return;
        };
        let visible_empties: Vec<usize> = self.reels[reel]
            .iter()
            .enumerate()
            .filter(|(index, entry)| {
                entry.is_none()
                    && (0..BOARD_ROWS as i32)
                        .contains(&self.positions[reel].get(*index).copied().unwrap_or(-1))
            })
            .map(|(index, _)| index)
            .collect();
        let all_empties: Vec<usize> = self.reels[reel]
            .iter()
            .enumerate()
            .filter(|(_, entry)| entry.is_none())
            .map(|(index, _)| index)
            .collect();
        if !visible_empties.is_empty() {
            let slot = visible_empties[rng.randi_max(visible_empties.len())];
            self.reels[reel][slot] = Some(deck_index);
        } else if !all_empties.is_empty() {
            let slot = all_empties[rng.randi_max(all_empties.len())];
            self.reels[reel][slot] = Some(deck_index);
        } else {
            self.reels[reel].push(Some(deck_index));
        }
        self.rebuild_positions();
        self.sync_slots();
    }

    pub fn prune_invalid(&mut self, deck_len: usize) {
        if !self.initialized {
            return;
        }
        for reel in 0..BOARD_COLS {
            for entry in self.reels[reel].iter_mut() {
                if entry.map(|index| index >= deck_len).unwrap_or(false) {
                    *entry = None;
                }
            }
        }
        self.rebuild_positions();
        self.sync_slots();
    }

    /// Remove deck symbols from the reels and shift later deck indices.
    pub fn remove_deck_indices(&mut self, removed: &[usize]) {
        for reel in 0..BOARD_COLS {
            for entry in self.reels[reel].iter_mut() {
                if let Some(index) = *entry {
                    if removed.contains(&index) {
                        *entry = None;
                    } else {
                        let shift = removed.iter().filter(|value| **value < index).count();
                        *entry = Some(index - shift);
                    }
                }
            }
        }
        if self.initialized {
            self.rebuild_positions();
            self.sync_slots();
        }
    }
}

fn retain_deck(
    deck: &mut Vec<Symbol>,
    data: &GameData,
    keep: impl Fn(&crate::data::Def) -> bool,
) -> Vec<usize> {
    let mut removed = Vec::new();
    for (index, symbol) in deck.iter().enumerate() {
        if !keep(&data.symbols[symbol.type_id as usize]) {
            removed.push(index);
        }
    }
    let removed_set = removed.clone();
    let mut kept = Vec::with_capacity(deck.len() - removed.len());
    for (index, symbol) in deck.iter().enumerate() {
        if !removed_set.contains(&index) {
            kept.push(*symbol);
        }
    }
    *deck = kept;
    removed
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Phase {
    Ready,
    SpinReward { offer: Vec<usize> },
    SymbolReward { offer: Vec<usize> },
    ItemReward { offer: Vec<usize> },
    EssenceReward { offer: Vec<usize> },
    Done,
}

#[derive(Debug, Clone, Copy, Default)]
pub struct SpinOutcome {
    pub income: i32,
    pub coins: i32,
    pub occupied: usize,
    pub spins_left: i32,
    pub rent_paid: bool,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum FollowUp {
    Ready,
    ItemReward,
    SymbolReward,
}

pub struct RunState<'a> {
    pub data: &'a GameData,
    pub rng: RngStreams,
    pub deck: Vec<Symbol>,
    pub board: Board,
    pub coins: i32,
    pub rents_paid: u32,
    pub spins_left: i32,
    pub total_spins: u32,
    pub reroll_tokens: i32,
    pub removal_tokens: i32,
    pub essence_tokens: i32,
    pub essences_held: Vec<usize>,
    essence_saved: HashMap<usize, i32>,
    pending_essence_offers: i32,
    pending_extra_symbol_offers: i32,
    pending_bonus_symbols: Vec<Option<Rarity>>,
    pending_bonus_items: i32,
    pre_spin_income: i32,
    spin_column_mult: [f64; BOARD_COLS],
    essence_pool: Vec<usize>,
    pub last_income: i32,
    /// Slot index (row-major) and coin value for each paid-out symbol.
    pub last_symbol_income: Vec<(usize, i32)>,
    /// Debug breakdown of item and essence income for the latest spin.
    pub last_item_gains: Vec<(String, i32)>,
    pub last_extra_income: i32,
    /// Raw effect-stream randf values consumed by the latest spin, for
    /// comparing against Godot's `_bh_rng_effect` call sequence.
    pub last_effect_rng: Vec<f64>,
    /// Reel-shuffle stream values consumed this spin (layout diagnostics).
    pub last_reel_shuffle: Vec<f64>,
    /// Effect-shuffle stream values consumed this spin.
    pub last_effect_shuffle: Vec<f64>,
    pub last_board: [i16; BOARD_SLOTS],
    pub phase: Phase,
    pub won: bool,
    pub seed: u64,
    pub items_held: Vec<usize>,
    pub fine_prints: Vec<u32>,
    /// Real clauses name one dynamic icon per reliant clause; effects only hit
    /// that symbol/item type.
    fine_print_icons: HashMap<u32, String>,
    fine_print_counter: i32,
    spin_rarity_penalty: f64,
    spin_rarity_factor: f64,
    stored_eggs: i32,
    stored_goldfish: i32,
    /// Godot computes the reroll item's replacement rolls eagerly while the
    /// item registers its conditional effects, before symbol registration.
    reroll_d3_roll: Option<i32>,
    reroll_d5_roll: Option<i32>,
    item_ages: HashMap<usize, i32>,
    item_saved: HashMap<usize, i32>,
    essence_destroy_counters: HashMap<usize, i32>,
    lefty_saved: i32,
    telescope_saved: i32,
    protractor_saved: i32,
    credit_card_saved: i32,
    pending_extra_symbol_choices: usize,
    spin_all_adjacent: bool,
    spin_corner_adjacent: bool,
    destroyed_history: Vec<u16>,
    compost_destroyed: u32,
    permanent_name_bonus: HashMap<String, i32>,
    permanent_group_bonus: HashMap<String, i32>,
    permanent_name_mult: HashMap<String, f64>,
    permanent_group_mult: HashMap<String, f64>,
    copycat_active: bool,
    clear_sky_active: bool,
    grave_robber_mult: i32,
    shrine_active: bool,
    reroll_essence_rerolls: i32,
    spawner_spawns: i32,
    spawner_bonus_mult: f64,
    swapping_essence_swaps: i32,
    item_offer_floor: Option<(Rarity, usize)>,
    symbol_forced_queue: Vec<(Rarity, bool)>,
    force_skip_reward: bool,
    force_take_symbol: bool,
    boss_fight: bool,
    boss_hp: i32,
    base_rarity_pools: [Vec<usize>; 4],
    chemical_seven_common: bool,
    rarity_pools: [Vec<usize>; 4],
    item_pools: [Vec<usize>; 4],
}

impl<'a> RunState<'a> {
    pub fn new(data: &'a GameData, seed: u64) -> Self {
        Self::build(data, seed, RngStreams::from_seed_u64(seed))
    }

    pub fn new_with_seed_text(data: &'a GameData, text: &str) -> Self {
        let rng = RngStreams::from_seed_text(text);
        let seed = rng.landlord_seed as u64;
        Self::build(data, seed, rng)
    }

    fn build(data: &'a GameData, seed: u64, rng: RngStreams) -> Self {
        let mut deck = Vec::with_capacity(20);
        for name in ["coin", "cherry", "pearl", "flower", "cat"] {
            if let Some(index) = data.symbol_id(name) {
                deck.push(Symbol::new(index, data.symbols[index].base_value));
            }
        }
        if let Some(dud) = data.symbol_id("dud") {
            for _ in 0..3 {
                deck.push(Symbol::new(dud, 0));
            }
        }
        let mut rarity_pools: [Vec<usize>; 4] = Default::default();
        for (index, def) in data.symbols.iter().enumerate() {
            if def.name == "dud" || def.name == "empty" {
                continue;
            }
            if let Some(rarity) = Rarity::parse(&def.rarity) {
                rarity_pools[rarity as usize].push(index);
            }
        }
        let mut item_pools: [Vec<usize>; 4] = Default::default();
        for (index, def) in data.items.iter().enumerate() {
            if let Some(rarity) = Rarity::parse(&def.rarity) {
                item_pools[rarity as usize].push(index);
            }
        }
        Self {
            data,
            rng,
            deck,
            board: Board::empty(),
            coins: 1,
            rents_paid: 0,
            spins_left: 5,
            total_spins: 0,
            reroll_tokens: 0,
            removal_tokens: 0,
            essence_tokens: 0,
            essences_held: Vec::new(),
            essence_saved: HashMap::new(),
            pending_essence_offers: 0,
            pending_extra_symbol_offers: 0,
            pending_bonus_symbols: Vec::new(),
            pending_bonus_items: 0,
            pre_spin_income: 0,
            spin_column_mult: [1.0; BOARD_COLS],
            essence_pool: (0..data.essences.len()).collect(),
            last_income: 0,
            last_symbol_income: Vec::new(),
            last_item_gains: Vec::new(),
            last_extra_income: 0,
            last_effect_rng: Vec::new(),
            last_reel_shuffle: Vec::new(),
            last_effect_shuffle: Vec::new(),
            last_board: [-1; BOARD_SLOTS],
            phase: Phase::Ready,
            won: false,
            seed,
            items_held: Vec::new(),
            fine_prints: Vec::new(),
            fine_print_icons: HashMap::new(),
            fine_print_counter: 0,
            spin_rarity_penalty: 1.0,
            spin_rarity_factor: 1.0,
            stored_eggs: 0,
            stored_goldfish: 0,
            reroll_d3_roll: None,
            reroll_d5_roll: None,
            item_ages: HashMap::new(),
            item_saved: HashMap::new(),
            essence_destroy_counters: HashMap::new(),
            lefty_saved: 0,
            telescope_saved: 0,
            protractor_saved: 0,
            credit_card_saved: 0,
            pending_extra_symbol_choices: 0,
            spin_all_adjacent: false,
            spin_corner_adjacent: false,
            destroyed_history: Vec::new(),
            compost_destroyed: 0,
            permanent_name_bonus: HashMap::new(),
            permanent_group_bonus: HashMap::new(),
            permanent_name_mult: HashMap::new(),
            permanent_group_mult: HashMap::new(),
            copycat_active: false,
            clear_sky_active: false,
            grave_robber_mult: 1,
            shrine_active: false,
            reroll_essence_rerolls: 0,
            spawner_spawns: 0,
            spawner_bonus_mult: 1.0,
            swapping_essence_swaps: 0,
            item_offer_floor: None,
            symbol_forced_queue: Vec::new(),
            force_skip_reward: false,
            force_take_symbol: false,
            boss_fight: false,
            boss_hp: 0,
            base_rarity_pools: rarity_pools.clone(),
            chemical_seven_common: false,
            rarity_pools,
            item_pools,
        }
    }

    /// Rebuilds the symbol offer pools the way the real game's
    /// `rarity_database` is derived: base JSON order, then the rarity moves
    /// contributed by currently held items, then the Highlander gate.
    fn refresh_symbol_pools(&mut self) {
        let mut pools = self.base_rarity_pools.clone();
        let move_symbol = |pools: &mut [Vec<usize>; 4], name: &str, from: Rarity, to: Rarity| {
            let Some(id) = self.data.symbol_id(name) else {
                return;
            };
            if let Some(position) = pools[from as usize].iter().position(|entry| *entry == id) {
                pools[from as usize].remove(position);
            }
            if !pools[to as usize].contains(&id) {
                pools[to as usize].push(id);
            }
        };
        for item_id in &self.items_held {
            let name = self.data.items[*item_id].name.as_str();
            match name {
                "rain_cloud" => move_symbol(&mut pools, "rain", Rarity::Uncommon, Rarity::Common),
                "dark_humor" => {
                    move_symbol(&mut pools, "comedian", Rarity::Rare, Rarity::Uncommon)
                }
                "void_party" => {
                    for symbol in ["void_creature", "void_stone", "void_fruit"] {
                        move_symbol(&mut pools, symbol, Rarity::Uncommon, Rarity::Common);
                    }
                }
                "flush" => {
                    for symbol in ["clubs", "diamonds", "hearts", "spades"] {
                        move_symbol(&mut pools, symbol, Rarity::Uncommon, Rarity::Common);
                    }
                }
                _ => {}
            }
        }
        if self.chemical_seven_common {
            move_symbol(&mut pools, "chemical_seven", Rarity::Uncommon, Rarity::Common);
        }
        let highlander = self.data.symbol_id("highlander");
        if let Some(highlander) = highlander {
            if self
                .deck
                .iter()
                .any(|symbol| symbol.type_id as usize == highlander)
            {
                pools[Rarity::VeryRare as usize].retain(|id| *id != highlander);
            }
        }
        self.rarity_pools = pools;
    }

    /// Rebuilds a board from trace names and returns the income the spin would
    /// produce plus each paid-out slot. Per-item saved state is not in the
    /// trace, so this is only meaningful for the early game.
    pub fn replay_income(
        data: &'a GameData,
        deck_entries: &[(String, i32, u32, u32)],
        item_names: &[String],
        board_names: &[String],
    ) -> Option<(i32, Vec<(usize, i32)>)> {
        let mut state = RunState::new(data, 0);
        state.deck.clear();
        state.items_held.clear();
        for (name, value, displayed, given) in deck_entries {
            let type_id = data.symbol_id(name)?;
            let mut symbol = Symbol::with_current_value(type_id, *value, data);
            symbol.times_displayed = *displayed;
            symbol.times_coins_given = *given;
            state.deck.push(symbol);
        }
        for name in item_names {
            state.items_held.push(*data.item_index.get(name)?);
        }
        let mut used = vec![false; state.deck.len()];
        let mut slots = [None; BOARD_SLOTS];
        for (slot, name) in board_names.iter().enumerate().take(BOARD_SLOTS) {
            if name == "empty" {
                continue;
            }
            let type_id = data.symbol_id(name)?;
            let position = state
                .deck
                .iter()
                .enumerate()
                .position(|(index, symbol)| {
                    !used[index] && symbol.type_id as usize == type_id
                })?;
            used[position] = true;
            slots[slot] = Some(position);
        }
        state.board.slots = slots;
        let income = state.resolve_spin_effects();
        Some((income, state.last_symbol_income.clone()))
    }

    pub fn rent_due(&self) -> i32 {
        if self.rents_paid >= 12 {
            1000 + (self.rents_paid as i32 - 12) * 500
        } else {
            RENT_SCHEDULE_FLOOR_20[self.rents_paid as usize]
        }
    }

    pub fn boss_fight_active(&self) -> bool {
        self.boss_fight
    }

    pub fn boss_hp(&self) -> i32 {
        self.boss_hp
    }

    pub fn done(&self) -> bool {
        self.phase == Phase::Done
    }

    pub fn is_full(&self) -> bool {
        self.deck.len() >= BOARD_SLOTS
    }

    /// Floor 20 adds a dud every 15 spins while the run is still alive.
    fn add_dud_if_due(&mut self) {
        if self.rents_paid < 12 && self.total_spins > 0 && self.total_spins % 15 == 0 {
            if let Some(index) = self.data.symbol_id("dud") {
                self.deck.push(Symbol::new(index, 0));
            }
        }
    }

    fn store_eggs_and_fish(&mut self) {
        let has_carton = self
            .items_held
            .iter()
            .any(|id| self.data.items[*id].name == "egg_carton");
        let carton_essence = self
            .data
            .essence_index
            .get("egg_carton_essence")
            .copied()
            .filter(|id| self.essences_held.contains(id));
        if has_carton || carton_essence.is_some() {
            let before = self.deck.len();
            let removed = retain_deck(&mut self.deck, self.data, |def| def.name != "egg");
            if !removed.is_empty() {
                self.board.remove_deck_indices(&removed);
            }
            let count = (before - self.deck.len()) as i32;
            if has_carton {
                self.stored_eggs += count;
            }
            if let Some(essence_id) = carton_essence {
                if count > 0 {
                    let values = &self.data.essences[essence_id].values;
                    let threshold = values.first().copied().unwrap_or(12.0) as i32;
                    let per_egg = values.get(1).copied().unwrap_or(1.0) as i32;
                    self.coins += count * per_egg;
                    *self.essence_saved.entry(essence_id).or_insert(0) += count;
                    if self.essence_saved.get(&essence_id).copied().unwrap_or(0)
                        >= threshold.max(1)
                    {
                        self.try_destroy_essence(essence_id);
                    }
                }
            }
        }
        if self
            .items_held
            .iter()
            .any(|id| self.data.items[*id].name == "fish_bowl")
        {
            let before = self.deck.len();
            let removed = retain_deck(&mut self.deck, self.data, |def| def.name != "goldfish");
            if !removed.is_empty() {
                self.board.remove_deck_indices(&removed);
            }
            self.stored_goldfish += (before - self.deck.len()) as i32;
        }
    }

    /// Coin-on-a-string, flush and the lint roller consume matching symbols
    /// before the reels are dealt, paying a fixed amount per symbol consumed.
    fn consume_symbols_pre_spin(&mut self) {
        let mut item_rules: Vec<(&'static str, i32, bool)> = Vec::new();
        for item_id in self.items_held.clone() {
            let name = self.data.items[item_id].name.clone();
            let values = self.data.items[item_id].values.clone();
            let v0 = values.first().copied().unwrap_or(0.0) as i32;
            let v1 = values.get(1).copied().unwrap_or(0.0) as i32;
            match name.as_str() {
                "coin_on_a_string" => item_rules.push(("coin", v1, true)),
                "flush" => item_rules.push(("suit", v0, false)),
                "lint_roller" => item_rules.push(("rabbit_fluff", v0, false)),
                _ => {}
            }
        }
        let mut essence_rules: Vec<(&'static str, i32, usize, i32)> = Vec::new();
        for essence_id in self.essences_held.clone() {
            let name = self.data.essences[essence_id].name.clone();
            let values = self.data.essences[essence_id].values.clone();
            let v0 = values.first().copied().unwrap_or(0.0) as i32;
            let v1 = values.get(1).copied().unwrap_or(0.0) as i32;
            let (target, payout) = match name.as_str() {
                "coin_on_a_string_essence" => ("coin", v1),
                "flush_essence" => ("suit", v1),
                "lint_roller_essence" => ("rabbit_fluff", v1),
                "fish_bowl_essence" => ("goldfish", v1),
                "barrel_o_dwarves_essence" => ("dwarf", v1),
                _ => continue,
            };
            essence_rules.push((target, payout, essence_id, v0.max(1)));
        }
        if item_rules.is_empty() && essence_rules.is_empty() {
            return;
        }
        let mut income = 0;
        for (target, payout, blocked_by_fp20) in item_rules {
            if blocked_by_fp20 && self.has_fine_print(20) {
                continue;
            }
            let removed = retain_deck(&mut self.deck, self.data, |def| {
                !match target {
                    "suit" => def.groups.iter().any(|group| group == "suit"),
                    other => def.name == other,
                }
            });
            self.board.remove_deck_indices(&removed);
            income += removed.len() as i32 * payout;
        }
        let mut destroyed: Vec<usize> = Vec::new();
        for (target, payout, essence_id, threshold) in essence_rules {
            let removed = retain_deck(&mut self.deck, self.data, |def| {
                !match target {
                    "suit" => def.groups.iter().any(|group| group == "suit"),
                    other => def.name == other,
                }
            });
            self.board.remove_deck_indices(&removed);
            if removed.is_empty() {
                continue;
            }
            income += removed.len() as i32 * payout;
            let saved = self.essence_saved.entry(essence_id).or_insert(0);
            *saved += removed.len() as i32;
            if *saved >= threshold {
                destroyed.push(essence_id);
            }
        }
        for essence_id in destroyed {
            self.try_destroy_essence(essence_id);
        }
        self.pre_spin_income += income;
    }

    /// Oil can: after a five-spin charge it re-deals the most valuable reel and
    /// doubles that column's value for the spin.
    fn apply_oil_can(&mut self) {
        let oil_cans: Vec<usize> = self
            .items_held
            .iter()
            .copied()
            .filter(|id| self.data.items[*id].name == "oil_can")
            .collect();
        if oil_cans.is_empty() {
            return;
        }
        let def = &self.data.items[oil_cans[0]];
        let threshold = def.values.first().copied().unwrap_or(5.0) as i32;
        let multiplier = def.values.get(1).copied().unwrap_or(2.0);
        let copies = oil_cans.len() as i32;
        let saved = self.item_saved.entry(oil_cans[0]).or_insert(0);
        *saved += copies;
        if *saved < (threshold - 1).max(1) {
            return;
        }
        *saved -= threshold;
        let saved_value = *saved;
        self.item_saved.insert(oil_cans[0], saved_value);
        self.respin_best_column(multiplier);
    }

    fn respin_best_column(&mut self, multiplier: f64) {
        let mut best_column = 0usize;
        let mut best_value = i64::MIN;
        for x in 0..BOARD_COLS {
            let mut total = 0i64;
            for y in 0..BOARD_ROWS {
                if let Some(index) = self.board.slots[y * BOARD_COLS + x] {
                    if index < self.deck.len() {
                        total += self.deck[index].current_value(self.data) as i64;
                    }
                }
            }
            if total > best_value {
                best_value = total;
                best_column = x;
            }
        }
        let mut keep: Vec<usize> = Vec::new();
        for x in 0..BOARD_COLS {
            if x == best_column {
                continue;
            }
            for y in 0..BOARD_ROWS {
                if let Some(index) = self.board.slots[y * BOARD_COLS + x] {
                    keep.push(index);
                }
            }
        }
        let mut pool: Vec<usize> = (0..self.deck.len())
            .filter(|index| !keep.contains(index))
            .collect();
        self.rng.oil_can().shuffle(&mut pool);
        for y in 0..BOARD_ROWS {
            self.board.slots[y * BOARD_COLS + best_column] = pool.get(y).copied();
        }
        self.spin_column_mult[best_column] = multiplier;
    }

    fn apply_oil_can_essence(&mut self) {
        let Some(essence_id) = self
            .data
            .essence_index
            .get("oil_can_essence")
            .copied()
            .filter(|id| self.essences_held.contains(id))
        else {
            return;
        };
        let (need, multiplier) = {
            let def = &self.data.essences[essence_id];
            (
                def.values.first().copied().unwrap_or(7.0) as i32,
                def.values.get(1).copied().unwrap_or(2.0),
            )
        };
        self.respin_best_column(multiplier);
        let saved = self.essence_saved.entry(essence_id).or_insert(0);
        *saved += 1;
        if *saved >= need.max(1) {
            self.try_destroy_essence(essence_id);
        }
    }

    /// Swapping device: greedy one swap per copy, moving a bonus receiver next
    /// to its dog/bee/omelette source when that raises the adjacency total.
    fn apply_swapping_device(&mut self) {
        let copies = self
            .items_held
            .iter()
            .filter(|id| self.data.items[**id].name == "swapping_device")
            .count();
        if copies == 0 {
            return;
        }
        let mut current = self.adjacency_source_score();
        for _ in 0..copies.min(3) {
            let occupied: Vec<usize> = (0..BOARD_SLOTS)
                .filter(|slot| self.board.slots[*slot].is_some())
                .collect();
            let mut best: Option<(i32, usize, usize)> = None;
            for i in 0..occupied.len() {
                for j in (i + 1)..occupied.len() {
                    let a = occupied[i];
                    let b = occupied[j];
                    self.board.slots.swap(a, b);
                    let score = self.adjacency_source_score();
                    self.board.slots.swap(a, b);
                    if score > current
                        && best.map_or(true, |(best_score, _, _)| score > best_score)
                    {
                        best = Some((score, a, b));
                    }
                }
            }
            match best {
                Some((score, a, b)) => {
                    self.board.slots.swap(a, b);
                    current = score;
                }
                None => break,
            }
        }
    }

    fn adjacency_source_score(&self) -> i32 {
        let dog_bonus = self
            .data
            .symbol_id("dog")
            .and_then(|id| self.data.symbols[id].values.first().copied())
            .unwrap_or(2.0) as i32;
        let bee_bonus = self
            .data
            .symbol_id("bee")
            .and_then(|id| self.data.symbols[id].values.get(1).copied())
            .unwrap_or(1.0) as i32;
        let omelette_bonus = self
            .data
            .symbol_id("omelette")
            .and_then(|id| self.data.symbols[id].values.first().copied())
            .unwrap_or(2.0) as i32;
        let sources = [
            (self.data.symbol_id("dog"), "doglikes", dog_bonus),
            (self.data.symbol_id("bee"), "beelikes", bee_bonus),
            (self.data.symbol_id("omelette"), "omelettestuff", omelette_bonus),
        ];
        let mut score = 0;
        for slot in 0..BOARD_SLOTS {
            let Some(index) = self.board.slots[slot] else {
                continue;
            };
            let def = &self.data.symbols[self.deck[index].type_id as usize];
            let x = (slot % BOARD_COLS) as i32;
            let y = (slot / BOARD_COLS) as i32;
            for (source, group, amount) in sources {
                let Some(source) = source else {
                    continue;
                };
                if !def.groups.iter().any(|held| held == group) {
                    continue;
                }
                for dy in -1i32..=1 {
                    for dx in -1i32..=1 {
                        if dx == 0 && dy == 0 {
                            continue;
                        }
                        let nx = x + dx;
                        let ny = y + dy;
                        if nx < 0
                            || nx >= BOARD_COLS as i32
                            || ny < 0
                            || ny >= BOARD_ROWS as i32
                        {
                            continue;
                        }
                        if let Some(neighbor) =
                            self.board.slots[ny as usize * BOARD_COLS + nx as usize]
                        {
                            if self.deck[neighbor].type_id as usize == source {
                                score += amount;
                            }
                        }
                    }
                }
            }
        }
        score
    }

    /// Wildcards (the wildcard symbol, copycat cats and suits next to a card
    /// shark) share the best value in their connected component, mirroring the
    /// game's recursive wildcard propagation.
    fn apply_wildcard_values(
        &self,
        entries: &[(usize, usize, usize)],
        destroyed: &[bool],
        mult: &[f64],
        spin_bonus: &mut [i32],
    ) {
        let mut wildcarded = vec![false; self.deck.len()];
        for (index, _, _) in entries.iter().copied() {
            if destroyed.get(index).copied().unwrap_or(true) {
                continue;
            }
            let name = self.data.symbols[self.deck[index].type_id as usize]
                .name
                .as_str();
            wildcarded[index] = name == "wildcard"
                || (name == "cat" && (self.has_item("copycat") || self.copycat_active));
        }
        for (shark, x, y) in entries.iter().copied() {
            if destroyed[shark]
                || self.data.symbols[self.deck[shark].type_id as usize].name != "card_shark"
            {
                continue;
            }
            for n in self.neighbor_indexes(x, y, shark) {
                if !destroyed[n] && self.has_group(self.deck[n].type_id as usize, "suit") {
                    wildcarded[n] = true;
                }
            }
        }
        if !wildcarded.iter().any(|flag| *flag) {
            return;
        }
        let value_of = |index: usize, spin_bonus: &[i32]| -> f64 {
            let symbol = &self.deck[index];
            let def = &self.data.symbols[symbol.type_id as usize];
            (((def.base_value + symbol.bonus) as f64 * mult[index]) + spin_bonus[index] as f64)
                .max(0.0)
        };
        let mut visited = vec![false; self.deck.len()];
        for (start, _, _) in entries.iter().copied() {
            if destroyed[start] || !wildcarded[start] || visited[start] {
                continue;
            }
            let mut queue = vec![start];
            visited[start] = true;
            let mut component = Vec::new();
            let mut best = 0.0f64;
            while let Some(current) = queue.pop() {
                component.push(current);
                best = best.max(value_of(current, spin_bonus));
                let Some((_, x, y)) = entries
                    .iter()
                    .find(|(other, _, _)| *other == current)
                    .copied()
                else {
                    continue;
                };
                for n in self.neighbor_indexes(x, y, current) {
                    if destroyed[n] {
                        continue;
                    }
                    best = best.max(value_of(n, spin_bonus));
                    if wildcarded[n] && !visited[n] {
                        visited[n] = true;
                        queue.push(n);
                    }
                }
            }
            for member in component {
                let current = value_of(member, spin_bonus);
                if best > current + 0.5 {
                    spin_bonus[member] += (best - current).round() as i32;
                }
            }
        }
    }

    fn tick_items(&mut self) -> Vec<usize> {
        let mut spawned = Vec::new();
        let mut removed = Vec::new();
        for item_id in self.items_held.clone() {
            let (name, threshold) = {
                let def = &self.data.items[item_id];
                (
                    def.name.clone(),
                    def.values.first().copied().unwrap_or(0.0) as i32,
                )
            };
            match name.as_str() {
                "sunglasses" => {
                    let need = self.data.items[item_id]
                        .values
                        .get(1)
                        .copied()
                        .unwrap_or(3.0) as i32;
                    if self.removal_tokens >= need {
                        self.removal_tokens += threshold;
                    }
                    continue;
                }
                "dishwasher" => {
                    let need = self.data.items[item_id]
                        .values
                        .get(1)
                        .copied()
                        .unwrap_or(12.0)
                        .max(1.0) as i32;
                    let charge = self.item_saved.entry(item_id).or_insert(0);
                    *charge += 1;
                    if *charge >= need {
                        *charge = 0;
                        self.essence_tokens += threshold.max(1);
                    }
                    continue;
                }
                "frozen_pizza" => {
                    let charge = self.item_saved.entry(item_id).or_insert(0);
                    *charge += 1;
                    if *charge >= threshold.max(1) {
                        *charge = 0;
                        self.pending_extra_symbol_offers += 1;
                    }
                    continue;
                }
                "recycling" => {
                    self.reroll_tokens += threshold.max(1);
                    continue;
                }
                _ => {}
            }
            let adds: &[&str] = match name.as_str() {
                "treasure_map" => &["key", "treasure_chest"],
                "blue_suits" => &["clubs", "spades"],
                "red_suits" => &["diamonds", "hearts"],
                "cardboard_box" => {
                    let threshold = self.data.items[item_id]
                        .values
                        .get(1)
                        .copied()
                        .unwrap_or(10.0)
                        .max(1.0) as i32;
                    let age = self.item_ages.entry(item_id).or_insert(0);
                    *age += 1;
                    if *age >= threshold {
                        *age = 0;
                        let tokens = self.data.items[item_id]
                            .values
                            .first()
                            .copied()
                            .unwrap_or(1.0) as i32;
                        self.removal_tokens += tokens;
                    }
                    continue;
                }
                _ => continue,
            };
            let age = self.item_ages.entry(item_id).or_insert(0);
            *age += 1;
            if *age < threshold.max(1) {
                continue;
            }
            removed.push(item_id);
            self.item_ages.remove(&item_id);
            for tag in adds {
                if let Some(type_id) = self.data.symbol_id(tag) {
                    spawned.push(type_id);
                }
            }
        }
        self.items_held.retain(|id| !removed.contains(id));
        self.item_saved
            .retain(|id, _| self.items_held.contains(id));
        spawned
    }

    pub fn spin(&mut self) -> SpinOutcome {
        if self.phase != Phase::Ready {
            return SpinOutcome::default();
        }
        if self.coins >= 1_000_000_000 && self.has_essence("guillotine_essence") {
            self.phase = Phase::Done;
            return SpinOutcome::default();
        }
        if !self.boss_fight
            && self.spins_left == 2
            && self.has_essence("comfy_pillow_essence")
            && self.coins >= self.rent_due()
        {
            let occupied = self.board.occupied();
            if let Some(essence_id) = self
                .data
                .essence_index
                .get("comfy_pillow_essence")
                .copied()
            {
                self.try_destroy_essence(essence_id);
            }
            if !self.has_essence("comfy_pillow_essence") {
                self.spins_left = 0;
                self.item_offer_floor = Some((Rarity::VeryRare, 1));
                self.last_income = 0;
                self.pay_rent();
                return SpinOutcome {
                    income: 0,
                    coins: self.coins,
                    occupied,
                    spins_left: self.spins_left,
                    rent_paid: true,
                };
            }
        }
        if !self.boss_fight && self.spins_left == 1 && self.has_item("comfy_pillow") {
            let count = self
                .items_held
                .iter()
                .find(|id| self.data.items[**id].name == "comfy_pillow")
                .map(|id| {
                    self.data.items[*id]
                        .values
                        .get(1)
                        .copied()
                        .unwrap_or(1.0) as usize
                })
                .unwrap_or(1)
                .max(1);
            if self.coins >= self.rent_due() {
                let occupied = self.board.occupied();
                self.spins_left = 0;
                self.item_offer_floor = Some((Rarity::Rare, count));
                self.last_income = 0;
                self.pay_rent();
                return SpinOutcome {
                    income: 0,
                    coins: self.coins,
                    occupied,
                    spins_left: self.spins_left,
                    rent_paid: true,
                };
            }
        }
        self.add_dud_if_due();
        self.store_eggs_and_fish();
        self.consume_symbols_pre_spin();
        self.spin_column_mult = [1.0; BOARD_COLS];
        self.board.prune_invalid(self.deck.len());
        self.rng.begin_spin(self.total_spins + 1);
        self.rng.effect().begin_log();
        let mut reel_shuffle = self.rng.reel_shuffle();
        self.board.deal(&self.deck, &mut reel_shuffle, self.data);
        self.apply_oil_can();
        self.apply_oil_can_essence();
        self.apply_swapping_device();
        let occupied = self.board.occupied();
        let income = self.resolve_spin_effects();
        self.last_income = income;
        for (slot, index) in self.board.slots.iter().enumerate() {
            self.last_board[slot] = index
                .and_then(|index| self.deck.get(index))
                .map(|symbol| symbol.type_id as i16)
                .unwrap_or(-1);
        }
        // Real runs start with 1 coin, and every spin costs 1 coin.
        self.coins = (self.coins + income - 1).max(0);
        self.spins_left -= 1;
        self.total_spins += 1;

        if self.boss_fight {
            // Real boss damage is the spin's coin income; fine prints come
            // from that damage, not from rent payments.
            self.apply_boss_damage(income.min(self.boss_hp));
            self.boss_hp = (self.boss_hp - income).max(0);
            if self.boss_hp == 0 {
                self.phase = Phase::Done;
                self.won = true;
                return SpinOutcome {
                    income,
                    coins: self.coins,
                    occupied,
                    spins_left: self.spins_left,
                    rent_paid: false,
                };
            }
        }

        let mut rent_paid = false;
        if self.spins_left <= 0 {
            if self.coins < self.rent_due() {
                self.cash_out_savings();
            }
            if self.coins >= self.rent_due() {
                self.pay_rent();
                rent_paid = true;
            } else if let Some(position) = self
                .items_held
                .iter()
                .position(|id| self.data.items[*id].name == "devils_deal")
            {
                self.items_held.remove(position);
                self.item_saved.retain(|id, _| self.items_held.contains(id));
                self.pay_rent_waived();
                rent_paid = true;
            } else if self.has_essence("coffee_essence") {
                // Coffee essence buys a few extra spins instead of ending the run.
                let extra = self
                    .data
                    .essence_index
                    .get("coffee_essence")
                    .map(|id| {
                        self.data.essences[*id]
                            .values
                            .first()
                            .copied()
                            .unwrap_or(0.0) as i32
                            + 1
                    })
                    .unwrap_or(2);
                if let Some(position) = self
                    .essences_held
                    .iter()
                    .position(|id| self.data.essences[*id].name == "coffee_essence")
                {
                    self.essences_held.remove(position);
                }
                self.essence_saved
                    .retain(|id, _| self.essences_held.contains(id));
                self.spins_left += extra;
            } else if let Some(position) = self
                .items_held
                .iter()
                .position(|id| self.data.items[*id].name == "coffee")
            {
                // Coffee buys exactly one more spin before the next rent check.
                self.items_held.remove(position);
                self.item_saved.retain(|id, _| self.items_held.contains(id));
                self.spins_left += 1;
            } else {
                self.phase = Phase::Done;
            }
        } else if self.force_skip_reward {
            self.force_skip_reward = false;
            self.phase = Phase::Ready;
        } else {
            self.queue_extra_symbol_offers();
            let offer = self.roll_spin_reward();
            self.phase = Phase::SpinReward { offer };
        }
        SpinOutcome {
            income,
            coins: self.coins,
            occupied,
            spins_left: self.spins_left,
            rent_paid,
        }
    }

    /// Advances essence charge counters and resolves their one-shot payouts.
    /// Essences are free pickups offered at rent from payment four onward.
    fn tick_essences(&mut self, entries: &[(usize, usize, usize)], destroyed_count: i32) -> i32 {
        if self.essences_held.is_empty() {
            return 0;
        }
        let mut counts: HashMap<usize, i32> = HashMap::new();
        for id in &self.essences_held {
            *counts.entry(*id).or_insert(0) += 1;
        }
        let mut booze_displayed = false;
        let mut chest_displayed = false;
        for (index, _, _) in entries {
            let def = &self.data.symbols[self.deck[*index].type_id as usize];
            if def.groups.iter().any(|group| group == "booze") {
                booze_displayed = true;
            }
            if def.groups.iter().any(|group| group == "chest") {
                chest_displayed = true;
            }
        }

        let mut payout = 0;
        let mut destroyed: Vec<usize> = Vec::new();
        for (essence_id, copies) in counts {
            let def = &self.data.essences[essence_id];
            let name = def.name.clone();
            let v0 = def.values.first().copied().unwrap_or(0.0) as i32;
            let v1 = def.values.get(1).copied().unwrap_or(0.0) as i32;
            let v2 = def.values.get(2).copied().unwrap_or(0.0) as i32;
            if name == "black_pepper_essence" {
                if destroyed_count > 0 {
                    payout += v0 * destroyed_count * copies;
                    destroyed.push(essence_id);
                }
                continue;
            }
            if name == "recycling_essence" {
                self.reroll_tokens += v1.max(1);
                destroyed.push(essence_id);
                continue;
            }
            if name == "checkered_flag_essence" {
                let charges = entries
                    .iter()
                    .filter(|(index, _, _)| {
                        self.has_group(self.deck[*index].type_id as usize, "slow")
                            && self.deck[*index].current_value(self.data) > 0
                    })
                    .count() as i32;
                if charges > 0 {
                    *self.essence_saved.entry(essence_id).or_insert(0) += charges;
                    let saved = self.essence_saved.get(&essence_id).copied().unwrap_or(0);
                    if saved >= v0.max(1) {
                        destroyed.push(essence_id);
                    }
                }
                continue;
            }
            if name == "piggy_bank_essence" {
                *self.essence_saved.entry(essence_id).or_insert(0) += copies;
                let saved = self.essence_saved.get(&essence_id).copied().unwrap_or(0);
                if saved >= v1.max(1) {
                    destroyed.push(essence_id);
                    payout += v2 * copies;
                }
                continue;
            }
            if matches!(
                name.as_str(),
                "horseshoe_essence"
                    | "pool_ball_essence"
                    | "bowling_ball_essence"
                    | "four_leaf_clover_essence"
            ) {
                payout += v1 * copies;
            }
            let increments = match name.as_str() {
                "lockpick_essence" => {
                    if chest_displayed {
                        copies
                    } else {
                        0
                    }
                }
                "happy_hour_essence" => {
                    if booze_displayed {
                        copies
                    } else {
                        0
                    }
                }
                "lunchbox_essence"
                | "adoption_papers_essence"
                | "coin_on_a_string_essence"
                | "flush_essence"
                | "lint_roller_essence"
                | "fish_bowl_essence"
                | "barrel_o_dwarves_essence"
                | "nori_the_rabbit_essence"
                | "lucky_cat_essence"
                | "black_cat_essence"
                | "ritual_candle_essence"
                | "holy_water_essence"
                | "anthropology_degree_essence"
                | "blue_suits_essence"
                | "red_suits_essence"
                | "triple_coins_essence"
                | "birdhouse_essence"
                | "undertaker_essence"
                | "red_pepper_essence"
                | "green_pepper_essence"
                | "blue_pepper_essence"
                | "yellow_pepper_essence"
                | "purple_pepper_essence"
                | "cyan_pepper_essence"
                | "tax_evasion_essence"
                | "looting_glove_essence"
                | "kyle_the_kernite_essence"
                | "ricky_the_banana_essence"
                | "quigley_the_wolf_essence"
                | "compost_heap_essence"
                | "cleaning_rag_essence"
                | "fruit_basket_essence"
                | "goldilocks_essence"
                | "egg_carton_essence"
                | "lemon_essence"
                | "gray_pepper_essence"
                | "lime_pepper_essence"
                | "pink_pepper_essence"
                | "brown_pepper_essence"
                | "shedding_season_essence"
                | "sunglasses_essence"
                | "ancient_lizard_blade_essence"
                | "turtle_and_rabbit_essence"
                | "ninja_and_mouse_essence"
                | "rain_cloud_essence"
                | "cursed_katana_essence"
                | "fifth_ace_essence"
                | "copycat_essence"
                | "clear_sky_essence"
                | "mining_pick_essence"
                | "mobius_strip_essence"
                | "void_party_essence"
                | "void_portal_essence"
                | "credit_card_essence"
                | "dark_humor_essence"
                | "dwarven_anvil_essence"
                | "jackolantern_essence"
                | "maxwell_the_bear_essence"
                | "oswald_the_monkey_essence"
                | "wanted_poster_essence"
                | "zaroffs_contract_essence"
                | "grave_robber_essence"
                | "shrine_essence"
                | "frying_pan_essence"
                | "rusty_gear_essence"
                | "guillotine_essence"
                | "chicken_coop_essence"
                | "chili_powder_essence"
                | "comfy_pillow_essence"
                | "conveyor_belt_essence"
                | "oil_can_essence"
                | "reroll_essence"
                | "swapping_device_essence" => 0,
                _ => copies,
            };
            if increments == 0 {
                continue;
            }
            *self.essence_saved.entry(essence_id).or_insert(0) += increments;
            let saved = self.essence_saved.get(&essence_id).copied().unwrap_or(0);
            let threshold = match name.as_str() {
                "booster_pack_essence" => v1,
                "golden_carrot_essence" | "lucky_carrot_essence" => v1,
                _ => v0,
            }
            .max(1);
            if saved < threshold {
                continue;
            }
            destroyed.push(essence_id);
            match name.as_str() {
                "shattered_mirror_essence" => payout += v2 * copies,
                "dishwasher_essence" => self.essence_tokens += v1 * copies,
                "cardboard_box_essence" => self.removal_tokens += v1 * copies,
                "symbol_bomb_small_essence"
                | "symbol_bomb_big_essence"
                | "symbol_bomb_very_big_essence" => {
                    self.reroll_tokens += v1 * copies;
                    self.removal_tokens += v1 * copies;
                }
                "symbol_bomb_quantum_essence" => {
                    if let Some(item) = self.data.item_index.get("symbol_bomb_quantum").copied() {
                        for _ in 0..copies {
                            self.grant_item(item);
                        }
                    }
                }
                "golden_carrot_essence" | "lucky_carrot_essence" => {
                    let extra = (v0 as usize * copies as usize).max(1);
                    for _ in 0..extra {
                        self.symbol_forced_queue.push((Rarity::VeryRare, true));
                    }
                }
                "treasure_map_essence" => {
                    for name in ["key", "mega_chest"] {
                        if let Some(symbol) = self.data.symbol_id(name) {
                            let value = self.data.symbols[symbol].base_value;
                            self.deck.push(Symbol::new(symbol, value));
                            self.note_symbol_added(symbol);
                        }
                    }
                }
                "lucky_seven_essence" => {
                    self.chemical_seven_common = true;
                    self.refresh_symbol_pools();
                }
                "happy_hour_essence" => {
                    let multiplier = def.values.get(1).copied().unwrap_or(1.0);
                    for symbol in self.deck.iter_mut() {
                        if self.data.symbols[symbol.type_id as usize]
                            .groups
                            .iter()
                            .any(|group| group == "booze")
                        {
                            let extra =
                                (symbol.current_value(self.data) as f64 * (multiplier - 1.0))
                                    .round() as i32;
                            symbol.bonus += extra.max(1);
                        }
                    }
                }
                _ => {}
            }
        }
        for essence_id in destroyed {
            self.try_destroy_essence(essence_id);
        }
        payout
    }

    /// Essences that consume themselves once enough matching symbols are shown
    /// and permanently buff every symbol of that kind.
    fn tick_trigger_essences(&mut self, entries: &[(usize, usize, usize)]) {
        if self.essences_held.is_empty() {
            return;
        }
        let mut name_counts: HashMap<&str, i32> = HashMap::new();
        let mut group_counts: HashMap<&str, i32> = HashMap::new();
        for (index, _, _) in entries {
            let def = &self.data.symbols[self.deck[*index].type_id as usize];
            *name_counts.entry(def.name.as_str()).or_default() += 1;
            for group in &def.groups {
                *group_counts.entry(group.as_str()).or_default() += 1;
            }
        }
        let name_count = |name: &str| name_counts.get(name).copied().unwrap_or(0);
        let group_count = |group: &str| group_counts.get(group).copied().unwrap_or(0);
        let mut counts: HashMap<usize, i32> = HashMap::new();
        for id in &self.essences_held {
            *counts.entry(*id).or_insert(0) += 1;
        }
        let mut fired: Vec<(usize, String, i32, f64)> = Vec::new();
        for (essence_id, copies) in counts {
            let def = &self.data.essences[essence_id];
            let name = def.name.clone();
            let v0 = def.values.first().copied().unwrap_or(0.0) as i32;
            let v1 = def.values.get(1).copied().unwrap_or(1.0);
            let reached = match name.as_str() {
                "nori_the_rabbit_essence" => {
                    name_count("rabbit") + name_count("rabbit_fluff") >= v0
                }
                "lucky_cat_essence" | "black_cat_essence" => name_count("cat") >= v0,
                "ritual_candle_essence" => group_count("fossillikes") >= v0,
                "holy_water_essence" => group_count("hex") >= v0,
                "anthropology_degree_essence" => group_count("human") >= v0,
                "blue_suits_essence" => name_count("clubs") + name_count("spades") >= v0,
                "red_suits_essence" => name_count("hearts") + name_count("diamonds") >= v0,
                "triple_coins_essence" => name_count("coin") >= v0,
                "birdhouse_essence" => group_count("bird") >= v0,
                "undertaker_essence" => name_count("spirit") >= v0,
                "kyle_the_kernite_essence" => group_count("kyle") >= v0,
                "ricky_the_banana_essence" => {
                    name_count("banana") + name_count("banana_peel") >= v0
                }
                "quigley_the_wolf_essence" => name_count("dog") + name_count("wolf") >= v0,
                "cleaning_rag_essence" => group_count("gem") >= v0,
                "fruit_basket_essence" => group_count("fruit") >= v0,
                "goldilocks_essence" => name_count("bear") >= v0,
                _ => false,
            };
            if reached {
                fired.push((essence_id, name, copies, v1));
            }
        }
        for (essence_id, name, copies, v1) in fired {
            let (names, groups): (&[&str], &[&str]) = match name.as_str() {
                "nori_the_rabbit_essence" => (&["rabbit", "rabbit_fluff"], &[]),
                "black_cat_essence" => (&["cat"], &[]),
                "ritual_candle_essence" => (&[], &["fossillikes"]),
                "holy_water_essence" => (&[], &["hex"]),
                "anthropology_degree_essence" => (&[], &["human"]),
                "blue_suits_essence" => (&["clubs", "spades"], &[]),
                "red_suits_essence" => (&["hearts", "diamonds"], &[]),
                "triple_coins_essence" => (&["coin"], &[]),
                "birdhouse_essence" => (&[], &["bird"]),
                "undertaker_essence" => (&["spirit"], &[]),
                "kyle_the_kernite_essence" => (&[], &["kyle"]),
                "ricky_the_banana_essence" => (&["banana", "banana_peel"], &[]),
                "quigley_the_wolf_essence" => (&["wolf"], &[]),
                "cleaning_rag_essence" => (&[], &["gem"]),
                "fruit_basket_essence" => (&[], &["fruit"]),
                "goldilocks_essence" => (&["bear"], &[]),
                _ => (&[], &[]),
            };
            if names.is_empty() && groups.is_empty() {
                continue;
            }
            let multiplier = matches!(
                name.as_str(),
                "nori_the_rabbit_essence"
                    | "black_cat_essence"
                    | "ritual_candle_essence"
                    | "holy_water_essence"
                    | "anthropology_degree_essence"
                    | "cleaning_rag_essence"
                    | "fruit_basket_essence"
            );
            for symbol in self.deck.iter_mut() {
                let def = &self.data.symbols[symbol.type_id as usize];
                let hit = names.iter().any(|name| def.name == *name)
                    || groups
                        .iter()
                        .any(|group| def.groups.iter().any(|tag| tag == group));
                if !hit {
                    continue;
                }
                if multiplier {
                    let current = symbol.current_value(self.data);
                    symbol.multiplier = 1;
                    symbol.bonus =
                        (current as f64 * v1.powi(copies)).round() as i32 - def.base_value;
                } else {
                    symbol.bonus += v1 as i32 * copies;
                }
            }
            self.try_destroy_essence(essence_id);
        }
        self.essence_saved
            .retain(|id, _| self.essences_held.contains(id));
    }

    fn tick_permanent_essences(
        &mut self,
        entries: &[(usize, usize, usize)],
        destroyed: &[bool],
        destroyed_count: i32,
    ) -> i32 {
        if self.essences_held.is_empty() {
            return 0;
        }
        let mut counts: HashMap<usize, i32> = HashMap::new();
        for id in &self.essences_held {
            *counts.entry(*id).or_insert(0) += 1;
        }
        let destroyed_boxes = self
            .destroyed_history
            .iter()
            .filter(|type_id| self.has_group(**type_id as usize, "box"))
            .count() as i32;
        let destroyed_void = self
            .destroyed_history
            .iter()
            .filter(|type_id| self.has_group(**type_id as usize, "void"))
            .count() as i32;
        let destroyed_minerlikes = self
            .destroyed_history
            .iter()
            .filter(|type_id| self.has_group(**type_id as usize, "minerlikes"))
            .count() as i32;
        let destroyed_spirits = self
            .destroyed_history
            .iter()
            .filter(|type_id| {
                self.data.symbols[**type_id as usize].name == "spirit"
            })
            .count() as i32;
        let mut fired: Vec<(usize, String, i32)> = Vec::new();
        for (essence_id, copies) in counts {
            let def = &self.data.essences[essence_id];
            let name = def.name.clone();
            let v0 = def.values.first().copied().unwrap_or(0.0) as i32;
            let trigger = match name.as_str() {
                "turtle_and_rabbit_essence" => {
                    self.has_adjacent_pair(entries, "rabbit", "turtle")
                }
                "ninja_and_mouse_essence" => {
                    self.has_adjacent_pair(entries, "ninja", "mouse")
                }
                "rain_cloud_essence" => self.has_adjacent_pair(entries, "rain", "flower"),
                "cursed_katana_essence" => {
                    entries
                        .iter()
                        .filter(|(index, _, _)| {
                            self.data.symbols[self.deck[*index].type_id as usize].name == "ninja"
                        })
                        .count() as i32
                        >= v0
                }
                "fifth_ace_essence" => {
                    entries
                        .iter()
                        .filter(|(index, _, _)| {
                            self.has_group(self.deck[*index].type_id as usize, "suit")
                        })
                        .count() as i32
                        >= v0
                }
                "copycat_essence" => self.has_clump_of(entries, "cat", v0.max(1) as usize),
                "clear_sky_essence" => self.has_adjacent_pair(entries, "sun", "moon"),
                "mining_pick_essence" => destroyed_minerlikes >= v0,
                "mobius_strip_essence" => destroyed_boxes >= v0,
                "void_party_essence" => destroyed_void >= v0,
                "void_portal_essence" => destroyed_count > 0,
                "dark_humor_essence" => {
                    self.destroyed_matching(entries, destroyed, Some("comedian"), None)
                        && self.displayed_matching(entries, destroyed, "banana_peel")
                }
                "dwarven_anvil_essence" => {
                    self.destroyed_matching(entries, destroyed, None, Some("anvillikes"))
                        && self.displayed_matching(entries, destroyed, "dwarf")
                }
                "jackolantern_essence" => {
                    self.destroyed_matching(entries, destroyed, Some("candy"), None)
                        && self.displayed_matching(entries, destroyed, "toddler")
                }
                "maxwell_the_bear_essence" => {
                    self.destroyed_matching(entries, destroyed, Some("honey"), None)
                        && self.displayed_matching(entries, destroyed, "bear")
                }
                "oswald_the_monkey_essence" => {
                    self.destroyed_matching(entries, destroyed, None, Some("monkeylikes"))
                        && self.displayed_matching(entries, destroyed, "monkey")
                }
                "wanted_poster_essence" => {
                    self.destroyed_matching(entries, destroyed, Some("thief"), None)
                        && self.displayed_matching(entries, destroyed, "bounty_hunter")
                }
                "zaroffs_contract_essence" => {
                    self.destroyed_matching(entries, destroyed, None, Some("human"))
                        && (self.displayed_matching(entries, destroyed, "bounty_hunter")
                            || self.displayed_matching(entries, destroyed, "general_zaroff"))
                }
                "grave_robber_essence" => {
                    self.destroyed_matching(entries, destroyed, None, Some("spiritbox"))
                }
                "shrine_essence" => destroyed_spirits >= v0,
                "rusty_gear_essence" => {
                    let mut names: Vec<String> = entries
                        .iter()
                        .map(|(index, _, _)| {
                            self.data.symbols[self.deck[*index].type_id as usize]
                                .name
                                .clone()
                        })
                        .collect();
                    names.sort();
                    names.dedup();
                    names.iter().any(|name| {
                        self.has_clump_of(entries, name.as_str(), v0.max(1) as usize)
                    })
                }
                _ => false,
            };
            if trigger {
                fired.push((essence_id, name, copies));
            }
        }
        let mut payout = 0;
        for (essence_id, name, copies) in fired {
            let values = &self.data.essences[essence_id].values;
            let v0 = values.first().copied().unwrap_or(0.0);
            let v1 = values.get(1).copied().unwrap_or(1.0);
            match name.as_str() {
                "turtle_and_rabbit_essence" => {
                    self.add_permanent_name_bonus("rabbit", v0 as i32 * copies);
                    self.add_permanent_name_bonus("turtle", v0 as i32 * copies);
                }
                "ninja_and_mouse_essence" => {
                    self.add_permanent_name_bonus("ninja", v0 as i32 * copies);
                    self.add_permanent_name_bonus("mouse", v0 as i32 * copies);
                }
                "rain_cloud_essence" => {
                    self.add_permanent_name_bonus("rain", v0 as i32 * copies);
                    self.add_permanent_name_bonus("flower", v0 as i32 * copies);
                }
                "cursed_katana_essence" => {
                    self.add_permanent_name_bonus("ninja", v1 as i32 * copies);
                }
                "fifth_ace_essence" => {
                    self.add_permanent_group_bonus("suit", v1 as i32 * copies);
                }
                "copycat_essence" => {
                    self.copycat_active = true;
                    self.multiply_permanent_name("cat", v1.powi(copies));
                }
                "clear_sky_essence" => {
                    self.clear_sky_active = true;
                    self.multiply_permanent_name("sun", v0.powi(copies));
                    self.multiply_permanent_name("moon", v0.powi(copies));
                }
                "mining_pick_essence" => {
                    payout += v1 as i32 * copies;
                }
                "mobius_strip_essence" => {
                    self.multiply_permanent_group("box", v1.powi(copies));
                }
                "void_party_essence" => {
                    self.multiply_permanent_group("void", v1.powi(copies));
                }
                "void_portal_essence" => {
                    let step = v1.max(1.0) as i32;
                    payout += (self.destroyed_history.len() as i32 / step) * v0 as i32 * copies;
                }
                "dark_humor_essence" => {
                    self.add_permanent_group_bonus("funny", v0 as i32 * copies);
                }
                "dwarven_anvil_essence" => {
                    self.multiply_permanent_name("dwarf", v0.powi(copies));
                }
                "jackolantern_essence" => {
                    self.multiply_permanent_group("halloween", v0.powi(copies));
                }
                "maxwell_the_bear_essence" => {
                    self.multiply_permanent_name("bear", v0.powi(copies));
                }
                "oswald_the_monkey_essence" => {
                    self.multiply_permanent_name("monkey", v0.powi(copies));
                }
                "wanted_poster_essence" => {
                    self.multiply_permanent_name("bounty_hunter", v0.powi(copies));
                }
                "zaroffs_contract_essence" => {
                    payout += v0 as i32 * copies;
                }
                "grave_robber_essence" => {
                    self.grave_robber_mult = self
                        .grave_robber_mult
                        .saturating_mul((v0 as i32).pow(copies as u32));
                }
                "shrine_essence" => {
                    self.shrine_active = true;
                }
                "rusty_gear_essence" => {
                    let mut names: Vec<String> = entries
                        .iter()
                        .map(|(index, _, _)| {
                            self.data.symbols[self.deck[*index].type_id as usize]
                                .name
                                .clone()
                        })
                        .collect();
                    names.sort();
                    names.dedup();
                    for name in names {
                        if self.has_clump_of(entries, &name, v0.max(1.0) as usize) {
                            self.multiply_permanent_name(&name, v1.powi(copies));
                        }
                    }
                }
                _ => {}
            }
            self.try_destroy_essence(essence_id);
        }
        payout
    }

    pub fn has_fine_print(&self, number: u32) -> bool {
        self.fine_prints.contains(&number)
    }

    pub fn has_item(&self, name: &str) -> bool {
        self.items_held
            .iter()
            .any(|id| self.data.items[*id].name == name)
    }

    pub fn has_essence(&self, name: &str) -> bool {
        self.essences_held
            .iter()
            .any(|id| self.data.essences[*id].name == name)
    }

    fn essence_protection_limit(&self) -> Option<i32> {
        let item_limit = self
            .items_held
            .iter()
            .find(|id| self.data.items[**id].name == "popsicle")
            .map(|id| {
                self.data.items[*id]
                    .values
                    .first()
                    .copied()
                    .unwrap_or(2.0) as i32
            });
        let essence_limit = self
            .data
            .essence_index
            .get("popsicle_essence")
            .copied()
            .filter(|id| self.essences_held.contains(id))
            .map(|id| {
                self.data.essences[id]
                    .values
                    .first()
                    .copied()
                    .unwrap_or(2.0) as i32
            });
        item_limit.or(essence_limit)
    }

    fn try_destroy_essence(&mut self, essence_id: usize) {
        if let Some(limit) = self.essence_protection_limit() {
            let counter = self
                .essence_destroy_counters
                .entry(essence_id)
                .or_insert(0);
            *counter += 1;
            if *counter < limit.max(1) {
                return;
            }
        }
        self.essences_held.retain(|held| *held != essence_id);
        self.essence_destroy_counters.remove(&essence_id);
        self.essence_saved
            .retain(|held, _| self.essences_held.contains(held));
        if let Some(popsicle) = self.data.essence_index.get("popsicle_essence").copied() {
            if popsicle != essence_id && self.essences_held.contains(&popsicle) {
                self.essences_held.retain(|held| *held != popsicle);
                self.essence_saved
                    .retain(|held, _| self.essences_held.contains(held));
            }
        }
    }

    fn spawn_multiplier(&self) -> f64 {
        let item_product = self
            .items_held
            .iter()
            .filter(|id| self.data.items[**id].name == "conveyor_belt")
            .map(|id| self.data.items[*id].values.first().copied().unwrap_or(2.0))
            .product::<f64>()
            .max(1.0);
        item_product * self.spawner_bonus_mult
    }

    fn note_spawner_spawns(&mut self, count: usize) {
        if count == 0 || !self.has_essence("conveyor_belt_essence") {
            return;
        }
        self.spawner_spawns += count as i32;
        let need = self
            .data
            .essence_index
            .get("conveyor_belt_essence")
            .map(|id| {
                self.data.essences[*id]
                    .values
                    .first()
                    .copied()
                    .unwrap_or(5.0) as i32
            })
            .unwrap_or(5)
            .max(1);
        if self.spawner_spawns >= need {
            let factor = self
                .data
                .essence_index
                .get("conveyor_belt_essence")
                .map(|id| {
                    self.data.essences[*id]
                        .values
                        .get(1)
                        .copied()
                        .unwrap_or(2.0)
                })
                .unwrap_or(2.0);
            if let Some(essence_id) = self
                .data
                .essence_index
                .get("conveyor_belt_essence")
                .copied()
            {
                self.try_destroy_essence(essence_id);
                if !self.has_essence("conveyor_belt_essence") {
                    self.spawner_bonus_mult *= factor;
                }
            }
        }
    }

    fn item_multiplier(&self, name: &str, index: usize) -> f64 {
        self.items_held
            .iter()
            .filter(|id| self.data.items[**id].name == name)
            .map(|id| {
                self.data.items[*id]
                    .values
                    .get(index)
                    .copied()
                    .unwrap_or(1.0)
            })
            .product::<f64>()
            .max(1.0)
    }

    fn permanent_bonus_for(&self, def: &crate::data::Def) -> i32 {
        let mut bonus = self
            .permanent_name_bonus
            .get(&def.name)
            .copied()
            .unwrap_or(0);
        for group in &def.groups {
            bonus += self.permanent_group_bonus.get(group).copied().unwrap_or(0);
        }
        bonus
    }

    fn permanent_mult_for(&self, def: &crate::data::Def) -> f64 {
        let mut multiplier = self
            .permanent_name_mult
            .get(&def.name)
            .copied()
            .unwrap_or(1.0);
        for group in &def.groups {
            multiplier *= self.permanent_group_mult.get(group).copied().unwrap_or(1.0);
        }
        multiplier
    }

    fn add_permanent_name_bonus(&mut self, name: &str, amount: i32) {
        if amount == 0 {
            return;
        }
        *self
            .permanent_name_bonus
            .entry(name.to_string())
            .or_insert(0) += amount;
    }

    fn add_permanent_group_bonus(&mut self, group: &str, amount: i32) {
        if amount == 0 {
            return;
        }
        *self
            .permanent_group_bonus
            .entry(group.to_string())
            .or_insert(0) += amount;
    }

    fn multiply_permanent_name(&mut self, name: &str, factor: f64) {
        *self
            .permanent_name_mult
            .entry(name.to_string())
            .or_insert(1.0) *= factor;
    }

    fn multiply_permanent_group(&mut self, group: &str, factor: f64) {
        *self
            .permanent_group_mult
            .entry(group.to_string())
            .or_insert(1.0) *= factor;
    }

    fn destroyed_matching(
        &self,
        entries: &[(usize, usize, usize)],
        destroyed: &[bool],
        name: Option<&str>,
        group: Option<&str>,
    ) -> bool {
        entries.iter().any(|(index, _, _)| {
            if !destroyed[*index] {
                return false;
            }
            let def = &self.data.symbols[self.deck[*index].type_id as usize];
            name.map_or(true, |wanted| def.name == wanted)
                && group.map_or(true, |wanted| {
                    def.groups.iter().any(|tag| tag == wanted)
                })
        })
    }

    fn displayed_matching(
        &self,
        entries: &[(usize, usize, usize)],
        destroyed: &[bool],
        name: &str,
    ) -> bool {
        entries.iter().any(|(index, _, _)| {
            !destroyed[*index]
                && self.data.symbols[self.deck[*index].type_id as usize].name == name
        })
    }

    fn has_adjacent_pair(
        &self,
        entries: &[(usize, usize, usize)],
        first: &str,
        second: &str,
    ) -> bool {
        for (index, x, y) in entries {
            if self.data.symbols[self.deck[*index].type_id as usize].name != first {
                continue;
            }
            for neighbor in self.neighbor_indexes(*x, *y, *index) {
                if self.data.symbols[self.deck[neighbor].type_id as usize].name == second {
                    return true;
                }
            }
        }
        false
    }

    fn has_clump_of(
        &self,
        entries: &[(usize, usize, usize)],
        name: &str,
        want: usize,
    ) -> bool {
        use std::collections::{HashSet, VecDeque};
        let mut cells: HashMap<(usize, usize), usize> = HashMap::new();
        for (index, x, y) in entries {
            if self.data.symbols[self.deck[*index].type_id as usize].name == name {
                cells.insert((*x, *y), *index);
            }
        }
        let mut seen: HashSet<(usize, usize)> = HashSet::new();
        for start in cells.keys().copied() {
            if seen.contains(&start) {
                continue;
            }
            let mut queue = VecDeque::new();
            let mut size = 0usize;
            queue.push_back(start);
            seen.insert(start);
            while let Some((x, y)) = queue.pop_front() {
                size += 1;
                if size >= want {
                    return true;
                }
                for dx in -1i32..=1 {
                    for dy in -1i32..=1 {
                        if dx == 0 && dy == 0 {
                            continue;
                        }
                        let nx = x as i32 + dx;
                        let ny = y as i32 + dy;
                        if nx < 0 || ny < 0 || nx >= BOARD_COLS as i32 || ny >= BOARD_ROWS as i32 {
                            continue;
                        }
                        let position = (nx as usize, ny as usize);
                        if cells.contains_key(&position) && seen.insert(position) {
                            queue.push_back(position);
                        }
                    }
                }
            }
        }
        false
    }

    fn essence_count(&self, name: &str) -> i32 {
        self.essences_held
            .iter()
            .filter(|id| self.data.essences[**id].name == name)
            .count() as i32
    }

    pub fn must_take_symbol(&self) -> bool {
        self.force_take_symbol
    }

    fn capsule_multiplier(&self) -> i32 {
        let mut multiplier = 1i32;
        for item_id in &self.items_held {
            let def = &self.data.items[*item_id];
            if def.name == "capsule_machine" {
                multiplier *= def.values.first().copied().unwrap_or(1.0) as i32;
            }
        }
        for essence_id in &self.essences_held {
            let def = &self.data.essences[*essence_id];
            if def.name == "capsule_machine_essence" {
                multiplier *= def.values.first().copied().unwrap_or(1.0) as i32;
            }
        }
        multiplier.max(1)
    }

    /// Piggy bank and swear jar can be smashed during the rent popup; each
    /// pays out saved coins times its second value.
    fn cash_out_savings(&mut self) {
        let due = self.rent_due();
        let gap = due - self.coins;
        if gap <= 0 {
            return;
        }
        // Devil's deal essences are one-shot rent bailouts.
        if self.has_essence("devils_deal_essence") {
            let payout = self
                .data
                .essence_index
                .get("devils_deal_essence")
                .map(|id| {
                    self.data.essences[*id]
                        .values
                        .first()
                        .copied()
                        .unwrap_or(33.0) as i32
                })
                .unwrap_or(33);
            while self.coins < due && self.has_essence("devils_deal_essence") {
                if let Some(position) = self
                    .essences_held
                    .iter()
                    .position(|id| self.data.essences[*id].name == "devils_deal_essence")
                {
                    self.essences_held.remove(position);
                    self.coins += payout;
                }
            }
            self.essence_saved
                .retain(|id, _| self.essences_held.contains(id));
            if self.coins >= due {
                return;
            }
        }
        let mut options: Vec<(usize, usize, i32)> = Vec::new();
        for (position, item_id) in self.items_held.iter().copied().enumerate() {
            let def = &self.data.items[item_id];
            if def.name != "piggy_bank" && def.name != "swear_jar" {
                continue;
            }
            let saved = self.item_saved.get(&item_id).copied().unwrap_or(0);
            let multiplier = def.values.get(1).copied().unwrap_or(0.0);
            let payout = (saved as f64 * multiplier).round() as i32;
            if payout > 0 {
                options.push((position, item_id, payout));
            }
        }
        if options.is_empty() {
            return;
        }
        let smallest_sufficient = options
            .iter()
            .copied()
            .filter(|(_, _, payout)| *payout >= gap)
            .min_by_key(|(_, _, payout)| *payout);
        let chosen: Vec<(usize, usize, i32)> = match smallest_sufficient {
            Some(pick) => vec![pick],
            None => {
                let mut all = options;
                all.sort_unstable_by(|a, b| b.2.cmp(&a.2));
                all
            }
        };
        let mut removal_order = chosen;
        removal_order.sort_unstable_by(|a, b| b.0.cmp(&a.0));
        for (position, item_id, payout) in removal_order {
            if position < self.items_held.len() && self.items_held[position] == item_id {
                self.items_held.remove(position);
                self.item_saved.remove(&item_id);
                self.coins += payout;
            }
        }
    }

    fn fp_value(&self, number: u32, index: usize) -> f64 {
        if !self.has_fine_print(number) {
            return 0.0;
        }
        self.data
            .fine_prints
            .get(&number)
            .and_then(|def| def.values.get(index).copied())
            .unwrap_or(0.0)
    }

    pub(crate) fn fp_affects(&self, number: u32, name: &str) -> bool {
        self.has_fine_print(number)
            && self
                .fine_print_icons
                .get(&number)
                .map_or(true, |icon| icon == name)
    }

    /// Pick the one symbol/item type a reliant clause names; the real game
    /// chooses among the most-held matching types.
    fn choose_fine_print_icon(&mut self, number: u32) -> Option<String> {
        let def = self.data.fine_prints.get(&number)?;
        if let Some(name) = def
            .reliant_types
            .iter()
            .find_map(|value| value.strip_prefix("item_"))
        {
            return Some(name.to_string());
        }
        if let Some(group) = def
            .reliant_groups
            .iter()
            .find_map(|value| value.strip_prefix("item_"))
        {
            let group = group.to_string();
            let names = self.most_common_item(|item| {
                item.groups.iter().any(|value| value == &group)
            });
            if !names.is_empty() {
                let pick = self.rng.fineprint().randi_max(names.len());
                return Some(names[pick].clone());
            }
            return None;
        }
        if let Some(name) = def.reliant_types.first() {
            return Some(name.clone());
        }
        if let Some(group) = def.reliant_groups.first() {
            let group = group.clone();
            let names = self.most_common_symbol(|symbol| {
                symbol.groups.iter().any(|value| value == &group)
            });
            if !names.is_empty() {
                let pick = self.rng.fineprint().randi_max(names.len());
                return Some(names[pick].clone());
            }
        }
        None
    }

    fn most_common_symbol<F>(&self, matches: F) -> Vec<String>
    where
        F: Fn(&crate::data::Def) -> bool,
    {
        let mut counts: HashMap<&str, usize> = HashMap::new();
        for symbol in &self.deck {
            let def = &self.data.symbols[symbol.type_id as usize];
            if def.name != "empty" && matches(def) {
                *counts.entry(def.name.as_str()).or_default() += 1;
            }
        }
        let Some(highest) = counts.values().copied().max() else {
            return Vec::new();
        };
        counts
            .into_iter()
            .filter(|(_, count)| *count == highest)
            .map(|(name, _)| name.to_string())
            .collect()
    }

    fn most_common_item<F>(&self, matches: F) -> Vec<String>
    where
        F: Fn(&crate::data::Def) -> bool,
    {
        let mut counts: HashMap<&str, usize> = HashMap::new();
        for item_id in &self.items_held {
            let def = &self.data.items[*item_id];
            if matches(def) {
                *counts.entry(def.name.as_str()).or_default() += 1;
            }
        }
        let Some(highest) = counts.values().copied().max() else {
            return Vec::new();
        };
        counts
            .into_iter()
            .filter(|(_, count)| *count == highest)
            .map(|(name, _)| name.to_string())
            .collect()
    }

    fn fp_applicable(&self, number: u32) -> bool {
        let Some(def) = self.data.fine_prints.get(&number) else {
            return false;
        };
        if !def.reliant_types.is_empty() {
            let has = def.reliant_types.iter().any(|name| {
                if let Some(item_name) = name.strip_prefix("item_") {
                    self.items_held
                        .iter()
                        .any(|id| self.data.items[*id].name == item_name)
                } else {
                    self.deck
                        .iter()
                        .any(|symbol| self.data.symbols[symbol.type_id as usize].name == *name)
                }
            });
            if !has {
                return false;
            }
        }
        if !def.reliant_groups.is_empty() {
            let has = def.reliant_groups.iter().any(|group| {
                if let Some(item_group) = group.strip_prefix("item_") {
                    self.items_held.iter().any(|id| {
                        self.data.items[*id]
                            .groups
                            .iter()
                            .any(|item| item == item_group)
                    })
                } else {
                    self.deck.iter().any(|symbol| {
                        self.data.symbols[symbol.type_id as usize]
                            .groups
                            .iter()
                            .any(|item| item == group)
                    })
                }
            });
            if !has {
                return false;
            }
        }
        true
    }

    /// A20 boss: three fine prints for every 150 damage the landlord takes.
    fn apply_boss_damage(&mut self, damage: i32) {
        self.fine_print_counter += damage;
        while self.fine_print_counter >= 150 && self.fine_prints.len() < 31 {
            self.fine_print_counter -= 150;
            for _ in 0..3 {
                if self.fine_prints.len() >= 31 {
                    break;
                }
                // Base game pool is 4 plus 8-37; the 1/2/3/5/6/7 theft
                // clauses are legacy entries the game never selects.
                const BASE_FINE_PRINTS: [u32; 31] = [
                    4, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26,
                    27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37,
                ];
                let candidates: Vec<u32> = BASE_FINE_PRINTS
                    .iter()
                    .copied()
                    .filter(|number| {
                        !self.fine_prints.contains(number) && self.fp_applicable(*number)
                    })
                    .collect();
                if candidates.is_empty() {
                    break;
                }
                let pick = self.rng.fineprint().range(0, candidates.len());
                let number = candidates[pick];
                self.fine_prints.push(number);
                if let Some(icon) = self.choose_fine_print_icon(number) {
                    self.fine_print_icons.insert(number, icon);
                }
                match number {
                    1 => self.steal_item_of_rarity("common"),
                    2 => self.steal_item_of_rarity("uncommon"),
                    3 => self.steal_item_of_rarity("rare"),
                    5 => self.steal_symbol_of_rarity("common"),
                    6 => self.steal_symbol_of_rarity("uncommon"),
                    7 => self.steal_symbol_of_rarity("rare"),
                    4 => {
                        if let Some(dud) = self.data.symbol_id("dud") {
                            self.deck.push(Symbol::new(dud, 0));
                        }
                    }
                    14 => {
                        if let Some(hex) = self.data.symbol_id("hex_of_destruction") {
                            self.deck.push(Symbol::new(hex, 0));
                        }
                    }
                    _ => {}
                }
            }
        }
    }

    /// Clauses 1-3: the landlord takes one copy of the most-held item of a rarity.
    fn steal_item_of_rarity(&mut self, rarity: &str) {
        let mut counts: HashMap<usize, usize> = HashMap::new();
        for item_id in &self.items_held {
            if self.data.items[*item_id].rarity == rarity {
                *counts.entry(*item_id).or_default() += 1;
            }
        }
        let Some(highest) = counts.values().copied().max() else {
            return;
        };
        let mut choices: Vec<usize> = counts
            .into_iter()
            .filter(|(_, count)| *count == highest)
            .map(|(item_id, _)| item_id)
            .collect();
        choices.sort_unstable();
        let pick = choices[self.rng.range(0, choices.len())];
        if let Some(position) = self.items_held.iter().position(|item| *item == pick) {
            self.items_held.remove(position);
            self.item_saved.remove(&pick);
        }
    }

    /// Clauses 5-7: the landlord empties one copy of the most-held symbol of a rarity.
    fn steal_symbol_of_rarity(&mut self, rarity: &str) {
        let mut counts: HashMap<usize, usize> = HashMap::new();
        for symbol in &self.deck {
            let def = &self.data.symbols[symbol.type_id as usize];
            if def.rarity == rarity {
                *counts.entry(symbol.type_id as usize).or_default() += 1;
            }
        }
        let Some(highest) = counts.values().copied().max() else {
            return;
        };
        let mut choices: Vec<usize> = counts
            .into_iter()
            .filter(|(_, count)| *count == highest)
            .map(|(type_id, _)| type_id)
            .collect();
        choices.sort_unstable();
        let pick = choices[self.rng.range(0, choices.len())] as u16;
        if let Some(position) = self.deck.iter().position(|symbol| symbol.type_id == pick) {
            self.board.remove_deck_indices(&[position]);
            self.deck.remove(position);
        }
    }

    fn has_group(&self, type_id: usize, group: &str) -> bool {
        self.data.symbols[type_id].groups.iter().any(|item| item == group)
    }

    fn neighbor_indexes(&self, x: usize, y: usize, exclude: usize) -> Vec<usize> {
        if self.spin_all_adjacent {
            let mut all = Vec::with_capacity(BOARD_SLOTS);
            for entry in self.board.slots.iter() {
                if let Some(index) = *entry {
                    if index != exclude {
                        all.push(index);
                    }
                }
            }
            return all;
        }
        let mut result = Vec::with_capacity(8);
        for dy in -1i32..=1 {
            for dx in -1i32..=1 {
                if dx == 0 && dy == 0 {
                    continue;
                }
                // Fine print 22: symbols directly above or below are not adjacent.
                if dx == 0 && self.has_fine_print(22) {
                    continue;
                }
                let nx = x as i32 + dx;
                let ny = y as i32 + dy;
                if nx < 0
                    || nx >= BOARD_COLS as i32
                    || ny < 0
                    || ny >= BOARD_ROWS as i32
                {
                    continue;
                }
                if let Some(index) = self.board.slots[ny as usize * BOARD_COLS + nx as usize] {
                    if index != exclude && !result.contains(&index) {
                        result.push(index);
                    }
                }
            }
        }
        if self.spin_corner_adjacent {
            let corner = (x == 0 || x == BOARD_COLS - 1) && (y == 0 || y == BOARD_ROWS - 1);
            if corner {
                return self
                    .board
                    .slots
                    .iter()
                    .filter_map(|entry| *entry)
                    .filter(|index| *index != exclude)
                    .collect();
            }
            for (cx, cy) in [
                (0usize, 0usize),
                (0, BOARD_ROWS - 1),
                (BOARD_COLS - 1, 0),
                (BOARD_COLS - 1, BOARD_ROWS - 1),
            ] {
                if let Some(index) = self.board.slots[cy * BOARD_COLS + cx] {
                    if index != exclude && !result.contains(&index) {
                        result.push(index);
                    }
                }
            }
        }
        result
    }

    fn empty_neighbor_count(&self, x: usize, y: usize) -> i32 {
        let mut count = 0;
        for dy in -1i32..=1 {
            for dx in -1i32..=1 {
                if dx == 0 && dy == 0 {
                    continue;
                }
                if dx == 0 && self.has_fine_print(22) {
                    continue;
                }
                let nx = x as i32 + dx;
                let ny = y as i32 + dy;
                if nx < 0
                    || nx >= BOARD_COLS as i32
                    || ny < 0
                    || ny >= BOARD_ROWS as i32
                {
                    continue;
                }
                if self.board.slots[ny as usize * BOARD_COLS + nx as usize].is_none() {
                    count += 1;
                }
            }
        }
        count
    }

    fn pick_group(&mut self, group: &str) -> Option<usize> {
        self.pick_group_rarity(group, None)
    }

    fn pick_group_min(&mut self, group: &str, min: Rarity) -> Option<usize> {
        // `Rarity::Common` is the callers' "no minimum" marker.
        let min = if min == Rarity::Common {
            None
        } else {
            Some(min)
        };
        self.pick_group_rarity(group, min)
    }

    /// Godot's `tiles_to_add` with a `group`: first roll a rarity with the
    /// per-rent symbol chances (effect stream), then pick a member of that
    /// rarity with another effect-stream draw.  Falls back to the other
    /// rarities in common/uncommon/rare/very_rare order.
    fn pick_group_rarity(&mut self, group: &str, min: Option<Rarity>) -> Option<usize> {
        let (uncommon, rare, very_rare) = self.spin_rarity_chances();
        let lucky = self.rarity_lucky_factor();
        let uncommon = uncommon * lucky;
        let rare = rare * lucky;
        let very_rare = very_rare * lucky;
        let roll = self.rng.unit();
        let mut pools: [Vec<usize>; 4] = Default::default();
        for (index, def) in self.data.symbols.iter().enumerate() {
            if !def.groups.iter().any(|item| item == group) {
                continue;
            }
            if let Some(rarity) = Rarity::parse(&def.rarity) {
                pools[rarity as usize].push(index);
            }
        }
        let mut rarity = min.unwrap_or(Rarity::Common);
        // Godot starts from `min_rarity` (or common) and still lets the roll
        // upgrade the tier; only the upgrade chain is applied.
        if roll < very_rare && !pools[Rarity::VeryRare as usize].is_empty() {
            rarity = Rarity::VeryRare;
        } else if roll < very_rare + rare
            && rarity != Rarity::VeryRare
            && !pools[Rarity::Rare as usize].is_empty()
        {
            rarity = Rarity::Rare;
        } else if roll < very_rare + rare + uncommon
            && rarity != Rarity::Rare
            && rarity != Rarity::VeryRare
            && !pools[Rarity::Uncommon as usize].is_empty()
        {
            rarity = Rarity::Uncommon;
        }
        let mut order = vec![rarity];
        if min.is_none() {
            for candidate in [Rarity::Common, Rarity::Uncommon, Rarity::Rare, Rarity::VeryRare]
            {
                if candidate != rarity {
                    order.push(candidate);
                }
            }
        }
        for candidate in order {
            let pool = &pools[candidate as usize];
            if !pool.is_empty() {
                let pick = self.rng.range(0, pool.len());
                return Some(pool[pick]);
            }
        }
        None
    }

    fn rarity_lucky_factor(&self) -> f64 {
        self.spin_rarity_factor
    }

    /// Godot resets `rarity_bonuses` at the start of every spin and only the
    /// rarity-modifying icons that are actually displayed on that board
    /// multiply the chances.  Items (lucky/golden carrots, lucky cat) apply
    /// unconditionally.
    fn compute_rarity_factor(&self, entries: &[(usize, usize, usize)]) -> f64 {
        let cat_count = self
            .deck
            .iter()
            .filter(|symbol| self.data.symbols[symbol.type_id as usize].name == "cat")
            .count() as i32;
        let lucky_cats = self
            .items_held
            .iter()
            .filter(|id| self.data.items[**id].name == "lucky_cat")
            .count() as f64;
        let mut lucky = 1.0f64;
        if lucky_cats > 0.0 {
            let multiplier = self
                .data
                .item_index
                .get("lucky_cat")
                .map(|id| self.data.items[*id].values.first().copied().unwrap_or(1.0))
                .unwrap_or(1.0)
                * lucky_cats;
            lucky *= multiplier.powi(cat_count);
        }
        for item_id in &self.items_held {
            let item = &self.data.items[*item_id];
            if matches!(item.name.as_str(), "lucky_carrot" | "golden_carrot") {
                lucky *= item.values.first().copied().unwrap_or(1.0);
            }
        }
        if let Some(lucky_cat) = self
            .data
            .essence_index
            .get("lucky_cat_essence")
            .copied()
            .filter(|id| self.essences_held.contains(id))
        {
            lucky *= self.data.essences[lucky_cat]
                .values
                .get(1)
                .copied()
                .unwrap_or(2.5);
        }
        for (index, _, _) in entries.iter().copied() {
            let def = &self.data.symbols[self.deck[index].type_id as usize];
            if matches!(def.name.as_str(), "shiny_pebble" | "rabbit_fluff") {
                lucky *= def.values.first().copied().unwrap_or(1.0);
            }
        }
        lucky
    }

    /// Mirrors Godot's `_rl_add_effects` registration phase for symbols.
    ///
    /// Every random comparison in `add_conditional_effects` evaluates its
    /// `_sir_rand_range(...)` arguments eagerly, so these draws happen in
    /// column order before any icon is resolved.  The values are mostly
    /// discarded (a second roll decides the outcome at check time); arrows,
    /// dice and hex targets are decided here and reused later.
    /// Godot evaluates the reroll item's two random replacement rolls while
    /// registering item effects, before any symbol effects are registered.
    /// Those rolls are later used when a die comes up below the threshold.
    fn register_item_effects_pre_spin(&mut self) {
        self.reroll_d3_roll = None;
        self.reroll_d5_roll = None;
        for item_id in self.items_held.clone() {
            let item = &self.data.items[item_id];
            if item.name != "reroll" {
                continue;
            }
            // `floor(_itr_rand_range(values[0], values[1] + 1))`: d3 rolls
            // 1..3, d5 rolls 1..5.
            let d3 = (1.0 + self.rng.unit() * 3.0) as i32;
            let d5 = (1.0 + self.rng.unit() * 5.0) as i32;
            if self.reroll_d3_roll.is_none() {
                self.reroll_d3_roll = Some(d3);
            }
            if self.reroll_d5_roll.is_none() {
                self.reroll_d5_roll = Some(d5);
            }
        }
    }

    fn preregister_symbol_effects(
        &mut self,
        entries: &[(usize, usize, usize)],
    ) -> HashMap<usize, SpinPreRoll> {
        let holy_water = self.has_item("holy_water") || self.has_essence("holy_water_essence");
        let mut result: HashMap<usize, SpinPreRoll> = HashMap::with_capacity(entries.len());
        for (index, x, y) in entries.iter().copied() {
            self.deck[index].times_displayed += 1;
            let type_id = self.deck[index].type_id as usize;
            let name = self.data.symbols[type_id].name.clone();
            let values = self.data.symbols[type_id].values.clone();
            let mut pre = SpinPreRoll::default();
            match name.as_str() {
                "d3" | "d5" => {
                    let lucky = self.has_essence("reroll_essence") || self.has_item("lucky_dice");
                    if !lucky {
                        let high = if name == "d3" {
                            values.get(1).copied().unwrap_or(3.0) as usize
                        } else {
                            values.get(1).copied().unwrap_or(5.0) as usize
                        };
                        pre.dice = Some(self.rng.range(1, high.max(2) + 1) as i32);
                    }
                }
                "seed" => {
                    if !self.has_fine_print(34) {
                        let _ = self.rng.unit();
                    }
                }
                "beehive" | "tomb" | "oyster" | "cow" | "goose" | "bartender" => {
                    let _ = self.rng.unit();
                }
                "chicken" => {
                    let _ = self.rng.unit();
                    let _ = self.rng.unit();
                }
                "chick" | "egg" => {
                    let _ = self.rng.unit();
                }
                "hex_of_thievery" | "hex_of_midas" => {
                    if !holy_water {
                        let _ = self.rng.unit();
                    }
                }
                "hex_of_emptiness" | "hex_of_hoarding" => {
                    if !holy_water && self.spins_left != 1 {
                        let _ = self.rng.unit();
                    }
                }
                "hex_of_destruction" => {
                    if !holy_water {
                        let possible: Vec<usize> = self
                            .neighbor_indexes(x, y, index)
                            .into_iter()
                            .filter(|neighbor| {
                                self.data.symbols[self.deck[*neighbor].type_id as usize].name
                                    != "empty"
                            })
                            .collect();
                        if !possible.is_empty() {
                            let pick = possible[self.rng.range(0, possible.len())];
                            pre.destruction_target = Some(pick);
                            let _ = self.rng.unit();
                        }
                    }
                }
                "hex_of_draining" => {
                    if !holy_water {
                        let mut possible: Vec<usize> = self
                            .neighbor_indexes(x, y, index)
                            .into_iter()
                            .filter(|neighbor| {
                                self.data.symbols[self.deck[*neighbor].type_id as usize].name
                                    != "empty"
                            })
                            .collect();
                        if !possible.is_empty() {
                            self.rng.shuffle(&mut possible);
                            pre.draining_target = Some(possible[0]);
                            let _ = self.rng.unit();
                        }
                    }
                }
                "bronze_arrow" | "silver_arrow" | "golden_arrow" => {
                    let first = 1 + self.rng.range(0, 8);
                    let mut directions = vec![first];
                    if self.has_item("quiver") || self.has_essence("quiver_essence") {
                        let mut remaining: Vec<usize> =
                            (1..=8).filter(|direction| *direction != first).collect();
                        let pick = self.rng.range(0, remaining.len());
                        directions.push(remaining.remove(pick));
                    }
                    pre.arrow_directions = directions;
                }
                _ => {}
            }
            result.insert(index, pre);
        }
        result
    }

    fn resolve_spin_effects(&mut self) -> i32 {
        self.spin_rarity_penalty = 1.0;
        self.board.prune_invalid(self.deck.len());
        let deck_len = self.deck.len();
        let mut spin_bonus = vec![0i32; deck_len];
        let mut mult = vec![1.0f64; deck_len];
        let mut destroyed = vec![false; deck_len];
        let mut spawned: Vec<usize> = Vec::new();
        // (deck index, x, y, coin per empty neighbour, destroy threshold,
        //  fine-print 36 keeps the symbol alive regardless of empty count)
        let mut void_effects: Vec<(usize, usize, usize, i32, i32, bool)> = Vec::new();
        let mut extra_income = 0i32;
        let mut item_gains_sink: Vec<(String, i32)> = Vec::new();
        let items_at_start = self.items_held.clone();

        // Godot resolves effects column by column (`for x` then `for y`), and
        // random effects consume the effect stream in that same order.
        let mut entries: Vec<(usize, usize, usize)> = Vec::new();
        for x in 0..BOARD_COLS {
            for y in 0..BOARD_ROWS {
                if let Some(index) = self.board.slots[y * BOARD_COLS + x] {
                    entries.push((index, x, y));
                }
            }
        }

        self.spin_rarity_factor = self.compute_rarity_factor(&entries);

        // Quigley the Wolf turns every displayed dog into a wolf when the
        // item is held (Godot registers this as a `type` change effect).
        if self.has_item("quigley_the_wolf") {
            if let Some(wolf) = self.data.symbol_id("wolf") {
                for (index, _, _) in entries.iter().copied() {
                    if self.data.symbols[self.deck[index].type_id as usize].name == "dog" {
                        self.deck[index].type_id = wolf as u16;
                        // Godot's `change_type` keeps the dog's original
                        // `value` and the already-applied doglikes +2 bonus.
                        // Swapping the type id here raises base 1 -> 2 and
                        // makes the doglikes pass below see a wolf instead of
                        // a dog, so the net correction is +1 per conversion.
                        spin_bonus[index] += 1;
                    }
                }
            }
        }

        if let Some(essence_id) = self
            .data
            .essence_index
            .get("swapping_device_essence")
            .copied()
            .filter(|id| self.essences_held.contains(id))
        {
            let (need, factor) = {
                let def = &self.data.essences[essence_id];
                (
                    def.values.first().copied().unwrap_or(7.0) as i32,
                    def.values.get(2).copied().unwrap_or(2.0),
                )
            };
            let mut ranked: Vec<(i32, usize)> = entries
                .iter()
                .map(|(index, _, _)| (self.deck[*index].current_value(self.data), *index))
                .collect();
            ranked.sort_unstable_by(|a, b| b.0.cmp(&a.0));
            for (_, index) in ranked.into_iter().take(2) {
                mult[index] *= factor;
            }
            self.swapping_essence_swaps += 1;
            if self.swapping_essence_swaps >= need.max(1) {
                self.try_destroy_essence(essence_id);
            }
        }

        let mut destroy_turtle_item = false;
        let mut milk_eaten = false;
        let mut egg_became_chick = false;
        let mut chick_became_chicken = false;

        self.spin_all_adjacent = self.has_essence("telescope_essence");
        self.spin_corner_adjacent = self.has_essence("protractor_essence");
        let telescope_copies = self
            .items_held
            .iter()
            .filter(|id| self.data.items[**id].name == "telescope")
            .count() as i32;
        if telescope_copies > 0 {
            let threshold = self
                .data
                .item_index
                .get("telescope")
                .map(|id| {
                    self.data.items[*id]
                        .values
                        .first()
                        .copied()
                        .unwrap_or(3.0)
                })
                .unwrap_or(3.0) as i32;
            self.telescope_saved += telescope_copies;
            while self.telescope_saved >= threshold.max(1) {
                self.telescope_saved -= threshold.max(1);
                self.spin_all_adjacent = true;
            }
        }
        let protractor_copies = self
            .items_held
            .iter()
            .filter(|id| self.data.items[**id].name == "protractor")
            .count() as i32;
        if protractor_copies > 0 {
            let threshold = self
                .data
                .item_index
                .get("protractor")
                .map(|id| {
                    self.data.items[*id]
                        .values
                        .first()
                        .copied()
                        .unwrap_or(3.0)
                })
                .unwrap_or(3.0) as i32;
            self.protractor_saved += protractor_copies;
            while self.protractor_saved >= threshold.max(1) {
                self.protractor_saved -= threshold.max(1);
                self.spin_corner_adjacent = true;
            }
        }
        let credit_card_copies = self
            .items_held
            .iter()
            .filter(|id| self.data.items[**id].name == "credit_card")
            .count() as i32;
        if credit_card_copies > 0 {
            let (threshold, extra) = self
                .data
                .item_index
                .get("credit_card")
                .map(|id| {
                    let values = &self.data.items[*id].values;
                    (
                        values.first().copied().unwrap_or(7.0) as i32,
                        values.get(1).copied().unwrap_or(17.0) as usize,
                    )
                })
                .unwrap_or((7, 17));
            self.credit_card_saved += credit_card_copies;
            while self.credit_card_saved >= threshold.max(1) {
                self.credit_card_saved -= threshold.max(1);
                self.pending_extra_symbol_choices += extra * credit_card_copies as usize;
            }
        }
        if let Some(essence_id) = self
            .data
            .essence_index
            .get("credit_card_essence")
            .copied()
            .filter(|id| self.essences_held.contains(id))
        {
            let extra = self.data.essences[essence_id]
                .values
                .first()
                .copied()
                .unwrap_or(27.0) as usize;
            self.pending_extra_symbol_choices += extra;
            self.try_destroy_essence(essence_id);
        }

        // Reverse adjacency effects: symbols carrying these groups gain a
        // bonus for every adjacent source symbol (dog/bee/omelette).
        let dog_bonus = self
            .data
            .symbol_id("dog")
            .and_then(|id| self.data.symbols[id].values.first().copied())
            .unwrap_or(2.0) as i32;
        let bee_bonus = self
            .data
            .symbol_id("bee")
            .and_then(|id| self.data.symbols[id].values.get(1).copied())
            .unwrap_or(1.0) as i32;
        let omelette_bonus = self
            .data
            .symbol_id("omelette")
            .and_then(|id| self.data.symbols[id].values.first().copied())
            .unwrap_or(2.0) as i32;

        for item_id in self.items_held.clone() {
            let item = &self.data.items[item_id];
            let name = item.name.clone();
            let v0 = item.values.first().copied().unwrap_or(0.0);
            let v1 = item.values.get(1).copied().unwrap_or(0.0);
            let v2 = item.values.get(2).copied().unwrap_or(0.0);
            let flat: Option<(bool, &[&str], f64)> = match name.as_str() {
                "fruit_basket" => Some((true, &["fruit"][..], v0)),
                "cleaning_rag" => Some((true, &["gem"][..], v0)),
                "birdhouse" => Some((true, &["bird"][..], v0)),
                "happy_hour" => Some((true, &["booze"][..], v0)),
                "kyle_the_kernite" => Some((true, &["kyle"][..], v0)),
                "anthropology_degree" => Some((true, &["human"][..], v0)),
                "ritual_candle" => Some((true, &["fossillikes"][..], v0)),
                "holy_water" => Some((true, &["hex"][..], v0)),
                "chicken_coop" => Some((true, &["chickenstuff"][..], v2)),
                "nori_the_rabbit" => Some((false, &["rabbit", "rabbit_fluff"][..], v0)),
                "pizza_the_cat" => Some((false, &["cat"][..], v0)),
                "quigley_the_wolf" => Some((false, &["wolf"][..], v0)),
                "ricky_the_banana" => Some((false, &["banana", "banana_peel"][..], v0)),
                "rain_cloud" => Some((false, &["rain"][..], v0)),
                "watering_can" => Some((false, &["seed"][..], v1)),
                _ => None,
            };
            if let Some((is_group, tags, amount)) = flat {
                for (index, _, _) in entries.iter().copied() {
                    let type_id = self.deck[index].type_id as usize;
                    let hit = tags.iter().any(|tag| {
                        if is_group {
                            self.has_group(type_id, tag)
                        } else {
                            self.data.symbols[type_id].name == *tag
                        }
                    });
                    if hit {
                        spin_bonus[index] += amount as i32;
                    }
                }
            }
            let mult_tag: Option<(bool, &str, f64)> = match name.as_str() {
                "wanted_poster" => Some((false, "thief", v0)),
                "jackolantern" => Some((true, "halloween", v0)),
                "maxwell_the_bear" => Some((false, "bear", v0)),
                "dwarven_anvil" => Some((false, "dwarf", v0)),
                "oswald_the_monkey" => Some((false, "monkey", v0)),
                "black_cat" => Some((false, "cat", v2)),
                "lucky_carrot" | "golden_carrot" => Some((false, "rabbit", v1)),
                "triple_coins" => Some((false, "coin", v0)),
                _ => None,
            };
            if let Some((is_group, tag, amount)) = mult_tag {
                for (index, _, _) in entries.iter().copied() {
                    let type_id = self.deck[index].type_id as usize;
                    let hit = if is_group {
                        self.has_group(type_id, tag)
                    } else {
                        self.data.symbols[type_id].name == tag
                    };
                    if hit {
                        mult[index] *= amount;
                    }
                }
            }
            if name == "rusty_gear" {
                // Godot's `make_clumps` builds connected components of
                // adjacent identical icons (8 neighbours).  Rusty gear only
                // fires for a clump of at least values[0] (=3) icons, and
                // then multiplies exactly the icons of types that collected
                // at least values[0] members in such clumps.  Counting all
                // copies of a type on the board is not the same rule.
                let threshold = v0.max(1.0) as usize;
                let factor = if v1 > 1.0 { v1 } else { 2.0 };
                let mut pos: HashMap<usize, (usize, usize)> = HashMap::new();
                let mut by_slot: HashMap<(usize, usize), usize> = HashMap::new();
                for (index, x, y) in entries.iter().copied() {
                    pos.insert(index, (x, y));
                    by_slot.insert((x, y), index);
                }
                let mut visited: HashSet<usize> = HashSet::new();
                let mut clump_members: HashMap<u16, Vec<usize>> = HashMap::new();
                for (index, _, _) in entries.iter().copied() {
                    if !visited.insert(index) {
                        continue;
                    }
                    let mut clump = vec![index];
                    let mut cursor = 0;
                    while cursor < clump.len() {
                        let cur = clump[cursor];
                        cursor += 1;
                        let (cx, cy) = pos[&cur];
                        for dy in -1i32..=1 {
                            for dx in -1i32..=1 {
                                if dx == 0 && dy == 0 {
                                    continue;
                                }
                                let nx = cx as i32 + dx;
                                let ny = cy as i32 + dy;
                                if nx < 0 || ny < 0 {
                                    continue;
                                }
                                let Some(other) = by_slot.get(&(nx as usize, ny as usize))
                                else {
                                    continue;
                                };
                                if self.deck[*other].type_id != self.deck[index].type_id
                                    || !visited.insert(*other)
                                {
                                    continue;
                                }
                                clump.push(*other);
                            }
                        }
                    }
                    if clump.len() >= threshold {
                        clump_members
                            .entry(self.deck[index].type_id)
                            .or_default()
                            .extend(clump);
                    }
                }
                for (_, members) in clump_members {
                    if members.len() >= threshold {
                        for index in members {
                            if !destroyed[index] {
                                mult[index] *= factor;
                            }
                        }
                    }
                }
            }
            if name == "lefty_the_rabbit" {
                let need = v0.max(1.0) as i32;
                self.lefty_saved += 1;
                if let Some(fluff) = self.data.symbol_id("rabbit_fluff") {
                    while self.lefty_saved >= need {
                        spawned.push(fluff);
                        self.lefty_saved -= need;
                    }
                }
                let factor = if v1 > 1.0 { v1 } else { 2.0 };
                for (index, x, _) in entries.iter().copied() {
                    if x != 0 || destroyed[index] {
                        continue;
                    }
                    let symbol_name = self.data.symbols[self.deck[index].type_id as usize]
                        .name
                        .as_str();
                    if symbol_name == "rabbit" || symbol_name == "rabbit_fluff" {
                        mult[index] *= factor;
                    }
                }
            }
            let flat_income = match name.as_str() {
                "pool_ball" | "horseshoe" | "bowling_ball" | "four_leaf_clover"
                | "coin_on_a_string" => v0 as i32,
                // Pink pepper pays when a reward is skipped, not every spin.
                "egg_carton" => self.stored_eggs.min(v1 as i32) * v0 as i32,
                "fish_bowl" => self.stored_goldfish * v0 as i32,
                _ => 0,
            };
            extra_income += flat_income;
            match name.as_str() {
                "dwarven_anvil" => {
                    // Dwarves also destroy adjacent minerlikes and gain the
                    // victim's permanent value times the anvil's value[0].
                    for (index, x, y) in entries.iter().copied() {
                        if self.data.symbols[self.deck[index].type_id as usize].name != "dwarf" {
                            continue;
                        }
                        for neighbor in self.neighbor_indexes(x, y, index) {
                            if destroyed[neighbor]
                                || !self.has_group(self.deck[neighbor].type_id as usize, "minerlikes")
                            {
                                continue;
                            }
                            destroyed[neighbor] = true;
                            let victim_def =
                                &self.data.symbols[self.deck[neighbor].type_id as usize];
                            let victim = ((self.deck[neighbor].bonus + victim_def.base_value)
                                as f64
                                + self.permanent_bonus_for(victim_def) as f64)
                                * self.permanent_mult_for(victim_def);
                            spin_bonus[index] += (victim * v0).round() as i32;
                        }
                    }
                }
                "ninja_and_mouse" => {
                    let mut found = false;
                    for (index, x, y) in entries.iter().copied() {
                        let name = self.data.symbols[self.deck[index].type_id as usize].name.clone();
                        let wanted = match name.as_str() {
                            "ninja" => "mouse",
                            "mouse" => "ninja",
                            _ => continue,
                        };
                        for neighbor in self.neighbor_indexes(x, y, index) {
                            if self.data.symbols[self.deck[neighbor].type_id as usize].name == wanted
                                && !destroyed[index]
                                && !destroyed[neighbor]
                            {
                                mult[index] *= v0;
                                mult[neighbor] *= v0;
                                found = true;
                                break;
                            }
                        }
                        if found {
                            break;
                        }
                    }
                }
                "fifth_ace" => {
                    for (index, x, y) in entries.iter().copied() {
                        let name = self.data.symbols[self.deck[index].type_id as usize].name.clone();
                        let (wanted, amount) = match name.as_str() {
                            "clubs" | "spades" => (["diamonds", "hearts"], v0),
                            "diamonds" | "hearts" => (["clubs", "spades"], v1),
                            _ => continue,
                        };
                        for neighbor in self.neighbor_indexes(x, y, index) {
                            let other =
                                self.data.symbols[self.deck[neighbor].type_id as usize].name.clone();
                            if wanted.contains(&other.as_str()) {
                                spin_bonus[index] += amount as i32;
                                break;
                            }
                        }
                    }
                }
                "turtle_and_rabbit" => {
                    let rabbit_left = entries.iter().any(|(index, x, _)| {
                        *x == 0
                            && self.data.symbols[self.deck[*index].type_id as usize].name == "rabbit"
                    });
                    let turtle_right = entries.iter().any(|(index, x, _)| {
                        *x == BOARD_COLS - 1
                            && self.data.symbols[self.deck[*index].type_id as usize].name == "turtle"
                    });
                    if rabbit_left && turtle_right {
                        extra_income += v0 as i32;
                        destroy_turtle_item = true;
                    }
                }
                "lockpick" | "grave_robber" => {
                    // Godot evaluates item conditional effects in
                    // `check_effects`, after every symbol effect has been
                    // registered, so the roll must not happen in this
                    // pre-registration pass.  Handled below.
                }
                "guillotine" => {
                    if !self.has_fine_print(18) {
                        for (index, _, _) in entries.iter().copied() {
                            if self.data.symbols[self.deck[index].type_id as usize].name
                                == "billionaire"
                            {
                                destroyed[index] = true;
                            }
                        }
                    }
                }
                _ => {}
            }
        }
        // Registration phase: consume the effect stream in column order
        // exactly like Godot's `_rl_add_effects`.
        self.register_item_effects_pre_spin();
        let pre_rolls = self.preregister_symbol_effects(&entries);
        // Godot 把锁匠/盗墓者的“概率摧毁”效果挂在图标自身上，等这个图标
        // 被检查时才掷点。所以摇号必须发生在该图标的回合，而不是所有符号
        // 之前；否则整条 effect 流会提前错位一格。
        let mut item_destroy_chances: Vec<(&'static str, f64)> = Vec::new();
        {
            let mut seen: Vec<String> = Vec::new();
            for item_id in self.items_held.clone() {
                let item = &self.data.items[item_id];
                let name = item.name.clone();
                let group = match name.as_str() {
                    "lockpick" => "chest",
                    "grave_robber" => "spiritbox",
                    _ => continue,
                };
                if seen.contains(&name) {
                    continue;
                }
                seen.push(name.clone());
                let copies = self
                    .items_held
                    .iter()
                    .filter(|id| self.data.items[**id].name == name)
                    .count();
                let chance = item.values.first().copied().unwrap_or(0.0) * copies as f64;
                item_destroy_chances.push((group, chance));
            }
        }
        if let Some(lefty_essence) = self
            .data
            .essence_index
            .get("lefty_the_rabbit_essence")
            .copied()
            .filter(|id| self.essences_held.contains(id))
        {
            let def = &self.data.essences[lefty_essence];
            let need = def.values.first().copied().unwrap_or(6.0) as i32;
            let factor = def.values.get(1).copied().unwrap_or(3.0);
            let mut shown = 0;
            for (index, x, _) in entries.iter().copied() {
                if destroyed[index] {
                    continue;
                }
                let symbol_name =
                    self.data.symbols[self.deck[index].type_id as usize].name.as_str();
                if symbol_name == "rabbit" || symbol_name == "rabbit_fluff" {
                    shown += 1;
                    if x == 0 {
                        mult[index] *= factor;
                    }
                }
            }
            if shown >= need.max(1) {
                self.try_destroy_essence(lefty_essence);
            }
        }
        if destroy_turtle_item {
            self.items_held
                .retain(|item_id| self.data.items[*item_id].name != "turtle_and_rabbit");
        }

        let mut dog_targets: std::collections::BTreeSet<usize> = std::collections::BTreeSet::new();
        let mut omelette_targets: std::collections::BTreeSet<usize> =
            std::collections::BTreeSet::new();

        // Godot registers reverse adjacency effects for every displayed icon
        // before any effect resolves, so destruction order does not matter.
        // A dog/omelette gains its flat bonus only once per spin
        // (`one_time` effects are deduplicated per target), while a bee gains
        // its bonus for every adjacent beelikes symbol.
        for (index, x, y) in entries.iter().copied() {
            let type_id = self.deck[index].type_id as usize;
            let neighbors = self.neighbor_indexes(x, y, index);
            if self.has_group(type_id, "doglikes") {
                for n in &neighbors {
                    if self.data.symbols[self.deck[*n].type_id as usize].name == "dog" {
                        dog_targets.insert(*n);
                    }
                }
            }
            if self.has_group(type_id, "omelettestuff") {
                for n in &neighbors {
                    if self.data.symbols[self.deck[*n].type_id as usize].name == "omelette" {
                        omelette_targets.insert(*n);
                    }
                }
            }
            if self.has_group(type_id, "beelikes") {
                for n in &neighbors {
                    if self.data.symbols[self.deck[*n].type_id as usize].name == "bee" {
                        spin_bonus[*n] += bee_bonus;
                    }
                }
            }
        }
        for n in dog_targets {
            spin_bonus[n] += dog_bonus;
        }
        for n in omelette_targets {
            spin_bonus[n] += omelette_bonus;
        }

        // Every displayed void symbol registers its empty-cell effect at the
        // start of the spin, even one destroyed before its own turn arrives.
        for (index, x, y) in entries.iter().copied() {
            let def = &self.data.symbols[self.deck[index].type_id as usize];
            if matches!(def.name.as_str(), "void_fruit" | "void_creature" | "void_stone") {
                void_effects.push((
                    index,
                    x,
                    y,
                    def.values.first().copied().unwrap_or(1.0) as i32,
                    def.values.get(1).copied().unwrap_or(0.0) as i32,
                    self.fp_affects(36, &def.name),
                ));
            }
        }

        for (index, x, y) in entries.iter().copied() {
            if !destroyed[index] && !item_destroy_chances.is_empty() {
                let type_id = self.deck[index].type_id as usize;
                for (group, chance) in item_destroy_chances.iter().copied() {
                    if self.has_group(type_id, group) && self.rng.unit() * 100.0 < chance {
                        destroyed[index] = true;
                        break;
                    }
                }
            }
            let type_id = self.deck[index].type_id as usize;
            let name = self.data.symbols[type_id].name.clone();
            let values = self.data.symbols[type_id].values.clone();
            let neighbors = self.neighbor_indexes(x, y, index);
            if destroyed[index] {
                // Godot registers every icon's conditional effects before any
                // of them resolves, so an icon destroyed earlier in the pass
                // still applies the effects it already registered.  Only the
                // deterministic, outgoing ones are replayed here; rolls and
                // self-triggered effects stay with the registration phase.
                let v0 = values.first().copied().unwrap_or(0.0);
                match name.as_str() {
                    "bee" | "light_bulb" | "dame" | "dwarven_anvil" | "chef" => {
                        let tag = match name.as_str() {
                            "bee" => "beelikes",
                            "light_bulb" | "dame" => "gem",
                            "chef" => "food",
                            _ => "dwarflikes",
                        };
                        for n in &neighbors {
                            if self.has_group(self.deck[*n].type_id as usize, tag) {
                                mult[*n] *= v0.max(1.0);
                            }
                        }
                    }
                    "beastmaster" | "witch" | "farmer" | "sun" | "moon"
                    | "king_midas" => {
                        let tag = match name.as_str() {
                            "beastmaster" => "animal",
                            "witch" => "witchlikes",
                            "farmer" => "farmerlikes",
                            "moon" => "night",
                            _ => "",
                        };
                        for n in &neighbors {
                            let target = self.deck[*n].type_id as usize;
                            let hit = if tag.is_empty() {
                                self.data.symbols[target].name
                                    == if name == "sun" { "flower" } else { "coin" }
                            } else {
                                self.has_group(target, tag)
                            };
                            if hit {
                                mult[*n] *= if name == "sun" { 5.0 } else if name == "moon" { 3.0 } else if name == "king_midas" { 3.0 } else { v0.max(1.0) };
                            }
                        }
                    }
                    "cat" | "mouse" | "bear" | "miner" | "toddler" | "goldfish"
                    | "hooligan" | "mrs_fruit" | "monkey" | "dwarf" => {
                        let (tag, target_name) = match name.as_str() {
                            "cat" => ("", "milk"),
                            "mouse" => ("", "cheese"),
                            "bear" => ("", "honey"),
                            "goldfish" => ("", "bubble"),
                            "miner" => ("minerlikes", ""),
                            "toddler" => ("toddlerlikes", ""),
                            "hooligan" => ("spiritbox", ""),
                            "mrs_fruit" => ("fruitlikes", ""),
                            "monkey" => ("monkeylikes", ""),
                            _ => ("dwarflikes", ""),
                        };
                        let gain = match name.as_str() {
                            "cat" | "miner" | "toddler" | "goldfish" | "hooligan" => v0 as i32,
                            "mouse" | "bear" => v0 as i32,
                            _ => 0,
                        };
                        for n in &neighbors {
                            let target = self.deck[*n].type_id as usize;
                            let hit = if tag.is_empty() {
                                self.data.symbols[target].name == target_name
                            } else {
                                self.has_group(target, tag)
                            };
                            if !hit {
                                continue;
                            }
                            destroyed[*n] = true;
                            if name == "cat" {
                                milk_eaten = true;
                                spin_bonus[index] += gain;
                            } else if name == "mrs_fruit" {
                                self.deck[index].bonus += v0 as i32;
                            } else if name == "monkey" {
                                let victim_def = &self.data.symbols[target];
                                let victim = ((self.deck[*n].bonus + victim_def.base_value) as f64
                                    + self.permanent_bonus_for(victim_def) as f64)
                                    * self.permanent_mult_for(victim_def);
                                spin_bonus[index] += (victim * v0).round() as i32;
                            } else if name == "dwarf" {
                                let victim_def = &self.data.symbols[target];
                                let victim = ((self.deck[*n].bonus + victim_def.base_value
                                    + spin_bonus[*n]) as f64
                                    + self.permanent_bonus_for(victim_def) as f64)
                                    * self.permanent_mult_for(victim_def)
                                    * mult[*n];
                                spin_bonus[index] += (victim * v0).round() as i32;
                            } else {
                                spin_bonus[index] += gain;
                            }
                        }
                    }
                    _ => {}
                }
                continue;
            }
            let mut destroy_self = false;
            let chance_allowed = !self.fp_affects(13, &name);
            match name.as_str() {
                "dud" => {
                    if self.deck[index].times_displayed >= 33 {
                        destroy_self = true;
                    }
                }
                "bee" => {
                    for n in neighbors {
                        if self.has_group(self.deck[n].type_id as usize, "beelikes") {
                            mult[n] *= values.first().copied().unwrap_or(2.0);
                        }
                    }
                }
                "light_bulb" => {
                    let mut gems = 0;
                    for n in neighbors {
                        if self.has_group(self.deck[n].type_id as usize, "gem") {
                            mult[n] *= values.first().copied().unwrap_or(2.0);
                            gems += 1;
                        }
                    }
                    // Godot accumulates each adjacent gem in `saved_value`
                    // and destroys the bulb at the threshold.
                    self.deck[index].saved += gems;
                    if self.deck[index].saved >= values.get(1).copied().unwrap_or(5.0) as i32 {
                        destroy_self = true;
                    }
                }
                "beastmaster" => {
                    for n in neighbors {
                        if self.has_group(self.deck[n].type_id as usize, "animal") {
                            mult[n] *= values.first().copied().unwrap_or(2.0);
                        }
                    }
                }
                "witch" => {
                    for n in neighbors {
                        if self.has_group(self.deck[n].type_id as usize, "witchlikes") {
                            mult[n] *= values.first().copied().unwrap_or(2.0);
                        }
                    }
                }
                "dame" => {
                    for n in neighbors {
                        if self.has_group(self.deck[n].type_id as usize, "gem") {
                            mult[n] *= values.first().copied().unwrap_or(2.0);
                        } else if self.data.symbols[self.deck[n].type_id as usize].name == "martini"
                        {
                            destroyed[n] = true;
                            spin_bonus[index] += values.get(1).copied().unwrap_or(40.0) as i32;
                        }
                    }
                }
                "farmer" => {
                    for n in neighbors {
                        if self.has_group(self.deck[n].type_id as usize, "farmerlikes") {
                            mult[n] *= values.first().copied().unwrap_or(2.0);
                        }
                    }
                }
                "sun" => {
                    let targets: Vec<usize> = if self.has_item("clear_sky") || self.clear_sky_active {
                        entries.iter().map(|(index, _, _)| *index).collect()
                    } else {
                        neighbors.clone()
                    };
                    for n in targets {
                        if self.data.symbols[self.deck[n].type_id as usize].name == "flower" {
                            mult[n] *= values.first().copied().unwrap_or(5.0);
                        }
                    }
                }
                "moon" => {
                    let targets: Vec<usize> = if self.has_item("clear_sky") || self.clear_sky_active {
                        entries.iter().map(|(index, _, _)| *index).collect()
                    } else {
                        neighbors.clone()
                    };
                    for n in targets {
                        if self.has_group(self.deck[n].type_id as usize, "night") {
                            mult[n] *= values.first().copied().unwrap_or(3.0);
                        }
                    }
                }
                "king_midas" => {
                    for n in neighbors {
                        if self.data.symbols[self.deck[n].type_id as usize].name == "coin" {
                            mult[n] *= values.first().copied().unwrap_or(3.0);
                        }
                    }
                    if let Some(coin) = self.data.symbol_id("coin") {
                        spawned.push(coin);
                    }
                }
                "cat" => {
                    for n in &neighbors {
                        if self.data.symbols[self.deck[*n].type_id as usize].name == "milk" {
                            destroyed[*n] = true;
                            milk_eaten = true;
                            spin_bonus[index] += values.first().copied().unwrap_or(9.0) as i32;
                        }
                    }
                }
                "mouse" => {
                    for n in neighbors {
                        if self.data.symbols[self.deck[n].type_id as usize].name == "cheese" {
                            destroyed[n] = true;
                            spin_bonus[index] += values.first().copied().unwrap_or(20.0) as i32;
                        }
                    }
                }
                "bear" => {
                    for n in neighbors {
                        if self.data.symbols[self.deck[n].type_id as usize].name == "honey" {
                            destroyed[n] = true;
                            spin_bonus[index] += values.first().copied().unwrap_or(40.0) as i32;
                        }
                    }
                }
                "miner" => {
                    let gain = values.first().copied().unwrap_or(20.0) as i32;
                    for n in neighbors {
                        if self.has_group(self.deck[n].type_id as usize, "minerlikes") {
                            destroyed[n] = true;
                            spin_bonus[index] += gain;
                        }
                    }
                }
                "toddler" => {
                    let gain = values.first().copied().unwrap_or(6.0) as i32;
                    for n in neighbors {
                        if self.has_group(self.deck[n].type_id as usize, "toddlerlikes") {
                            destroyed[n] = true;
                            spin_bonus[index] += gain;
                        }
                    }
                }
                "hooligan" => {
                    let gain = values.first().copied().unwrap_or(6.0) as i32;
                    for n in neighbors {
                        if self.has_group(self.deck[n].type_id as usize, "spiritbox") {
                            destroyed[n] = true;
                            spin_bonus[index] += gain;
                        }
                    }
                }
                "chemical_seven" => {
                    extra_income += values.first().copied().unwrap_or(7.0) as i32;
                    if let Some(seven) = self.data.item_index.get("lucky_seven").copied() {
                        self.grant_item(seven);
                    }
                    destroy_self = true;
                }
                // Void effects are registered for every displayed icon in the
                // pre-pass above and settled after all destructions.
                "void_fruit" | "void_creature" | "void_stone" => {}
                "hex_of_tedium" => {
                    let penalty = values.first().copied().unwrap_or(1.3);
                    if penalty > 0.0 {
                        self.spin_rarity_penalty /= penalty;
                    }
                }
                "midas_bomb" => {
                    let factor = values.first().copied().unwrap_or(7.0) as i32;
                    for n in &neighbors {
                        if destroyed[*n] {
                            continue;
                        }
                        let other = &self.deck[*n];
                        let def = &self.data.symbols[other.type_id as usize];
                        if def.name == "empty" {
                            continue;
                        }
                        destroyed[*n] = true;
                        let permanent_bonus = self.permanent_bonus_for(def);
                        let permanent_mult = self.permanent_mult_for(def);
                        let slot_mult = self.spin_column_mult[n % BOARD_COLS];
                        let sum = def.base_value + other.bonus + spin_bonus[*n] + permanent_bonus;
                        let product = sum as f64 * mult[*n] * permanent_mult * slot_mult;
                        let value = if product < 0.0 {
                            sum
                        } else {
                            product.round() as i32
                        };
                        extra_income += value * factor;
                    }
                    destroy_self = true;
                }
                "hex_of_destruction" => {
                    let mut chance = values.first().copied().unwrap_or(30.0);
                    if self.fp_affects(8, "hex_of_destruction") {
                        chance *= self.fp_value(8, 0).max(1.0);
                    }
                    if chance_allowed && self.rng.unit() * 100.0 < chance {
                        if let Some(target) = pre_rolls
                            .get(&index)
                            .and_then(|pre| pre.destruction_target)
                        {
                            if !destroyed[target] {
                                let def = &self.data.symbols[self.deck[target].type_id as usize];
                                let blocked = def.name == "empty"
                                    || (def.name == "egg" && self.fp_affects(21, "egg"))
                                    || (def.groups.iter().any(|group| group == "chest")
                                        && self.fp_affects(10, &def.name))
                                    || (def.groups.iter().any(|group| group == "void")
                                        && self.fp_affects(36, &def.name));
                                if !blocked {
                                    destroyed[target] = true;
                                }
                            }
                        }
                    }
                }
                "hex_of_draining" => {
                    let mut chance = values.first().copied().unwrap_or(30.0);
                    if self.fp_affects(8, "hex_of_draining") {
                        chance *= self.fp_value(8, 0).max(1.0);
                    }
                    if let Some(target) = pre_rolls
                        .get(&index)
                        .and_then(|pre| pre.draining_target)
                    {
                        if !destroyed[target] && self.rng.unit() * 100.0 < chance {
                            mult[target] = 0.0;
                        }
                    }
                }
                "magic_key" => {
                    let factor = values.first().copied().unwrap_or(3.0);
                    for n in neighbors {
                        let neighbor_name =
                            self.data.symbols[self.deck[n].type_id as usize].name.clone();
                        if matches!(
                            neighbor_name.as_str(),
                            "lockbox" | "safe" | "treasure_chest" | "mega_chest"
                        ) && !destroyed[n]
                            && !self.fp_affects(10, &neighbor_name)
                        {
                            destroyed[n] = true;
                            let fp9 = if self.fp_affects(9, &neighbor_name) {
                                self.fp_value(9, 0)
                            } else {
                                0.0
                            };
                            let bonus = self.data.symbols[self.deck[n].type_id as usize]
                                .values
                                .first()
                                .copied()
                                .unwrap_or(0.0)
                                - fp9;
                            extra_income += (bonus.max(0.0) * (factor - 1.0)) as i32;
                        }
                    }
                    destroy_self = true;
                }
                "archaeologist" => {
                    let gain = values.first().copied().unwrap_or(1.0) as i32;
                    for n in neighbors {
                        if self.has_group(self.deck[n].type_id as usize, "archlikes")
                            && !destroyed[n]
                        {
                            destroyed[n] = true;
                            self.deck[index].bonus += gain;
                        }
                    }
                }
                "diver" => {
                    let gain = values.first().copied().unwrap_or(1.0) as i32;
                    for n in neighbors {
                        if self.has_group(self.deck[n].type_id as usize, "poslikes")
                            && !destroyed[n]
                        {
                            destroyed[n] = true;
                            self.deck[index].bonus += gain;
                        }
                    }
                }
                "pirate" => {
                    let gain = values.first().copied().unwrap_or(1.0) as i32;
                    for n in neighbors {
                        if self.has_group(self.deck[n].type_id as usize, "piratelikes")
                        {
                            destroyed[n] = true;
                            self.deck[index].bonus += gain;
                        }
                    }
                }
                "amethyst" | "pear" => {
                    self.deck[index].bonus += values.first().copied().unwrap_or(1.0) as i32;
                }
                "mrs_fruit" => {
                    let gain = values.first().copied().unwrap_or(1.0) as i32;
                    for n in neighbors {
                        if self.has_group(self.deck[n].type_id as usize, "fruitlikes")
                        {
                            destroyed[n] = true;
                            self.deck[index].bonus += gain;
                        }
                    }
                }
                "dwarf" => {
                    let gain = values.first().copied().unwrap_or(10.0);
                    for n in neighbors {
                        if self.has_group(self.deck[n].type_id as usize, "dwarflikes")
                        {
                            destroyed[n] = true;
                            // Godot multiplies the dwarf's values[0] by the
                            // victim's `non_prev_final_value`, which is
                            // recomputed with `get_non_prev_value("coin")` when
                            // effects resolve.  That helper includes this
                            // spin's `value_bonus_arr` (e.g. happy_hour +1 to
                            // booze) and `value_multiplier_arr`, so the victim
                            // is not just its permanent value.  A beer under
                            // happy_hour is worth 2, giving dwarf +20, not +10.
                            let victim_def =
                                &self.data.symbols[self.deck[n].type_id as usize];
                            let victim = ((self.deck[n].bonus + victim_def.base_value
                                + spin_bonus[n]) as f64
                                + self.permanent_bonus_for(victim_def) as f64)
                                * self.permanent_mult_for(victim_def)
                                * mult[n];
                            spin_bonus[index] += (victim * gain).round() as i32;
                        }
                    }
                }
                "key" => {
                    let mut opened = false;
                    for n in neighbors {
                        let chest_name =
                            self.data.symbols[self.deck[n].type_id as usize].name.clone();
                        if self.has_group(self.deck[n].type_id as usize, "chest")
                            && !destroyed[n]
                            && !self.fp_affects(10, &chest_name)
                        {
                            destroyed[n] = true;
                            opened = true;
                        }
                    }
                    if opened {
                        destroy_self = true;
                    }
                }
                "mine" => {
                    if let Some(ore) = self.data.symbol_id("ore") {
                        spawned.push(ore);
                    }
                    let threshold =
                        (values.first().copied().unwrap_or(5.0) as u32).saturating_sub(1);
                    if self.deck[index].times_coins_given >= threshold {
                        destroy_self = true;
                    }
                }
                "golem" => {
                    if self.deck[index].times_displayed >= 5 {
                        destroy_self = true;
                        if let Some(ore) = self.data.symbol_id("ore") {
                            for _ in 0..5 {
                                spawned.push(ore);
                            }
                        }
                    }
                }
                "matryoshka_doll_1" | "matryoshka_doll_2" | "matryoshka_doll_3"
                | "matryoshka_doll_4" => {
                    let threshold = values.first().copied().unwrap_or(3.0) as u32;
                    if self.deck[index].times_displayed >= threshold {
                        destroy_self = true;
                        let next = match name.as_str() {
                            "matryoshka_doll_1" => "matryoshka_doll_2",
                            "matryoshka_doll_2" => "matryoshka_doll_3",
                            "matryoshka_doll_3" => "matryoshka_doll_4",
                            _ => "matryoshka_doll_5",
                        };
                        if let Some(next_id) = self.data.symbol_id(next) {
                            spawned.push(next_id);
                        }
                    }
                }
                "chef" => {
                    for n in neighbors {
                        if self.has_group(self.deck[n].type_id as usize, "food") {
                            mult[n] *= values.first().copied().unwrap_or(2.0);
                        }
                    }
                }
                "ninja" => {
                    let katanas: i32 = self
                        .items_held
                        .iter()
                        .filter(|item_id| self.data.items[**item_id].name == "cursed_katana")
                        .map(|item_id| {
                            self.data.items[*item_id]
                                .values
                                .first()
                                .copied()
                                .unwrap_or(2.0) as i32
                        })
                        .sum();
                    let ninjas = entries
                        .iter()
                        .filter(|(other, _, _)| {
                            self.data.symbols[self.deck[*other].type_id as usize].name == "ninja"
                        })
                        .count() as i32
                        - 1;
                    // Godot: (-values[0] + cursed_katana_diff) * counted ninjas,
                    // and `counted_symbols` excludes the ninja itself.
                    spin_bonus[index] += (katanas - 1) * ninjas.max(0);
                }
                "general_zaroff" => {
                    let gain = values.first().copied().unwrap_or(25.0) as i32;
                    for n in neighbors {
                        if self.has_group(self.deck[n].type_id as usize, "human")
                            && !destroyed[n]
                        {
                            destroyed[n] = true;
                            spin_bonus[index] += gain;
                        }
                    }
                }
                "bounty_hunter" => {
                    let gain = values.first().copied().unwrap_or(20.0) as i32;
                    let contract_bonus = self
                        .items_held
                        .iter()
                        .filter(|item_id| self.data.items[**item_id].name == "zaroffs_contract")
                        .count() as i32
                        * 25;
                    for n in neighbors {
                        if destroyed[n] {
                            continue;
                        }
                        let target_is_thief =
                            self.data.symbols[self.deck[n].type_id as usize].name == "thief";
                        let target_is_human =
                            self.has_group(self.deck[n].type_id as usize, "human");
                        if target_is_thief {
                            destroyed[n] = true;
                            spin_bonus[index] += gain;
                        } else if contract_bonus > 0 && target_is_human {
                            destroyed[n] = true;
                            spin_bonus[index] += contract_bonus;
                        }
                    }
                }
                "bartender" => {
                    if chance_allowed
                        && self.rng.unit() * 100.0
                            < values.first().copied().unwrap_or(10.0) * self.spawn_multiplier()
                    {
                        let mut booze: Vec<usize> = Vec::new();
                        for (type_id, def) in self.data.symbols.iter().enumerate() {
                            if def.groups.iter().any(|group| group == "booze") {
                                booze.push(type_id);
                            }
                        }
                        if !booze.is_empty() {
                            let pick = self.rng.range(0, booze.len());
                            spawned.push(booze[pick]);
                            self.note_spawner_spawns(1);
                        }
                    }
                }
                "diamond" => {
                    // Symbols in the `eachother` group are counted without
                    // themselves: Godot's `count_symbols` skips the first
                    // occurrence (`first_bools[c] = false`).
                    let count = entries
                        .iter()
                        .filter(|(other, _, _)| {
                            self.data.symbols[self.deck[*other].type_id as usize].name
                                == "diamond"
                        })
                        .count() as i32
                        - 1;
                    spin_bonus[index] += count.max(0);
                }
                "watermelon" => {
                    let count = entries
                        .iter()
                        .filter(|(other, _, _)| {
                            self.data.symbols[self.deck[*other].type_id as usize].name
                                == "watermelon"
                        })
                        .count() as i32
                        - 1;
                    spin_bonus[index] += count.max(0);
                }
                "cultist" => {
                    let count = entries
                        .iter()
                        .filter(|(other, _, _)| {
                            self.data.symbols[self.deck[*other].type_id as usize].name
                                == "cultist"
                        })
                        .count() as i32
                        - 1;
                    spin_bonus[index] += count.max(0);
                    if count >= values.get(2).copied().unwrap_or(3.0) as i32 {
                        spin_bonus[index] += values.get(1).copied().unwrap_or(1.0) as i32;
                    }
                }
                "crab" => {
                    for (other, _, other_y) in entries.iter().copied() {
                        if other != index
                            && other_y == y
                            && self.data.symbols[self.deck[other].type_id as usize].name == "crab"
                        {
                            spin_bonus[other] += values.first().copied().unwrap_or(3.0) as i32;
                        }
                    }
                }
                "d3" | "d5" => {
                    let high = if name == "d3" {
                        values.get(1).copied().unwrap_or(3.0) as i32
                    } else {
                        values.get(1).copied().unwrap_or(5.0) as i32
                    };
                    let reroll_dice = self.has_essence("reroll_essence");
                    if reroll_dice || self.has_item("lucky_dice") {
                        spin_bonus[index] += high;
                    } else {
                        let mut roll = pre_rolls
                            .get(&index)
                            .and_then(|pre| pre.dice)
                            .unwrap_or_else(|| {
                                self.rng.range(1, high.max(2) as usize + 1) as i32
                            });
                        if let Some(item_id) = self
                            .items_held
                            .iter()
                            .copied()
                            .find(|id| self.data.items[*id].name == "reroll")
                        {
                            let threshold = self.data.items[item_id]
                                .values
                                .get(if name == "d3" { 0 } else { 1 })
                                .copied()
                                .unwrap_or(2.0) as i32;
                            if roll < threshold.max(1) {
                                // The replacement was rolled during item
                                // registration; reuse it instead of drawing
                                // a fresh value here.
                                let replacement = if name == "d3" {
                                    self.reroll_d3_roll
                                } else {
                                    self.reroll_d5_roll
                                };
                                if let Some(value) = replacement {
                                    roll = value;
                                }
                            }
                        }
                        spin_bonus[index] += roll;
                        // A displayed gambler destroys itself whenever a d3 or
                        // d5 actually rolls a one.
                        if roll == 1 {
                            for (other, _, _) in entries.iter().copied() {
                                if !destroyed[other]
                                    && self.data.symbols[self.deck[other].type_id as usize].name
                                        == "gambler"
                                {
                                    destroyed[other] = true;
                                }
                            }
                        }
                    }
                    if reroll_dice {
                        self.reroll_essence_rerolls += 1;
                        let need = self
                            .data
                            .essence_index
                            .get("reroll_essence")
                            .map(|id| {
                                self.data.essences[*id]
                                    .values
                                    .get(2)
                                    .copied()
                                    .unwrap_or(10.0) as i32
                            })
                            .unwrap_or(10)
                            .max(1);
                        if self.reroll_essence_rerolls >= need {
                            if let Some(essence_id) =
                                self.data.essence_index.get("reroll_essence").copied()
                            {
                                self.try_destroy_essence(essence_id);
                            }
                        }
                    }
                    if self.has_essence("lucky_dice_essence") {
                        let step = self
                            .data
                            .essence_index
                            .get("lucky_dice_essence")
                            .map(|id| {
                                self.data.essences[*id]
                                    .values
                                    .get(1)
                                    .copied()
                                    .unwrap_or(1.0) as i32
                            })
                            .unwrap_or(1);
                        spin_bonus[index] += step;
                    }
                }
                "coal" => {
                    let mut threshold = self.data.symbols[type_id]
                        .values
                        .first()
                        .copied()
                        .unwrap_or(20.0) as u32;
                    if self.has_essence("time_machine_essence") {
                        threshold = 0;
                    }
                    let machines = self
                        .items_held
                        .iter()
                        .filter(|id| self.data.items[**id].name == "time_machine")
                        .count() as u32;
                    if machines > 0 {
                        let step = self
                            .data
                            .item_index
                            .get("time_machine")
                            .map(|id| {
                                self.data.items[*id]
                                    .values
                                    .first()
                                    .copied()
                                    .unwrap_or(5.0) as u32
                            })
                            .unwrap_or(5);
                        threshold = threshold.saturating_sub(machines * step);
                    }
                    if self.deck[index].times_displayed >= threshold {
                        if let Some(diamond) = self.data.symbol_id("diamond") {
                            self.deck[index].type_id = diamond as u16;
                            self.deck[index].times_displayed = 0;
                        }
                    }
                }
                "present" => {
                    if self.deck[index].times_displayed
                        >= values.first().copied().unwrap_or(12.0) as u32
                    {
                        destroy_self = true;
                    }
                }
                "bubble" => {
                    let threshold =
                        (values.first().copied().unwrap_or(3.0) as u32).saturating_sub(1);
                    if self.deck[index].times_coins_given >= threshold {
                        destroy_self = true;
                    }
                }
                "spirit" => {
                    let threshold =
                        (values.first().copied().unwrap_or(4.0) as u32).saturating_sub(1);
                    if self.deck[index].times_coins_given >= threshold {
                        destroy_self = true;
                    }
                }
                "bar_of_soap" => {
                    if let Some(bubble) = self.data.symbol_id("bubble") {
                        spawned.push(bubble);
                    }
                    let threshold =
                        (values.first().copied().unwrap_or(3.0) as u32).saturating_sub(1);
                    if self.deck[index].times_coins_given >= threshold {
                        destroy_self = true;
                    }
                }
                "seed" => {
                    if self.has_essence("watering_can_essence") {
                        let bonus = self
                            .data
                            .essence_index
                            .get("watering_can_essence")
                            .map(|id| {
                                self.data.essences[*id]
                                    .values
                                    .first()
                                    .copied()
                                    .unwrap_or(20.0) as i32
                            })
                            .unwrap_or(20);
                        spin_bonus[index] += bonus;
                    }
                    let fertilizer_essence = self.has_essence("fertilizer_essence");
                    let can_multiplier = self
                        .items_held
                        .iter()
                        .filter(|item_id| self.data.items[**item_id].name == "watering_can")
                        .map(|item_id| {
                            1.0 + self.data.items[*item_id].values.first().copied().unwrap_or(0.0)
                                / 100.0
                        })
                        .product::<f64>();
                    let rain_multiplier: f64 = neighbors
                        .iter()
                        .filter_map(|neighbor| {
                            let def = &self.data.symbols[self.deck[*neighbor].type_id as usize];
                            if def.name == "rain" {
                                Some(
                                    1.0 + def.values.get(1).copied().unwrap_or(50.0) / 100.0,
                                )
                            } else {
                                None
                            }
                        })
                        .product::<f64>()
                        .max(1.0);
                    let fertilizer_copies = self
                        .items_held
                        .iter()
                        .filter(|item_id| self.data.items[**item_id].name == "fertilizer")
                        .map(|item_id| {
                            self.data.items[*item_id]
                                .values
                                .first()
                                .copied()
                                .unwrap_or(50.0)
                                / 100.0
                        })
                        .sum::<f64>();
                    if !self.has_fine_print(34)
                        && chance_allowed
                        && self.rng.unit() * 100.0
                            < values.first().copied().unwrap_or(25.0)
                                * can_multiplier
                                * (1.0 + fertilizer_copies)
                                * rain_multiplier
                    {
                        let plant = if fertilizer_essence {
                            self.pick_group_min("plant", Rarity::VeryRare)
                        } else if fertilizer_copies > 0.0 {
                            self.pick_group_min("plant", Rarity::Rare)
                        } else {
                            self.pick_group("plant")
                        };
                        if let Some(plant) = plant {
                            self.deck[index].type_id = plant as u16;
                            if fertilizer_essence {
                                if let Some(id) =
                                    self.data.essence_index.get("fertilizer_essence").copied()
                                {
                                    self.try_destroy_essence(id);
                                }
                            }
                            if let Some(id) =
                                self.data.essence_index.get("watering_can_essence").copied()
                            {
                                self.try_destroy_essence(id);
                            }
                        }
                    }
                }
                "snail" | "sloth" | "turtle" | "owl" | "magpie" | "crow" => {
                    let flag_reduction = self
                        .items_held
                        .iter()
                        .filter(|item_id| self.data.items[**item_id].name == "checkered_flag")
                        .map(|item_id| {
                            self.data.items[*item_id].values.first().copied().unwrap_or(0.0) as u32
                        })
                        .sum::<u32>();
                    let fp12 = if self.fp_affects(12, &name) {
                        self.fp_value(12, 0) as u32
                    } else {
                        0
                    };
                    let threshold = (values.get(1).copied().unwrap_or(3.0) as u32)
                        .saturating_sub(flag_reduction)
                        + fp12;
                    let instant = self.has_essence("checkered_flag_essence");
                    if instant || self.deck[index].times_displayed >= threshold.max(1) {
                        spin_bonus[index] += values.first().copied().unwrap_or(0.0) as i32;
                        self.deck[index].times_displayed = 0;
                    }
                }
                "robin_hood" => {
                    let flag_reduction = self
                        .items_held
                        .iter()
                        .filter(|item_id| self.data.items[**item_id].name == "checkered_flag")
                        .map(|item_id| {
                            self.data.items[*item_id].values.first().copied().unwrap_or(0.0) as u32
                        })
                        .sum::<u32>();
                    let fp12 = if self.fp_affects(12, &name) {
                        self.fp_value(12, 0) as u32
                    } else {
                        0
                    };
                    let threshold = (values.get(1).copied().unwrap_or(4.0) as u32)
                        .saturating_sub(flag_reduction)
                        + fp12;
                    let instant = self.has_essence("checkered_flag_essence");
                    if instant || self.deck[index].times_displayed >= threshold.max(1) {
                        spin_bonus[index] += values.first().copied().unwrap_or(25.0) as i32;
                        self.deck[index].times_displayed = 0;
                    }
                    let steal = values.get(3).copied().unwrap_or(15.0) as i32;
                    for n in neighbors {
                        let other = self.deck[n].type_id as usize;
                        if self.has_group(other, "robinlikes") {
                            spin_bonus[n] += values.get(2).copied().unwrap_or(3.0) as i32;
                        } else if self.has_group(other, "robinhates") {
                            destroyed[n] = true;
                            spin_bonus[index] += steal;
                        }
                    }
                }
                "wine" => {
                    if self.deck[index].times_displayed >= values.get(1).copied().unwrap_or(8.0) as u32
                    {
                        self.deck[index].bonus += values.first().copied().unwrap_or(1.0) as i32;
                        self.deck[index].times_displayed = 0;
                    }
                }
                "tomb" => {
                    if chance_allowed
                        && self.rng.unit() * 100.0
                            < values.first().copied().unwrap_or(6.0) * self.spawn_multiplier()
                    {
                        if let Some(spirit) = self.data.symbol_id("spirit") {
                            spawned.push(spirit);
                            self.note_spawner_spawns(1);
                        }
                    }
                }
                "goldfish" => {
                    let gain = values.first().copied().unwrap_or(15.0) as i32;
                    for n in neighbors {
                        if self.data.symbols[self.deck[n].type_id as usize].name == "bubble" {
                            destroyed[n] = true;
                            spin_bonus[index] += gain;
                        }
                    }
                }
                "ruby" | "emerald" => {
                    let count = entries
                        .iter()
                        .filter(|(other, _, _)| {
                            self.data.symbols[self.deck[*other].type_id as usize].name == name
                        })
                        .count() as i32;
                    if count >= values.get(1).copied().unwrap_or(2.0) as i32 {
                        spin_bonus[index] += values.first().copied().unwrap_or(1.0) as i32;
                    }
                }
                "strawberry" => {
                    let count = entries
                        .iter()
                        .filter(|(other, _, _)| {
                            self.data.symbols[self.deck[*other].type_id as usize].name == "strawberry"
                        })
                        .count() as i32;
                    if count >= values.get(1).copied().unwrap_or(2.0) as i32 {
                        spin_bonus[index] += values.first().copied().unwrap_or(1.0) as i32;
                    }
                }
                "joker" => {
                    let amount = values.first().copied().unwrap_or(2.0);
                    for n in neighbors {
                        if self.has_group(self.deck[n].type_id as usize, "suit") {
                            mult[n] *= amount;
                        }
                    }
                }
                "clubs" | "spades" | "diamonds" | "hearts" => {
                    let suit_count = entries
                        .iter()
                        .filter(|(other, _, _)| {
                            self.has_group(self.deck[*other].type_id as usize, "suit")
                        })
                        .count() as i32;
                    if suit_count >= values.get(2).copied().unwrap_or(3.0) as i32 {
                        spin_bonus[index] += values.get(1).copied().unwrap_or(1.0) as i32;
                    }
                    let blue = matches!(name.as_str(), "clubs" | "spades");
                    for n in neighbors {
                        if destroyed[n] {
                            continue;
                        }
                        let other = self.data.symbols[self.deck[n].type_id as usize]
                            .name
                            .as_str();
                        let same_color = if blue {
                            matches!(other, "clubs" | "spades")
                        } else {
                            matches!(other, "diamonds" | "hearts")
                        };
                        if same_color {
                            spin_bonus[n] += values.first().copied().unwrap_or(1.0) as i32;
                        }
                    }
                }
                "comedian" => {
                    let amount = values.first().copied().unwrap_or(3.0);
                    let dark_humor = self.data.item_index.get("dark_humor").copied();
                    let dark_factor = dark_humor
                        .map(|id| {
                            let copies = self
                                .items_held
                                .iter()
                                .filter(|held| self.data.items[**held].name == "dark_humor")
                                .count() as i32;
                            self.data.items[id]
                                .values
                                .get(1)
                                .copied()
                                .unwrap_or(3.0)
                                .powi(copies)
                        })
                        .unwrap_or(1.0);
                    for n in neighbors {
                        if self.has_group(self.deck[n].type_id as usize, "funny") {
                            mult[n] *= amount;
                        }
                        if dark_humor.is_some()
                            && self.has_group(self.deck[n].type_id as usize, "darkhumor")
                        {
                            mult[n] *= dark_factor;
                        }
                    }
                }
                "beehive" => {
                    if chance_allowed
                        && self.rng.unit() * 100.0
                            < values.first().copied().unwrap_or(10.0) * self.spawn_multiplier()
                    {
                        if let Some(honey) = self.data.symbol_id("honey") {
                            spawned.push(honey);
                            self.note_spawner_spawns(1);
                        }
                    }
                }
                "cow" => {
                    if chance_allowed
                        && self.rng.unit() * 100.0
                            < values.first().copied().unwrap_or(15.0) * self.spawn_multiplier()
                    {
                        if let Some(milk) = self.data.symbol_id("milk") {
                            spawned.push(milk);
                            self.note_spawner_spawns(1);
                        }
                    }
                }
                "frozen_fossil" => {
                    if self.deck[index].times_displayed
                        >= values.first().copied().unwrap_or(20.0) as u32
                    {
                        destroy_self = true;
                    }
                }
                "rabbit" => {
                    if self.deck[index].times_displayed
                        >= values.get(1).copied().unwrap_or(10.0) as u32
                    {
                        self.deck[index].bonus += values.first().copied().unwrap_or(2.0) as i32;
                        self.deck[index].times_displayed = 0;
                    }
                    let shedding: f64 = self
                        .items_held
                        .iter()
                        .filter(|id| self.data.items[**id].name == "shedding_season")
                        .map(|id| {
                            self.data.items[*id]
                                .values
                                .first()
                                .copied()
                                .unwrap_or(15.0)
                        })
                        .sum();
                    if shedding > 0.0
                        && chance_allowed
                        && self.rng.unit() * 100.0 < shedding * self.spawn_multiplier()
                    {
                        if let Some(fluff) = self.data.symbol_id("rabbit_fluff") {
                            spawned.push(fluff);
                        }
                    }
                }
                "billionaire" => {
                    let multiplier = values.first().copied().unwrap_or(2.0);
                    for n in neighbors {
                        if self.has_group(self.deck[n].type_id as usize, "richlikes")
                            && !destroyed[n]
                        {
                            mult[n] *= multiplier;
                        }
                    }
                }
                "bronze_arrow" | "silver_arrow" | "golden_arrow" => {
                    // Godot numbers directions 1..8 clockwise from NW and
                    // picks them with `floor(_sir_rand_range(1, 9))`.
                    let offsets = [
                        (-1i32, -1i32), // 1 NW
                        (0, -1),        // 2 N
                        (1, -1),        // 3 NE
                        (1, 0),         // 4 E
                        (1, 1),         // 5 SE
                        (0, 1),         // 6 S
                        (-1, 1),        // 7 SW
                        (-1, 0),        // 8 W
                    ];
                    let chosen = pre_rolls
                        .get(&index)
                        .map(|pre| pre.arrow_directions.clone())
                        .filter(|directions| !directions.is_empty())
                        .unwrap_or_else(|| {
                            let first = 1 + self.rng.range(0, 8);
                            let mut directions = vec![first];
                            if self.has_item("quiver") || self.has_essence("quiver_essence") {
                                let mut remaining: Vec<usize> =
                                    (1..=8).filter(|direction| *direction != first).collect();
                                let pick = self.rng.range(0, remaining.len());
                                directions.push(remaining.remove(pick));
                            }
                            directions
                        });
                    let multiplier = values.first().copied().unwrap_or(2.0);
                    let mut hit: Vec<usize> = Vec::new();
                    for direction in chosen {
                        let (dx, dy) = offsets[direction - 1];
                        let mut cx = x as i32 + dx;
                        let mut cy = y as i32 + dy;
                        while cx >= 0
                            && cx < BOARD_COLS as i32
                            && cy >= 0
                            && cy < BOARD_ROWS as i32
                        {
                            if let Some(other) =
                                self.board.slots[cy as usize * BOARD_COLS + cx as usize]
                            {
                                if !destroyed[other] && !hit.contains(&other) {
                                    hit.push(other);
                                    mult[other] *= multiplier;
                                    if self.data.symbols[self.deck[other].type_id as usize].name
                                        == "target"
                                    {
                                        destroyed[other] = true;
                                    }
                                }
                            }
                            cx += dx;
                            cy += dy;
                        }
                    }
                }
                "buffing_powder" => {
                    let multiplier = values.first().copied().unwrap_or(2.0);
                    for n in neighbors {
                        mult[n] *= multiplier;
                    }
                    destroy_self = true;
                }
                "rain" => {
                    for n in neighbors {
                        if self.data.symbols[self.deck[n].type_id as usize].name == "flower" {
                            mult[n] *= values.first().copied().unwrap_or(2.0);
                        }
                    }
                }
                "hex_of_thievery" => {
                    let mut chance = values.first().copied().unwrap_or(30.0);
                    if self.fp_affects(8, "hex_of_thievery") {
                        chance *= self.fp_value(8, 0).max(1.0);
                    }
                    if chance_allowed && self.rng.unit() * 100.0 < chance {
                        extra_income -= values.get(1).copied().unwrap_or(6.0) as i32;
                    }
                }
                "hex_of_midas" => {
                    let mut chance =
                        values.first().copied().unwrap_or(30.0) * self.spawn_multiplier();
                    if self.fp_affects(8, "hex_of_midas") {
                        chance *= self.fp_value(8, 0).max(1.0);
                    }
                    if chance_allowed && self.rng.unit() * 100.0 < chance {
                        if let Some(coin) = self.data.symbol_id("coin") {
                            spawned.push(coin);
                            self.note_spawner_spawns(1);
                        }
                    }
                }
                "hex_of_emptiness" => {
                    let mut chance = values.first().copied().unwrap_or(30.0);
                    if self.fp_affects(8, "hex_of_emptiness") {
                        chance *= self.fp_value(8, 0).max(1.0);
                    }
                    if chance_allowed
                        && self.spins_left != 1
                        && !self.has_item("holy_water")
                        && !self.force_take_symbol
                        && self.rng.unit() * 100.0 < chance
                    {
                        self.force_skip_reward = true;
                    }
                }
                "hex_of_hoarding" => {
                    let mut chance = values.first().copied().unwrap_or(30.0);
                    if self.fp_affects(8, "hex_of_hoarding") {
                        chance *= self.fp_value(8, 0).max(1.0);
                    }
                    if chance_allowed
                        && self.spins_left != 1
                        && !self.has_item("holy_water")
                        && !self.force_skip_reward
                        && self.rng.unit() * 100.0 < chance
                    {
                        self.force_take_symbol = true;
                    }
                }
                "essence_capsule" => {
                    self.essence_tokens +=
                        values.first().copied().unwrap_or(1.0) as i32 * self.capsule_multiplier();
                    destroy_self = true;
                }
                "time_capsule" => {
                    let candidates: Vec<u16> = self
                        .destroyed_history
                        .iter()
                        .copied()
                        .filter(|type_id| {
                            let def = &self.data.symbols[*type_id as usize];
                            def.name != "time_capsule"
                                && def.rarity != "none"
                                && !def
                                    .groups
                                    .iter()
                                    .any(|group| group == "time_capsule_effects")
                        })
                        .collect();
                    if !candidates.is_empty() {
                        let copies = (values.first().copied().unwrap_or(1.0) as i32
                            * self.capsule_multiplier())
                        .max(1) as usize;
                        for _ in 0..copies {
                            let pick = candidates[self.rng.range(0, candidates.len())];
                            spawned.push(pick as usize);
                        }
                    }
                    destroy_self = true;
                }
                "rarity_capsule" => {
                    let count = (values.first().copied().unwrap_or(1.0) as i32
                        * self.capsule_multiplier())
                    .max(1) as usize;
                    for _ in 0..count {
                        self.symbol_forced_queue.push((Rarity::Rare, true));
                    }
                    destroy_self = true;
                }
                "hustler" => {
                    // The pool ball is granted when the hustler is destroyed
                    // (`items_to_add` on tbd), so it is handled in the
                    // destroyed-symbol pass below.
                    destroy_self = true;
                }
                "item_capsule" => {
                    let count = (values.first().copied().unwrap_or(1.0) as i32
                        * self.capsule_multiplier())
                    .max(1);
                    for _ in 0..count {
                        let mut pool = self.item_pools[0].clone();
                        pool.retain(|item| !self.items_held.contains(item));
                        if pool.is_empty() {
                            break;
                        }
                        let pick = pool[self.rng.range(0, pool.len())];
                        self.grant_item(pick);
                    }
                    destroy_self = true;
                }
                "tedium_capsule" => {
                    extra_income += values.first().copied().unwrap_or(5.0) as i32
                        * self.capsule_multiplier();
                    let count = values.get(1).copied().unwrap_or(1.0).max(1.0) as usize;
                    for _ in 0..count {
                        self.symbol_forced_queue.push((Rarity::Common, false));
                    }
                    destroy_self = true;
                }
                "anchor" => {
                    if (x == 0 || x == BOARD_COLS - 1) && (y == 0 || y == BOARD_ROWS - 1) {
                        spin_bonus[index] += values.first().copied().unwrap_or(4.0) as i32;
                    }
                }
                "monkey" => {
                    let gain = values.first().copied().unwrap_or(6.0);
                    for n in neighbors {
                        if self.has_group(self.deck[n].type_id as usize, "monkeylikes") {
                            destroyed[n] = true;
                            let victim_def =
                                &self.data.symbols[self.deck[n].type_id as usize];
                            let victim = ((self.deck[n].bonus + victim_def.base_value) as f64
                                + self.permanent_bonus_for(victim_def) as f64)
                                * self.permanent_mult_for(victim_def);
                            spin_bonus[index] += (victim * gain).round() as i32;
                        }
                    }
                }
                "goose" => {
                    if chance_allowed
                        && self.rng.unit() * 100.0
                            < values.first().copied().unwrap_or(1.0) * self.spawn_multiplier()
                    {
                        if let Some(golden) = self.data.symbol_id("golden_egg") {
                            spawned.push(golden);
                            self.note_spawner_spawns(1);
                        }
                    }
                }
                "banana_peel" => {
                    let mut eaten = false;
                    for n in neighbors {
                        if self.data.symbols[self.deck[n].type_id as usize].name == "thief"
                            && !destroyed[n]
                        {
                            destroyed[n] = true;
                            eaten = true;
                        }
                    }
                    if eaten {
                        destroy_self = true;
                    }
                }
                "oyster" => {
                    if chance_allowed
                        && self.rng.unit() * 100.0
                            < values.first().copied().unwrap_or(20.0) * self.spawn_multiplier()
                    {
                        if let Some(pearl) = self.data.symbol_id("pearl") {
                            spawned.push(pearl);
                            self.note_spawner_spawns(1);
                        }
                    }
                }
                "egg" => {
                    let coop = self.item_multiplier("chicken_coop", 1);
                    if chance_allowed
                        && self.rng.unit() * 100.0
                            < values.first().copied().unwrap_or(10.0) * coop
                    {
                        if let Some(chick) = self.data.symbol_id("chick") {
                            // The real game pays the icon once before the type
                            // change and once after it (egg 1 + chick 1 = 2).
                            spin_bonus[index] += self.deck[index].current_value(self.data);
                            self.deck[index].type_id = chick as u16;
                            egg_became_chick = true;
                        }
                    }
                }
                "chick" => {
                    if chance_allowed
                        && self.rng.unit() * 100.0 < values.first().copied().unwrap_or(10.0)
                    {
                        if let Some(chicken) = self.data.symbol_id("chicken") {
                            // chick 1 + chicken 2 = 3 in a single coin sum.
                            spin_bonus[index] += self.deck[index].current_value(self.data);
                            self.deck[index].type_id = chicken as u16;
                            chick_became_chicken = true;
                        }
                    }
                }
                "chicken" => {
                    let spawn_multiplier = self.spawn_multiplier();
                    let coop = self.item_multiplier("chicken_coop", 0);
                    let egg_chance =
                        values.first().copied().unwrap_or(5.0) * spawn_multiplier * coop;
                    let golden_chance =
                        values.get(1).copied().unwrap_or(1.0) * spawn_multiplier * coop;
                    if chance_allowed && self.rng.unit() * 100.0 < egg_chance {
                        if let Some(egg) = self.data.symbol_id("egg") {
                            spawned.push(egg);
                            self.note_spawner_spawns(1);
                        }
                    }
                    if chance_allowed && self.rng.unit() * 100.0 < golden_chance {
                        if let Some(golden) = self.data.symbol_id("golden_egg") {
                            spawned.push(golden);
                            self.note_spawner_spawns(1);
                        }
                    }
                }
                "lucky_capsule" => {
                    if !self.fp_affects(37, "lucky_capsule") {
                        extra_income += values.first().copied().unwrap_or(10.0) as i32
                            * self.capsule_multiplier();
                        destroy_self = true;
                    }
                }
                "reroll_capsule" => {
                    if !self.fp_affects(37, "reroll_capsule") {
                        self.reroll_tokens += values.first().copied().unwrap_or(1.0) as i32
                            * self.capsule_multiplier();
                        destroy_self = true;
                    }
                }
                "removal_capsule" => {
                    if !self.fp_affects(37, "removal_capsule") {
                        self.removal_tokens += values.first().copied().unwrap_or(1.0) as i32
                            * self.capsule_multiplier();
                        destroy_self = true;
                    }
                }
                _ => {}
            }
            if destroy_self {
                destroyed[index] = true;
            }
        }

        // Items gained during the spin (item capsule, hustler) run their
        // per-spin effects in the same spin in Godot: the next `check_effects`
        // pass registers them before `check_values`.
        for item_id in self.items_held.clone() {
            if items_at_start.contains(&item_id) {
                continue;
            }
            let item = &self.data.items[item_id];
            let name = item.name.clone();
            let v0 = item.values.first().copied().unwrap_or(0.0);
            let v1 = item.values.get(1).copied().unwrap_or(0.0);
            let v2 = item.values.get(2).copied().unwrap_or(0.0);
            let flat: Option<(bool, &[&str], f64)> = match name.as_str() {
                "fruit_basket" => Some((true, &["fruit"][..], v0)),
                "cleaning_rag" => Some((true, &["gem"][..], v0)),
                "birdhouse" => Some((true, &["bird"][..], v0)),
                "happy_hour" => Some((true, &["booze"][..], v0)),
                "kyle_the_kernite" => Some((true, &["kyle"][..], v0)),
                "anthropology_degree" => Some((true, &["human"][..], v0)),
                "ritual_candle" => Some((true, &["fossillikes"][..], v0)),
                "holy_water" => Some((true, &["hex"][..], v0)),
                "chicken_coop" => Some((true, &["chickenstuff"][..], v2)),
                "nori_the_rabbit" => Some((false, &["rabbit", "rabbit_fluff"][..], v0)),
                "pizza_the_cat" => Some((false, &["cat"][..], v0)),
                "quigley_the_wolf" => Some((false, &["wolf"][..], v0)),
                "ricky_the_banana" => Some((false, &["banana", "banana_peel"][..], v0)),
                "rain_cloud" => Some((false, &["rain"][..], v0)),
                "watering_can" => Some((false, &["seed"][..], v1)),
                _ => None,
            };
            if let Some((is_group, tags, amount)) = flat {
                for (index, _, _) in entries.iter().copied() {
                    if destroyed[index] {
                        continue;
                    }
                    let type_id = self.deck[index].type_id as usize;
                    let hit = tags.iter().any(|tag| {
                        if is_group {
                            self.has_group(type_id, tag)
                        } else {
                            self.data.symbols[type_id].name == *tag
                        }
                    });
                    if hit {
                        spin_bonus[index] += amount as i32;
                    }
                }
            }
            let mult_tag: Option<(bool, &str, f64)> = match name.as_str() {
                "wanted_poster" => Some((false, "thief", v0)),
                "jackolantern" => Some((true, "halloween", v0)),
                "maxwell_the_bear" => Some((false, "bear", v0)),
                "dwarven_anvil" => Some((false, "dwarf", v0)),
                "oswald_the_monkey" => Some((false, "monkey", v0)),
                "black_cat" => Some((false, "cat", v2)),
                "lucky_carrot" | "golden_carrot" => Some((false, "rabbit", v1)),
                "triple_coins" => Some((false, "coin", v0)),
                _ => None,
            };
            if let Some((is_group, tag, amount)) = mult_tag {
                for (index, _, _) in entries.iter().copied() {
                    if destroyed[index] {
                        continue;
                    }
                    let type_id = self.deck[index].type_id as usize;
                    let hit = if is_group {
                        self.has_group(type_id, tag)
                    } else {
                        self.data.symbols[type_id].name == tag
                    };
                    if hit {
                        mult[index] *= amount;
                    }
                }
            }
            let flat_income = match name.as_str() {
                "pool_ball" | "horseshoe" | "bowling_ball" | "four_leaf_clover"
                | "coin_on_a_string" => v0 as i32,
                "egg_carton" => self.stored_eggs.min(v1 as i32) * v0 as i32,
                "fish_bowl" => self.stored_goldfish * v0 as i32,
                _ => 0,
            };
            extra_income += flat_income;
        }

        if egg_became_chick && chick_became_chicken && self.has_essence("chicken_coop_essence") {
            let bonus = self
                .data
                .essence_index
                .get("chicken_coop_essence")
                .map(|id| {
                    self.data.essences[*id]
                        .values
                        .first()
                        .copied()
                        .unwrap_or(4.0) as i32
                })
                .unwrap_or(4);
            if let Some(essence_id) = self
                .data
                .essence_index
                .get("chicken_coop_essence")
                .copied()
            {
                self.try_destroy_essence(essence_id);
                if !self.has_essence("chicken_coop_essence") {
                    self.add_permanent_group_bonus("chickenstuff", bonus);
                }
            }
        }

        // Void symbols pay 1 coin for every adjacent empty cell and destroy
        // themselves when none is left.  Godot decides the destruction at
        // registration time (before anything has been destroyed this spin),
        // while the coin bonus is a conditional effect checked later, after
        // destroyed icons have turned into empty cells.  The two therefore
        // see slightly different neighbourhoods.
        if !void_effects.is_empty() {
            let effective_empties = |x: usize, y: usize, destroyed: &[bool]| -> i32 {
                let mut count = 0;
                for dy in -1i32..=1 {
                    for dx in -1i32..=1 {
                        if (dx == 0 && dy == 0) || (dx == 0 && self.has_fine_print(22)) {
                            continue;
                        }
                        let nx = x as i32 + dx;
                        let ny = y as i32 + dy;
                        if nx < 0
                            || nx >= BOARD_COLS as i32
                            || ny < 0
                            || ny >= BOARD_ROWS as i32
                        {
                            continue;
                        }
                        match self.board.slots[ny as usize * BOARD_COLS + nx as usize] {
                            None => count += 1,
                            Some(other) if destroyed[other] => count += 1,
                            _ => {}
                        }
                    }
                }
                count
            };
            for &(index, x, y, _, threshold, protected) in &void_effects {
                if !protected
                    && !destroyed[index]
                    && self.empty_neighbor_count(x, y) <= threshold
                {
                    destroyed[index] = true;
                }
            }
            for &(_, x, y, gain, _, _) in &void_effects {
                extra_income += effective_empties(x, y, &destroyed) * gain;
            }
        }

        let mut reanimated: HashMap<usize, i32> = HashMap::new();
        for (index, _, _) in entries.iter().copied() {
            if !destroyed[index] {
                continue;
            }
            let type_id = self.deck[index].type_id as usize;
            let name = self.data.symbols[type_id].name.as_str();
            for item_id in self.items_held.clone() {
                let item = &self.data.items[item_id];
                let group = match item.name.as_str() {
                    "void_party" => "void",
                    "mobius_strip" => "box",
                    _ => continue,
                };
                if !self.has_group(type_id, group) {
                    continue;
                }
                let chance = item.values.first().copied().unwrap_or(50.0);
                let limit = item
                    .values
                    .get(1)
                    .copied()
                    .unwrap_or(7.0)
                    .max(1.0) as i32;
                let used = reanimated.get(&item_id).copied().unwrap_or(0);
                if used < limit && self.rng.unit() * 100.0 < chance {
                    reanimated.insert(item_id, used + 1);
                    spawned.push(type_id);
                }
            }
            let gem_floor = if self.has_essence("x_ray_machine_essence") {
                Rarity::VeryRare
            } else if self.has_item("x_ray_machine") {
                Rarity::Rare
            } else {
                Rarity::Common
            };
            match name {
                "ore" => {
                    if !self.fp_affects(15, name) {
                        if let Some(gem) = self.pick_group_min("gem", gem_floor) {
                            spawned.push(gem);
                        }
                        if let Some(essence_id) = self
                            .data
                            .essence_index
                            .get("x_ray_machine_essence")
                            .copied()
                            .filter(|id| self.essences_held.contains(id))
                        {
                            self.try_destroy_essence(essence_id);
                        }
                    }
                }
                "big_ore" => {
                    if !self.fp_affects(15, name) {
                        for _ in 0..self.data.symbols[type_id]
                            .values
                            .first()
                            .copied()
                            .unwrap_or(2.0) as i32
                        {
                            if let Some(gem) = self.pick_group_min("gem", gem_floor) {
                                spawned.push(gem);
                            }
                        }
                    }
                }
                "coconut" => {
                    if let Some(half) = self.data.symbol_id("coconut_half") {
                        for _ in 0..self.data.symbols[type_id]
                            .values
                            .first()
                            .copied()
                            .unwrap_or(2.0) as i32
                        {
                            spawned.push(half);
                        }
                    }
                }
                "lockbox" | "safe" | "treasure_chest" | "mega_chest" => {
                    let fp9 = if self.fp_affects(9, &name) {
                        self.fp_value(9, 0)
                    } else {
                        0.0
                    };
                    let bonus = self.data.symbols[type_id]
                        .values
                        .first()
                        .copied()
                        .unwrap_or(0.0)
                        - fp9;
                    let mut glove_multiplier: f64 = self
                        .items_held
                        .iter()
                        .filter(|item_id| self.data.items[**item_id].name == "looting_glove")
                        .map(|item_id| {
                            self.data.items[*item_id]
                                .values
                                .first()
                                .copied()
                                .unwrap_or(1.5)
                        })
                        .product::<f64>()
                        .max(1.0);
                    if let Some(essence_id) = self
                        .data
                        .essence_index
                        .get("looting_glove_essence")
                        .copied()
                        .filter(|id| self.essences_held.contains(id))
                    {
                        glove_multiplier *= self.data.essences[essence_id]
                            .values
                            .first()
                            .copied()
                            .unwrap_or(3.5);
                        self.try_destroy_essence(essence_id);
                    }
                    extra_income += (bonus.max(0.0) * glove_multiplier).round() as i32;
                }
                "thief" => {
                    extra_income += self.deck[index].times_displayed as i32
                        * self.data.symbols[type_id]
                            .values
                            .get(1)
                            .copied()
                            .unwrap_or(4.0) as i32;
                }
                "gambler" => {
                    extra_income += self.deck[index].times_displayed as i32
                        * self.data.symbols[type_id]
                            .values
                            .get(1)
                            .copied()
                            .unwrap_or(2.0) as i32;
                }
                "hustler" => {
                    let count = (self.data.symbols[type_id]
                        .values
                        .first()
                        .copied()
                        .unwrap_or(1.0) as i32
                        * self.capsule_multiplier())
                    .max(1);
                    if let Some(pool_ball) = self.data.item_index.get("pool_ball").copied() {
                        for _ in 0..count {
                            self.grant_item(pool_ball);
                        }
                        // A newly granted pool ball pays its 1 coin in the
                        // same spin.
                        extra_income += count
                            * self.data.items[pool_ball]
                                .values
                                .first()
                                .copied()
                                .unwrap_or(1.0) as i32;
                    }
                }
                "present" => {
                    // Present pays values[1] only when it is (or was) destroyed
                    // this spin, whichever effect removed it.
                    extra_income += self.data.symbols[type_id]
                        .values
                        .get(1)
                        .copied()
                        .unwrap_or(10.0) as i32;
                }
                "banana" => {
                    if let Some(peel) = self.data.symbol_id("banana_peel") {
                        spawned.push(peel);
                    }
                }
                "peach" => {
                    if let Some(seed) = self.data.symbol_id("seed") {
                        spawned.push(seed);
                    }
                }
                "frozen_fossil" => {
                    if let Some(beast) = self.data.symbol_id("eldritch_beast") {
                        spawned.push(beast);
                    }
                }
                "pinata" => {
                    if let Some(candy) = self.data.symbol_id("candy") {
                        for _ in 0..self.data.symbols[type_id]
                            .values
                            .first()
                            .copied()
                            .unwrap_or(7.0) as i32
                        {
                            spawned.push(candy);
                        }
                    }
                }
                "urn" => {
                    if let Some(spirit) = self.data.symbol_id("spirit") {
                        let fp11 = if self.fp_affects(11, "urn") {
                            self.fp_value(11, 0) as i32
                        } else {
                            0
                        };
                        let count = ((1 - fp11).max(0) * self.grave_robber_mult).max(0);
                        for _ in 0..count {
                            spawned.push(spirit);
                        }
                    }
                }
                "tomb" => {
                    if let Some(spirit) = self.data.symbol_id("spirit") {
                        let fp11 = if self.fp_affects(11, "tomb") {
                            self.fp_value(11, 0) as i32
                        } else {
                            0
                        };
                        let count = ((self.data.symbols[type_id]
                            .values
                            .get(1)
                            .copied()
                            .unwrap_or(4.0) as i32
                            - fp11)
                            .max(0)
                            * self.grave_robber_mult)
                            .max(0);
                        for _ in 0..count {
                            spawned.push(spirit);
                        }
                    }
                }
                "big_urn" => {
                    if let Some(spirit) = self.data.symbol_id("spirit") {
                        let fp11 = if self.fp_affects(11, "big_urn") {
                            self.fp_value(11, 0) as i32
                        } else {
                            0
                        };
                        let count = ((self.data.symbols[type_id]
                            .values
                            .first()
                            .copied()
                            .unwrap_or(2.0) as i32
                            - fp11)
                            .max(0)
                            * self.grave_robber_mult)
                            .max(0);
                        for _ in 0..count {
                            spawned.push(spirit);
                        }
                    }
                }
                "spirit" => {
                    if self.shrine_active && !self.has_item("undertaker") {
                        if let Some(organism) = self.pick_group("organism") {
                            spawned.push(organism);
                        }
                    }
                }
                "moon" => {
                    if let Some(cheese) = self.data.symbol_id("cheese") {
                        for _ in 0..self.data.symbols[type_id]
                            .values
                            .first()
                            .copied()
                            .unwrap_or(3.0) as i32
                        {
                            spawned.push(cheese);
                        }
                    }
                }
                "billionaire" => {
                    extra_income += self.data.symbols[type_id]
                        .values
                        .get(1)
                        .copied()
                        .unwrap_or(39.0) as i32;
                }
                "void_fruit" | "void_creature" | "void_stone" => {
                    extra_income += self.data.symbols[type_id]
                        .values
                        .get(2)
                        .copied()
                        .unwrap_or(8.0) as i32;
                }
                "target" => {
                    extra_income += self.data.symbols[type_id]
                        .values
                        .first()
                        .copied()
                        .unwrap_or(10.0) as i32;
                }
                _ => {}
            }
        }

        let shrine_copies = self
            .items_held
            .iter()
            .filter(|id| self.data.items[**id].name == "shrine")
            .count();
        if shrine_copies > 0 {
            if let Some(spirit) = self.data.symbol_id("spirit") {
                let organisms = entries
                    .iter()
                    .filter(|(index, _, _)| {
                        destroyed[*index]
                            && self.has_group(self.deck[*index].type_id as usize, "organism")
                    })
                    .count();
                for _ in 0..organisms * shrine_copies {
                    spawned.push(spirit);
                }
            }
        }

        let doves: Vec<(usize, usize, usize)> = entries
            .iter()
            .copied()
            .filter(|(index, _, _)| {
                !destroyed[*index]
                    && self.data.symbols[self.deck[*index].type_id as usize].name == "dove"
            })
            .collect();
        for (dove_index, x, y) in doves {
            let mut prevented = 0;
            for neighbor in self.neighbor_indexes(x, y, dove_index) {
                if destroyed[neighbor] {
                    destroyed[neighbor] = false;
                    prevented += 1;
                }
            }
            if prevented > 0 {
                let gain = self.data.symbols[self.deck[dove_index].type_id as usize]
                    .values
                    .first()
                    .copied()
                    .unwrap_or(1.0) as i32;
                self.deck[dove_index].bonus += gain * prevented;
            }
        }

        let frying_pan_essence = self
            .data
            .essence_index
            .get("frying_pan_essence")
            .copied()
            .filter(|id| self.essences_held.contains(id));
        let frying_pans = self
            .items_held
            .iter()
            .filter(|id| self.data.items[**id].name == "frying_pan")
            .count() as i32;
        if frying_pan_essence.is_some() || frying_pans > 0 {
            let mut fried = false;
            let egg_id = self.data.symbol_id("egg");
            let omelette_id = self.data.symbol_id("omelette");
            for (index, x, y) in entries.iter().copied() {
                if destroyed[index]
                    || Some(self.deck[index].type_id as usize) != egg_id
                    || self.has_fine_print(21)
                {
                    continue;
                }
                let adjacent_pan = self
                    .neighbor_indexes(x, y, index)
                    .iter()
                    .any(|neighbor| {
                        !destroyed[*neighbor]
                            && self.has_group(self.deck[*neighbor].type_id as usize, "omelettestuff")
                    });
                if adjacent_pan {
                    destroyed[index] = true;
                    fried = true;
                    let count = frying_pans + i32::from(frying_pan_essence.is_some());
                    if let Some(omelette) = omelette_id {
                        for _ in 0..count {
                            spawned.push(omelette);
                        }
                    }
                }
            }
            if fried {
                if let Some(essence_id) = frying_pan_essence {
                    self.try_destroy_essence(essence_id);
                    if !self.has_essence("frying_pan_essence") {
                        let factor = self.data.essences[essence_id]
                            .values
                            .first()
                            .copied()
                            .unwrap_or(3.0);
                        self.multiply_permanent_name("omelette", factor);
                    }
                }
            }
        }
        let destroyed_count = destroyed.iter().filter(|flag| **flag).count() as i32;
        if destroyed_count >= 3 {
            if let Some(essence_id) = self
                .data
                .essence_index
                .get("compost_heap_essence")
                .copied()
                .filter(|id| self.essences_held.contains(id))
            {
                let copies = self.essence_count("compost_heap_essence").max(1);
                let melons = self.data.essences[essence_id]
                    .values
                    .first()
                    .copied()
                    .unwrap_or(2.0) as i32
                    * copies;
                if let Some(watermelon) = self.data.symbol_id("watermelon") {
                    for _ in 0..melons {
                        spawned.push(watermelon);
                    }
                }
                self.try_destroy_essence(essence_id);
            }
        }
        for (index, _, _) in entries.iter().copied() {
            if destroyed[index] {
                self.destroyed_history.push(self.deck[index].type_id);
            }
        }
        if self.has_item("compost_heap") {
            self.compost_destroyed += destroyed_count as u32;
            let need = self
                .items_held
                .iter()
                .find(|id| self.data.items[**id].name == "compost_heap")
                .map(|id| {
                    self.data.items[*id]
                        .values
                        .first()
                        .copied()
                        .unwrap_or(3.0)
                        .max(1.0) as u32
                })
                .unwrap_or(3);
            while self.compost_destroyed >= need {
                self.compost_destroyed -= need;
                if let Some(seed) = self.data.symbol_id("seed") {
                    spawned.push(seed);
                }
            }
        } else {
            self.compost_destroyed = 0;
        }
        let added_count = spawned.len() as i32;
        if added_count > 0 {
            if let Some(essence_id) = self
                .data
                .essence_index
                .get("brown_pepper_essence")
                .copied()
                .filter(|id| self.essences_held.contains(id))
            {
                let gain = self.data.essences[essence_id]
                    .values
                    .first()
                    .copied()
                    .unwrap_or(10.0) as i32;
                extra_income += gain * added_count;
                self.try_destroy_essence(essence_id);
            }
        }
        if let Some(essence_id) = self
            .data
            .essence_index
            .get("shedding_season_essence")
            .copied()
            .filter(|id| self.essences_held.contains(id))
        {
            let rabbits = entries
                .iter()
                .filter(|(index, _, _)| {
                    !destroyed[*index]
                        && self.data.symbols[self.deck[*index].type_id as usize].name == "rabbit"
                })
                .count();
            if rabbits > 0 {
                let values = &self.data.essences[essence_id].values;
                let chance = values.first().copied().unwrap_or(100.0);
                let count = values.get(1).copied().unwrap_or(5.0) as usize;
                if self.rng.unit() * 100.0 < chance {
                    if let Some(fluff) = self.data.symbol_id("rabbit_fluff") {
                        for _ in 0..count {
                            spawned.push(fluff);
                        }
                    }
                    self.try_destroy_essence(essence_id);
                }
            }
        }
        if let Some(essence_id) = self
            .data
            .essence_index
            .get("sunglasses_essence")
            .copied()
            .filter(|id| self.essences_held.contains(id))
        {
            let values = &self.data.essences[essence_id].values;
            let give = values.first().copied().unwrap_or(5.0) as i32;
            let need = values.get(1).copied().unwrap_or(3.0) as i32;
            if self.removal_tokens >= need.max(1) {
                self.removal_tokens += give;
                self.try_destroy_essence(essence_id);
            }
        }
        let survivors: Vec<usize> = entries
            .iter()
            .map(|(index, _, _)| *index)
            .filter(|index| !destroyed[*index])
            .collect();
        // Godot checks item conditions in the first `check_effects` pass,
        // before any symbol effect has resolved, so peppers and lemons see
        // the board exactly as it was dealt.  Destroyed icons still count
        // as present here.
        let empty_count = (BOARD_SLOTS - entries.len()) as i32;
        let mut type_counts: HashMap<u16, i32> = HashMap::new();
        for (index, _, _) in entries.iter().copied() {
            *type_counts.entry(self.deck[index].type_id).or_default() += 1;
        }
        let max_same = type_counts.values().copied().max().unwrap_or(0);
        // `check_effects` re-runs item condition checks after icons have been
        // destroyed, so a condition that only becomes true on the thinner
        // board still fires that spin.
        let mut survivor_type_counts: HashMap<u16, i32> = HashMap::new();
        for index in &survivors {
            *survivor_type_counts
                .entry(self.deck[*index].type_id)
                .or_default() += 1;
        }
        let survivor_max_same = survivor_type_counts.values().copied().max().unwrap_or(0);
        let survivor_empty_count = (BOARD_SLOTS - survivors.len()) as i32;
        let survivor_all_different = survivor_type_counts.len() == survivors.len();
        let all_different = type_counts.len() == entries.len();
        let mut triple_adjacent = false;
        for (index, x, y) in entries.iter().copied() {
            let type_id = self.deck[index].type_id;
            let same_neighbors = self
                .neighbor_indexes(x, y, index)
                .iter()
                .filter(|neighbor| self.deck[**neighbor].type_id == type_id)
                .count();
            if same_neighbors >= 2 {
                triple_adjacent = true;
                break;
            }
        }
        let destroyed_minerlikes = entries
            .iter()
            .filter(|(index, _, _)| {
                destroyed[*index] && self.has_group(self.deck[*index].type_id as usize, "minerlikes")
            })
            .count() as i32;
        let pepper_items: i32 = self
            .items_held
            .iter()
            .filter(|id| {
                self.data.items[**id]
                    .groups
                    .iter()
                    .any(|group| group == "pepper")
            })
            .count() as i32
            * 2;
        for item_id in self.items_held.clone() {
            let item = &self.data.items[item_id];
            let name = item.name.as_str();
            let v0 = item.values.first().copied().unwrap_or(0.0) as i32;
            let v1 = item.values.get(1).copied().unwrap_or(0.0) as i32;
            let is_pepper = item.groups.iter().any(|group| group == "pepper");
            let gain = match name {
                "black_pepper" => v0 * destroyed_count,
                "brown_pepper" => v0 * added_count,
                "blue_pepper" if empty_count >= v1 || survivor_empty_count >= v1 => v0,
                "yellow_pepper" if empty_count == 0 => v0,
                "green_pepper" if max_same >= v1 => v0,
                "cyan_pepper" if max_same < v1 || survivor_max_same < v1 => v0,
                "red_pepper" if all_different || survivor_all_different => v0,
                "purple_pepper" if triple_adjacent => v0,
                "mining_pick" => v0 * destroyed_minerlikes,
                "lemon" => v0 * empty_count,
                "chili_powder" => v0.max(pepper_items),
                "shattered_mirror" => {
                    let charge = self.item_saved.entry(item_id).or_insert(0);
                    *charge += 1;
                    if *charge >= v1.max(1) {
                        *charge = 0;
                        v0
                    } else {
                        0
                    }
                }
                _ => 0,
            };
            let gain = if is_pepper && self.fp_affects(17, name) {
                (gain - self.fp_value(17, 0) as i32).max(0)
            } else {
                gain
            };
            if gain != 0 {
                item_gains_sink.push((self.data.items[item_id].name.clone(), gain));
            }
            extra_income += gain;
        }
        for (essence_name, triggered) in [
            ("red_pepper_essence", all_different),
            ("green_pepper_essence", max_same >= 3),
            ("blue_pepper_essence", empty_count >= 3),
            ("yellow_pepper_essence", empty_count == 0),
            ("purple_pepper_essence", triple_adjacent),
            (
                "cyan_pepper_essence",
                (max_same < 3 || survivor_max_same < 3) && !survivors.is_empty(),
            ),
        ] {
            if !triggered || !self.has_essence(essence_name) {
                continue;
            }
            if let Some(essence_id) = self.data.essence_index.get(essence_name).copied() {
                extra_income += self.data.essences[essence_id]
                    .values
                    .first()
                    .copied()
                    .unwrap_or(0.0) as i32;
                self.try_destroy_essence(essence_id);
            }
        }
        if let Some(lemon_essence) = self
            .data
            .essence_index
            .get("lemon_essence")
            .copied()
            .filter(|id| self.essences_held.contains(id))
        {
            let def = &self.data.essences[lemon_essence];
            let threshold = def.values.first().copied().unwrap_or(4.0) as i32;
            let per_empty = def.values.get(1).copied().unwrap_or(4.0) as i32;
            extra_income += empty_count * per_empty;
            if empty_count >= threshold.max(1) {
                self.try_destroy_essence(lemon_essence);
            }
        }

        if milk_eaten && self.has_essence("pizza_the_cat_essence") {
            let factor = self
                .data
                .essence_index
                .get("pizza_the_cat_essence")
                .map(|id| {
                    self.data.essences[*id]
                        .values
                        .first()
                        .copied()
                        .unwrap_or(1.5)
                })
                .unwrap_or(1.5);
            for symbol in self.deck.iter_mut() {
                if self.data.symbols[symbol.type_id as usize].name == "cat" {
                    let extra =
                        (symbol.current_value(self.data) as f64 * (factor - 1.0)).round() as i32;
                    symbol.bonus += extra.max(1);
                }
            }
            self.essences_held
                .retain(|id| self.data.essences[*id].name != "pizza_the_cat_essence");
            self.essence_saved
                .retain(|id, _| self.essences_held.contains(id));
        }

        extra_income += self.tick_essences(&entries, destroyed_count);
        self.tick_trigger_essences(&entries);
        extra_income += self.tick_permanent_essences(&entries, &destroyed, destroyed_count);
        self.apply_wildcard_values(&entries, &destroyed, &mult, &mut spin_bonus);

        extra_income += self.pre_spin_income;
        self.pre_spin_income = 0;
        self.last_item_gains = item_gains_sink;
        self.last_extra_income = extra_income;
        let mut income = extra_income;
        let mut symbol_income: Vec<(usize, i32)> = Vec::new();
        // Pre-spawn value state per slot: (sum before multiplier, multiplier,
        // final value). Spawned symbols placed in empty slots can still buff
        // surviving ones, so their values are adjusted after placement.
        let mut slot_state: [Option<(i32, f64, i32)>; BOARD_SLOTS] = [None; BOARD_SLOTS];
        let tax_essence = self
            .data
            .essence_index
            .get("tax_evasion_essence")
            .copied();
        let mut tax_used = false;
        for (index, x, y) in entries.iter().copied() {
            // Godot counts every icon that is still displayed at the end of
            // the spin, including icons destroyed this spin (they are only
            // removed after `check_values`).  A cat eating milk still pays
            // the milk's own coin value.
            let type_id = self.deck[index].type_id as usize;
            let def = &self.data.symbols[type_id];
            let base = def.base_value;
            let permanent_bonus = self.permanent_bonus_for(def);
            let permanent_mult = self.permanent_mult_for(def);
            // Godot's `get_value("coin")` adds every `value_bonus` (dog,
            // bee, dice, omelette, fine prints) *before* the multipliers,
            // and skips all multipliers when the product is negative.
            let mut flat = spin_bonus[index];
            if self.has_fine_print(26) && def.rarity == "very_rare" {
                flat -= self.fp_value(26, 0) as i32;
            }
            if self.fp_affects(27, &def.name)
                && def.groups.iter().any(|group| group == "eachother")
            {
                flat -= self.fp_value(27, 0) as i32;
            }
            if self.fp_affects(33, &def.name)
                && def.groups.iter().any(|group| group == "suit")
            {
                flat -= self.fp_value(33, 0) as i32;
            }
            if def.name == "d3" && self.has_fine_print(29) {
                flat -= self.fp_value(29, 0) as i32;
            }
            if def.name == "d5" && self.has_fine_print(35) {
                flat -= self.fp_value(35, 0) as i32;
            }
            let mut multiplier = mult[index] * permanent_mult * self.spin_column_mult[x];
            if def.name == "dwarf" && self.has_fine_print(28) {
                multiplier *= 0.5;
            }
            if def.name == "monkey" && self.has_fine_print(32) {
                multiplier *= 0.5;
            }
            if def.groups.iter().any(|group| group == "scaler") && self.fp_affects(19, &def.name) {
                multiplier *= 0.5;
            }
            let sum = base + self.deck[index].bonus + permanent_bonus + flat;
            let product = sum as f64 * multiplier;
            let mut value = if product < 0.0 {
                sum
            } else {
                product.round() as i32
            };
            if value < 0 {
                let relief: i32 = self
                    .items_held
                    .iter()
                    .filter(|id| self.data.items[**id].name == "tax_evasion")
                    .map(|id| {
                        self.data.items[*id]
                            .values
                            .first()
                            .copied()
                            .unwrap_or(1.0) as i32
                    })
                    .sum();
                value += relief;
            }
            if value < 0 && !tax_used {
                if let Some(essence_id) =
                    tax_essence.filter(|id| self.essences_held.contains(id))
                {
                    value += self.data.essences[essence_id]
                        .values
                        .first()
                        .copied()
                        .unwrap_or(25.0) as i32;
                    tax_used = true;
                }
            }
            income += value;
            slot_state[y * BOARD_COLS + x] = Some((sum, multiplier, value));
            if value != 0 {
                symbol_income.push((y * BOARD_COLS + x, value));
            }
            if value > 0 {
                self.deck[index].times_coins_given += 1;
            }
        }
        if tax_used {
            if let Some(essence_id) = tax_essence {
                self.try_destroy_essence(essence_id);
            }
        }

        let mut removed: Vec<usize> = (0..deck_len).filter(|index| destroyed[*index]).collect();
        self.board.remove_deck_indices(&removed);
        removed.sort_unstable_by(|a, b| b.cmp(a));
        for index in removed {
            self.deck.remove(index);
        }
        let item_spawns = self.tick_items();
        let item_spawn_count = item_spawns.len() as i32;
        spawned.extend(item_spawns);
        let mut spawned_indices: Vec<usize> = Vec::new();
        for type_id in spawned {
            let value = self.data.symbols[type_id].base_value;
            self.deck.push(Symbol::new(type_id, value));
            let index = self.deck.len() - 1;
            // Godot's `add_tile` places each new symbol into a visible
            // empty slot right away; the end-of-spin board already shows it.
            self.board.add_symbol(index, &mut self.rng.reel());
            spawned_indices.push(index);
        }
        // Godot re-checks icons added this spin before `check_values`:
        // they can still be eaten, buffed by a neighbour, or destroyed.
        let (post_removed, extra_added) =
            self.apply_spawned_effects(&spawned_indices, &slot_state, &mut income);
        if extra_added + item_spawn_count > 0 {
            // Brown pepper counts every symbol added this spin, including
            // gems created when a newly spawned ore is destroyed and symbols
            // added by items at spin end.
            let delta = extra_added + item_spawn_count;
            for item_id in self.items_held.clone() {
                let def = &self.data.items[item_id];
                if def.name == "brown_pepper" {
                    income += def.values.first().copied().unwrap_or(1.0) as i32 * delta;
                }
            }
        }
        if !post_removed.is_empty() {
            let mut unique = post_removed;
            unique.sort_unstable();
            unique.dedup();
            self.board.remove_deck_indices(&unique);
            unique.sort_unstable_by(|a, b| b.cmp(a));
            for index in unique {
                self.deck.remove(index);
            }
        }
        // Income-dependent item/essence payouts happen at `check_values`,
        // after spawned symbols have been added and counted.
        self.apply_item_income_conditions(&mut income);
        self.spin_all_adjacent = false;
        self.spin_corner_adjacent = false;
        self.last_symbol_income = symbol_income;
        self.last_effect_rng = self.rng.effect().take_log();
        self.last_reel_shuffle = self.rng.reel_shuffle().take_log();
        self.last_effect_shuffle = self.rng.effect_shuffle().take_log();
        income
    }

    fn board_slot_of(&self, index: usize) -> Option<usize> {
        self.board
            .slots
            .iter()
            .position(|slot| *slot == Some(index))
    }

    /// Item-driven type changes that Godot applies to newly added icons too
    /// (currently Quigley the Wolf's dog -> wolf conversion).
    fn spawn_type_after_items(&mut self, index: usize) -> usize {
        let type_id = self.deck[index].type_id as usize;
        if self.has_item("quigley_the_wolf") && self.data.symbols[type_id].name == "dog" {
            if let Some(wolf) = self.data.symbol_id("wolf") {
                self.deck[index].type_id = wolf as u16;
                return wolf;
            }
        }
        type_id
    }

    /// Permanent (non-spin) value of a symbol, matching Godot's
    /// `non_prev_final_value` used by dwarf/anvil payouts.
    fn non_prev_value(&self, index: usize) -> i32 {
        let def = &self.data.symbols[self.deck[index].type_id as usize];
        let sum = def.base_value + self.deck[index].bonus + self.permanent_bonus_for(def);
        (sum as f64 * self.permanent_mult_for(def)).round() as i32
    }

    fn clear_board_slots(&mut self, removed: &HashSet<usize>) {
        if removed.is_empty() {
            return;
        }
        for reel in 0..BOARD_COLS {
            for entry in self.board.reels[reel].iter_mut() {
                if entry.map(|index| removed.contains(&index)).unwrap_or(false) {
                    *entry = None;
                }
            }
        }
        self.board.rebuild_positions();
        self.board.sync_slots();
    }

    /// Item bonuses that apply to symbols which appeared mid-spin.
    fn apply_spawn_item_bonuses(&self, index: usize, states: &mut HashMap<usize, SpawnedValue>) {
        let type_id = self.deck[index].type_id as usize;
        let def = &self.data.symbols[type_id];
        let Some(state) = states.get_mut(&index) else {
            return;
        };
        let group = |tag: &str| def.groups.iter().any(|item| item == tag);
        for item_id in &self.items_held {
            let item = &self.data.items[*item_id];
            let name = item.name.as_str();
            let v0 = item.values.first().copied().unwrap_or(0.0) as i32;
            let v1 = item.values.get(1).copied().unwrap_or(0.0) as i32;
            let v2 = item.values.get(2).copied().unwrap_or(0.0) as i32;
            let flat = match name {
                "fruit_basket" if group("fruit") => Some(v0),
                "cleaning_rag" if group("gem") => Some(v0),
                "birdhouse" if group("bird") => Some(v0),
                "happy_hour" if group("booze") => Some(v0),
                "kyle_the_kernite" if group("kyle") => Some(v0),
                "anthropology_degree" if group("human") => Some(v0),
                "ritual_candle" if group("fossillikes") => Some(v0),
                "holy_water" if group("hex") => Some(v0),
                "chicken_coop" if group("chickenstuff") => Some(v2),
                "nori_the_rabbit" if def.name == "rabbit" || def.name == "rabbit_fluff" => Some(v0),
                "pizza_the_cat" if def.name == "cat" => Some(v0),
                "quigley_the_wolf" if def.name == "wolf" => Some(v0),
                "ricky_the_banana" if def.name == "banana" || def.name == "banana_peel" => Some(v0),
                "rain_cloud" if def.name == "rain" => Some(v0),
                "watering_can" if def.name == "seed" => Some(v1),
                _ => None,
            };
            if let Some(amount) = flat {
                state.sum += amount;
            }
            let mult = match name {
                "wanted_poster" if def.name == "thief" => Some(item.values.first().copied().unwrap_or(1.0)),
                "jackolantern" if group("halloween") => Some(item.values.first().copied().unwrap_or(1.0)),
                "maxwell_the_bear" if def.name == "bear" => Some(item.values.first().copied().unwrap_or(1.0)),
                "dwarven_anvil" if def.name == "dwarf" => Some(item.values.first().copied().unwrap_or(1.0)),
                "oswald_the_monkey" if def.name == "monkey" => Some(item.values.first().copied().unwrap_or(1.0)),
                "black_cat" if def.name == "cat" => Some(item.values.get(2).copied().unwrap_or(1.0)),
                "lucky_carrot" | "golden_carrot" if def.name == "rabbit" => {
                    Some(item.values.get(1).copied().unwrap_or(1.0))
                }
                "triple_coins" if def.name == "coin" => Some(item.values.first().copied().unwrap_or(1.0)),
                _ => None,
            };
            if let Some(factor) = mult {
                if factor != 1.0 {
                    state.mult *= factor;
                }
            }
            if name == "rusty_gear" {
                let copies = self
                    .deck
                    .iter()
                    .filter(|symbol| symbol.type_id == self.deck[index].type_id)
                    .count();
                if copies >= 2 {
                    let factor = if v1 > 1 { v1 as f64 } else { 2.0 };
                    state.mult *= factor;
                }
            }
        }
    }

    fn add_spawn_flat(
        states: &mut HashMap<usize, SpawnedValue>,
        survivor: &mut HashMap<usize, (usize, i32, f64, i32)>,
        survivor_destroyed: &HashSet<usize>,
        index: usize,
        is_spawned: bool,
        amount: i32,
        income: &mut i32,
    ) {
        if amount == 0 {
            return;
        }
        if is_spawned {
            if let Some(state) = states.get_mut(&index) {
                if !state.destroyed {
                    state.sum += amount;
                }
            }
        } else if !survivor_destroyed.contains(&index) {
            if let Some((_, sum, mult, value)) = survivor.get_mut(&index) {
                *sum += amount;
                let new_value = spawned_value(*sum, *mult);
                *income += new_value - *value;
                *value = new_value;
            }
        }
    }

    fn multiply_spawn_target(
        states: &mut HashMap<usize, SpawnedValue>,
        survivor: &mut HashMap<usize, (usize, i32, f64, i32)>,
        survivor_destroyed: &HashSet<usize>,
        index: usize,
        is_spawned: bool,
        factor: f64,
        income: &mut i32,
    ) {
        if factor == 1.0 {
            return;
        }
        if is_spawned {
            if let Some(state) = states.get_mut(&index) {
                if !state.destroyed {
                    state.mult *= factor;
                }
            }
        } else if !survivor_destroyed.contains(&index) {
            if let Some((_, sum, mult, value)) = survivor.get_mut(&index) {
                *mult *= factor;
                let new_value = spawned_value(*sum, *mult);
                *income += new_value - *value;
                *value = new_value;
            }
        }
    }

    /// Applies one adjacency effect from `source` onto `target`.
    fn apply_spawn_edge(
        &mut self,
        source: usize,
        target: usize,
        states: &mut HashMap<usize, SpawnedValue>,
        survivor: &mut HashMap<usize, (usize, i32, f64, i32)>,
        survivor_destroyed: &mut HashSet<usize>,
        dog_applied: &mut HashSet<usize>,
        omelette_applied: &mut HashSet<usize>,
        income: &mut i32,
    ) {
        let target_is_spawned = states.contains_key(&target);
        if target_is_spawned {
            if states.get(&target).map(|state| state.destroyed).unwrap_or(true) {
                return;
            }
        } else if survivor_destroyed.contains(&target) || !survivor.contains_key(&target) {
            return;
        }
        let source_is_spawned = states.contains_key(&source);
        if source_is_spawned && states.get(&source).map(|state| state.destroyed).unwrap_or(true) {
            return;
        }
        if !source_is_spawned && (survivor_destroyed.contains(&source) || !survivor.contains_key(&source)) {
            return;
        }

        let source_type = self.deck[source].type_id as usize;
        let source_name = self.data.symbols[source_type].name.clone();
        let source_values = self.data.symbols[source_type].values.clone();
        let source_groups = self.data.symbols[source_type].groups.clone();
        let target_type = self.deck[target].type_id as usize;
        let target_name = self.data.symbols[target_type].name.clone();
        let target_groups = self.data.symbols[target_type].groups.clone();
        let target_values = self.data.symbols[target_type].values.clone();
        let target_has = |group: &str| target_groups.iter().any(|item| item == group);
        let source_has = |group: &str| source_groups.iter().any(|item| item == group);
        let v0 = source_values.first().copied().unwrap_or(0.0);

        let mut factor: Option<f64> = None;
        let mut destroy: Option<(f64, i32, bool)> = None; // (victim factor, flat gain, self destroy)
        match source_name.as_str() {
            "bee" if target_has("beelikes") => factor = Some(v0),
            "light_bulb" if target_has("gem") => factor = Some(v0),
            "beastmaster" if target_has("animal") => factor = Some(v0),
            "witch" if target_has("witchlikes") => factor = Some(v0),
            "farmer" if target_has("farmerlikes") => factor = Some(v0),
            "dame" if target_has("gem") => factor = Some(v0),
            "king_midas" if target_name == "coin" => factor = Some(v0),
            "chef" if target_has("food") => factor = Some(v0),
            "sun" if target_name == "flower" => factor = Some(v0),
            "moon" if target_has("night") => factor = Some(v0),
            "cat" if target_name == "milk" => destroy = Some((0.0, v0 as i32, false)),
            "mouse" if target_name == "cheese" => destroy = Some((0.0, v0 as i32, false)),
            "toddler" if target_has("toddlerlikes") => destroy = Some((0.0, v0 as i32, false)),
            "bear" if target_name == "honey" => destroy = Some((0.0, v0 as i32, false)),
            "miner" if target_has("minerlikes") => destroy = Some((0.0, v0 as i32, false)),
            "goldfish" if target_name == "bubble" => destroy = Some((0.0, v0 as i32, false)),
            "hooligan" if target_has("spiritbox") => destroy = Some((0.0, v0 as i32, false)),
            "monkey" if target_has("monkeylikes") => destroy = Some((v0, 0, false)),
            "dwarf" if target_has("dwarflikes") => destroy = Some((v0, 0, false)),
            "dame" if target_name == "martini" => {
                destroy = Some((0.0, source_values.get(1).copied().unwrap_or(0.0) as i32, false))
            }
            "general_zaroff" if target_has("human") => destroy = Some((0.0, v0 as i32, false)),
            "bounty_hunter" if target_name == "thief" => destroy = Some((0.0, v0 as i32, false)),
            "key" if target_has("chest") => destroy = Some((0.0, 0, true)),
            "banana_peel" if target_name == "thief" => destroy = Some((0.0, 0, true)),
            _ => {}
        }

        if source_name == "light_bulb" && target_has("gem") {
            let threshold = source_values.get(1).copied().unwrap_or(5.0) as i32;
            let saved = if source_is_spawned {
                if let Some(state) = states.get_mut(&source) {
                    state.saved += 1;
                    state.saved
                } else {
                    0
                }
            } else {
                self.deck[source].saved += 1;
                self.deck[source].saved
            };
            if saved >= threshold {
                if source_is_spawned {
                    if let Some(state) = states.get_mut(&source) {
                        state.destroyed = true;
                    }
                } else {
                    survivor_destroyed.insert(source);
                }
            }
        }

        if let Some(factor) = factor {
            Self::multiply_spawn_target(
                states,
                survivor,
                survivor_destroyed,
                target,
                target_is_spawned,
                factor,
                income,
            );
        }

        // Reverse bonuses: doglikes/beelikes/omelettestuff give their
        // adjacent dog/bee/omelette a flat bonus.
        if source_has("doglikes") && target_name == "dog" && dog_applied.insert(target) {
            let amount = self
                .data
                .symbol_id("dog")
                .and_then(|id| self.data.symbols[id].values.first().copied())
                .unwrap_or(2.0) as i32;
            Self::add_spawn_flat(states, survivor, survivor_destroyed, target, target_is_spawned, amount, income);
        }
        if source_has("omelettestuff") && target_name == "omelette" && omelette_applied.insert(target) {
            let amount = self
                .data
                .symbol_id("omelette")
                .and_then(|id| self.data.symbols[id].values.first().copied())
                .unwrap_or(2.0) as i32;
            Self::add_spawn_flat(states, survivor, survivor_destroyed, target, target_is_spawned, amount, income);
        }
        if source_has("beelikes") && target_name == "bee" {
            let amount = self
                .data
                .symbol_id("bee")
                .and_then(|id| self.data.symbols[id].values.get(1).copied())
                .unwrap_or(1.0) as i32;
            Self::add_spawn_flat(states, survivor, survivor_destroyed, target, target_is_spawned, amount, income);
        }

        if let Some((victim_factor, flat_gain, self_destroy)) = destroy {
            let newly = if target_is_spawned {
                if let Some(state) = states.get_mut(&target) {
                    if !state.destroyed {
                        state.destroyed = true;
                        true
                    } else {
                        false
                    }
                } else {
                    false
                }
            } else if survivor_destroyed.insert(target) {
                true
            } else {
                false
            };
            if newly {
                let gain = if victim_factor != 0.0 {
                    (victim_factor * self.non_prev_value(target) as f64).round() as i32
                } else {
                    flat_gain
                };
                Self::add_spawn_flat(states, survivor, survivor_destroyed, source, source_is_spawned, gain, income);
                if !target_is_spawned {
                    // Chests and presents pay their on-destroy bonus even
                    // when the last hit lands after the main pass.
                    match target_name.as_str() {
                        "present" => {
                            *income += target_values.get(1).copied().unwrap_or(10.0) as i32;
                        }
                        "lockbox" | "safe" | "treasure_chest" | "mega_chest" => {
                            *income += target_values.first().copied().unwrap_or(15.0) as i32;
                        }
                        _ => {}
                    }
                }
            }
            if self_destroy {
                if source_is_spawned {
                    if let Some(state) = states.get_mut(&source) {
                        state.destroyed = true;
                    }
                } else {
                    survivor_destroyed.insert(source);
                }
            }
        }
    }

    /// Runs the post-spawn effect check for symbols added during the spin.
    /// Returns the deck indices that must be removed afterwards.
    fn apply_spawned_effects(
        &mut self,
        first_wave: &[usize],
        slot_state: &[Option<(i32, f64, i32)>; BOARD_SLOTS],
        income: &mut i32,
    ) -> (Vec<usize>, i32) {
        if first_wave.is_empty() {
            return (Vec::new(), 0);
        }
        let mut states: HashMap<usize, SpawnedValue> = HashMap::new();
        let mut survivor: HashMap<usize, (usize, i32, f64, i32)> = HashMap::new();
        let mut survivor_destroyed: HashSet<usize> = HashSet::new();
        let mut dog_applied: HashSet<usize> = HashSet::new();
        let mut omelette_applied: HashSet<usize> = HashSet::new();
        let mut item_done: HashSet<usize> = HashSet::new();
        let mut death_done: HashSet<usize> = HashSet::new();
        let mut edges: HashSet<(usize, usize)> = HashSet::new();
        let mut all_spawned: Vec<usize> = Vec::new();
        let mut pending: Vec<usize> = first_wave.to_vec();
        let mut extra_added = 0i32;

        for &index in first_wave {
            let type_id = self.spawn_type_after_items(index);
            let def = &self.data.symbols[type_id];
            let sum = def.base_value + self.deck[index].bonus + self.permanent_bonus_for(def);
            let x = self.board_slot_of(index).map(|slot| slot % BOARD_COLS).unwrap_or(0);
            let mult = self.permanent_mult_for(def) * self.spin_column_mult[x];
            states.insert(
                index,
                SpawnedValue {
                    sum,
                    mult,
                    saved: 0,
                    destroyed: false,
                    visible: self.board_slot_of(index).is_some(),
                },
            );
            all_spawned.push(index);
        }
        for slot in 0..BOARD_SLOTS {
            let Some((sum, mult, value)) = slot_state[slot] else {
                continue;
            };
            let Some(index) = self.board.slots[slot] else {
                continue;
            };
            if states.contains_key(&index) {
                continue;
            }
            survivor.insert(index, (slot, sum, mult, value));
        }
        // Dogs/omelettes that already received their one-time bonus in the
        // main pass should not receive it again from a new doglike.
        for &index in survivor.keys() {
            let name = self.data.symbols[self.deck[index].type_id as usize]
                .name
                .clone();
            if name != "dog" && name != "omelette" {
                continue;
            }
            let slot = survivor[&index].0;
            let (x, y) = (slot % BOARD_COLS, slot / BOARD_COLS);
            let wanted = if name == "dog" { "doglikes" } else { "omelettestuff" };
            let has_source = self.neighbor_indexes(x, y, index).iter().any(|neighbor| {
                survivor.contains_key(neighbor)
                    && self.has_group(self.deck[*neighbor].type_id as usize, wanted)
            });
            if has_source {
                if name == "dog" {
                    dog_applied.insert(index);
                } else {
                    omelette_applied.insert(index);
                }
            }
        }

        let mut capsule_done: HashSet<usize> = HashSet::new();
        for _ in 0..3 {
            for &index in &pending {
                if item_done.insert(index) {
                    self.apply_spawn_item_bonuses(index, &mut states);
                }
                if !all_spawned.contains(&index) {
                    all_spawned.push(index);
                }
            }

            // Godot re-registers effects for every symbol added during the
            // spin, so a capsule spawned this spin also opens and destroys
            // itself before `check_values`.
            for &index in &all_spawned {
                if !capsule_done.insert(index) {
                    continue;
                }
                let type_id = self.spawn_type_after_items(index);
                let name = self.data.symbols[type_id].name.clone();
                let values = self.data.symbols[type_id].values.clone();
                let count = (values.first().copied().unwrap_or(1.0) as i32
                    * self.capsule_multiplier())
                .max(1);
                match name.as_str() {
                    "essence_capsule" => {
                        self.essence_tokens += count;
                        if let Some(state) = states.get_mut(&index) {
                            state.destroyed = true;
                        }
                    }
                    "item_capsule" => {
                        for _ in 0..count {
                            let mut pool = self.item_pools[0].clone();
                            pool.retain(|item| !self.items_held.contains(item));
                            if pool.is_empty() {
                                break;
                            }
                            let pick = pool[self.rng.range(0, pool.len())];
                            self.grant_item(pick);
                        }
                        if let Some(state) = states.get_mut(&index) {
                            state.destroyed = true;
                        }
                    }
                    "reroll_capsule" => {
                        self.reroll_tokens += count;
                        if let Some(state) = states.get_mut(&index) {
                            state.destroyed = true;
                        }
                    }
                    "removal_capsule" => {
                        self.removal_tokens += count;
                        if let Some(state) = states.get_mut(&index) {
                            state.destroyed = true;
                        }
                    }
                    _ => {}
                }
            }

            let targets: Vec<usize> = all_spawned.clone();
            for &target in &targets {
                let Some(slot) = self.board_slot_of(target) else {
                    continue;
                };
                let (x, y) = (slot % BOARD_COLS, slot / BOARD_COLS);
                for source in self.neighbor_indexes(x, y, target) {
                    if edges.insert((source, target)) {
                        self.apply_spawn_edge(
                            source,
                            target,
                            &mut states,
                            &mut survivor,
                            &mut survivor_destroyed,
                            &mut dog_applied,
                            &mut omelette_applied,
                            income,
                        );
                    }
                }
            }
            let sources: Vec<usize> = all_spawned.clone();
            for &source in &sources {
                let Some(slot) = self.board_slot_of(source) else {
                    continue;
                };
                let (x, y) = (slot % BOARD_COLS, slot / BOARD_COLS);
                for target in self.neighbor_indexes(x, y, source) {
                    if edges.insert((source, target)) {
                        self.apply_spawn_edge(
                            source,
                            target,
                            &mut states,
                            &mut survivor,
                            &mut survivor_destroyed,
                            &mut dog_applied,
                            &mut omelette_applied,
                            income,
                        );
                    }
                }
            }

            // Destroyed spawned symbols run their on-destroy effects.
            let newly_dead: Vec<usize> = states
                .iter()
                .filter(|(index, state)| state.destroyed && !death_done.contains(index))
                .map(|(index, _)| *index)
                .collect();
            let mut next: Vec<usize> = Vec::new();
            let mut queued: Vec<usize> = Vec::new();
            for &index in &newly_dead {
                death_done.insert(index);
                let type_id = self.deck[index].type_id as usize;
                let name = self.data.symbols[type_id].name.clone();
                let values = self.data.symbols[type_id].values.clone();
                match name.as_str() {
                    "ore" => {
                        let floor = if self.has_essence("x_ray_machine_essence") {
                            Rarity::VeryRare
                        } else if self.has_item("x_ray_machine") {
                            Rarity::Rare
                        } else {
                            Rarity::Common
                        };
                        if let Some(gem) = self.pick_group_min("gem", floor) {
                            queued.push(gem);
                        }
                    }
                    "big_ore" => {
                        let floor = if self.has_essence("x_ray_machine_essence") {
                            Rarity::VeryRare
                        } else if self.has_item("x_ray_machine") {
                            Rarity::Rare
                        } else {
                            Rarity::Common
                        };
                        for _ in 0..values.first().copied().unwrap_or(2.0) as i32 {
                            if let Some(gem) = self.pick_group_min("gem", floor) {
                                queued.push(gem);
                            }
                        }
                    }
                    "present" => {
                        *income += values.get(1).copied().unwrap_or(10.0) as i32;
                    }
                    "lockbox" | "safe" | "treasure_chest" | "mega_chest" => {
                        *income += values.first().copied().unwrap_or(15.0) as i32;
                    }
                    "pinata" => {
                        if let Some(candy) = self.data.symbol_id("candy") {
                            for _ in 0..values.first().copied().unwrap_or(7.0) as i32 {
                                queued.push(candy);
                            }
                        }
                    }
                    "coconut" => {
                        if let Some(half) = self.data.symbol_id("coconut_half") {
                            for _ in 0..values.first().copied().unwrap_or(2.0) as i32 {
                                queued.push(half);
                            }
                        }
                    }
                    "urn" => {
                        if let Some(spirit) = self.data.symbol_id("spirit") {
                            queued.push(spirit);
                        }
                    }
                    "tomb" | "big_urn" => {
                        if let Some(spirit) = self.data.symbol_id("spirit") {
                            let (index, values_at) = if name == "tomb" { (1, 4.0) } else { (0, 2.0) };
                            let count = values.get(index).copied().unwrap_or(values_at) as i32;
                            for _ in 0..count.max(0) {
                                queued.push(spirit);
                            }
                        }
                    }
                    _ => {}
                }
            }

            let dead: HashSet<usize> = states
                .iter()
                .filter(|(_, state)| state.destroyed)
                .map(|(index, _)| *index)
                .chain(survivor_destroyed.iter().copied())
                .collect();
            self.clear_board_slots(&dead);
            for type_id in queued {
                let value = self.data.symbols[type_id].base_value;
                self.deck.push(Symbol::new(type_id, value));
                let index = self.deck.len() - 1;
                self.board.add_symbol(index, &mut self.rng.reel());
                let type_id = self.spawn_type_after_items(index);
                let def = &self.data.symbols[type_id];
                let sum = def.base_value + self.permanent_bonus_for(def);
                let x = self.board_slot_of(index).map(|slot| slot % BOARD_COLS).unwrap_or(0);
                let mult = self.permanent_mult_for(def) * self.spin_column_mult[x];
                states.insert(
                    index,
                    SpawnedValue {
                        sum,
                        mult,
                        saved: 0,
                        destroyed: false,
                        visible: self.board_slot_of(index).is_some(),
                    },
                );
                all_spawned.push(index);
                next.push(index);
                extra_added += 1;
            }
            if next.is_empty() {
                break;
            }
            pending = next;
        }

        // A void symbol added during the spin also registers its empty-cell
        // effect in the next effect pass, so it pays for every adjacent empty
        // cell that is still empty at check_values time.
        let spawned_voids: Vec<(usize, usize, i32)> = states
            .iter()
            .filter_map(|(&index, state)| {
                if !state.visible || state.destroyed {
                    return None;
                }
                let def = &self.data.symbols[self.spawn_type_after_items(index)];
                if !matches!(def.name.as_str(), "void_fruit" | "void_creature" | "void_stone") {
                    return None;
                }
                let slot = self.board_slot_of(index)?;
                Some((
                    slot % BOARD_COLS,
                    slot / BOARD_COLS,
                    def.values.first().copied().unwrap_or(1.0) as i32,
                ))
            })
            .collect();
        for &(x, y, gain) in &spawned_voids {
            for dy in -1i32..=1 {
                for dx in -1i32..=1 {
                    if dx == 0 && dy == 0 {
                        continue;
                    }
                    let nx = x as i32 + dx;
                    let ny = y as i32 + dy;
                    if nx < 0 || ny < 0 || nx >= BOARD_COLS as i32 || ny >= BOARD_ROWS as i32 {
                        continue;
                    }
                    if self.board.slots[ny as usize * BOARD_COLS + nx as usize].is_none() {
                        *income += gain;
                    }
                }
            }
        }

        for (index, state) in &states {
            if state.visible {
                let value = spawned_value(state.sum, state.mult);
                *income += value;
                // Godot increments `times_coins_given` for every displayed
                // icon in `check_values`, including icons added this spin.
                if value > 0 {
                    self.deck[*index].times_coins_given += 1;
                }
            }
        }
        // A survivor buffed from 0 to a positive value by a new neighbour also
        // gets its per-spin counter bumped once.
        for (index, (_, sum, mult, value)) in &survivor {
            let final_value = spawned_value(*sum, *mult);
            if final_value > 0 && *value <= 0 {
                self.deck[*index].times_coins_given += 1;
            }
        }
        let mut removed: Vec<usize> = states
            .iter()
            .filter(|(_, state)| state.destroyed)
            .map(|(index, _)| *index)
            .collect();
        removed.extend(survivor_destroyed.iter().copied());
        (removed, extra_added)
    }

    /// Item and essence payouts whose value depends on the final spin income.
    /// Godot evaluates these in `check_values`, after all effects and spawned
    /// symbols are resolved.
    fn apply_item_income_conditions(&mut self, income: &mut i32) {
        // Lucky seven pays once per group of `values[0]` held copies, during
        // check_values (not when the copy is picked up).
        if let Some(seven) = self.data.item_index.get("lucky_seven").copied() {
            let (needed, payout) = {
                let def = &self.data.items[seven];
                (
                    def.values.first().copied().unwrap_or(3.0).max(1.0) as usize,
                    def.values.get(1).copied().unwrap_or(77.0) as i32,
                )
            };
            loop {
                let count = self.items_held.iter().filter(|id| **id == seven).count();
                if count < needed {
                    break;
                }
                *income += payout;
                let mut remaining = needed;
                let mut kept = Vec::with_capacity(self.items_held.len());
                for held in self.items_held.iter().copied() {
                    if held == seven && remaining > 0 {
                        remaining -= 1;
                    } else {
                        kept.push(held);
                    }
                }
                self.items_held = kept;
            }
        }
        for item_id in self.items_held.clone() {
            let item = &self.data.items[item_id];
            let name = item.name.clone();
            let v0 = item.values.first().copied().unwrap_or(0.0) as i32;
            let v1 = item.values.get(1).copied().unwrap_or(0.0) as i32;
            let v2 = item.values.get(2).copied().unwrap_or(0.0) as i32;
            if v1 <= 0 {
                continue;
            }
            match name.as_str() {
                "bowling_ball_essence" | "horseshoe_essence" | "pool_ball_essence"
                | "four_leaf_clover_essence" => {
                    // These essences pay their value every spin and self
                    // destroy after `values[0]` spins.
                    *income += v1;
                    let saved = self.item_saved.entry(item_id).or_insert(0);
                    *saved += 1;
                    if *saved >= v0.max(1) {
                        if let Some(position) =
                            self.items_held.iter().position(|id| *id == item_id)
                        {
                            self.items_held.remove(position);
                            self.item_saved.remove(&item_id);
                        }
                    }
                }
                "ancient_lizard_blade" => {
                    let need = item.values.get(2).copied().unwrap_or(2.0) as i32;
                    let floor = item.values.get(3).copied().unwrap_or(0.0) as i32;
                    let mut counts: HashMap<u16, i32> = HashMap::new();
                    for symbol in &self.deck {
                        if self.data.symbols[symbol.type_id as usize].name != "empty" {
                            *counts.entry(symbol.type_id).or_default() += 1;
                        }
                    }
                    let non_uniques = counts
                        .values()
                        .filter(|count| **count >= need.max(1))
                        .count() as i32;
                    *income += (v0 - non_uniques * v1).max(floor);
                }
                "void_portal" => {
                    *income += (self.destroyed_history.len() as i32 / v1.max(1)) * v0;
                }
                "white_pepper" | "black_cat" => {
                    if *income % v1 == 0 {
                        *income += v0;
                    }
                }
                "piggy_bank" => {
                    *income = (*income - v0 - self.fp_value(16, 0) as i32).max(0);
                    *self.item_saved.entry(item_id).or_insert(0) += v0;
                }
                "swear_jar" => {
                    // The jar only swallows a coin when the spin earned little.
                    if v2 <= 0 || *income < v2 {
                        *income = (*income - v0 - self.fp_value(16, 0) as i32).max(0);
                        *self.item_saved.entry(item_id).or_insert(0) += v0;
                    }
                }
                _ => {}
            }
        }
        if let Some(lizard) = self
            .data
            .essence_index
            .get("ancient_lizard_blade_essence")
            .copied()
            .filter(|id| self.essences_held.contains(id))
        {
            let values = &self.data.essences[lizard].values;
            let v0 = values.first().copied().unwrap_or(40.0) as i32;
            let v1 = values.get(1).copied().unwrap_or(8.0) as i32;
            let need = values.get(2).copied().unwrap_or(2.0) as i32;
            let floor = values.get(3).copied().unwrap_or(0.0) as i32;
            let mut counts: HashMap<u16, i32> = HashMap::new();
            for symbol in &self.deck {
                if self.data.symbols[symbol.type_id as usize].name != "empty" {
                    *counts.entry(symbol.type_id).or_default() += 1;
                }
            }
            let non_uniques = counts
                .values()
                .filter(|count| **count >= need.max(1))
                .count() as i32;
            *income += (v0 - non_uniques * v1).max(floor);
        }
        if let Some(white_pepper) = self
            .data
            .essence_index
            .get("white_pepper_essence")
            .copied()
            .filter(|id| self.essences_held.contains(id))
        {
            let (bonus, step) = {
                let def = &self.data.essences[white_pepper];
                (
                    def.values.first().copied().unwrap_or(30.0) as i32,
                    def.values.get(1).copied().unwrap_or(3.0) as i32,
                )
            };
            if step > 0 && *income % step == 0 {
                *income += bonus;
                self.try_destroy_essence(white_pepper);
            }
        }
        if let Some(swear_jar) = self
            .data
            .essence_index
            .get("swear_jar_essence")
            .copied()
            .filter(|id| self.essences_held.contains(id))
        {
            let (limit, payout) = {
                let values = &self.data.essences[swear_jar].values;
                (
                    values.first().copied().unwrap_or(25.0) as i32,
                    values.get(1).copied().unwrap_or(50.0) as i32,
                )
            };
            if *income <= limit {
                *income += payout;
                self.try_destroy_essence(swear_jar);
            }
        }
    }

    fn pay_rent(&mut self) {
        self.pay_rent_with_charge(true);
    }

    fn pay_rent_waived(&mut self) {
        self.pay_rent_with_charge(false);
    }

    fn pay_rent_with_charge(&mut self, charge: bool) {
        let due = self.rent_due();
        if charge {
            self.coins -= due;
        }
        self.rents_paid += 1;
        if self.has_essence("chili_powder_essence") {
            let count = self
                .data
                .essence_index
                .get("chili_powder_essence")
                .map(|id| {
                    self.data.essences[*id]
                        .values
                        .get(1)
                        .copied()
                        .unwrap_or(1.0) as usize
                })
                .unwrap_or(1);
            if let Some(essence_id) = self
                .data
                .essence_index
                .get("chili_powder_essence")
                .copied()
            {
                self.try_destroy_essence(essence_id);
            }
            if !self.has_essence("chili_powder_essence") {
                for _ in 0..count {
                    let peppers: Vec<usize> = self
                        .data
                        .items
                        .iter()
                        .enumerate()
                        .filter(|(_, def)| def.groups.iter().any(|group| group == "pepper"))
                        .map(|(index, _)| index)
                        .collect();
                    if peppers.is_empty() {
                        break;
                    }
                    let pick = self.rng.range(0, peppers.len());
                    let item_id = peppers[pick];
                    self.grant_item(item_id);
                }
            }
        }
        if self.rents_paid >= 12 {
            if !self.boss_fight {
                self.boss_fight = true;
                self.boss_hp = 1500;
            }
            self.spins_left = 10;
        } else {
            self.spins_left += 5 + (self.rents_paid / 2) as i32;
        }
        // Essence tokens banked from earlier comrade help convert into one
        // essence choice each at this payment, before the symbol reward.
        let pending_essences = self.essence_tokens.max(0);
        self.essence_tokens = 0;
        // A20 comrade help: the third payment and every odd payment after it.
        if self.rents_paid >= 3 && self.rents_paid % 2 == 1 {
            self.reroll_tokens += 1;
            self.removal_tokens += 1;
            self.essence_tokens += 1;
        }
        if pending_essences > 0 {
            self.pending_essence_offers = pending_essences;
            self.queue_extra_symbol_offers();
            let offer = self.roll_essence_reward();
            self.phase = Phase::EssenceReward { offer };
        } else {
            self.queue_extra_symbol_offers();
            let offer = self.roll_symbol_reward();
            self.phase = Phase::SymbolReward { offer };
        }
    }

    fn queue_extra_symbol_offers(&mut self) {
        self.pending_extra_symbol_offers += self.essence_count("frozen_pizza_essence");
    }

    fn roll_essence_reward(&mut self) -> Vec<usize> {
        let mut domain = self.essence_pool.clone();
        domain.sort_by(|left, right| {
            self.data.essences[*left]
                .name
                .cmp(&self.data.essences[*right].name)
        });
        self.rng.begin_essence_pick(self.rents_paid, &domain);
        let filtered: Vec<usize> = domain
            .iter()
            .copied()
            .filter(|id| !self.essences_held.contains(id))
            .collect();
        let mut offer = Vec::with_capacity(3);
        for _ in 0..3 {
            if let Some(pick) = self.rng.pick_essence(&filtered) {
                offer.push(pick);
            }
        }
        offer.sort_by(|left, right| {
            self.data.essences[*left]
                .name
                .cmp(&self.data.essences[*right].name)
        });
        offer
    }

    fn grant_essence(&mut self, essence_id: usize) {
        let (name, values) = {
            let def = &self.data.essences[essence_id];
            (def.name.clone(), def.values.clone())
        };
        match name.as_str() {
            "bag_of_holding_essence" => {
                self.pending_bonus_items += values.first().copied().unwrap_or(2.0) as i32;
                self.essences_held.push(essence_id);
            }
            _ => self.essences_held.push(essence_id),
        }
    }

    fn add_symbols(&mut self, tag: &str, count: i32) {
        if let Some(type_id) = self.data.symbol_id(tag) {
            let value = self.data.symbols[type_id].base_value;
            for _ in 0..count.max(0) {
                self.deck.push(Symbol::new(type_id, value));
                self.note_symbol_added(type_id);
            }
        }
    }

    fn note_symbol_added(&mut self, type_id: usize) {
        for (name, group) in [
            ("lunchbox_essence", "food"),
            ("adoption_papers_essence", "animal"),
        ] {
            if !self.has_group(type_id, group) {
                continue;
            }
            let Some(essence_id) = self
                .data
                .essence_index
                .get(name)
                .copied()
                .filter(|id| self.essences_held.contains(id))
            else {
                continue;
            };
            let threshold = self.data.essences[essence_id]
                .values
                .first()
                .copied()
                .unwrap_or(7.0) as i32;
            let payout = self.data.essences[essence_id]
                .values
                .get(1)
                .copied()
                .unwrap_or(0.0) as i32;
            let count = self.essence_saved.entry(essence_id).or_insert(0);
            *count += 1;
            if *count >= threshold.max(1) {
                self.try_destroy_essence(essence_id);
                if self.has_essence(name) {
                    // Popsicle protection delayed the destruction; keep the
                    // trigger primed for the next added symbol.
                    self.essence_saved.insert(essence_id, threshold.max(1));
                } else {
                    self.essence_saved.remove(&essence_id);
                    self.coins += payout;
                }
            }
        }
    }

    fn advance_after_essence(&mut self) {
        self.pending_essence_offers -= 1;
        if self.pending_essence_offers > 0 {
            let offer = self.roll_essence_reward();
            self.phase = Phase::EssenceReward { offer };
        } else {
            self.next_offer_phase(FollowUp::SymbolReward);
        }
    }

    pub fn choose_essence(&mut self, option: usize) {
        let Phase::EssenceReward { offer } = &self.phase else {
            return;
        };
        if let Some(essence_id) = offer.get(option).copied() {
            self.grant_essence(essence_id);
        }
        self.advance_after_essence();
    }

    pub fn skip_essence(&mut self) {
        if matches!(self.phase, Phase::EssenceReward { .. }) {
            self.token_pepper_income("pink_pepper");
            self.spend_pepper_essence("pink_pepper_essence");
            self.advance_after_essence();
        }
    }

    fn rent_reward_rarities(&self) -> [Rarity; 3] {
        match self.rents_paid {
            1..=5 => [Rarity::Uncommon; 3],
            6..=8 => [Rarity::Rare, Rarity::Uncommon, Rarity::Uncommon],
            9 => [Rarity::Rare, Rarity::Rare, Rarity::Uncommon],
            _ => [Rarity::Rare; 3],
        }
    }

    fn spin_rarity_chances(&self) -> (f64, f64, f64) {
        match self.rents_paid {
            0 => (0.0, 0.0, 0.0),
            1 => (0.1, 0.0, 0.0),
            2 => (0.2, 0.01, 0.0),
            3 => (0.25, 0.01, 0.0),
            4 => (0.29, 0.015, 0.005),
            _ => (0.3, 0.015, 0.005),
        }
    }

    /// Pops queued `forced_rarities` entries exactly like
    /// `Pop-up.get_forced_rarities`: at most `count` entries, and the first
    /// entry decides whether the whole batch is "this rarity or better".
    fn take_symbol_forced_rarities(&mut self, count: usize) -> (Vec<Rarity>, bool) {
        let or_better = self
            .symbol_forced_queue
            .first()
            .map(|(_, or_better)| *or_better)
            .unwrap_or(true);
        let take = count.min(self.symbol_forced_queue.len());
        let entries: Vec<Rarity> = self
            .symbol_forced_queue
            .drain(..take)
            .map(|(rarity, _)| rarity)
            .collect();
        (entries, or_better)
    }

    /// Picks one symbol from `rarity`, falling back to lower rarities when the
    /// bucket is empty. Returns false when no bucket has anything left.
    fn pick_offer_symbol(
        &mut self,
        mut rarity: Rarity,
        pools: &mut [Vec<usize>; 4],
        offer: &mut Vec<usize>,
    ) -> bool {
        loop {
            if !pools[rarity as usize].is_empty() {
                break;
            }
            rarity = match rarity {
                Rarity::VeryRare => Rarity::Rare,
                Rarity::Rare => Rarity::Uncommon,
                Rarity::Uncommon => Rarity::Common,
                Rarity::Common => return false,
            };
        }
        let name = match rarity {
            Rarity::Common => "common",
            Rarity::Uncommon => "uncommon",
            Rarity::Rare => "rare",
            Rarity::VeryRare => "very_rare",
        };
        let pool = &mut pools[rarity as usize];
        let pick = self.rng.pick_symbol(name, pool.len());
        offer.push(pool.remove(pick));
        true
    }

    fn roll_spin_reward(&mut self) -> Vec<usize> {
        self.refresh_symbol_pools();
        let (mut uncommon, mut rare, mut very_rare) = self.spin_rarity_chances();
        let lucky = self.rarity_lucky_factor();
        uncommon *= lucky;
        rare *= lucky;
        very_rare *= lucky;
        let factor = (if self.has_fine_print(23) {
            1.0 / self.fp_value(23, 0).max(1.0)
        } else {
            1.0
        }) * self.spin_rarity_penalty;
        let uncommon = uncommon * factor;
        let rare = rare * factor;
        let very_rare = very_rare * factor;
        let count = 3usize
            .saturating_sub(self.fp_value(30, 0) as usize + self.shattered_mirror_penalty())
            .max(1);
        let (forced, or_better) = self.take_symbol_forced_rarities(count);
        let mut offer = Vec::with_capacity(count);
        let mut pools: [Vec<usize>; 4] = [
            self.rarity_pools[0].clone(),
            self.rarity_pools[1].clone(),
            self.rarity_pools[2].clone(),
            self.rarity_pools[3].clone(),
        ];
        for index in 0..count {
            let roll = self.rng.symbol_rarity_roll();
            let mut rarity = if roll < very_rare && !pools[3].is_empty() {
                Rarity::VeryRare
            } else if roll < very_rare + rare && !pools[2].is_empty() {
                Rarity::Rare
            } else if roll < very_rare + rare + uncommon && !pools[1].is_empty() {
                Rarity::Uncommon
            } else {
                Rarity::Common
            };
            if index < forced.len() {
                if or_better {
                    if (forced[index] as usize) > (rarity as usize) {
                        rarity = forced[index];
                    }
                } else {
                    rarity = forced[index];
                }
            }
            if !self.pick_offer_symbol(rarity, &mut pools, &mut offer) {
                break;
            }
        }
        offer.sort_by(|left, right| {
            self.data.symbols[*left]
                .name
                .cmp(&self.data.symbols[*right].name)
        });
        offer
    }

    pub fn choose_spin(&mut self, option: usize) {
        let Phase::SpinReward { offer } = &self.phase else {
            return;
        };
        if let Some(type_id) = offer.get(option).copied() {
            let value = self.data.symbols[type_id].base_value;
            self.deck.push(Symbol::new(type_id, value));
            let index = self.deck.len() - 1;
            self.board.add_symbol(index, &mut self.rng.reel());
            self.note_symbol_added(type_id);
        }
        self.finish_spin_choice();
    }

    pub fn skip_spin(&mut self) {
        if matches!(self.phase, Phase::SpinReward { .. }) {
            self.token_pepper_income("pink_pepper");
            self.spend_pepper_essence("pink_pepper_essence");
            self.finish_spin_choice();
        }
    }

    fn finish_spin_choice(&mut self) {
        self.force_take_symbol = false;
        self.next_offer_phase(FollowUp::Ready);
    }

    /// Routes the next queued bonus offer (item/essence acquisition can queue
    /// guaranteed symbol offers), then falls back to the normal reward flow.
    fn next_offer_phase(&mut self, fallback: FollowUp) {
        if let Some(rarity) = self.pending_bonus_symbols.pop() {
            let offer = self.roll_bonus_symbol_offer(rarity);
            self.phase = Phase::SpinReward { offer };
            return;
        }
        if self.pending_extra_symbol_offers > 0 {
            self.pending_extra_symbol_offers -= 1;
            let offer = self.roll_spin_reward();
            self.phase = Phase::SpinReward { offer };
            return;
        }
        if self.pending_bonus_items > 0 {
            self.pending_bonus_items -= 1;
            let offer = self.roll_item_reward();
            self.phase = Phase::ItemReward { offer };
            return;
        }
        self.phase = match fallback {
            FollowUp::Ready => Phase::Ready,
            FollowUp::ItemReward => Phase::ItemReward {
                offer: self.roll_item_reward(),
            },
            FollowUp::SymbolReward => Phase::SymbolReward {
                offer: self.roll_symbol_reward(),
            },
        };
    }

    fn roll_bonus_symbol_offer(&mut self, rarity: Option<Rarity>) -> Vec<usize> {
        let Some(rarity) = rarity else {
            return self.roll_spin_reward();
        };
        self.refresh_symbol_pools();
        let pool = &self.rarity_pools[rarity as usize];
        if pool.is_empty() {
            return self.roll_spin_reward();
        }
        let pick = pool[self.rng.range(0, pool.len())];
        vec![pick; 3]
    }

    /// 碎镜（每份）会让符号奖励少一个选项。真机的普通旋后奖励和交租奖励
    /// 都会扣，所以两个入口共用这个计数。
    fn shattered_mirror_penalty(&self) -> usize {
        self.items_held
            .iter()
            .filter(|item_id| self.data.items[**item_id].name == "shattered_mirror")
            .map(|item_id| {
                self.data.items[*item_id]
                    .values
                    .get(2)
                    .copied()
                    .unwrap_or(1.0) as usize
            })
            .sum()
    }

    fn roll_symbol_reward(&mut self) -> Vec<usize> {
        self.refresh_symbol_pools();
        let rarities = self.rent_reward_rarities();
        let mirror_penalty = self.shattered_mirror_penalty();
        let count = 3usize
            .saturating_sub(self.fp_value(30, 0) as usize + mirror_penalty)
            .max(1);
        let extra = self.pending_extra_symbol_choices;
        self.pending_extra_symbol_choices = 0;
        let (mut forced, or_better) = self.take_symbol_forced_rarities(count);
        for index in forced.len()..count {
            forced.push(rarities[index]);
        }
        if !forced.is_empty() {
            self.rng.forced_rarity().shuffle(&mut forced);
        }
        let (mut uncommon, mut rare, mut very_rare) = self.spin_rarity_chances();
        let lucky = self.rarity_lucky_factor();
        uncommon *= lucky;
        rare *= lucky;
        very_rare *= lucky;
        let factor = (if self.has_fine_print(23) {
            1.0 / self.fp_value(23, 0).max(1.0)
        } else {
            1.0
        }) * self.spin_rarity_penalty;
        let uncommon = uncommon * factor;
        let rare = rare * factor;
        let very_rare = very_rare * factor;
        let mut pools: [Vec<usize>; 4] = [
            self.rarity_pools[0].clone(),
            self.rarity_pools[1].clone(),
            self.rarity_pools[2].clone(),
            self.rarity_pools[3].clone(),
        ];
        let mut offer = Vec::with_capacity(count + extra);
        for index in 0..count + extra {
            let roll = self.rng.symbol_rarity_roll();
            let mut rarity = if roll < very_rare && !pools[3].is_empty() {
                Rarity::VeryRare
            } else if roll < very_rare + rare && !pools[2].is_empty() {
                Rarity::Rare
            } else if roll < very_rare + rare + uncommon && !pools[1].is_empty() {
                Rarity::Uncommon
            } else {
                Rarity::Common
            };
            if index < forced.len() {
                if or_better {
                    if (forced[index] as usize) > (rarity as usize) {
                        rarity = forced[index];
                    }
                } else {
                    rarity = forced[index];
                }
            }
            if !self.pick_offer_symbol(rarity, &mut pools, &mut offer) {
                break;
            }
        }
        offer.sort_by(|left, right| {
            self.data.symbols[*left]
                .name
                .cmp(&self.data.symbols[*right].name)
        });
        offer
    }

    fn roll_item_reward(&mut self) -> Vec<usize> {
        let (mut uncommon, mut rare, mut very_rare) = match self.rents_paid {
            0 | 1 => (0.0, 0.0, 0.0),
            2 => (0.1, 0.0, 0.0),
            3 => (0.2, 0.025, 0.0),
            4 => (0.25, 0.025, 0.0),
            5 => (0.3, 0.0375, 0.0125),
            _ => (0.375, 0.05, 0.015),
        };
        if self.has_fine_print(23) {
            let factor = 1.0 / self.fp_value(23, 0).max(1.0);
            uncommon *= factor;
            rare *= factor;
            very_rare *= factor;
        }
        let extra_offers = self
            .items_held
            .iter()
            .filter(|id| self.data.items[**id].name == "bag_of_holding")
            .count();
        let mut domains: BTreeMap<String, Vec<usize>> = BTreeMap::new();
        for (index, rarity) in [
            Rarity::Common,
            Rarity::Uncommon,
            Rarity::Rare,
            Rarity::VeryRare,
        ]
        .iter()
        .enumerate()
        {
            let mut domain = self.item_pools[index].clone();
            domain.sort_by(|left, right| {
                self.data.items[*left]
                    .name
                    .cmp(&self.data.items[*right].name)
            });
            domains.insert(
                match rarity {
                    Rarity::Common => "common",
                    Rarity::Uncommon => "uncommon",
                    Rarity::Rare => "rare",
                    Rarity::VeryRare => "very_rare",
                }
                .to_string(),
                domain,
            );
        }
        self.rng.begin_item_pick(self.rents_paid, &domains);
        let mut offer = Vec::with_capacity(3 + extra_offers);
        for _ in 0..3 + extra_offers {
            let roll = self.rng.item_rarity_roll(self.rents_paid);
            let rarity = if roll < very_rare {
                Rarity::VeryRare
            } else if roll < very_rare + rare {
                Rarity::Rare
            } else if roll < very_rare + rare + uncommon {
                Rarity::Uncommon
            } else {
                Rarity::Common
            };
            let filtered: Vec<usize> = self.item_pools[rarity as usize]
                .iter()
                .copied()
                .filter(|item| !self.items_held.contains(item))
                .collect();
            let name = match rarity {
                Rarity::Common => "common",
                Rarity::Uncommon => "uncommon",
                Rarity::Rare => "rare",
                Rarity::VeryRare => "very_rare",
            };
            if let Some(pick) = self.rng.pick_item(name, &filtered) {
                offer.push(pick);
            }
        }
        if let Some((min, wanted)) = self.item_offer_floor.take() {
            let min_index = min as usize;
            let qualifies = |data: &GameData, id: usize| {
                Rarity::parse(&data.items[id].rarity)
                    .map(|rarity| rarity as usize >= min_index)
                    .unwrap_or(false)
            };
            let mut forced = offer
                .iter()
                .filter(|id| qualifies(self.data, **id))
                .count();
            if forced < wanted {
                let mut candidates: Vec<usize> = (min_index..4)
                    .flat_map(|rarity| self.item_pools[rarity].clone())
                    .filter(|item| {
                        !self.items_held.contains(item) && !offer.contains(item)
                    })
                    .collect();
                for slot in offer.iter_mut() {
                    if forced >= wanted || candidates.is_empty() {
                        break;
                    }
                    if qualifies(self.data, *slot) {
                        continue;
                    }
                    let pick = self.rng.range(0, candidates.len());
                    *slot = candidates.swap_remove(pick);
                    forced += 1;
                }
            }
        }
        offer.sort_by(|left, right| {
            self.data.items[*left]
                .name
                .cmp(&self.data.items[*right].name)
        });
        offer
    }

    pub fn choose_symbol(&mut self, option: usize) {
        let Phase::SymbolReward { offer } = &self.phase else {
            return;
        };
        if let Some(type_id) = offer.get(option).copied() {
            let value = self.data.symbols[type_id].base_value;
            self.deck.push(Symbol::new(type_id, value));
            let index = self.deck.len() - 1;
            self.board.add_symbol(index, &mut self.rng.reel());
            self.note_symbol_added(type_id);
        }
        self.finish_symbol_choice();
    }

    pub fn skip_symbol(&mut self) {
        if matches!(self.phase, Phase::SymbolReward { .. }) {
            self.token_pepper_income("pink_pepper");
            self.spend_pepper_essence("pink_pepper_essence");
            self.finish_symbol_choice();
        }
    }

    fn finish_symbol_choice(&mut self) {
        self.next_offer_phase(FollowUp::ItemReward);
    }

    /// Advances a pending reward without granting anything. The real game can
    /// swallow a click when its animation timer has not finished; replaying
    /// that action must not add a symbol or fire skip-pepper effects.
    pub fn swallow_reward(&mut self) {
        match self.phase.clone() {
            Phase::SpinReward { .. } => self.finish_spin_choice(),
            Phase::SymbolReward { .. } => self.finish_symbol_choice(),
            Phase::ItemReward { .. } => self.next_offer_phase(FollowUp::Ready),
            Phase::EssenceReward { .. } => self.advance_after_essence(),
            _ => {}
        }
    }

    fn grant_item(&mut self, item_id: usize) {
        // Items stay in the inventory.  Godot only fires the one-shot effects
        // (barrel of dwarves, lunchbox, symbol bombs, ...) when the player
        // destroys the item, so they must not be cashed in on pickup.
        self.items_held.push(item_id);
    }

    /// Destroys an item the way the player's "use item" button does.
    pub fn use_item(&mut self, item_id: usize) -> bool {
        let Some(position) = self
            .items_held
            .iter()
            .position(|id| *id == item_id)
        else {
            return false;
        };
        let def = &self.data.items[item_id];
        let name = def.name.clone();
        let needed = def.values.first().copied().unwrap_or(0.0) as usize;
        let payout = def.values.get(1).copied().unwrap_or(0.0) as i32;
        match name.as_str() {
            "coffee" => self.spins_left += 1,
            "devils_deal" => {
                let count = needed.min(20) as i32;
                self.add_symbols("dud", count);
            }
            "barrel_o_dwarves" => {
                let count = needed.min(20) as i32;
                self.add_symbols("dwarf", count);
            }
            "goldilocks" => {
                let count = needed.min(20) as i32;
                self.add_symbols("bear", count);
            }
            "lunchbox" | "adoption_papers" => {
                let group = if name == "lunchbox" { "food" } else { "animal" };
                for _ in 0..needed.max(1) {
                    if let Some(type_id) = self.pick_group(group) {
                        let value = self.data.symbols[type_id].base_value;
                        self.deck.push(Symbol::new(type_id, value));
                        self.note_symbol_added(type_id);
                    }
                }
            }
            "booster_pack" => {
                let common = needed as i32;
                let uncommon = def.values.get(1).copied().unwrap_or(3.0) as i32;
                let rare = def.values.get(2).copied().unwrap_or(1.0) as i32;
                for _ in 0..common {
                    self.pending_bonus_symbols.push(Some(Rarity::Common));
                }
                for _ in 0..uncommon {
                    self.pending_bonus_symbols.push(Some(Rarity::Uncommon));
                }
                for _ in 0..rare {
                    self.pending_bonus_symbols.push(Some(Rarity::Rare));
                }
            }
            "symbol_bomb_small" | "symbol_bomb_big" | "symbol_bomb_very_big" => {
                for _ in 0..needed as i32 {
                    self.pending_bonus_symbols.push(None);
                }
            }
            "piggy_bank" | "swear_jar" => {
                let saved = self.item_saved.get(&item_id).copied().unwrap_or(0);
                self.coins += (saved * payout).max(0);
            }
            "treasure_map" => {
                let saved = self.item_saved.get(&item_id).copied().unwrap_or(0);
                if saved < needed as i32 {
                    return false;
                }
                self.add_symbols("key", 1);
                self.add_symbols("treasure_chest", 1);
            }
            _ => {}
        }
        self.items_held.remove(position);
        self.item_saved.remove(&item_id);
        true
    }

    pub fn choose_item(&mut self, option: usize) {
        let Phase::ItemReward { offer } = &self.phase else {
            return;
        };
        if let Some(item_id) = offer.get(option).copied() {
            self.grant_item(item_id);
        }
        self.next_offer_phase(FollowUp::Ready);
    }

    pub fn skip_item(&mut self) {
        if matches!(self.phase, Phase::ItemReward { .. }) {
            self.token_pepper_income("pink_pepper");
            self.spend_pepper_essence("pink_pepper_essence");
            self.next_offer_phase(FollowUp::Ready);
        }
    }

    pub fn reroll_cost(&self) -> i32 {
        1 + self.fp_value(24, 0) as i32
    }

    pub fn removal_cost(&self) -> i32 {
        1 + self.fp_value(25, 0) as i32
    }

    pub fn reroll_reward(&mut self) {
        let cost = self.reroll_cost();
        if self.reroll_tokens < cost {
            return;
        }
        let offer = match self.phase {
            Phase::SpinReward { .. } => self.roll_spin_reward(),
            Phase::SymbolReward { .. } => self.roll_symbol_reward(),
            Phase::ItemReward { .. } => self.roll_item_reward(),
            Phase::EssenceReward { .. } => self.roll_essence_reward(),
            _ => return,
        };
        self.reroll_tokens -= cost;
        self.token_pepper_income("lime_pepper");
        self.spend_pepper_essence("lime_pepper_essence");
        if matches!(self.phase, Phase::SpinReward { .. }) {
            self.phase = Phase::SpinReward { offer };
        } else if matches!(self.phase, Phase::SymbolReward { .. }) {
            self.phase = Phase::SymbolReward { offer };
        } else if matches!(self.phase, Phase::EssenceReward { .. }) {
            self.phase = Phase::EssenceReward { offer };
        } else {
            self.phase = Phase::ItemReward { offer };
        }
    }

    fn token_pepper_income(&mut self, item_name: &str) {
        let mut total = 0;
        for id in &self.items_held {
            let def = &self.data.items[*id];
            if def.name == item_name {
                total += def.values.first().copied().unwrap_or(0.0) as i32;
            }
        }
        if total > 0 {
            self.coins += total;
        }
    }

    fn spend_pepper_essence(&mut self, essence_name: &str) {
        if let Some(essence_id) = self
            .data
            .essence_index
            .get(essence_name)
            .copied()
            .filter(|id| self.essences_held.contains(id))
        {
            self.coins += self.data.essences[essence_id]
                .values
                .first()
                .copied()
                .unwrap_or(0.0) as i32;
            self.try_destroy_essence(essence_id);
        }
    }

    pub fn can_remove_symbol(&self, deck_index: usize) -> bool {
        let Some(symbol) = self.deck.get(deck_index) else {
            return false;
        };
        let def = &self.data.symbols[symbol.type_id as usize];
        if def.name == "dud" {
            return false;
        }
        if def.name == "coin" && self.has_fine_print(20) {
            return false;
        }
        if def.name == "egg" && self.has_fine_print(21) {
            return false;
        }
        if def.groups.iter().any(|group| group == "void") && self.fp_affects(36, &def.name) {
            return false;
        }
        true
    }

    pub fn remove_symbol(&mut self, deck_index: usize) {
        let cost = self.removal_cost();
        if self.removal_tokens < cost || !self.can_remove_symbol(deck_index) {
            return;
        }
        self.removal_tokens -= cost;
        self.token_pepper_income("gray_pepper");
        self.spend_pepper_essence("gray_pepper_essence");
        let removed_name = self
            .deck
            .get(deck_index)
            .map(|symbol| self.data.symbols[symbol.type_id as usize].name.clone());
        match removed_name.as_deref() {
            Some("pufferfish") => {
                let gain = self.data.symbol_id("pufferfish").map_or(0, |type_id| {
                    self.data.symbols[type_id]
                        .values
                        .first()
                        .copied()
                        .unwrap_or(1.0) as i32
                });
                self.reroll_tokens += gain;
            }
            Some("jellyfish") => {
                let gain = self.data.symbol_id("jellyfish").map_or(0, |type_id| {
                    self.data.symbols[type_id]
                        .values
                        .first()
                        .copied()
                        .unwrap_or(1.0) as i32
                });
                self.removal_tokens += gain;
            }
            Some("sand_dollar") => {
                let gain = self.data.symbol_id("sand_dollar").map_or(0, |type_id| {
                    self.data.symbols[type_id]
                        .values
                        .first()
                        .copied()
                        .unwrap_or(10.0) as i32
                });
                self.coins += gain;
            }
            _ => {}
        }
        self.board.remove_deck_indices(&[deck_index]);
        self.deck.remove(deck_index);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn data() -> GameData {
        GameData::load(crate::default_data_dir()).expect("game data should load")
    }

    #[test]
    fn seed_text_matches_godot_first_board() {
        let data = data();
        let mut state = RunState::new_with_seed_text(&data, "B2AG00000000");
        state.spin();
        let names: Vec<&str> = state
            .board
            .slots
            .iter()
            .map(|slot| {
                slot.map(|index| data.symbols[state.deck[index].type_id as usize].name.as_str())
                    .unwrap_or("empty")
            })
            .collect();
        assert_eq!(
            names,
            vec![
                "dud", "coin", "empty", "dud", "empty",
                "empty", "flower", "empty", "empty", "empty",
                "empty", "pearl", "empty", "empty", "empty",
                "empty", "cherry", "empty", "dud", "cat",
            ]
        );
    }

    #[test]
    fn seed_text_matches_godot_first_boards_for_twenty_seeds() {
        let data = data();
        let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../../alignment/fixtures/first_boards.json");
        let fixtures: serde_json::Map<String, serde_json::Value> = serde_json::from_str(
            &std::fs::read_to_string(&path).expect("first board fixture"),
        )
        .expect("valid fixture json");
        for (seed, expected) in fixtures {
            let mut state = RunState::new_with_seed_text(&data, &seed);
            state.spin();
            let names: Vec<&str> = state
                .board
                .slots
                .iter()
                .map(|slot| {
                    slot.map(|index| {
                        data.symbols[state.deck[index].type_id as usize].name.as_str()
                    })
                    .unwrap_or("empty")
                })
                .collect();
            let expected: Vec<&str> = expected
                .as_array()
                .expect("board array")
                .iter()
                .map(|value| value.as_str().expect("board name"))
                .collect();
            assert_eq!(names, expected, "seed {seed}");
        }
    }

    #[test]
    fn seed_text_matches_godot_first_three_spins() {
        let data = data();
        let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../../alignment/fixtures/first_three_spins.json");
        let fixtures: serde_json::Map<String, serde_json::Value> = serde_json::from_str(
            &std::fs::read_to_string(&path).expect("first three spins fixture"),
        )
        .expect("valid fixture json");
        for (seed, spins) in fixtures {
            let mut state = RunState::new_with_seed_text(&data, &seed);
            for expected in spins.as_array().expect("spin array") {
                state.spin();
                let board: Vec<&str> = state
                    .board
                    .slots
                    .iter()
                    .map(|slot| {
                        slot.map(|index| {
                            data.symbols[state.deck[index].type_id as usize].name.as_str()
                        })
                        .unwrap_or("empty")
                    })
                    .collect();
                let expected_board: Vec<&str> = expected["board"]
                    .as_array()
                    .expect("board")
                    .iter()
                    .map(|value| value.as_str().expect("name"))
                    .collect();
                assert_eq!(board, expected_board, "seed {seed} spin {}", expected["spin"]);
                let Phase::SpinReward { offer } = &state.phase else {
                    panic!("seed {seed}: expected spin reward");
                };
                let mut offers: Vec<&str> = offer
                    .iter()
                    .map(|index| data.symbols[*index].name.as_str())
                    .collect();
                offers.sort();
                let mut expected_offers: Vec<&str> = expected["offers"]
                    .as_array()
                    .expect("offers")
                    .iter()
                    .map(|value| value.as_str().expect("offer"))
                    .collect();
                expected_offers.sort();
                assert_eq!(offers, expected_offers, "seed {seed} spin {}", expected["spin"]);
                if let Some(chosen) = expected["chosen"].as_str() {
                    let option = offer
                        .iter()
                        .position(|index| data.symbols[*index].name == chosen)
                        .unwrap_or_else(|| panic!("seed {seed}: chosen {chosen} missing"));
                    state.choose_spin(option);
                }
            }
        }
    }

    #[test]
    fn seed_text_matches_godot_first_five_spins_and_item_offer() {
        let data = data();
        let spins_path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../../alignment/fixtures/first_five_spins.json");
        let items_path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../../alignment/fixtures/first_item_offers.json");
        let spins_fixture: serde_json::Map<String, serde_json::Value> =
            serde_json::from_str(&std::fs::read_to_string(&spins_path).expect("five spins"))
                .expect("valid five spins");
        let items_fixture: serde_json::Map<String, serde_json::Value> =
            serde_json::from_str(&std::fs::read_to_string(&items_path).expect("item offers"))
                .expect("valid item offers");
        for (seed, spins) in spins_fixture {
            let mut state = RunState::new_with_seed_text(&data, &seed);
            for expected in spins.as_array().expect("spin array") {
                state.spin();
                let board: Vec<&str> = state
                    .board
                    .slots
                    .iter()
                    .map(|slot| {
                        slot.map(|index| {
                            data.symbols[state.deck[index].type_id as usize].name.as_str()
                        })
                        .unwrap_or("empty")
                    })
                    .collect();
                let expected_board: Vec<&str> = expected["board"]
                    .as_array()
                    .expect("board")
                    .iter()
                    .map(|value| value.as_str().expect("name"))
                    .collect();
                assert_eq!(board, expected_board, "seed {seed} spin {}", expected["spin"]);
                let (offer, is_spin) = match &state.phase {
                    Phase::SpinReward { offer } => (offer.clone(), true),
                    Phase::SymbolReward { offer } => (offer.clone(), false),
                    other => panic!("seed {seed}: unexpected phase {other:?}"),
                };
                let mut offers: Vec<&str> = offer
                    .iter()
                    .map(|index| data.symbols[*index].name.as_str())
                    .collect();
                offers.sort();
                let mut expected_offers: Vec<&str> = expected["offers"]
                    .as_array()
                    .expect("offers")
                    .iter()
                    .map(|value| value.as_str().expect("offer"))
                    .collect();
                expected_offers.sort();
                assert_eq!(offers, expected_offers, "seed {seed} spin {}", expected["spin"]);
                if let Some(chosen) = expected["chosen"].as_str() {
                    let option = offer
                        .iter()
                        .position(|index| data.symbols[*index].name == chosen)
                        .unwrap_or_else(|| panic!("seed {seed}: chosen {chosen} missing"));
                    if is_spin {
                        state.choose_spin(option);
                    } else {
                        state.choose_symbol(option);
                    }
                }
            }
            let Phase::ItemReward { offer } = &state.phase else {
                panic!("seed {seed}: expected item reward");
            };
            let mut items: Vec<&str> = offer
                .iter()
                .map(|index| data.items[*index].name.as_str())
                .collect();
            items.sort();
            let mut expected_items: Vec<&str> = items_fixture[&seed]
                .as_array()
                .expect("item offers")
                .iter()
                .map(|value| value.as_str().expect("item"))
                .collect();
            expected_items.sort();
            assert_eq!(items, expected_items, "seed {seed} item offer");
        }
    }

    #[test]
    fn seed_text_matches_godot_first_offers_for_twenty_seeds() {
        let data = data();
        let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../../alignment/fixtures/first_offers.json");
        let fixtures: serde_json::Map<String, serde_json::Value> = serde_json::from_str(
            &std::fs::read_to_string(&path).expect("first offer fixture"),
        )
        .expect("valid fixture json");
        for (seed, expected) in fixtures {
            let mut state = RunState::new_with_seed_text(&data, &seed);
            state.spin();
            let Phase::SpinReward { offer } = &state.phase else {
                panic!("seed {seed}: expected spin reward");
            };
            let names: Vec<&str> = offer
                .iter()
                .map(|index| data.symbols[*index].name.as_str())
                .collect();
            let expected: Vec<&str> = expected
                .as_array()
                .expect("offer array")
                .iter()
                .map(|value| value.as_str().expect("offer name"))
                .collect();
            assert_eq!(names, expected, "seed {seed}");
        }
    }

    #[test]
    fn recycling_item_banks_a_reroll_token_each_spin() {
        let data = data();
        let mut state = RunState::new(&data, 60);
        state.items_held.push(data.item_index["recycling"]);
        state.resolve_spin_effects();
        assert_eq!(state.reroll_tokens, 1);
        state.resolve_spin_effects();
        assert_eq!(state.reroll_tokens, 2);
    }

    #[test]
    fn ancient_lizard_blade_pays_less_for_duplicate_stacks() {
        let data = data();
        let mut state = RunState::new(&data, 61);
        let coin = data.symbol_id("coin").expect("coin");
        let rabbit = data.symbol_id("rabbit").expect("rabbit");
        state.deck.clear();
        for _ in 0..2 {
            state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
            state.deck.push(Symbol::new(rabbit, data.symbols[rabbit].base_value));
        }
        for slot in 0..4 {
            state.board.slots[slot] = Some(slot);
        }
        state.items_held.push(data.item_index["ancient_lizard_blade"]);
        assert_eq!(state.resolve_spin_effects(), 11);
    }

    #[test]
    fn ancient_lizard_blade_essence_pays_flat_until_duplicates_pile_up() {
        let data = data();
        let mut state = RunState::new(&data, 62);
        let coin = data.symbol_id("coin").expect("coin");
        let rabbit = data.symbol_id("rabbit").expect("rabbit");
        state.deck.clear();
        for _ in 0..2 {
            state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
            state.deck.push(Symbol::new(rabbit, data.symbols[rabbit].base_value));
        }
        for slot in 0..4 {
            state.board.slots[slot] = Some(slot);
        }
        state
            .essences_held
            .push(data.essence_index["ancient_lizard_blade_essence"]);
        assert_eq!(state.resolve_spin_effects(), 28);
    }

    #[test]
    fn tax_evasion_item_covers_negative_symbols_every_spin() {
        let data = data();
        let mut state = RunState::new(&data, 63);
        let magpie = data.symbol_id("magpie").expect("magpie");
        state.deck.clear();
        state.deck.push(Symbol::new(magpie, data.symbols[magpie].base_value));
        state.board.slots[0] = Some(0);
        state.items_held.push(data.item_index["tax_evasion"]);
        assert_eq!(state.resolve_spin_effects(), 0);
    }

    #[test]
    fn copycat_turns_cats_into_wildcards() {
        let data = data();
        let mut state = RunState::new(&data, 64);
        let diamond = data.symbol_id("diamond").expect("diamond");
        let cat = data.symbol_id("cat").expect("cat");
        state.deck.clear();
        state.deck.push(Symbol::new(diamond, data.symbols[diamond].base_value));
        state.deck.push(Symbol::new(cat, data.symbols[cat].base_value));
        state.board.slots[0] = Some(0);
        state.board.slots[1] = Some(1);
        state.items_held.push(data.item_index["copycat"]);
        // Diamond in the `eachother` group counts itself out: 5. The
        // wildcard cat copies that 5.
        assert_eq!(state.resolve_spin_effects(), 10);
    }

    #[test]
    fn chicken_coop_buffs_chickenstuff_each_spin() {
        let data = data();
        let mut state = RunState::new(&data, 65);
        let chicken = data.symbol_id("chicken").expect("chicken");
        state.deck.clear();
        state.deck.push(Symbol::new(chicken, data.symbols[chicken].base_value));
        state.board.slots[0] = Some(0);
        state.items_held.push(data.item_index["chicken_coop"]);
        assert_eq!(state.resolve_spin_effects(), 4);
    }

    #[test]
    fn boss_fight_ends_when_damage_reaches_zero() {
        let data = data();
        let mut state = RunState::new(&data, 9);
        let coin = data.symbol_id("coin").expect("coin");
        state.deck.clear();
        for _ in 0..5 {
            state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        }
        state.rents_paid = 12;
        state.boss_fight = true;
        state.boss_hp = 5;
        state.coins = 10;
        state.spins_left = 10;
        state.spin();
        assert!(state.won);
        assert!(state.done());
    }

    #[test]
    fn boss_income_damage_generates_fine_prints() {
        let data = data();
        let mut state = RunState::new(&data, 11);
        let coin = data.symbol_id("coin").expect("coin");
        state.deck.clear();
        for _ in 0..5 {
            state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        }
        state.rents_paid = 12;
        state.boss_fight = true;
        state.boss_hp = 1500;
        state.spins_left = 10;
        state.coins = 10;
        state.add_permanent_name_bonus("coin", 29);
        assert!(state.fine_prints.is_empty());
        let outcome = state.spin();
        assert_eq!(outcome.income, 150);
        assert_eq!(state.boss_hp, 1350);
        assert_eq!(state.fine_prints.len(), 3);
    }

    #[test]
    fn paying_rent_does_not_add_fine_prints() {
        let data = data();
        let mut state = RunState::new(&data, 12);
        state.deck.clear();
        state.coins = 100;
        state.spins_left = 1;
        let outcome = state.spin();
        assert!(outcome.rent_paid);
        assert_eq!(state.rents_paid, 1);
        assert!(state.fine_prints.is_empty());
    }

    #[test]
    fn boss_fine_print_pool_skips_legacy_theft_clauses() {
        let data = data();
        let mut state = RunState::new(&data, 11);
        state.boss_fight = true;
        for _ in 0..20 {
            state.apply_boss_damage(150);
        }
        assert!(!state.fine_prints.is_empty());
        for legacy in [1u32, 2, 3, 5, 6, 7] {
            assert!(!state.fine_prints.contains(&legacy));
        }
    }

    #[test]
    fn item_reliant_fine_prints_need_matching_items() {
        let data = data();
        let mut state = RunState::new(&data, 11);
        assert!(!state.fp_applicable(18));
        assert!(!state.fp_applicable(17));
        state.items_held.push(data.item_index["guillotine"]);
        state.items_held.push(data.item_index["green_pepper"]);
        assert!(state.fp_applicable(18));
        assert!(state.fp_applicable(17));
    }

    #[test]
    fn time_capsule_copies_a_destroyed_symbol() {
        let data = data();
        let mut state = RunState::new(&data, 7);
        let dud = data.symbol_id("dud").expect("dud");
        let capsule = data.symbol_id("time_capsule").expect("time capsule");
        let coin = data.symbol_id("coin").expect("coin");
        state.deck.clear();
        state.deck.push(Symbol::new(capsule, 0));
        for _ in 0..4 {
            state.deck.push(Symbol::new(dud, 0));
        }
        state.destroyed_history.push(coin as u16);
        state.spin();
        assert!(state
            .deck
            .iter()
            .all(|symbol| symbol.type_id as usize != capsule));
        assert!(state
            .deck
            .iter()
            .any(|symbol| symbol.type_id as usize == coin));
    }

    #[test]
    fn compost_heap_turns_destroyed_symbols_into_seeds() {
        let data = data();
        let mut state = RunState::new(&data, 8);
        let dud = data.symbol_id("dud").expect("dud");
        let seed = data.symbol_id("seed").expect("seed");
        let compost = data.item_index["compost_heap"];
        state.deck.clear();
        for _ in 0..5 {
            state.deck.push(Symbol::new(dud, 0));
        }
        state.items_held.push(compost);
        state.compost_destroyed = 3;
        state.spin();
        assert!(state
            .deck
            .iter()
            .any(|symbol| symbol.type_id as usize == seed));
    }

    #[test]
    fn triple_coins_multiplies_coin_income() {
        let data = data();
        let mut state = RunState::new(&data, 3);
        let coin = data.symbol_id("coin").expect("coin");
        state.deck.clear();
        for _ in 0..5 {
            state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        }
        state.items_held.push(data.item_index["triple_coins"]);
        let outcome = state.spin();
        assert_eq!(outcome.income, 15);
    }

    #[test]
    fn rusty_gear_doubles_clumped_types_only() {
        // Godot's `make_clumps` groups *adjacent* identical icons; rusty gear
        // needs a connected clump of at least three, and only those members
        // receive the multiplier.  Counting all copies on the board is wrong.
        let data = data();
        let cherry = data.symbol_id("cherry").expect("cherry");

        // Three connected cherries (0,0), (1,0), (0,1) plus one isolated one.
        let mut clumped = RunState::new(&data, 4);
        clumped.deck.clear();
        for _ in 0..4 {
            clumped
                .deck
                .push(Symbol::new(cherry, data.symbols[cherry].base_value));
        }
        for (slot, index) in [(0, 0), (1, 1), (5, 2), (19, 3)] {
            clumped.board.slots[slot] = Some(index);
        }
        clumped.items_held.push(data.item_index["rusty_gear"]);
        assert_eq!(clumped.resolve_spin_effects(), 7);

        // Same four copies, but none adjacent: no clump, no multiplier.
        let mut scattered = RunState::new(&data, 4);
        scattered.deck.clear();
        for _ in 0..4 {
            scattered
                .deck
                .push(Symbol::new(cherry, data.symbols[cherry].base_value));
        }
        for (slot, index) in [(0, 0), (2, 1), (4, 2), (19, 3)] {
            scattered.board.slots[slot] = Some(index);
        }
        scattered.items_held.push(data.item_index["rusty_gear"]);
        assert_eq!(scattered.resolve_spin_effects(), 4);
    }

    #[test]
    fn lefty_the_rabbit_sheds_fluff_every_ten_saved_coins() {
        let data = data();
        let mut state = RunState::new(&data, 5);
        let coin = data.symbol_id("coin").expect("coin");
        let fluff = data.symbol_id("rabbit_fluff").expect("rabbit fluff");
        state.deck.clear();
        state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        state.items_held.push(data.item_index["lefty_the_rabbit"]);
        state.lefty_saved = 9;
        state.spin();
        assert_eq!(state.lefty_saved, 0);
        assert!(state
            .deck
            .iter()
            .any(|symbol| symbol.type_id as usize == fluff));
    }

    #[test]
    fn lemon_pays_one_coin_per_empty_slot() {
        let data = data();
        let mut state = RunState::new(&data, 6);
        let coin = data.symbol_id("coin").expect("coin");
        state.deck.clear();
        state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        state.items_held.push(data.item_index["lemon"]);
        let outcome = state.spin();
        assert_eq!(outcome.income, 20);
    }

    #[test]
    fn bag_of_holding_adds_an_extra_item_offer() {
        let data = data();
        let mut state = RunState::new(&data, 10);
        state.items_held.push(data.item_index["bag_of_holding"]);
        assert_eq!(state.roll_item_reward().len(), 4);
    }

    #[test]
    fn time_machine_speeds_up_coal_growth() {
        let data = data();
        let mut state = RunState::new(&data, 12);
        let coal = data.symbol_id("coal").expect("coal");
        let diamond = data.symbol_id("diamond").expect("diamond");
        state.deck.clear();
        let mut symbol = Symbol::new(coal, data.symbols[coal].base_value);
        symbol.times_displayed = 14;
        state.deck.push(symbol);
        state.items_held.push(data.item_index["time_machine"]);
        state.spin();
        assert_eq!(state.deck[0].type_id as usize, diamond);
    }

    #[test]
    fn token_peppers_pay_when_tokens_are_used() {
        let data = data();
        let mut state = RunState::new(&data, 13);
        let coin = data.symbol_id("coin").expect("coin");
        for _ in 0..5 {
            state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        }
        state.items_held.push(data.item_index["gray_pepper"]);
        state.items_held.push(data.item_index["lime_pepper"]);
        state.removal_tokens = 1;
        state.remove_symbol(0);
        assert_eq!(state.coins, 7);
        state.coins = 0;
        state.reroll_tokens = 1;
        state.phase = Phase::SpinReward { offer: Vec::new() };
        state.reroll_reward();
        assert_eq!(state.coins, 6);
    }

    #[test]
    fn pink_pepper_pays_when_a_reward_is_skipped() {
        let data = data();
        let mut state = RunState::new(&data, 14);
        let coin = data.symbol_id("coin").expect("coin");
        state.deck.clear();
        state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        state.items_held.push(data.item_index["pink_pepper"]);
        let outcome = state.spin();
        assert_eq!(outcome.income, 1);
        state.phase = Phase::SpinReward { offer: Vec::new() };
        let before = state.coins;
        state.skip_spin();
        assert_eq!(state.coins, before + 2);
    }

    #[test]
    fn gray_pepper_essence_pays_on_removal_and_is_consumed() {
        let data = data();
        let mut state = RunState::new(&data, 36);
        let coin = data.symbol_id("coin").expect("coin");
        state.deck.clear();
        for _ in 0..3 {
            state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        }
        state
            .essences_held
            .push(data.essence_index["gray_pepper_essence"]);
        state.removal_tokens = 1;
        state.remove_symbol(0);
        assert_eq!(state.coins, 31);
        assert!(!state.has_essence("gray_pepper_essence"));
        assert_eq!(state.deck.len(), 2);
    }

    #[test]
    fn lime_pepper_essence_pays_on_reroll_and_is_consumed() {
        let data = data();
        let mut state = RunState::new(&data, 37);
        state
            .essences_held
            .push(data.essence_index["lime_pepper_essence"]);
        state.reroll_tokens = 1;
        state.phase = Phase::SpinReward { offer: Vec::new() };
        state.reroll_reward();
        assert_eq!(state.coins, 31);
        assert!(!state.has_essence("lime_pepper_essence"));
    }

    #[test]
    fn pink_pepper_essence_pays_on_skip_and_is_consumed() {
        let data = data();
        let mut state = RunState::new(&data, 38);
        state
            .essences_held
            .push(data.essence_index["pink_pepper_essence"]);
        state.phase = Phase::SpinReward { offer: Vec::new() };
        state.skip_spin();
        assert_eq!(state.coins, 21);
        assert!(!state.has_essence("pink_pepper_essence"));
    }

    #[test]
    fn lemon_essence_pays_per_empty_slot_and_pops_at_four() {
        let data = data();
        let mut state = RunState::new(&data, 39);
        let coin = data.symbol_id("coin").expect("coin");
        state.deck.clear();
        for _ in 0..2 {
            state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        }
        state.essences_held.push(data.essence_index["lemon_essence"]);
        state.spins_left = 10;
        let outcome = state.spin();
        assert_eq!(outcome.income, 2 + 18 * 4);
        assert!(!state.has_essence("lemon_essence"));
    }

    #[test]
    fn clear_sky_lets_the_sun_reach_the_whole_board() {
        let data = data();
        let mut state = RunState::new(&data, 15);
        let sun = data.symbol_id("sun").expect("sun");
        let flower = data.symbol_id("flower").expect("flower");
        let dud = data.symbol_id("dud").expect("dud");
        state.deck.clear();
        state.deck.push(Symbol::new(sun, data.symbols[sun].base_value));
        state.deck.push(Symbol::new(flower, data.symbols[flower].base_value));
        for _ in 0..18 {
            state.deck.push(Symbol::new(dud, 0));
        }
        state.items_held.push(data.item_index["clear_sky"]);
        let outcome = state.spin();
        assert_eq!(outcome.income, 8);
    }

    #[test]
    fn shrine_turns_destroyed_organisms_into_spirits() {
        let data = data();
        let mut state = RunState::new(&data, 11);
        let coin = data.symbol_id("coin").expect("coin");
        let creature = data.symbol_id("void_creature").expect("void creature");
        let spirit = data.symbol_id("spirit").expect("spirit");
        state.deck.clear();
        for _ in 0..19 {
            state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        }
        state.deck.push(Symbol::new(creature, 0));
        state.items_held.push(data.item_index["shrine"]);
        state.spin();
        assert!(state
            .deck
            .iter()
            .any(|symbol| symbol.type_id as usize == spirit));
    }

    #[test]
    fn rent_four_consumes_banked_essence_tokens() {
        let data = data();
        let mut state = RunState::new(&data, 3);
        state.rents_paid = 3;
        state.coins = 1000;
        state.spins_left = 1;
        state.essence_tokens = 2;
        state.pay_rent();
        assert!(matches!(state.phase, Phase::EssenceReward { .. }));
        assert_eq!(state.pending_essence_offers, 2);
        state.choose_essence(0);
        assert!(matches!(state.phase, Phase::EssenceReward { .. }));
        assert_eq!(state.essences_held.len(), 1);
        state.choose_essence(0);
        // Some essences queue a guaranteed bonus symbol offer first.
        assert!(matches!(
            state.phase,
            Phase::SymbolReward { .. } | Phase::SpinReward { .. }
        ));
        assert_eq!(state.essences_held.len(), 2);
        assert_eq!(state.pending_essence_offers, 0);
        // Rent 4 is even, so this payment does not bank new tokens.
        assert_eq!(state.essence_tokens, 0);
    }

    #[test]
    fn essence_capsule_grants_tokens_and_destroys_itself() {
        let data = data();
        let mut state = RunState::new(&data, 5);
        let capsule = data.symbol_id("essence_capsule").expect("essence capsule");
        let coin = data.symbol_id("coin").expect("coin");
        state.deck.clear();
        for _ in 0..19 {
            state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        }
        state.deck.push(Symbol::new(capsule, 0));
        state.spins_left = 10;
        state.spin();
        assert_eq!(state.essence_tokens, 1);
        assert!(state
            .deck
            .iter()
            .all(|symbol| symbol.type_id as usize != capsule));
    }

    #[test]
    fn horseshoe_essence_charges_then_pays_out() {
        let data = data();
        let mut state = RunState::new(&data, 6);
        let essence = data.essence_index["horseshoe_essence"];
        state.essences_held.push(essence);
        state.essence_saved.insert(essence, 29);
        let payout = state.tick_essences(&[], 0);
        assert_eq!(payout, 2);
        assert!(state.essences_held.is_empty());
    }

    #[test]
    fn horseshoe_essence_pays_every_spin_until_it_expires() {
        let data = data();
        let mut state = RunState::new(&data, 40);
        let essence = data.essence_index["horseshoe_essence"];
        state.essences_held.push(essence);
        assert_eq!(state.tick_essences(&[], 0), 2);
        assert_eq!(state.tick_essences(&[], 0), 2);
        assert!(state.has_essence("horseshoe_essence"));
        state.essence_saved.insert(essence, 29);
        assert_eq!(state.tick_essences(&[], 0), 2);
        assert!(!state.has_essence("horseshoe_essence"));
    }

    #[test]
    fn lefty_essence_triples_leftmost_rabbits_then_expires() {
        let data = data();
        let mut state = RunState::new(&data, 42);
        let rabbit = data.symbol_id("rabbit").expect("rabbit");
        state.deck.clear();
        for _ in 0..6 {
            state.deck.push(Symbol::new(rabbit, data.symbols[rabbit].base_value));
        }
        state
            .essences_held
            .push(data.essence_index["lefty_the_rabbit_essence"]);
        for (slot, deck_index) in [(0, 0), (5, 1), (10, 2), (15, 3), (1, 4), (6, 5)] {
            state.board.slots[slot] = Some(deck_index);
        }
        let income = state.resolve_spin_effects();
        let base = data.symbols[rabbit].base_value as i32;
        assert_eq!(income, 4 * base * 3 + 2 * base);
        assert!(!state.has_essence("lefty_the_rabbit_essence"));
    }

    #[test]
    fn watering_can_essence_pays_on_seed_symbols() {
        let data = data();
        let mut state = RunState::new(&data, 54);
        let seed = data.symbol_id("seed").expect("seed");
        state.deck.clear();
        state.deck.push(Symbol::new(seed, data.symbols[seed].base_value));
        state.board.slots[0] = Some(0);
        state.fine_prints.push(34);
        state
            .essences_held
            .push(data.essence_index["watering_can_essence"]);
        let income = state.resolve_spin_effects();
        assert_eq!(income, data.symbols[seed].base_value as i32 + 20);
        assert!(state.has_essence("watering_can_essence"));
    }

    #[test]
    fn brown_pepper_essence_pays_per_added_symbol_then_dies() {
        let data = data();
        let mut state = RunState::new(&data, 55);
        let coin = data.symbol_id("coin").expect("coin");
        let fluff = data.symbol_id("rabbit_fluff").expect("rabbit fluff");
        state.deck.clear();
        for _ in 0..10 {
            state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        }
        state.items_held.push(data.item_index["lefty_the_rabbit"]);
        state.lefty_saved = 9;
        state
            .essences_held
            .push(data.essence_index["brown_pepper_essence"]);
        let outcome = state.spin();
        // 10 coins + 10 from the brown pepper essence + the spawned
        // rabbit fluff's own 2-coin value.
        assert_eq!(outcome.income, 22);
        assert!(!state.has_essence("brown_pepper_essence"));
        assert!(state
            .deck
            .iter()
            .any(|symbol| symbol.type_id as usize == fluff));
    }

    #[test]
    fn shedding_season_essence_spawns_fluff_and_dies() {
        let data = data();
        let mut state = RunState::new(&data, 56);
        let rabbit = data.symbol_id("rabbit").expect("rabbit");
        let fluff = data.symbol_id("rabbit_fluff").expect("rabbit fluff");
        state.deck.clear();
        state.deck.push(Symbol::new(rabbit, data.symbols[rabbit].base_value));
        state.board.slots[0] = Some(0);
        state
            .essences_held
            .push(data.essence_index["shedding_season_essence"]);
        let income = state.resolve_spin_effects();
        // The rabbit pays 1; four of the five spawned fluff icons land in
        // visible slots (2 coins each), one goes off-screen.
        assert_eq!(income, 9);
        assert!(!state.has_essence("shedding_season_essence"));
        assert_eq!(
            state
                .deck
                .iter()
                .filter(|symbol| symbol.type_id as usize == fluff)
                .count(),
            5
        );
    }

    #[test]
    fn sunglasses_essence_trades_tokens_and_dies() {
        let data = data();
        let mut state = RunState::new(&data, 57);
        state.removal_tokens = 2;
        state
            .essences_held
            .push(data.essence_index["sunglasses_essence"]);
        state.resolve_spin_effects();
        assert_eq!(state.removal_tokens, 2);
        assert!(state.has_essence("sunglasses_essence"));
        state.removal_tokens = 3;
        state.resolve_spin_effects();
        assert_eq!(state.removal_tokens, 8);
        assert!(!state.has_essence("sunglasses_essence"));
    }

    #[test]
    fn treasure_map_essence_adds_key_and_chest_after_33_spins() {
        let data = data();
        let mut state = RunState::new(&data, 58);
        let deck_len = state.deck.len();
        state
            .essences_held
            .push(data.essence_index["treasure_map_essence"]);
        for _ in 0..32 {
            state.tick_essences(&[], 0);
        }
        assert_eq!(state.deck.len(), deck_len);
        assert!(state.has_essence("treasure_map_essence"));
        state.tick_essences(&[], 0);
        assert!(!state.has_essence("treasure_map_essence"));
        assert_eq!(state.deck.len(), deck_len + 2);
        let names: Vec<&str> = state.deck[deck_len..]
            .iter()
            .map(|symbol| data.symbols[symbol.type_id as usize].name.as_str())
            .collect();
        assert_eq!(names, vec!["key", "mega_chest"]);
    }

    #[test]
    fn popsicle_makes_essences_trigger_twice_before_dying() {
        let data = data();
        let mut state = RunState::new(&data, 52);
        let coin = data.symbol_id("coin").expect("coin");
        state.deck.clear();
        for _ in 0..3 {
            state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        }
        for index in 0..3 {
            state.board.slots[index] = Some(index);
        }
        state.items_held.push(data.item_index["popsicle"]);
        state
            .essences_held
            .push(data.essence_index["white_pepper_essence"]);
        assert_eq!(state.resolve_spin_effects(), 33);
        assert!(state.has_essence("white_pepper_essence"));
        assert_eq!(state.resolve_spin_effects(), 33);
        assert!(!state.has_essence("white_pepper_essence"));
    }

    #[test]
    fn popsicle_essence_dies_with_the_essence_it_protects() {
        let data = data();
        let mut state = RunState::new(&data, 53);
        let coin = data.symbol_id("coin").expect("coin");
        state.deck.clear();
        for _ in 0..3 {
            state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        }
        for index in 0..3 {
            state.board.slots[index] = Some(index);
        }
        state
            .essences_held
            .push(data.essence_index["popsicle_essence"]);
        state
            .essences_held
            .push(data.essence_index["white_pepper_essence"]);
        assert_eq!(state.resolve_spin_effects(), 33);
        assert!(state.has_essence("white_pepper_essence"));
        assert!(state.has_essence("popsicle_essence"));
        assert_eq!(state.resolve_spin_effects(), 33);
        assert!(!state.has_essence("white_pepper_essence"));
        assert!(!state.has_essence("popsicle_essence"));
    }

    #[test]
    fn devils_deal_waives_rent_when_destroyed() {
        let data = data();
        let mut state = RunState::new(&data, 51);
        state.deck.clear();
        state.items_held.push(data.item_index["devils_deal"]);
        state.spins_left = 1;
        state.coins = 0;
        let outcome = state.spin();
        assert!(outcome.rent_paid);
        assert_eq!(state.rents_paid, 1);
        assert_eq!(state.coins, 0);
        assert!(state
            .items_held
            .iter()
            .all(|id| data.items[*id].name != "devils_deal"));
        assert!(matches!(state.phase, Phase::SymbolReward { .. }));
    }

    #[test]
    fn coffee_buys_one_more_spin_when_rent_is_short() {
        let data = data();
        let mut state = RunState::new(&data, 50);
        state.deck.clear();
        state.items_held.push(data.item_index["coffee"]);
        state.spins_left = 1;
        state.coins = 0;
        let outcome = state.spin();
        assert!(!outcome.rent_paid);
        assert!(!state.done());
        assert_eq!(state.spins_left, 1);
        assert!(state
            .items_held
            .iter()
            .all(|id| data.items[*id].name != "coffee"));
    }

    #[test]
    fn dove_protects_adjacent_symbols_and_grows() {
        let data = data();
        let mut state = RunState::new(&data, 49);
        let dove = data.symbol_id("dove").expect("dove");
        let billionaire = data.symbol_id("billionaire").expect("billionaire");
        state.deck.clear();
        state.deck.push(Symbol::new(dove, data.symbols[dove].base_value));
        state
            .deck
            .push(Symbol::new(billionaire, data.symbols[billionaire].base_value));
        state.items_held.push(data.item_index["guillotine"]);
        state.board.slots[0] = Some(0);
        state.board.slots[1] = Some(1);
        let _ = state.resolve_spin_effects();
        assert!(state
            .deck
            .iter()
            .any(|symbol| symbol.type_id as usize == billionaire));
        assert_eq!(state.deck[0].bonus, 1);
    }

    #[test]
    fn credit_card_adds_seventeen_symbol_choices_every_seven_spins() {
        let data = data();
        let mut state = RunState::new(&data, 48);
        state.deck.clear();
        state.items_held.push(data.item_index["credit_card"]);
        for _ in 0..6 {
            state.resolve_spin_effects();
        }
        assert_eq!(state.pending_extra_symbol_choices, 0);
        state.resolve_spin_effects();
        assert_eq!(state.credit_card_saved, 0);
        assert_eq!(state.pending_extra_symbol_choices, 17);
        let offer = state.roll_symbol_reward();
        assert_eq!(offer.len(), 3 + 17);
        assert_eq!(state.pending_extra_symbol_choices, 0);
    }

    #[test]
    fn telescope_charges_every_three_spins() {
        let data = data();
        let mut state = RunState::new(&data, 46);
        state.deck.clear();
        state.items_held.push(data.item_index["telescope"]);
        state.resolve_spin_effects();
        assert_eq!(state.telescope_saved, 1);
        state.resolve_spin_effects();
        assert_eq!(state.telescope_saved, 2);
        state.resolve_spin_effects();
        assert_eq!(state.telescope_saved, 0);
    }

    #[test]
    fn telescope_essence_and_protractor_adjacency_flags() {
        let data = data();
        let mut state = RunState::new(&data, 47);
        let coin = data.symbol_id("coin").expect("coin");
        state.deck.clear();
        for _ in 0..5 {
            state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        }
        state.board.slots[0] = Some(0);
        state.board.slots[6] = Some(1);
        state.board.slots[5] = Some(2);
        state.board.slots[19] = Some(3);
        state.spin_all_adjacent = true;
        assert_eq!(state.neighbor_indexes(0, 0, 0), vec![2, 1, 3]);
        state.spin_all_adjacent = false;
        state.spin_corner_adjacent = true;
        let middle = state.neighbor_indexes(1, 1, 1);
        assert!(middle.contains(&0));
        assert!(middle.contains(&3));
        assert!(middle.contains(&2));
        let corner = state.neighbor_indexes(0, 0, 0);
        assert!(corner.contains(&1));
        assert!(corner.contains(&2));
        assert!(corner.contains(&3));
    }

    #[test]
    fn lucky_seven_essence_makes_chemical_seven_common() {
        let data = data();
        let mut state = RunState::new(&data, 45);
        let essence = data.essence_index["lucky_seven_essence"];
        let seven = data.symbol_id("chemical_seven").expect("chemical seven");
        state.essences_held.push(essence);
        for _ in 0..6 {
            assert_eq!(state.tick_essences(&[], 0), 0);
        }
        assert!(state.has_essence("lucky_seven_essence"));
        assert_eq!(state.tick_essences(&[], 0), 0);
        assert!(!state.has_essence("lucky_seven_essence"));
        assert!(state.rarity_pools[Rarity::Uncommon as usize]
            .iter()
            .all(|id| *id != seven));
        assert!(state.rarity_pools[Rarity::Common as usize].contains(&seven));
    }

    #[test]
    fn piggy_bank_essence_hatches_after_twenty_five_spins() {
        let data = data();
        let mut state = RunState::new(&data, 43);
        let essence = data.essence_index["piggy_bank_essence"];
        state.essences_held.push(essence);
        for _ in 0..24 {
            assert_eq!(state.tick_essences(&[], 0), 0);
        }
        assert!(state.has_essence("piggy_bank_essence"));
        assert_eq!(state.tick_essences(&[], 0), 152);
        assert!(!state.has_essence("piggy_bank_essence"));
    }

    #[test]
    fn swear_jar_essence_pays_out_on_a_weak_spin() {
        let data = data();
        let mut state = RunState::new(&data, 44);
        let coin = data.symbol_id("coin").expect("coin");
        state.deck.clear();
        state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        state
            .essences_held
            .push(data.essence_index["swear_jar_essence"]);
        state.board.slots[0] = Some(0);
        let income = state.resolve_spin_effects();
        assert_eq!(income, 1 + 50);
        assert!(!state.has_essence("swear_jar_essence"));
    }

    #[test]
    fn egg_carton_essence_pays_per_removed_egg_and_expires() {
        let data = data();
        let mut state = RunState::new(&data, 41);
        let egg = data.symbol_id("egg").expect("egg");
        let essence = data.essence_index["egg_carton_essence"];
        state.deck.clear();
        for _ in 0..3 {
            state.deck.push(Symbol::new(egg, data.symbols[egg].base_value));
        }
        state.essences_held.push(essence);
        state.store_eggs_and_fish();
        assert_eq!(state.coins, 1 + 3 * 12);
        assert!(state.deck.is_empty());
        assert!(state.has_essence("egg_carton_essence"));
        for _ in 0..9 {
            state.deck.push(Symbol::new(egg, data.symbols[egg].base_value));
        }
        state.store_eggs_and_fish();
        assert_eq!(state.coins, 1 + 12 * 12);
        assert!(!state.has_essence("egg_carton_essence"));
    }

    #[test]
    fn freezer_pizza_essence_adds_an_extra_symbol_offer() {
        let data = data();
        let mut state = RunState::new(&data, 12);
        let essence = data.essence_index["frozen_pizza_essence"];
        let coin = data.symbol_id("coin").expect("coin");
        state.essences_held.push(essence);
        state.deck.clear();
        for _ in 0..5 {
            state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        }
        state.spins_left = 10;
        state.spin();
        assert!(matches!(state.phase, Phase::SpinReward { .. }));
        state.skip_spin();
        assert!(matches!(state.phase, Phase::SpinReward { .. }));
        state.skip_spin();
        assert!(matches!(state.phase, Phase::Ready));
        // One spin charged one use out of five.
        assert!(state.essences_held.contains(&essence));
    }

    #[test]
    fn booster_pack_essence_does_not_queue_offers_on_acquisition() {
        // 真机（Item.0.gd:2902）的 booster_pack_essence 是个靠 saved_value
        // 充能、到点自毁的道具，拿到时不会立刻插三选一。此前这里会凭空
        // 塞进 3 次"同稀有度三连"，导致整局后面多出三次决策。
        let data = data();
        let mut state = RunState::new(&data, 21);
        let essence = data.essence_index["booster_pack_essence"];
        state.grant_essence(essence);
        assert!(state.essences_held.contains(&essence));
        assert!(state.pending_bonus_symbols.is_empty());
    }

    #[test]
    fn goldilocks_adds_bears_when_used() {
        let data = data();
        let mut state = RunState::new(&data, 22);
        state.deck.clear();
        let item = data.item_index["goldilocks"];
        let count = data.items[item].values.first().copied().unwrap_or(3.0) as i32;
        state.grant_item(item);
        assert!(state.items_held.contains(&item));
        assert_eq!(state.deck.len(), 0);
        state.use_item(item);
        let bears = state
            .deck
            .iter()
            .filter(|symbol| data.symbols[symbol.type_id as usize].name == "bear")
            .count() as i32;
        assert_eq!(bears, count.max(0));
        assert!(!state.items_held.contains(&item));
    }

    #[test]
    fn coin_on_a_string_consumes_coins_for_three_each() {
        let data = data();
        let mut state = RunState::new(&data, 24);
        let coin = data.symbol_id("coin").expect("coin");
        let dud = data.symbol_id("dud").expect("dud");
        state.deck.clear();
        for _ in 0..3 {
            state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        }
        for _ in 0..5 {
            state.deck.push(Symbol::new(dud, 0));
        }
        state.items_held.push(data.item_index["coin_on_a_string"]);
        state.spins_left = 10;
        let outcome = state.spin();
        assert_eq!(outcome.income, 10, "3 coins x 3 + the item's own 1 coin");
        assert!(state
            .deck
            .iter()
            .all(|symbol| symbol.type_id as usize != coin));
    }

    #[test]
    fn flush_consumes_suits_for_twelve_each() {
        let data = data();
        let mut state = RunState::new(&data, 25);
        let dud = data.symbol_id("dud").expect("dud");
        let clubs = data.symbol_id("clubs").expect("clubs");
        let spades = data.symbol_id("spades").expect("spades");
        state.deck.clear();
        state.deck.push(Symbol::new(clubs, data.symbols[clubs].base_value));
        state.deck.push(Symbol::new(spades, data.symbols[spades].base_value));
        for _ in 0..5 {
            state.deck.push(Symbol::new(dud, 0));
        }
        state.items_held.push(data.item_index["flush"]);
        state.spins_left = 10;
        let outcome = state.spin();
        assert_eq!(outcome.income, 24);
        assert!(state
            .deck
            .iter()
            .all(|symbol| data.symbols[symbol.type_id as usize].name != "clubs"
                && data.symbols[symbol.type_id as usize].name != "spades"));
    }

    #[test]
    fn cleaning_rag_essence_doubles_gems() {
        let data = data();
        let mut state = RunState::new(&data, 46);
        let pearl = data.symbol_id("pearl").expect("pearl");
        let dud = data.symbol_id("dud").expect("dud");
        state.deck.clear();
        for _ in 0..5 {
            state
                .deck
                .push(Symbol::new(pearl, data.symbols[pearl].base_value));
        }
        for _ in 0..3 {
            state.deck.push(Symbol::new(dud, 0));
        }
        state
            .essences_held
            .push(data.essence_index["cleaning_rag_essence"]);
        state.spins_left = 10;
        let outcome = state.spin();
        assert_eq!(outcome.income, 10, "five pearls at 1 become 2 each");
        assert!(!state.has_essence("cleaning_rag_essence"));
        assert!(state
            .deck
            .iter()
            .filter(|symbol| data.symbols[symbol.type_id as usize].name == "pearl")
            .all(|symbol| symbol.current_value(&data) == 2));
    }

    #[test]
    fn compost_heap_essence_turns_three_deaths_into_watermelons() {
        let data = data();
        let mut state = RunState::new(&data, 45);
        let billionaire = data.symbol_id("billionaire").expect("billionaire");
        state.deck.clear();
        for _ in 0..3 {
            state.deck.push(Symbol::new(
                billionaire,
                data.symbols[billionaire].base_value,
            ));
        }
        state.items_held.push(data.item_index["guillotine"]);
        state
            .essences_held
            .push(data.essence_index["compost_heap_essence"]);
        for slot in 0..3 {
            state.board.slots[slot] = Some(slot);
        }
        state.resolve_spin_effects();
        assert!(!state.has_essence("compost_heap_essence"));
        let melons = state
            .deck
            .iter()
            .filter(|symbol| data.symbols[symbol.type_id as usize].name == "watermelon")
            .count();
        assert_eq!(melons, 2);
    }

    #[test]
    fn ricky_the_banana_essence_buffs_bananas_permanently() {
        let data = data();
        let mut state = RunState::new(&data, 44);
        let banana = data.symbol_id("banana").expect("banana");
        let dud = data.symbol_id("dud").expect("dud");
        state.deck.clear();
        for _ in 0..3 {
            state
                .deck
                .push(Symbol::new(banana, data.symbols[banana].base_value));
        }
        for _ in 0..4 {
            state.deck.push(Symbol::new(dud, 0));
        }
        state
            .essences_held
            .push(data.essence_index["ricky_the_banana_essence"]);
        state.spins_left = 10;
        state.spin();
        assert!(!state.has_essence("ricky_the_banana_essence"));
        assert!(state
            .deck
            .iter()
            .filter(|symbol| data.symbols[symbol.type_id as usize].name == "banana")
            .all(|symbol| symbol.current_value(&data) == 3));
    }

    #[test]
    fn zaroffs_contract_lets_the_bounty_hunt_humans() {
        let data = data();
        let mut state = RunState::new(&data, 42);
        let bounty = data.symbol_id("bounty_hunter").expect("bounty");
        let farmer = data.symbol_id("farmer").expect("farmer");
        let thief = data.symbol_id("thief").expect("thief");
        state.deck.clear();
        state.deck.push(Symbol::new(bounty, data.symbols[bounty].base_value));
        state.deck.push(Symbol::new(farmer, data.symbols[farmer].base_value));
        state.deck.push(Symbol::new(thief, data.symbols[thief].base_value));
        state.items_held.push(data.item_index["zaroffs_contract"]);
        state.board.slots[0] = Some(1);
        state.board.slots[1] = Some(0);
        state.board.slots[2] = Some(2);
        let income = state.resolve_spin_effects();
        // Bounty 1 + farmer 2 + thief -1 + farmer contract 25 + thief
        // contract 20 + thief on-destroy 4.
        assert_eq!(income, 51, "bounty + farmer + thief values and payouts");
    }

    #[test]
    fn cursed_katana_makes_ninjas_scale_with_ninja_count() {
        let data = data();
        let mut state = RunState::new(&data, 43);
        let ninja = data.symbol_id("ninja").expect("ninja");
        state.deck.clear();
        for _ in 0..2 {
            state
                .deck
                .push(Symbol::new(ninja, data.symbols[ninja].base_value));
        }
        state.items_held.push(data.item_index["cursed_katana"]);
        state.board.slots[0] = Some(0);
        state.board.slots[1] = Some(1);
        let income = state.resolve_spin_effects();
        assert_eq!(income, 6, "two ninjas with one katana: 3 each");
    }

    #[test]
    fn recycling_essence_arrives_as_reroll_tokens() {
        let data = data();
        let mut state = RunState::new(&data, 39);
        let coin = data.symbol_id("coin").expect("coin");
        state.deck.clear();
        for _ in 0..20 {
            state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        }
        state
            .essences_held
            .push(data.essence_index["recycling_essence"]);
        state.spins_left = 10;
        state.spin();
        assert_eq!(state.reroll_tokens, 5);
        assert!(!state.has_essence("recycling_essence"));
    }

    #[test]
    fn tax_evasion_essence_covers_a_negative_symbol_once() {
        let data = data();
        let mut state = RunState::new(&data, 40);
        let magpie = data.symbol_id("magpie").expect("magpie");
        state.deck.clear();
        state.deck.push(Symbol::new(magpie, data.symbols[magpie].base_value));
        state
            .essences_held
            .push(data.essence_index["tax_evasion_essence"]);
        state.spins_left = 10;
        let outcome = state.spin();
        assert_eq!(outcome.income, 24, "magpie's -1 plus the essence's 25");
        assert!(!state.has_essence("tax_evasion_essence"));
    }

    #[test]
    fn looting_glove_essence_boosts_a_box_once() {
        let data = data();
        let mut state = RunState::new(&data, 41);
        let key = data.symbol_id("key").expect("key");
        let lockbox = data.symbol_id("lockbox").expect("lockbox");
        state.deck.clear();
        state.deck.push(Symbol::new(key, data.symbols[key].base_value));
        state
            .deck
            .push(Symbol::new(lockbox, data.symbols[lockbox].base_value));
        state
            .essences_held
            .push(data.essence_index["looting_glove_essence"]);
        state.board.slots[0] = Some(0);
        state.board.slots[1] = Some(1);
        let income = state.resolve_spin_effects();
        assert_eq!(income, 55, "key 1 + lockbox 1 + 15 x 3.5");
        assert!(!state.has_essence("looting_glove_essence"));
    }

    #[test]
    fn yellow_pepper_essence_pays_when_the_board_is_full() {
        let data = data();
        let mut state = RunState::new(&data, 38);
        let coin = data.symbol_id("coin").expect("coin");
        state.deck.clear();
        for _ in 0..20 {
            state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        }
        state
            .essences_held
            .push(data.essence_index["yellow_pepper_essence"]);
        state.spins_left = 10;
        let outcome = state.spin();
        assert_eq!(outcome.income, 40, "20 coins + the essence's 20");
        assert!(!state.has_essence("yellow_pepper_essence"));
    }

    #[test]
    fn checkered_flag_essence_grows_slow_symbols_every_spin() {
        let data = data();
        let mut state = RunState::new(&data, 37);
        let owl = data.symbol_id("owl").expect("owl");
        let dud = data.symbol_id("dud").expect("dud");
        state.deck.clear();
        state.deck.push(Symbol::new(owl, data.symbols[owl].base_value));
        for _ in 0..4 {
            state.deck.push(Symbol::new(dud, 0));
        }
        state
            .essences_held
            .push(data.essence_index["checkered_flag_essence"]);
        state.spins_left = 10;
        let outcome = state.spin();
        assert_eq!(outcome.income, 2, "owl's 1 base plus its 1 growth");
        let essence = data.essence_index["checkered_flag_essence"];
        assert!(state.has_essence("checkered_flag_essence"));
        assert_eq!(state.essence_saved.get(&essence).copied().unwrap_or(0), 1);
    }

    #[test]
    fn conveyor_belts_multiply_spawn_chances() {
        let data = data();
        let mut state = RunState::new(&data, 36);
        let belt = data.item_index["conveyor_belt"];
        assert_eq!(state.spawn_multiplier(), 1.0);
        state.items_held.push(belt);
        assert_eq!(state.spawn_multiplier(), 2.0);
        state.items_held.push(belt);
        assert_eq!(state.spawn_multiplier(), 4.0);
    }

    #[test]
    fn white_pepper_essence_pays_on_multiples_of_three() {
        let data = data();
        let mut state = RunState::new(&data, 35);
        let coin = data.symbol_id("coin").expect("coin");
        let dud = data.symbol_id("dud").expect("dud");
        state.deck.clear();
        for _ in 0..3 {
            state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        }
        for _ in 0..5 {
            state.deck.push(Symbol::new(dud, 0));
        }
        state
            .essences_held
            .push(data.essence_index["white_pepper_essence"]);
        state.spins_left = 10;
        let outcome = state.spin();
        assert_eq!(outcome.income, 33, "three coins plus the essence's 30");
        assert!(!state.has_essence("white_pepper_essence"));
    }

    #[test]
    fn triple_coins_essence_permanently_buffs_coins() {
        let data = data();
        let mut state = RunState::new(&data, 33);
        let coin = data.symbol_id("coin").expect("coin");
        let dud = data.symbol_id("dud").expect("dud");
        state.deck.clear();
        for _ in 0..3 {
            state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        }
        for _ in 0..5 {
            state.deck.push(Symbol::new(dud, 0));
        }
        state.essences_held.push(data.essence_index["triple_coins_essence"]);
        state.spins_left = 10;
        let outcome = state.spin();
        assert_eq!(outcome.income, 9, "three coins at 1 + the essence's 2 bonus");
        assert!(!state.has_essence("triple_coins_essence"));
        assert!(state
            .deck
            .iter()
            .filter(|symbol| data.symbols[symbol.type_id as usize].name == "coin")
            .all(|symbol| symbol.current_value(&data) == 3));
    }

    #[test]
    fn nori_the_rabbit_essence_boosts_rabbits_and_fluff() {
        let data = data();
        let mut state = RunState::new(&data, 34);
        let rabbit = data.symbol_id("rabbit").expect("rabbit");
        let dud = data.symbol_id("dud").expect("dud");
        state.deck.clear();
        for _ in 0..4 {
            state
                .deck
                .push(Symbol::new(rabbit, data.symbols[rabbit].base_value));
        }
        for _ in 0..4 {
            state.deck.push(Symbol::new(dud, 0));
        }
        state
            .essences_held
            .push(data.essence_index["nori_the_rabbit_essence"]);
        state.spins_left = 10;
        state.spin();
        assert!(!state.has_essence("nori_the_rabbit_essence"));
        assert!(state
            .deck
            .iter()
            .filter(|symbol| data.symbols[symbol.type_id as usize].name == "rabbit")
            .all(|symbol| symbol.current_value(&data) == 2));
    }

    #[test]
    fn shattered_mirror_pays_out_every_seven_spins() {
        let data = data();
        let mut state = RunState::new(&data, 28);
        let coin = data.symbol_id("coin").expect("coin");
        state.deck.clear();
        for _ in 0..20 {
            state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        }
        let mirror = data.item_index["shattered_mirror"];
        state.items_held.push(mirror);
        state.item_saved.insert(mirror, 6);
        state.spins_left = 10;
        let outcome = state.spin();
        assert_eq!(outcome.income, 44, "20 coins + one 24-coin mirror payout");
        assert_eq!(state.item_saved.get(&mirror).copied().unwrap_or(0), 0);
    }

    #[test]
    fn shattered_mirror_removes_one_symbol_choice() {
        let data = data();
        let mut state = RunState::new(&data, 29);
        let mirror = data.item_index["shattered_mirror"];
        let plain = state.roll_symbol_reward();
        let plain_spin = state.roll_spin_reward();
        state.items_held.push(mirror);
        let reduced = state.roll_symbol_reward();
        let reduced_spin = state.roll_spin_reward();
        assert_eq!(plain.len(), 3);
        assert_eq!(reduced.len(), 2);
        // 普通旋后奖励也要扣：真机持有碎镜时不会给 3 个候选。
        assert_eq!(plain_spin.len(), 3);
        assert_eq!(reduced_spin.len(), 2);
    }

    #[test]
    fn sunglasses_generate_removal_tokens_once_the_stash_is_large() {
        let data = data();
        let mut state = RunState::new(&data, 30);
        let coin = data.symbol_id("coin").expect("coin");
        state.deck.clear();
        for _ in 0..20 {
            state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        }
        state.items_held.push(data.item_index["sunglasses"]);
        state.removal_tokens = 3;
        state.spins_left = 10;
        state.spin();
        assert_eq!(state.removal_tokens, 4);
    }

    #[test]
    fn dishwasher_turns_twelve_spins_into_an_essence_token() {
        let data = data();
        let mut state = RunState::new(&data, 31);
        let coin = data.symbol_id("coin").expect("coin");
        state.deck.clear();
        for _ in 0..20 {
            state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        }
        let dishwasher = data.item_index["dishwasher"];
        state.items_held.push(dishwasher);
        state.item_saved.insert(dishwasher, 11);
        state.spins_left = 10;
        state.spin();
        assert_eq!(state.essence_tokens, 1);
        assert_eq!(state.item_saved.get(&dishwasher).copied().unwrap_or(0), 0);
    }

    #[test]
    fn looting_glove_raises_the_box_payout() {
        let data = data();
        let mut state = RunState::new(&data, 32);
        let key = data.symbol_id("key").expect("key");
        let lockbox = data.symbol_id("lockbox").expect("lockbox");
        state.deck.clear();
        state.deck.push(Symbol::new(key, data.symbols[key].base_value));
        state
            .deck
            .push(Symbol::new(lockbox, data.symbols[lockbox].base_value));
        state.items_held.push(data.item_index["looting_glove"]);
        state.board.slots[0] = Some(0);
        state.board.slots[1] = Some(1);
        let income = state.resolve_spin_effects();
        assert_eq!(income, 25, "key 1 + lockbox 1 + 15 x 1.5");
        assert!(state
            .deck
            .iter()
            .all(|symbol| data.symbols[symbol.type_id as usize].name != "lockbox"));
    }

    #[test]
    fn oil_can_doubles_the_best_column_after_five_spins() {
        let data = data();
        let mut state = RunState::new(&data, 26);
        let coin = data.symbol_id("coin").expect("coin");
        state.deck.clear();
        for _ in 0..20 {
            state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        }
        let oil_can = data.item_index["oil_can"];
        state.items_held.push(oil_can);
        state.item_saved.insert(oil_can, 4);
        state.spins_left = 10;
        let outcome = state.spin();
        assert_eq!(outcome.income, 24, "20 coins + one doubled column of four");
        assert_eq!(state.item_saved.get(&oil_can).copied().unwrap_or(0), 0);
    }

    #[test]
    fn oil_can_charges_one_spin_at_a_time() {
        let data = data();
        let mut state = RunState::new(&data, 27);
        let coin = data.symbol_id("coin").expect("coin");
        state.deck.clear();
        for _ in 0..20 {
            state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        }
        let oil_can = data.item_index["oil_can"];
        state.items_held.push(oil_can);
        state.spins_left = 10;
        let outcome = state.spin();
        assert_eq!(outcome.income, 20, "no column doubles before the charge fills");
        assert_eq!(state.item_saved.get(&oil_can).copied().unwrap_or(0), 1);
    }

    #[test]
    fn turtle_and_rabbit_essence_buffs_both_permanently() {
        let data = data();
        let mut state = RunState::new(&data, 70);
        let rabbit = data.symbol_id("rabbit").expect("rabbit");
        let turtle = data.symbol_id("turtle").expect("turtle");
        state.deck.clear();
        state.deck.push(Symbol::new(rabbit, data.symbols[rabbit].base_value));
        state.deck.push(Symbol::new(turtle, data.symbols[turtle].base_value));
        state.board.slots[0] = Some(0);
        state.board.slots[1] = Some(1);
        state
            .essences_held
            .push(data.essence_index["turtle_and_rabbit_essence"]);
        let income = state.resolve_spin_effects();
        assert!(!state.has_essence("turtle_and_rabbit_essence"));
        assert_eq!(
            state.permanent_name_bonus.get("rabbit").copied(),
            Some(2)
        );
        assert_eq!(
            state.permanent_name_bonus.get("turtle").copied(),
            Some(2)
        );
        // rabbit 1+2, turtle 0+2
        assert_eq!(income, 5);
    }

    #[test]
    fn ninja_and_mouse_essence_buffs_both_permanently() {
        let data = data();
        let mut state = RunState::new(&data, 71);
        let ninja = data.symbol_id("ninja").expect("ninja");
        let mouse = data.symbol_id("mouse").expect("mouse");
        state.deck.clear();
        state.deck.push(Symbol::new(ninja, data.symbols[ninja].base_value));
        state.deck.push(Symbol::new(mouse, data.symbols[mouse].base_value));
        state.board.slots[0] = Some(0);
        state.board.slots[1] = Some(1);
        state
            .essences_held
            .push(data.essence_index["ninja_and_mouse_essence"]);
        let income = state.resolve_spin_effects();
        assert!(!state.has_essence("ninja_and_mouse_essence"));
        // ninja (2+2, no other ninja) + mouse (1+2)
        assert_eq!(income, 7);
    }

    #[test]
    fn rain_cloud_essence_buffs_rain_and_flower() {
        let data = data();
        let mut state = RunState::new(&data, 72);
        let rain = data.symbol_id("rain").expect("rain");
        let flower = data.symbol_id("flower").expect("flower");
        state.deck.clear();
        state.deck.push(Symbol::new(rain, data.symbols[rain].base_value));
        state.deck.push(Symbol::new(flower, data.symbols[flower].base_value));
        state.board.slots[0] = Some(0);
        state.board.slots[1] = Some(1);
        state
            .essences_held
            .push(data.essence_index["rain_cloud_essence"]);
        let income = state.resolve_spin_effects();
        assert!(!state.has_essence("rain_cloud_essence"));
        // rain 2+1, flower (1+1)*2 from the rain multiplier
        assert_eq!(income, 7);
    }

    #[test]
    fn cursed_katana_essence_needs_two_ninjas() {
        let data = data();
        let mut state = RunState::new(&data, 73);
        let ninja = data.symbol_id("ninja").expect("ninja");
        state.deck.clear();
        state.deck.push(Symbol::new(ninja, data.symbols[ninja].base_value));
        state.board.slots[0] = Some(0);
        state
            .essences_held
            .push(data.essence_index["cursed_katana_essence"]);
        state.resolve_spin_effects();
        assert!(state.has_essence("cursed_katana_essence"));
        state
            .deck
            .push(Symbol::new(ninja, data.symbols[ninja].base_value));
        state.board.slots[1] = Some(1);
        let income = state.resolve_spin_effects();
        assert!(!state.has_essence("cursed_katana_essence"));
        // each ninja is 2 base + 3 permanent bonus - 1 ninja penalty
        assert_eq!(income, 8);
    }

    #[test]
    fn suit_symbols_bonus_each_other_and_reward_three_suits() {
        let data = data();
        let mut state = RunState::new(&data, 104);
        let clubs = data.symbol_id("clubs").expect("clubs");
        let spades = data.symbol_id("spades").expect("spades");
        state.deck.clear();
        state.deck.push(Symbol::new(clubs, 1));
        state.deck.push(Symbol::new(clubs, 1));
        state.deck.push(Symbol::new(spades, 1));
        state.board.slots[0] = Some(0);
        state.board.slots[1] = Some(1);
        state.board.slots[2] = Some(2);
        // Three suits: each gains +1; the middle suit receives two neighbour
        // bonuses, the ends one each.
        assert_eq!(state.resolve_spin_effects(), 10);
    }

    #[test]
    fn card_shark_turns_an_adjacent_suit_into_a_wildcard() {
        let data = data();
        let mut state = RunState::new(&data, 105);
        let shark = data.symbol_id("card_shark").expect("card shark");
        let clubs = data.symbol_id("clubs").expect("clubs");
        let gem = data.symbol_id("diamond").expect("diamond symbol");
        state.deck.clear();
        state.deck.push(Symbol::new(shark, 0));
        state.deck.push(Symbol::new(clubs, 1));
        state.deck.push(Symbol::with_current_value(gem, 10, &data));
        state.board.slots[0] = Some(0);
        state.board.slots[1] = Some(1);
        state.board.slots[2] = Some(2);
        // The suit is worth as much as the 10-value neighbour.
        assert!(state.resolve_spin_effects() >= 14);
    }

    #[test]
    fn wildcard_values_propagate_through_a_chain() {
        let data = data();
        let mut state = RunState::new(&data, 107);
        let diamond = data.symbol_id("diamond").expect("diamond");
        let wildcard = data.symbol_id("wildcard").expect("wildcard");
        state.deck.clear();
        state.deck.push(Symbol::new(diamond, data.symbols[diamond].base_value));
        state.deck.push(Symbol::new(wildcard, 0));
        state.deck.push(Symbol::new(wildcard, 0));
        state.board.slots[0] = Some(0);
        state.board.slots[1] = Some(1);
        state.board.slots[7] = Some(2);
        // The lone diamond is worth 5 (its own symbol is not counted); both
        // wildcards copy it even though only the first one touches it.
        assert_eq!(state.resolve_spin_effects(), 15);
    }

    #[test]
    fn fifth_ace_essence_buffs_all_suits() {
        let data = data();
        let mut state = RunState::new(&data, 74);
        let mut deck = Vec::new();
        for name in ["clubs", "spades", "diamonds", "hearts"] {
            let id = data.symbol_id(name).expect(name);
            deck.push(Symbol::new(id, data.symbols[id].base_value));
        }
        state.deck = deck;
        for slot in 0..4 {
            state.board.slots[slot] = Some(slot);
        }
        state
            .essences_held
            .push(data.essence_index["fifth_ace_essence"]);
        state.resolve_spin_effects();
        assert!(!state.has_essence("fifth_ace_essence"));
        assert_eq!(
            state.permanent_group_bonus.get("suit").copied(),
            Some(1)
        );
    }

    #[test]
    fn copycat_essence_needs_a_clump_of_five_cats() {
        let data = data();
        let mut state = RunState::new(&data, 75);
        let cat = data.symbol_id("cat").expect("cat");
        state.deck.clear();
        for _ in 0..5 {
            state.deck.push(Symbol::new(cat, data.symbols[cat].base_value));
        }
        for (deck_index, slot) in [(0, 0), (1, 1), (2, 2), (3, 5), (4, 6)] {
            state.board.slots[slot] = Some(deck_index);
        }
        state
            .essences_held
            .push(data.essence_index["copycat_essence"]);
        state.resolve_spin_effects();
        assert!(!state.has_essence("copycat_essence"));
        assert!(state.copycat_active);
        assert_eq!(
            state.permanent_name_mult.get("cat").copied(),
            Some(1.5)
        );
    }

    #[test]
    fn clear_sky_essence_doubles_sun_and_moon_and_reaches_whole_board() {
        let data = data();
        let mut state = RunState::new(&data, 76);
        let sun = data.symbol_id("sun").expect("sun");
        let moon = data.symbol_id("moon").expect("moon");
        state.deck.clear();
        state.deck.push(Symbol::new(sun, data.symbols[sun].base_value));
        state.deck.push(Symbol::new(moon, data.symbols[moon].base_value));
        state.board.slots[0] = Some(0);
        state.board.slots[1] = Some(1);
        state
            .essences_held
            .push(data.essence_index["clear_sky_essence"]);
        let income = state.resolve_spin_effects();
        assert!(!state.has_essence("clear_sky_essence"));
        assert!(state.clear_sky_active);
        assert_eq!(income, 12);
    }

    #[test]
    fn mining_pick_essence_pays_after_five_miners_die() {
        let data = data();
        let mut state = RunState::new(&data, 77);
        let ore = data.symbol_id("ore").expect("ore");
        for _ in 0..5 {
            state.destroyed_history.push(ore as u16);
        }
        state
            .essences_held
            .push(data.essence_index["mining_pick_essence"]);
        assert_eq!(state.resolve_spin_effects(), 50);
        assert!(!state.has_essence("mining_pick_essence"));
    }

    #[test]
    fn mobius_strip_essence_multiplies_boxes_after_seven_deaths() {
        let data = data();
        let mut state = RunState::new(&data, 78);
        let box_id = data.symbol_id("lockbox").expect("lockbox");
        for _ in 0..7 {
            state.destroyed_history.push(box_id as u16);
        }
        state
            .essences_held
            .push(data.essence_index["mobius_strip_essence"]);
        state.resolve_spin_effects();
        assert!(!state.has_essence("mobius_strip_essence"));
        assert_eq!(
            state.permanent_group_mult.get("box").copied(),
            Some(1.5)
        );
    }

    #[test]
    fn void_party_essence_multiplies_void_symbols() {
        let data = data();
        let mut state = RunState::new(&data, 79);
        let void_id = data.symbol_id("void_creature").expect("void");
        for _ in 0..5 {
            state.destroyed_history.push(void_id as u16);
        }
        state
            .essences_held
            .push(data.essence_index["void_party_essence"]);
        state.resolve_spin_effects();
        assert!(!state.has_essence("void_party_essence"));
        assert_eq!(
            state.permanent_group_mult.get("void").copied(),
            Some(1.5)
        );
    }

    #[test]
    fn void_portal_essence_pays_per_eight_destroyed_symbols() {
        let data = data();
        let mut state = RunState::new(&data, 80);
        let ore = data.symbol_id("ore").expect("ore");
        for _ in 0..7 {
            state.destroyed_history.push(ore as u16);
        }
        let dud = data.symbol_id("dud").expect("dud");
        state.deck.clear();
        let mut symbol = Symbol::new(dud, 0);
        symbol.times_displayed = 33;
        state.deck.push(symbol);
        state.board.slots[0] = Some(0);
        state
            .essences_held
            .push(data.essence_index["void_portal_essence"]);
        let income = state.resolve_spin_effects();
        assert!(!state.has_essence("void_portal_essence"));
        assert_eq!(income, 8);
    }

    #[test]
    fn credit_card_essence_adds_27_symbol_choices_before_dying() {
        let data = data();
        let mut state = RunState::new(&data, 81);
        state
            .essences_held
            .push(data.essence_index["credit_card_essence"]);
        state.resolve_spin_effects();
        assert!(!state.has_essence("credit_card_essence"));
        assert_eq!(state.roll_symbol_reward().len(), 30);
    }

    fn trigger_permanent_essence<'a>(
        data: &'a GameData,
        essence: &str,
        deck: &[&str],
        destroyed_flags: &[bool],
        seed: u64,
    ) -> (RunState<'a>, i32) {
        let mut state = RunState::new(data, seed);
        state.deck.clear();
        for name in deck {
            let id = data.symbol_id(name).expect(name);
            state.deck.push(Symbol::new(id, data.symbols[id].base_value));
        }
        state
            .essences_held
            .push(data.essence_index[essence]);
        let entries: Vec<(usize, usize, usize)> = deck
            .iter()
            .enumerate()
            .map(|(index, _)| (index, index % BOARD_COLS, index / BOARD_COLS))
            .collect();
        let payout = state.tick_permanent_essences(
            &entries,
            destroyed_flags,
            destroyed_flags.iter().filter(|flag| **flag).count() as i32,
        );
        (state, payout)
    }

    #[test]
    fn dark_humor_essence_buffs_funny_after_a_comedian_dies() {
        let data = data();
        let (state, _) = trigger_permanent_essence(
            &data,
            "dark_humor_essence",
            &["comedian", "banana_peel"],
            &[true, false],
            82,
        );
        assert!(!state.has_essence("dark_humor_essence"));
        assert_eq!(state.permanent_group_bonus.get("funny").copied(), Some(2));
    }

    #[test]
    fn dwarven_anvil_essence_multiplies_dwarves() {
        let data = data();
        let (state, _) = trigger_permanent_essence(
            &data,
            "dwarven_anvil_essence",
            &["ore", "dwarf"],
            &[true, false],
            83,
        );
        assert_eq!(
            state.permanent_name_mult.get("dwarf").copied(),
            Some(1.5)
        );
    }

    #[test]
    fn jackolantern_essence_multiplies_halloween() {
        let data = data();
        let (state, _) = trigger_permanent_essence(
            &data,
            "jackolantern_essence",
            &["candy", "toddler"],
            &[true, false],
            84,
        );
        assert_eq!(
            state.permanent_group_mult.get("halloween").copied(),
            Some(1.5)
        );
    }

    #[test]
    fn maxwell_essence_multiplies_bears() {
        let data = data();
        let (state, _) = trigger_permanent_essence(
            &data,
            "maxwell_the_bear_essence",
            &["honey", "bear"],
            &[true, false],
            85,
        );
        assert_eq!(state.permanent_name_mult.get("bear").copied(), Some(1.5));
    }

    #[test]
    fn oswald_essence_multiplies_monkeys() {
        let data = data();
        let (state, _) = trigger_permanent_essence(
            &data,
            "oswald_the_monkey_essence",
            &["banana", "monkey"],
            &[true, false],
            86,
        );
        assert_eq!(state.permanent_name_mult.get("monkey").copied(), Some(2.0));
    }

    #[test]
    fn wanted_poster_essence_multiplies_bounty_hunters() {
        let data = data();
        let (state, _) = trigger_permanent_essence(
            &data,
            "wanted_poster_essence",
            &["thief", "bounty_hunter"],
            &[true, false],
            87,
        );
        assert_eq!(
            state
                .permanent_name_mult
                .get("bounty_hunter")
                .copied(),
            Some(2.5)
        );
    }

    #[test]
    fn zaroffs_contract_essence_pays_for_a_human_kill() {
        let data = data();
        let (state, payout) = trigger_permanent_essence(
            &data,
            "zaroffs_contract_essence",
            &["thief", "general_zaroff"],
            &[true, false],
            88,
        );
        assert_eq!(payout, 50);
        assert!(!state.has_essence("zaroffs_contract_essence"));
    }

    #[test]
    fn grave_robber_essence_doubles_tomb_spirits() {
        let data = data();
        let (state, _) = trigger_permanent_essence(
            &data,
            "grave_robber_essence",
            &["tomb", "ore"],
            &[true, false],
            89,
        );
        assert_eq!(state.grave_robber_mult, 2);
        assert!(!state.has_essence("grave_robber_essence"));
    }

    #[test]
    fn shrine_essence_activates_after_four_spirits_die() {
        let data = data();
        let mut state = RunState::new(&data, 90);
        let spirit = data.symbol_id("spirit").expect("spirit");
        for _ in 0..4 {
            state.destroyed_history.push(spirit as u16);
        }
        state.essences_held.push(data.essence_index["shrine_essence"]);
        let entries: Vec<(usize, usize, usize)> = Vec::new();
        let destroyed: Vec<bool> = Vec::new();
        state.tick_permanent_essences(&entries, &destroyed, 0);
        assert!(state.shrine_active);
        assert!(!state.has_essence("shrine_essence"));
    }

    #[test]
    fn frying_pan_essence_fries_eggs_next_to_omelettes() {
        let data = data();
        let mut state = RunState::new(&data, 91);
        let egg = data.symbol_id("egg").expect("egg");
        let omelette = data.symbol_id("omelette").expect("omelette");
        state.deck.clear();
        state.deck.push(Symbol::new(egg, data.symbols[egg].base_value));
        state.deck.push(Symbol::new(omelette, data.symbols[omelette].base_value));
        state.board.slots[0] = Some(0);
        state.board.slots[1] = Some(1);
        state
            .essences_held
            .push(data.essence_index["frying_pan_essence"]);
        let income = state.resolve_spin_effects();
        assert!(!state.has_essence("frying_pan_essence"));
        assert_eq!(
            state.permanent_name_mult.get("omelette").copied(),
            Some(3.0)
        );
        // The egg pays 1; the adjacent omelette gets the egg's +2 reverse
        // bonus, so (3 + 2) x 3 = 15. The pan-spawned omelette lands off-screen
        // in this artificial board, so it pays nothing.
        assert_eq!(income, 16);
    }

    #[test]
    fn fine_print_21_blocks_frying_pan() {
        let data = data();
        let mut state = RunState::new(&data, 91);
        let egg = data.symbol_id("egg").expect("egg");
        let omelette = data.symbol_id("omelette").expect("omelette");
        state.deck.clear();
        state.deck.push(Symbol::new(egg, data.symbols[egg].base_value));
        state.deck.push(Symbol::new(omelette, data.symbols[omelette].base_value));
        state.board.slots[0] = Some(0);
        state.board.slots[1] = Some(1);
        state
            .essences_held
            .push(data.essence_index["frying_pan_essence"]);
        state.fine_prints.push(21);
        let income = state.resolve_spin_effects();
        assert!(state.has_essence("frying_pan_essence"));
        assert_eq!(income, 6);
    }

    #[test]
    fn slow_fine_print_only_affects_its_dynamic_icon() {
        let data = data();
        let mut state = RunState::new(&data, 22);
        let snail = data.symbol_id("snail").expect("snail");
        let turtle = data.symbol_id("turtle").expect("turtle");
        state.deck.clear();
        state.deck.push(Symbol::new(snail, 0));
        state.deck.push(Symbol::new(turtle, 0));
        state.deck[0].times_displayed = 2;
        state.deck[1].times_displayed = 2;
        state.board.slots[0] = Some(0);
        state.board.slots[1] = Some(1);
        state.fine_prints.push(12);
        state.fine_print_icons.insert(12, "snail".to_string());
        let income = state.resolve_spin_effects();
        assert_eq!(income, 4);
    }

    #[test]
    fn hex_of_destruction_destroys_adjacent_symbols() {
        let data = data();
        let mut state = RunState::new(&data, 21);
        let hex = data.symbol_id("hex_of_destruction").expect("hex");
        let coin = data.symbol_id("coin").expect("coin");
        state.deck.clear();
        state.deck.push(Symbol::new(hex, 0));
        for _ in 0..5 {
            state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        }
        for (slot, index) in state.board.slots.iter_mut().enumerate().take(6) {
            *index = Some(slot);
        }
        state.fine_prints.push(8);
        state.fine_print_icons.insert(8, "hex_of_destruction".to_string());
        for _ in 0..20 {
            state.resolve_spin_effects();
            if !state.destroyed_history.is_empty() {
                break;
            }
        }
        assert!(!state.destroyed_history.is_empty());
    }

    #[test]
    fn midas_bomb_destroys_neighbours_and_pays_seven_times() {
        let data = data();
        let mut state = RunState::new(&data, 106);
        let bomb = data.symbol_id("midas_bomb").expect("midas bomb");
        let coin = data.symbol_id("coin").expect("coin");
        let cat = data.symbol_id("cat").expect("cat");
        state.deck.clear();
        state.deck.push(Symbol::new(bomb, 0));
        state.deck.push(Symbol::new(coin, 1));
        state.deck.push(Symbol::new(cat, 1));
        state.board.slots[0] = Some(0);
        state.board.slots[1] = Some(1);
        state.board.slots[12] = Some(2);
        let income = state.resolve_spin_effects();
        // Bomb 0 + destroyed coin 1 + destroyed cat 1 + 7 coin payout.
        assert_eq!(income, 9);
        assert_eq!(state.deck.len(), 1);
    }

    #[test]
    fn unremovable_symbols_follow_fine_prints() {
        let data = data();
        let mut state = RunState::new(&data, 23);
        let coin_pos = state
            .deck
            .iter()
            .position(|symbol| data.symbols[symbol.type_id as usize].name == "coin")
            .expect("coin");
        let dud_pos = state
            .deck
            .iter()
            .position(|symbol| data.symbols[symbol.type_id as usize].name == "dud")
            .expect("dud");
        assert!(state.can_remove_symbol(coin_pos));
        assert!(!state.can_remove_symbol(dud_pos));
        state.fine_prints.push(20);
        assert!(!state.can_remove_symbol(coin_pos));

        let egg = data.symbol_id("egg").expect("egg");
        state
            .deck
            .push(Symbol::new(egg, data.symbols[egg].base_value));
        let egg_pos = state.deck.len() - 1;
        assert!(state.can_remove_symbol(egg_pos));
        state.fine_prints.push(21);
        assert!(!state.can_remove_symbol(egg_pos));

        let stone = data.symbol_id("void_stone").expect("void stone");
        let fruit = data.symbol_id("void_fruit").expect("void fruit");
        state.deck.push(Symbol::new(stone, 0));
        state.deck.push(Symbol::new(fruit, 0));
        let stone_pos = state.deck.len() - 2;
        let fruit_pos = state.deck.len() - 1;
        state.fine_prints.push(36);
        state
            .fine_print_icons
            .insert(36, "void_stone".to_string());
        assert!(!state.can_remove_symbol(stone_pos));
        assert!(state.can_remove_symbol(fruit_pos));
    }

    #[test]
    fn reroll_essence_makes_dice_max_and_expires() {
        let data = data();
        let mut state = RunState::new(&data, 24);
        let d3 = data.symbol_id("d3").expect("d3");
        state.deck.clear();
        state.deck.push(Symbol::new(d3, 0));
        state.board.slots[0] = Some(0);
        state.essences_held.push(data.essence_index["reroll_essence"]);
        assert_eq!(state.resolve_spin_effects(), 3);
        for _ in 1..10 {
            state.resolve_spin_effects();
        }
        assert!(!state.has_essence("reroll_essence"));
    }

    #[test]
    fn conveyor_belt_essence_double_spawns_after_five() {
        let data = data();
        let mut state = RunState::new(&data, 25);
        state
            .essences_held
            .push(data.essence_index["conveyor_belt_essence"]);
        for _ in 0..4 {
            state.note_spawner_spawns(1);
        }
        assert!(state.has_essence("conveyor_belt_essence"));
        assert_eq!(state.spawn_multiplier(), 1.0);
        state.note_spawner_spawns(1);
        assert!(!state.has_essence("conveyor_belt_essence"));
        assert_eq!(state.spawn_multiplier(), 2.0);
    }

    #[test]
    fn chicken_coop_essence_triggers_on_same_spin_growth_chain() {
        let data = data();
        let mut state = RunState::new(&data, 26);
        let egg = data.symbol_id("egg").expect("egg");
        let chick = data.symbol_id("chick").expect("chick");
        state.deck.clear();
        for _ in 0..10 {
            state.deck.push(Symbol::new(egg, data.symbols[egg].base_value));
        }
        for _ in 0..10 {
            state.deck.push(Symbol::new(chick, data.symbols[chick].base_value));
        }
        for slot in 0..20 {
            state.board.slots[slot] = Some(slot);
        }
        state
            .essences_held
            .push(data.essence_index["chicken_coop_essence"]);
        for _ in 0..200 {
            state.resolve_spin_effects();
            if !state.has_essence("chicken_coop_essence") {
                break;
            }
        }
        assert!(!state.has_essence("chicken_coop_essence"));
        assert!(state
            .permanent_group_bonus
            .get("chickenstuff")
            .copied()
            .unwrap_or(0)
            >= 4);
    }

    #[test]
    fn oil_can_essence_respins_and_expires() {
        let data = data();
        let mut state = RunState::new(&data, 27);
        let coin = data.symbol_id("coin").expect("coin");
        state.deck.clear();
        for _ in 0..5 {
            state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        }
        for slot in 0..5 {
            state.board.slots[slot] = Some(slot);
        }
        state
            .essences_held
            .push(data.essence_index["oil_can_essence"]);
        for _ in 0..6 {
            state.apply_oil_can_essence();
        }
        assert!(state.has_essence("oil_can_essence"));
        assert!(state.spin_column_mult.iter().any(|value| *value == 2.0));
        state.apply_oil_can_essence();
        assert!(!state.has_essence("oil_can_essence"));
    }

    #[test]
    fn swapping_device_essence_doubles_top_symbols_and_expires() {
        let data = data();
        let mut state = RunState::new(&data, 28);
        let coin = data.symbol_id("coin").expect("coin");
        state.deck.clear();
        for _ in 0..3 {
            state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        }
        for slot in 0..3 {
            state.board.slots[slot] = Some(slot);
        }
        state
            .essences_held
            .push(data.essence_index["swapping_device_essence"]);
        assert_eq!(state.resolve_spin_effects(), 5);
        for _ in 1..7 {
            state.resolve_spin_effects();
        }
        assert!(!state.has_essence("swapping_device_essence"));
    }

    #[test]
    fn chili_powder_essence_adds_a_pepper_after_rent() {
        let data = data();
        let mut state = RunState::new(&data, 29);
        state.deck.clear();
        state.coins = 100;
        state.spins_left = 1;
        state
            .essences_held
            .push(data.essence_index["chili_powder_essence"]);
        let outcome = state.spin();
        assert!(outcome.rent_paid);
        assert!(!state.has_essence("chili_powder_essence"));
        assert!(state
            .items_held
            .iter()
            .any(|id| data.items[*id].groups.iter().any(|group| group == "pepper")));
    }

    #[test]
    fn comfy_pillow_essence_skips_two_spins_for_a_very_rare_item() {
        let data = data();
        let mut state = RunState::new(&data, 30);
        state.deck.clear();
        state.coins = 100;
        state.spins_left = 2;
        state
            .essences_held
            .push(data.essence_index["comfy_pillow_essence"]);
        let outcome = state.spin();
        assert!(outcome.rent_paid);
        assert_eq!(outcome.income, 0);
        assert!(!state.has_essence("comfy_pillow_essence"));
        state.choose_symbol(0);
        match &state.phase {
            Phase::ItemReward { offer } => assert!(offer
                .iter()
                .any(|id| data.items[*id].rarity == "very_rare")),
            other => panic!("expected item reward, got {other:?}"),
        }
    }

    #[test]
    fn comfy_pillow_item_skips_the_last_spin_for_a_rare_item() {
        let data = data();
        let mut state = RunState::new(&data, 31);
        state.deck.clear();
        state.coins = 100;
        state.spins_left = 1;
        state.items_held.push(data.item_index["comfy_pillow"]);
        let outcome = state.spin();
        assert!(outcome.rent_paid);
        assert_eq!(outcome.income, 0);
        assert!(state.has_item("comfy_pillow"));
        state.choose_symbol(0);
        match &state.phase {
            Phase::ItemReward { offer } => assert!(offer.iter().any(|id| {
                matches!(data.items[*id].rarity.as_str(), "rare" | "very_rare")
            })),
            other => panic!("expected item reward, got {other:?}"),
        }
    }

    #[test]
    fn swapping_device_item_moves_a_receiver_next_to_its_source() {
        let data = data();
        let mut state = RunState::new(&data, 103);
        let dog = data.symbol_id("dog").expect("dog");
        let coin = data.symbol_id("coin").expect("coin");
        let receiver = data.symbol_id("archaeologist").expect("archaeologist");
        state.deck.clear();
        state.deck.push(Symbol::new(dog, 0));
        state.deck.push(Symbol::new(coin, 0));
        state.deck.push(Symbol::new(receiver, 0));
        state.board.slots[0] = Some(0);
        state.board.slots[1] = Some(1);
        state.board.slots[12] = Some(2);
        state.items_held.push(data.item_index["swapping_device"]);
        state.apply_swapping_device();
        assert_eq!(state.board.slots[1], Some(2));
        assert_eq!(state.board.slots[12], Some(1));
    }

    #[test]
    fn rusty_gear_essence_multiplies_an_adjacent_triple() {
        let data = data();
        let mut state = RunState::new(&data, 92);
        let coin = data.symbol_id("coin").expect("coin");
        state.deck.clear();
        for _ in 0..3 {
            state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        }
        for slot in 0..3 {
            state.board.slots[slot] = Some(slot);
        }
        state
            .essences_held
            .push(data.essence_index["rusty_gear_essence"]);
        let income = state.resolve_spin_effects();
        assert!(!state.has_essence("rusty_gear_essence"));
        assert_eq!(
            state.permanent_name_mult.get("coin").copied(),
            Some(1.5)
        );
        assert_eq!(income, 6);
    }

    #[test]
    fn guillotine_essence_ends_the_run_at_a_billion_coins() {
        let data = data();
        let mut state = RunState::new(&data, 93);
        state.coins = 1_000_000_000;
        state
            .essences_held
            .push(data.essence_index["guillotine_essence"]);
        state.spin();
        assert!(state.done());
        assert!(!state.won);
    }

    #[test]
    fn coffee_essence_buys_extra_spins_instead_of_ending_the_run() {
        let data = data();
        let mut state = RunState::new(&data, 23);
        state.spins_left = 1;
        state.coins = 0;
        state.essences_held.push(data.essence_index["coffee_essence"]);
        state.spin();
        assert!(!state.done());
        assert!(state.spins_left > 0);
        assert!(!state.has_essence("coffee_essence"));
    }

    #[test]
    fn lunchbox_and_adoption_papers_add_symbols_when_used() {
        let data = data();
        let mut state = RunState::new(&data, 94);
        let lunchbox = data.item_index["lunchbox"];
        let before = state.deck.len();
        state.grant_item(lunchbox);
        assert!(state.items_held.contains(&lunchbox));
        assert_eq!(state.deck.len(), before);
        state.use_item(lunchbox);
        assert_eq!(state.deck.len(), before + 3);
        assert!(state.deck[before..]
            .iter()
            .all(|symbol| data.symbols[symbol.type_id as usize]
                .groups
                .iter()
                .any(|group| group == "food")));
        assert!(!state.items_held.contains(&lunchbox));

        let papers = data.item_index["adoption_papers"];
        let before = state.deck.len();
        state.grant_item(papers);
        assert!(state.items_held.contains(&papers));
        assert_eq!(state.deck.len(), before);
        state.use_item(papers);
        assert_eq!(state.deck.len(), before + 3);
        assert!(state.deck[before..]
            .iter()
            .all(|symbol| data.symbols[symbol.type_id as usize]
                .groups
                .iter()
                .any(|group| group == "animal")));
        assert!(!state.items_held.contains(&papers));
    }

    #[test]
    fn lunchbox_essence_pays_after_seven_food_symbols_are_added() {
        let data = data();
        let mut state = RunState::new(&data, 101);
        state.coins = 0;
        state
            .essences_held
            .push(data.essence_index["lunchbox_essence"]);
        state.add_symbols("cherry", 6);
        assert!(state.has_essence("lunchbox_essence"));
        assert_eq!(state.coins, 0);
        state.add_symbols("cherry", 1);
        assert!(!state.has_essence("lunchbox_essence"));
        assert_eq!(state.coins, 33);
    }

    #[test]
    fn adoption_papers_essence_pays_after_seven_animal_symbols_are_added() {
        let data = data();
        let mut state = RunState::new(&data, 102);
        state.coins = 0;
        state
            .essences_held
            .push(data.essence_index["adoption_papers_essence"]);
        state.add_symbols("rabbit", 6);
        assert!(state.has_essence("adoption_papers_essence"));
        state.add_symbols("rabbit", 1);
        assert!(!state.has_essence("adoption_papers_essence"));
        assert_eq!(state.coins, 77);
    }

    #[test]
    fn shedding_season_item_adds_fluff_when_rabbits_spin() {
        let data = data();
        let mut state = RunState::new(&data, 95);
        let rabbit = data.symbol_id("rabbit").expect("rabbit");
        state.deck.clear();
        state.deck.push(Symbol::new(rabbit, data.symbols[rabbit].base_value));
        state.board.slots[0] = Some(0);
        for _ in 0..100 {
            state.items_held.push(data.item_index["shedding_season"]);
        }
        state.resolve_spin_effects();
        let fluff = data.symbol_id("rabbit_fluff").expect("fluff");
        assert_eq!(
            state
                .deck
                .iter()
                .filter(|symbol| symbol.type_id as usize == fluff)
                .count(),
            1
        );
    }

    #[test]
    fn void_portal_item_pays_per_eight_destroyed_symbols() {
        let data = data();
        let mut state = RunState::new(&data, 96);
        let ore = data.symbol_id("ore").expect("ore");
        for _ in 0..16 {
            state.destroyed_history.push(ore as u16);
        }
        state.items_held.push(data.item_index["void_portal"]);
        assert_eq!(state.resolve_spin_effects(), 2);
    }

    #[test]
    fn void_party_item_reanimates_destroyed_void_symbols() {
        let data = data();
        let mut state = RunState::new(&data, 97);
        let void_id = data.symbol_id("void_creature").expect("void");
        let coin = data.symbol_id("coin").expect("coin");
        state.deck.clear();
        state.deck.push(Symbol::new(void_id, 0));
        for _ in 1..BOARD_SLOTS {
            state.deck.push(Symbol::new(coin, data.symbols[coin].base_value));
        }
        for slot in 0..BOARD_SLOTS {
            state.board.slots[slot] = Some(slot);
        }
        for _ in 0..40 {
            state.items_held.push(data.item_index["void_party"]);
        }
        state.resolve_spin_effects();
        assert!(state
            .deck
            .iter()
            .any(|symbol| symbol.type_id as usize == void_id));
        assert!(state.deck.len() > BOARD_SLOTS - 1);
    }

    #[test]
    fn mobius_strip_item_reanimates_destroyed_boxes() {
        let data = data();
        let mut state = RunState::new(&data, 98);
        let key = data.symbol_id("magic_key").expect("magic key");
        let box_id = data.symbol_id("lockbox").expect("lockbox");
        state.deck.clear();
        state.deck.push(Symbol::new(key, data.symbols[key].base_value));
        state.deck.push(Symbol::new(box_id, data.symbols[box_id].base_value));
        state.board.slots[6] = Some(0);
        state.board.slots[7] = Some(1);
        for _ in 0..40 {
            state.items_held.push(data.item_index["mobius_strip"]);
        }
        state.resolve_spin_effects();
        assert!(state
            .deck
            .iter()
            .any(|symbol| symbol.type_id as usize == box_id));
        assert!(state.deck.len() > 1);
    }

    #[test]
    fn frying_pan_item_fries_eggs_without_destroying_itself() {
        let data = data();
        let mut state = RunState::new(&data, 99);
        let egg = data.symbol_id("egg").expect("egg");
        let omelette = data.symbol_id("omelette").expect("omelette");
        state.deck.clear();
        state.deck.push(Symbol::new(egg, data.symbols[egg].base_value));
        state.deck.push(Symbol::new(omelette, data.symbols[omelette].base_value));
        state.board.slots[0] = Some(0);
        state.board.slots[1] = Some(1);
        state.items_held.push(data.item_index["frying_pan"]);
        // Fine print 13 suppresses the egg's own chance effect.
        state.fine_prints.push(13);
        state.resolve_spin_effects();
        assert!(state
            .deck
            .iter()
            .all(|symbol| symbol.type_id as usize != egg));
        assert_eq!(
            state
                .deck
                .iter()
                .filter(|symbol| symbol.type_id as usize == omelette)
                .count(),
            2
        );
        assert!(state.has_item("frying_pan"));
        assert!(state
            .permanent_name_mult
            .get("omelette")
            .copied()
            .unwrap_or(1.0)
            <= 1.0);
    }
}
