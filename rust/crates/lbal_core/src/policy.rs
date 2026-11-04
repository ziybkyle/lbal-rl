//! Port of the Python v13 marginal-value heuristic.
//!
//! The Python baseline estimates the marginal value of adding one symbol to
//! the current deck. Keeping the same model in Rust makes benchmark results
//! comparable and gives the later training stages a strong reference policy.

use crate::data::GameData;
use crate::state::Symbol;
use serde_json::Value;
use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::Path;

const CHESTS: [(&str, f64); 4] = [
    ("lockbox", 15.0),
    ("safe", 30.0),
    ("treasure_chest", 50.0),
    ("mega_chest", 100.0),
];

const GROWTH_CORES: [&str; 8] = [
    "diver",
    "archaeologist",
    "pirate",
    "dove",
    "mrs_fruit",
    "amethyst",
    "pear",
    "eldritch_beast",
];

#[derive(Debug, Clone, Default)]
pub struct MatchRule {
    pub all: bool,
    pub types: Vec<String>,
    pub groups: Vec<String>,
}

#[derive(Debug, Clone, Default)]
pub struct SymbolMeta {
    pub base: f64,
    pub raw_base: f64,
    pub groups: Vec<String>,
    pub rarity: String,
}

#[derive(Debug, Clone)]
pub struct PairRule {
    pub expected_eater: [f64; 4],
    pub expected_death: [f64; 4],
    pub eater_bonus: f64,
    pub death_bonus: f64,
}

impl Default for PairRule {
    fn default() -> Self {
        Self {
            expected_eater: [0.0; 4],
            expected_death: [0.0; 4],
            eater_bonus: 0.0,
            death_bonus: 0.0,
        }
    }
}

#[derive(Debug, Clone, Default)]
pub struct FeederRule {
    pub value_multiple: f64,
    pub growth: bool,
}

#[derive(Debug, Clone, Default)]
pub struct BoostRule {
    pub rule: MatchRule,
    pub multiplier: f64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum OneShotKind {
    #[default]
    Cash,
    Utility,
    Void,
    Adjacent,
}

#[derive(Debug, Clone, Default)]
pub struct OneShotRule {
    pub value: f64,
    pub skip_value: f64,
    pub kind: OneShotKind,
    pub capsule: bool,
    pub lucky_seven: bool,
}

#[derive(Debug, Clone)]
pub struct Horizon {
    pub reference: f64,
    pub cash_floor: f64,
    pub utility_floor: f64,
    pub cap: f64,
    pub void_empty_bonus: f64,
    pub adjacent_factor: f64,
    pub adjacent_cap: f64,
    pub capsule_machine: f64,
    pub capsule_machine_essence: f64,
    pub capsule_full_board_bonus: f64,
    pub lucky_seven_bonus: [f64; 3],
}

impl Default for Horizon {
    fn default() -> Self {
        Self {
            reference: 30.0,
            cash_floor: 0.5,
            utility_floor: 0.4,
            cap: 1.0,
            void_empty_bonus: 0.4,
            adjacent_factor: 0.35,
            adjacent_cap: 6.0,
            capsule_machine: 2.0,
            capsule_machine_essence: 3.0,
            capsule_full_board_bonus: 1.0,
            lucky_seven_bonus: [0.5, 1.5, 8.0],
        }
    }
}

#[derive(Debug, Clone, Default)]
pub struct FlushRule {
    pub item: String,
    pub essence: String,
    pub payout_item: f64,
    pub payout_essence: f64,
    pub essence_cap: f64,
    pub premium: f64,
}

#[derive(Debug, Clone, Default)]
pub struct HoverRule {
    pub item: String,
    pub essence: String,
    pub payout_item: f64,
    pub payout_essence: f64,
    pub essence_cap: f64,
    pub generators: HashMap<String, f64>,
}

#[derive(Debug, Clone, Default)]
pub struct CurseRule {
    pub kind: String,
    pub value: f64,
    pub base: f64,
    pub early_rent: f64,
    pub early_bonus: f64,
    pub late_decay: f64,
    pub average_penalty: f64,
    pub average_free: f64,
}

#[derive(Debug, Clone, Default)]
pub struct KingMidas {
    pub pirate_base: f64,
    pub pirate_per_remaining: f64,
    pub two_modifier_base: f64,
    pub two_modifier_per_remaining: f64,
    pub coin_modifiers: Vec<String>,
}

#[derive(Debug, Clone, Default)]
pub struct Hustler {
    pub income_per_spin: f64,
    pub upfront_cost: f64,
    pub remaining_discount: f64,
}

#[derive(Debug, Clone, Default)]
pub struct Wildcard {
    pub max_weight: f64,
    pub floor: f64,
}

#[derive(Debug, Clone, Default)]
pub struct Spiritbox {
    pub symbols: Vec<String>,
    pub grave_robber: String,
    pub grave_bonus: f64,
    pub hooligan_bonus: f64,
}

#[derive(Debug, Clone, Default)]
pub struct ItemRule {
    pub rule: MatchRule,
    pub amount: f64,
}

#[derive(Debug, Clone, Default)]
pub struct PolicyData {
    pub symbols: HashMap<String, SymbolMeta>,
    pub pairs: HashMap<String, PairRule>,
    pub feeders: HashMap<String, FeederRule>,
    pub boosters: HashMap<String, BoostRule>,
    pub multiplier_factors: HashMap<String, f64>,
    pub arrow_factors: HashMap<String, f64>,
    pub arrow_hit_probability: f64,
    pub adjacency: [f64; 4],
    pub item_additions: HashMap<String, ItemRule>,
    pub item_multipliers: HashMap<String, BoostRule>,
    pub item_passive: HashMap<String, f64>,
    pub one_turn: HashSet<String>,
    pub one_shot_symbols: HashSet<String>,
    pub void: HashSet<String>,
    pub timed: HashSet<String>,
    pub utility_proxies: HashSet<String>,
    pub suits: Vec<String>,
    pub flush: FlushRule,
    pub hover: HoverRule,
    pub one_shot: HashMap<String, OneShotRule>,
    pub horizon: Horizon,
    pub remaining_spins: Vec<(i32, f64)>,
    pub rent_curve_floor: HashMap<String, f64>,
    pub curses: HashMap<String, CurseRule>,
    pub negative_curses: HashSet<String>,
    pub king_midas: KingMidas,
    pub hustler: Hustler,
    pub wildcard: Wildcard,
    pub spiritbox: Spiritbox,
    pub growth_associations: HashMap<String, HashMap<String, f64>>,
    pub growth_block: HashSet<String>,
}

fn field_f64(value: &Value, key: &str, default: f64) -> f64 {
    value.get(key).and_then(Value::as_f64).unwrap_or(default)
}

fn field_str(value: &Value, key: &str, default: &str) -> String {
    value
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or(default)
        .to_string()
}

fn field_bool(value: &Value, key: &str) -> bool {
    value.get(key).and_then(Value::as_bool).unwrap_or(false)
}

fn field_strings(value: &Value, key: &str) -> Vec<String> {
    value
        .get(key)
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

fn string_set(value: Option<&Value>) -> HashSet<String> {
    value
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

fn parse_match(value: &Value) -> MatchRule {
    MatchRule {
        all: field_bool(value, "all"),
        types: field_strings(value, "types"),
        groups: field_strings(value, "groups"),
    }
}

fn parse_expected(value: Option<&Value>) -> [f64; 4] {
    let mut result = [0.0; 4];
    if let Some(object) = value.and_then(Value::as_object) {
        for count in 1..=3 {
            if let Some(number) = object.get(&count.to_string()).and_then(Value::as_f64) {
                result[count] = number;
            }
        }
    }
    result
}

fn parse_one_shot_kind(text: &str) -> OneShotKind {
    match text {
        "utility" => OneShotKind::Utility,
        "void" => OneShotKind::Void,
        "adjacent" => OneShotKind::Adjacent,
        _ => OneShotKind::Cash,
    }
}

impl PolicyData {
    pub fn load(path: &Path) -> Self {
        let Ok(text) = fs::read_to_string(path) else {
            return Self::default();
        };
        let Ok(root) = serde_json::from_str::<Value>(&text) else {
            return Self::default();
        };
        let mut policy = Self::default();
        if let Some(symbols) = root.get("symbols").and_then(Value::as_object) {
            for (name, def) in symbols {
                policy.symbols.insert(
                    name.clone(),
                    SymbolMeta {
                        base: field_f64(def, "base", 0.0),
                        raw_base: field_f64(def, "raw_base", 0.0),
                        groups: field_strings(def, "groups"),
                        rarity: field_str(def, "rarity", "none"),
                    },
                );
            }
        }
        if let Some(pairs) = root.get("pairs").and_then(Value::as_object) {
            for (key, def) in pairs {
                policy.pairs.insert(
                    key.clone(),
                    PairRule {
                        expected_eater: parse_expected(def.get("expected_eater")),
                        expected_death: parse_expected(def.get("expected_death")),
                        eater_bonus: field_f64(def, "eater_bonus", 0.0),
                        death_bonus: field_f64(def, "death_bonus", 0.0),
                    },
                );
            }
        }
        if let Some(feeders) = root.get("feeders").and_then(Value::as_object) {
            for (name, def) in feeders {
                policy.feeders.insert(
                    name.clone(),
                    FeederRule {
                        value_multiple: field_f64(def, "value_multiple", 0.0),
                        growth: field_bool(def, "growth"),
                    },
                );
            }
        }
        if let Some(boosters) = root.get("multipliers").and_then(Value::as_object) {
            for (name, def) in boosters {
                policy.boosters.insert(
                    name.clone(),
                    BoostRule {
                        rule: parse_match(def),
                        multiplier: field_f64(def, "multiplier", 1.0),
                    },
                );
            }
        }
        for (key, value) in number_map(root.get("multiplier_factors")) {
            policy.multiplier_factors.insert(key, value);
        }
        for (key, value) in number_map(root.get("arrow_factors")) {
            policy.arrow_factors.insert(key, value);
        }
        policy.arrow_hit_probability = field_f64(&root, "arrow_hit_probability", 0.0);
        if let Some(adjacency) = root.get("adjacency_probability").and_then(Value::as_object) {
            for count in 0..4 {
                policy.adjacency[count] = adjacency
                    .get(&count.to_string())
                    .and_then(Value::as_f64)
                    .unwrap_or(0.0);
            }
        }
        if let Some(additions) = root.get("item_additions").and_then(Value::as_object) {
            for (name, def) in additions {
                policy.item_additions.insert(
                    name.clone(),
                    ItemRule {
                        rule: parse_match(def),
                        amount: field_f64(def, "amount", 0.0),
                    },
                );
            }
        }
        if let Some(multipliers) = root.get("item_multipliers").and_then(Value::as_object) {
            for (name, def) in multipliers {
                policy.item_multipliers.insert(
                    name.clone(),
                    BoostRule {
                        rule: parse_match(def),
                        multiplier: field_f64(def, "multiplier", 1.0),
                    },
                );
            }
        }
        for (key, value) in number_map(root.get("item_passive")) {
            policy.item_passive.insert(key, value);
        }
        policy.one_turn = string_set(root.get("one_turn"));
        policy.void = string_set(root.get("void"));
        policy.timed = string_set(root.get("timed"));
        policy.utility_proxies = string_set(root.get("utility_proxies"));
        policy.negative_curses = string_set(root.get("negative_curses"));
        if let Some(combos) = root.get("special_combos") {
            policy.suits = field_strings(combos, "suits");
            if let Some(flush) = combos.get("flush") {
                policy.flush = FlushRule {
                    item: field_str(flush, "item", "flush"),
                    essence: field_str(flush, "essence", "flush_essence"),
                    payout_item: field_f64(flush, "payout_item", 12.0),
                    payout_essence: field_f64(flush, "payout_essence", 20.0),
                    essence_cap: field_f64(flush, "essence_cap", 5.0),
                    premium: field_f64(flush, "premium", 0.5),
                };
            }
            if let Some(hover) = combos.get("hover") {
                policy.hover = HoverRule {
                    item: field_str(hover, "item", "coin_on_a_string"),
                    essence: field_str(hover, "essence", "coin_on_a_string_essence"),
                    payout_item: field_f64(hover, "payout_item", 3.0),
                    payout_essence: field_f64(hover, "payout_essence", 10.0),
                    essence_cap: field_f64(hover, "essence_cap", 10.0),
                    generators: hover
                        .get("generators")
                        .and_then(Value::as_object)
                        .map(|generators| {
                            generators
                                .iter()
                                .filter_map(|(name, value)| {
                                    value.as_f64().map(|value| (name.clone(), value))
                                })
                                .collect()
                        })
                        .unwrap_or_default(),
                };
            }
        }
        if let Some(one_shot) = root.get("one_shot").and_then(Value::as_object) {
            for (name, def) in one_shot {
                policy.one_shot.insert(
                    name.clone(),
                    OneShotRule {
                        value: field_f64(def, "value", 0.0),
                        skip_value: field_f64(def, "skip_value", 0.0),
                        kind: parse_one_shot_kind(&field_str(def, "kind", "cash")),
                        capsule: field_bool(def, "capsule"),
                        lucky_seven: field_bool(def, "lucky_seven"),
                    },
                );
                policy.one_shot_symbols.insert(name.clone());
            }
        }
        if let Some(horizon) = root.get("one_shot_horizon") {
            policy.horizon = Horizon {
                reference: field_f64(horizon, "reference", 30.0),
                cash_floor: field_f64(horizon, "cash_floor", 0.5),
                utility_floor: field_f64(horizon, "utility_floor", 0.4),
                cap: field_f64(horizon, "cap", 1.0),
                void_empty_bonus: field_f64(horizon, "void_empty_bonus", 0.4),
                adjacent_factor: field_f64(horizon, "adjacent_factor", 0.35),
                adjacent_cap: field_f64(horizon, "adjacent_cap", 6.0),
                capsule_machine: field_f64(horizon, "capsule_machine", 2.0),
                capsule_machine_essence: field_f64(horizon, "capsule_machine_essence", 3.0),
                capsule_full_board_bonus: field_f64(horizon, "capsule_full_board_bonus", 1.0),
                lucky_seven_bonus: {
                    let bonus = horizon.get("lucky_seven_bonus");
                    let mut values = [0.5, 1.5, 8.0];
                    if let Some(bonus) = bonus {
                        for count in 0..3 {
                            values[count] = bonus
                                .get(&count.to_string())
                                .and_then(Value::as_f64)
                                .unwrap_or(values[count]);
                        }
                    }
                    values
                },
            };
        }
        if let Some(table) = root.get("remaining_spins").and_then(Value::as_object) {
            for (key, value) in table {
                if let (Ok(rent), Some(spins)) = (key.parse::<i32>(), value.as_f64()) {
                    policy.remaining_spins.push((rent, spins));
                }
            }
            policy.remaining_spins.sort_by_key(|(rent, _)| *rent);
        }
        for (key, value) in number_map(root.get("rent_curve_floor")) {
            policy.rent_curve_floor.insert(key, value);
        }
        if let Some(curses) = root.get("curse_values").and_then(Value::as_object) {
            for (name, def) in curses {
                policy.curses.insert(
                    name.clone(),
                    CurseRule {
                        kind: field_str(def, "kind", "flat"),
                        value: field_f64(def, "value", 0.0),
                        base: field_f64(def, "base", 2.5),
                        early_rent: field_f64(def, "early_rent", 150.0),
                        early_bonus: field_f64(def, "early_bonus", 0.0),
                        late_decay: field_f64(def, "late_decay", 0.0),
                        average_penalty: field_f64(def, "average_penalty", 0.3),
                        average_free: field_f64(def, "average_free", 1.5),
                    },
                );
            }
        }
        if let Some(rule) = root.get("king_midas") {
            policy.king_midas = KingMidas {
                pirate_base: field_f64(rule, "pirate_base", 12.0),
                pirate_per_remaining: field_f64(rule, "pirate_per_remaining", 0.4),
                two_modifier_base: field_f64(rule, "two_modifier_base", 8.0),
                two_modifier_per_remaining: field_f64(rule, "two_modifier_per_remaining", 0.15),
                coin_modifiers: field_strings(rule, "coin_modifiers"),
            };
        }
        if let Some(rule) = root.get("hustler") {
            policy.hustler = Hustler {
                income_per_spin: field_f64(rule, "income_per_spin", 1.0),
                upfront_cost: field_f64(rule, "upfront_cost", 7.0),
                remaining_discount: field_f64(rule, "remaining_discount", 0.6),
            };
        }
        if let Some(rule) = root.get("wildcard") {
            policy.wildcard = Wildcard {
                max_weight: field_f64(rule, "max_weight", 0.9),
                floor: field_f64(rule, "floor", 8.0),
            };
        }
        if let Some(rule) = root.get("spiritbox") {
            policy.spiritbox = Spiritbox {
                symbols: field_strings(rule, "symbols"),
                grave_robber: field_str(rule, "grave_robber", "grave_robber"),
                grave_bonus: field_f64(rule, "grave_bonus", 4.0),
                hooligan_bonus: field_f64(rule, "hooligan_bonus", 2.0),
            };
        }
        if let Some(associations) = root.get("growth_associations").and_then(Value::as_object) {
            for (core, foods) in associations {
                if let Some(foods) = foods.as_object() {
                    policy.growth_associations.insert(
                        core.clone(),
                        foods
                            .iter()
                            .filter_map(|(food, value)| {
                                value.as_f64().map(|value| (food.clone(), value))
                            })
                            .collect(),
                    );
                }
            }
        }
        policy.build_growth_block();
        policy
    }

    fn build_growth_block(&mut self) {
        let mut block: HashSet<String> = GROWTH_CORES.iter().map(|name| name.to_string()).collect();
        for key in self.pairs.keys() {
            if let Some((food, eater)) = key.split_once('/') {
                if GROWTH_CORES.contains(&eater) {
                    block.insert(food.to_string());
                }
            }
        }
        for (core, foods) in &self.growth_associations {
            block.extend(foods.keys().cloned());
            block.insert(core.clone());
        }
        self.growth_block = block;
    }

    pub fn matches(&self, name: &str, rule: &MatchRule) -> bool {
        if rule.all || rule.types.iter().any(|item| item == name) {
            return true;
        }
        if rule.groups.is_empty() {
            return false;
        }
        let Some(meta) = self.symbols.get(name) else {
            return false;
        };
        rule.groups.iter().any(|group| meta.groups.contains(group))
    }

    fn group_has(&self, name: &str, group: &str) -> bool {
        self.symbols
            .get(name)
            .map(|meta| meta.groups.iter().any(|item| item == group))
            .unwrap_or(false)
    }

    pub fn prob(&self, count: usize) -> f64 {
        self.adjacency[count.min(3)]
    }

    pub fn remaining_spins(&self, rent: i32) -> f64 {
        if self.remaining_spins.is_empty() {
            return 20.0;
        }
        let first = self.remaining_spins[0];
        if rent <= first.0 {
            return first.1;
        }
        let mut value = self.remaining_spins.last().map(|entry| entry.1).unwrap_or(20.0);
        for (key, spins) in &self.remaining_spins {
            if rent >= *key {
                value = *spins;
            }
        }
        value
    }

    pub fn one_shot_horizon_for(&self, kind: OneShotKind, rent: i32) -> f64 {
        let remaining = self.remaining_spins(rent);
        let reference = self.horizon.reference.max(1.0);
        let floor = if kind == OneShotKind::Utility {
            self.horizon.utility_floor
        } else {
            self.horizon.cash_floor
        };
        (remaining / reference).max(floor).min(self.horizon.cap)
    }

    pub fn one_shot_skip(&self, name: &str) -> f64 {
        self.one_shot.get(name).map(|rule| rule.skip_value).unwrap_or(0.0)
    }

    pub fn capsule_multiplier(&self, items: &HashMap<String, usize>) -> f64 {
        let mut multiplier = 1.0f64;
        if items.get("capsule_machine").copied().unwrap_or(0) > 0 {
            multiplier = multiplier.max(self.horizon.capsule_machine);
        }
        if items.get("capsule_machine_essence").copied().unwrap_or(0) > 0 {
            multiplier = multiplier.max(self.horizon.capsule_machine_essence);
        }
        multiplier
    }

    pub fn item_addition(&self, name: &str, items: &HashMap<String, usize>) -> f64 {
        let mut value = 0.0;
        for (item, rule) in &self.item_additions {
            let count = items.get(item).copied().unwrap_or(0);
            if count > 0 && self.matches(name, &rule.rule) {
                value += rule.amount * count as f64;
            }
        }
        value
    }

    pub fn item_multiplier(&self, name: &str, items: &HashMap<String, usize>) -> f64 {
        let mut factor = 1.0;
        for (item, rule) in &self.item_multipliers {
            let count = items.get(item).copied().unwrap_or(0);
            if count > 0 && self.matches(name, &rule.rule) {
                factor *= rule.multiplier.powi(count as i32);
            }
        }
        factor
    }

    pub fn expected_boost(
        &self,
        name: &str,
        deck: &HashMap<String, usize>,
        items: &HashMap<String, usize>,
    ) -> f64 {
        let mut factor = 1.0;
        for (booster, rule) in &self.boosters {
            if booster == "buffing_powder" {
                continue;
            }
            let count = deck.get(booster).copied().unwrap_or(0).min(3);
            if count > 0 && self.matches(name, &rule.rule) {
                let key = format!("{}:{}", rule.multiplier, count);
                factor *= self.multiplier_factors.get(&key).copied().unwrap_or(1.0);
            }
        }
        if name != "robin_hood" {
            let directions = if items.get("quiver").copied().unwrap_or(0) > 0
                || items.get("quiver_essence").copied().unwrap_or(0) > 0
            {
                2
            } else {
                1
            };
            for (arrow, mult) in [
                ("bronze_arrow", 2),
                ("silver_arrow", 3),
                ("golden_arrow", 4),
            ] {
                let count = deck.get(arrow).copied().unwrap_or(0).min(3);
                if count > 0 {
                    let key = format!("{directions}:{mult}:{count}");
                    factor *= self.arrow_factors.get(&key).copied().unwrap_or(1.0);
                }
            }
        }
        factor
    }

    pub fn allows_full(
        &self,
        name: &str,
        counts: &HashMap<String, usize>,
        due: i32,
        spins_left: i32,
    ) -> bool {
        let count = |other: &str| counts.get(other).copied().unwrap_or(0);
        if name == "cow" && count("cat") == 0 {
            return false;
        }
        if name == "key" && !CHESTS.iter().any(|(chest, _)| count(chest) > 0) {
            return false;
        }
        if name == "coal" && count("coal") >= 3 {
            return false;
        }
        if due >= 777 && spins_left <= 5 && self.growth_block.contains(name) {
            return false;
        }
        true
    }
}

#[derive(Debug, Clone, Copy, Default)]
pub struct Estimate {
    pub score: f64,
    pub gross: f64,
    pub penalty: f64,
    pub empty_bonus: f64,
    pub slots: f64,
    pub effective: f64,
    pub growth: bool,
}

pub struct EvalContext<'a> {
    pub data: &'a GameData,
    pub deck: &'a [Symbol],
    pub counts: HashMap<String, usize>,
    pub items: HashMap<String, usize>,
    pub rent: i32,
    /// Prospective deck length: current symbols plus the candidate.
    pub physical: usize,
    pub empty_compensation: bool,
}

impl<'a> EvalContext<'a> {
    pub fn new(
        data: &'a GameData,
        deck: &'a [Symbol],
        items_held: &[usize],
        rent: i32,
        empty_compensation: bool,
    ) -> Self {
        let mut counts = HashMap::new();
        for symbol in deck {
            *counts
                .entry(data.symbols[symbol.type_id as usize].name.clone())
                .or_insert(0) += 1;
        }
        let mut items = HashMap::new();
        for id in items_held {
            *items
                .entry(data.items[*id].name.clone())
                .or_insert(0) += 1;
        }
        Self {
            data,
            deck,
            counts,
            items,
            rent,
            physical: deck.len() + 1,
            empty_compensation,
        }
    }
}

impl PolicyData {
    fn symbol_name<'a>(&self, ctx: &'a EvalContext, symbol: &Symbol) -> &'a str {
        &ctx.data.symbols[symbol.type_id as usize].name
    }

    fn base_value(&self, name: &str, items: &HashMap<String, usize>) -> f64 {
        let base = self.symbols.get(name).map(|meta| meta.base).unwrap_or(0.0);
        (base + self.item_addition(name, items)) * self.item_multiplier(name, items)
    }

    fn instance_value(&self, symbol: &Symbol, ctx: &EvalContext) -> f64 {
        let def = &ctx.data.symbols[symbol.type_id as usize];
        let base = self
            .symbols
            .get(&def.name)
            .map(|meta| meta.base)
            .unwrap_or(def.base_value as f64);
        let addition = self.item_addition(&def.name, &ctx.items);
        (base + symbol.bonus as f64 + addition)
            * symbol.multiplier as f64
            * self.item_multiplier(&def.name, &ctx.items)
    }

    fn current_value(&self, symbol: &Symbol, ctx: &EvalContext) -> f64 {
        let name = self.symbol_name(ctx, symbol);
        self.instance_value(symbol, ctx)
            * self.expected_boost(name, &ctx.counts, &ctx.items)
    }

    fn deck_average(&self, ctx: &EvalContext) -> f64 {
        if ctx.deck.is_empty() {
            return 0.0;
        }
        let total: f64 = ctx
            .deck
            .iter()
            .map(|symbol| self.current_value(symbol, ctx).max(0.0))
            .sum();
        total / ctx.deck.len() as f64
    }

    fn suit_payout(&self, ctx: &EvalContext) -> f64 {
        let suits: usize = self
            .suits
            .iter()
            .map(|name| ctx.counts.get(name).copied().unwrap_or(0))
            .sum();
        let mut payout = 0.0;
        let essence = ctx.items.get(&self.flush.essence).copied().unwrap_or(0);
        if essence > 0 && (suits as f64) < self.flush.essence_cap {
            payout += self.flush.payout_essence;
        }
        if ctx.items.get(&self.flush.item).copied().unwrap_or(0) > 0 {
            payout += self.flush.payout_item;
        }
        payout
    }

    fn hover_payout(&self, items: &HashMap<String, usize>) -> f64 {
        let mut payout = 0.0;
        if items.get(&self.hover.item).copied().unwrap_or(0) > 0 {
            payout += self.hover.payout_item;
        }
        if items.get(&self.hover.essence).copied().unwrap_or(0) > 0 {
            payout += self.hover.payout_essence;
        }
        payout
    }

    fn rent_value(&self, data: &GameData, name: &str, rent: i32) -> Option<f64> {
        let points = data.rent_curves.get(name)?;
        if points.is_empty() {
            return None;
        }
        let rent = rent as f64;
        let value = if rent <= points[0].0 {
            points[0].1
        } else if rent >= points.last().unwrap().0 {
            points.last().unwrap().1
        } else {
            let mut value = points.last().unwrap().1;
            for pair in points.windows(2) {
                let (left_rent, left_value) = pair[0];
                let (right_rent, right_value) = pair[1];
                if left_rent <= rent && rent <= right_rent && right_rent > left_rent {
                    let share = (rent - left_rent) / (right_rent - left_rent);
                    value = left_value + (right_value - left_value) * share;
                    break;
                }
            }
            value
        };
        Some(
            value.max(
                self.rent_curve_floor
                    .get(name)
                    .copied()
                    .unwrap_or(0.0),
            ),
        )
    }

    fn slots_value(
        &self,
        name: &str,
        counts: &HashMap<String, usize>,
        physical: usize,
        items: &HashMap<String, usize>,
    ) -> f64 {
        if self.suits.iter().any(|suit| suit == name)
            && (items.get(&self.flush.item).copied().unwrap_or(0) > 0
                || items.get(&self.flush.essence).copied().unwrap_or(0) > 0)
        {
            return 0.0;
        }
        if self.one_turn.contains(name) || (self.void.contains(name) && physical >= 20) {
            return 0.2;
        }
        if self.timed.contains(name) {
            return 0.5;
        }
        if name == "key" || name == "magic_key" {
            if counts
                .keys()
                .any(|other| self.group_has(other, "chest"))
            {
                return 0.5;
            }
        }
        if self.group_has(name, "chest")
            && (items.get("lockpick").copied().unwrap_or(0) > 0
                || items.get("lockpick_essence").copied().unwrap_or(0) > 0)
        {
            return 0.5;
        }
        if self.feeders.keys().any(|eater| {
            counts.get(eater).copied().unwrap_or(0) > 0
                && self.pairs.contains_key(&format!("{name}/{eater}"))
        }) {
            return 0.5;
        }
        1.0
    }

    fn result(&self, name: &str, value: f64, ctx: &EvalContext, growth: bool) -> Estimate {
        let mut prospective = ctx.counts.clone();
        *prospective.entry(name.to_string()).or_insert(0) += 1;
        let effective: f64 = ctx
            .counts
            .iter()
            .map(|(other, count)| {
                *count as f64
                    * self.slots_value(other, &prospective, ctx.physical, &ctx.items)
            })
            .sum();
        let slots = self.slots_value(name, &ctx.counts, ctx.physical, &ctx.items);
        let penalty = 0.25 * (effective + slots - 20.0).max(0.0);
        let empty_bonus = if ctx.empty_compensation
            && value > 0.0
            && !self.negative_curses.contains(name)
        {
            0.25 * (20.0 - (effective + slots)).max(0.0)
        } else {
            0.0
        };
        Estimate {
            score: value - penalty + empty_bonus,
            gross: value,
            penalty,
            empty_bonus,
            slots,
            effective,
            growth,
        }
    }

    fn curse_score(&self, rule: &CurseRule, ctx: &EvalContext) -> f64 {
        if rule.kind == "thievery" {
            return rule.value;
        }
        let rent = ctx.rent as f64;
        let mut value = rule.base
            + (rule.early_rent - rent).max(0.0) * rule.early_bonus
            - (rent - rule.early_rent).max(0.0) * rule.late_decay;
        if rule.kind == "draining" {
            let average = self.deck_average(ctx);
            value -= rule.average_penalty * (average - rule.average_free).max(0.0);
        }
        value
    }

    fn key_value(&self, counts: &HashMap<String, usize>) -> f64 {
        let mut total = 0.0;
        let mut weighted = 0.0;
        for (chest, death) in CHESTS {
            let count = counts.get(chest).copied().unwrap_or(0);
            if count > 0 {
                total += count as f64;
                weighted += death * count as f64;
            }
        }
        if total == 0.0 {
            0.0
        } else {
            5.0 + 0.5 * (weighted / total)
        }
    }

    fn adjacent_one_turn_value(&self, ctx: &EvalContext) -> f64 {
        let mut values: Vec<f64> = ctx
            .deck
            .iter()
            .map(|symbol| {
                let name = self.symbol_name(ctx, symbol);
                self.base_value(name, &ctx.items).max(0.0)
            })
            .collect();
        if values.is_empty() {
            return 0.0;
        }
        values.sort_by(|left, right| left.partial_cmp(right).unwrap());
        let average = values.iter().sum::<f64>() / values.len() as f64;
        let expected = 0.25 * (20.min(ctx.deck.len())) as f64;
        self.horizon
            .adjacent_cap
            .min(self.horizon.adjacent_factor * expected * average)
    }

    fn one_shot_value(&self, rule: &OneShotRule, name: &str, ctx: &EvalContext) -> f64 {
        let deck_len = ctx.deck.len();
        let horizon = self.one_shot_horizon_for(rule.kind, ctx.rent);
        let multiplier = if rule.capsule {
            self.capsule_multiplier(&ctx.items)
        } else {
            1.0
        };
        let mut value = match rule.kind {
            OneShotKind::Adjacent => self.adjacent_one_turn_value(ctx),
            _ => rule.value,
        };
        if matches!(rule.kind, OneShotKind::Cash | OneShotKind::Void) {
            value *= self.expected_boost(name, &ctx.counts, &ctx.items);
        }
        value *= multiplier;
        if rule.kind == OneShotKind::Void && deck_len < 20 {
            value += self.horizon.void_empty_bonus;
        }
        if rule.lucky_seven {
            let count = ctx.items.get("lucky_seven").copied().unwrap_or(0).min(2);
            value += self.horizon.lucky_seven_bonus[count];
        }
        value *= horizon;
        if rule.capsule && deck_len >= 20 {
            value += self.horizon.capsule_full_board_bonus;
        }
        value
    }

    fn growth_food(&self, name: &str, counts: &HashMap<String, usize>) -> bool {
        self.feeders.iter().any(|(eater, rule)| {
            rule.growth
                && counts.get(eater).copied().unwrap_or(0) > 0
                && self.pairs.contains_key(&format!("{name}/{eater}"))
        })
    }

    fn spiritbox_bonus(&self, name: &str, ctx: &EvalContext) -> f64 {
        if !self.spiritbox.symbols.iter().any(|symbol| symbol == name) {
            return 0.0;
        }
        let mut bonus = 0.0;
        if ctx
            .items
            .get(&self.spiritbox.grave_robber)
            .copied()
            .unwrap_or(0)
            > 0
        {
            bonus += self.spiritbox.grave_bonus;
        }
        if ctx.counts.get("hooligan").copied().unwrap_or(0) > 0 {
            bonus += self.spiritbox.hooligan_bonus;
        }
        bonus
    }

    fn growth_food_bonus(&self, name: &str, counts: &HashMap<String, usize>) -> f64 {
        let mut bonus = 0.0f64;
        for (core, foods) in &self.growth_associations {
            if counts.get(core).copied().unwrap_or(0) > 0 {
                if let Some(value) = foods.get(name) {
                    bonus = bonus.max(*value);
                }
            }
        }
        bonus
    }

    fn item_effect_value(&self, name: &str, ctx: &EvalContext) -> f64 {
        let remaining = self.remaining_spins(ctx.rent) * 0.6;
        let mut value = self.item_passive.get(name).copied().unwrap_or(0.0) * remaining;
        if let Some(rule) = self.item_additions.get(name) {
            let count: usize = ctx
                .counts
                .iter()
                .filter(|(other, _)| self.matches(other, &rule.rule))
                .map(|(_, value)| *value)
                .sum();
            value += count as f64 * rule.amount;
        }
        if let Some(rule) = self.item_multipliers.get(name) {
            for symbol in ctx.deck {
                let other = self.symbol_name(ctx, symbol);
                if self.matches(other, &rule.rule) {
                    value += (rule.multiplier - 1.0) * self.current_value(symbol, ctx).max(0.0);
                }
            }
        }
        if name == self.spiritbox.grave_robber {
            let boxes: usize = self
                .spiritbox
                .symbols
                .iter()
                .map(|box_name| ctx.counts.get(box_name).copied().unwrap_or(0))
                .sum();
            if boxes > 0 {
                value += 6.0 + 4.0 * boxes as f64;
            }
        }
        if name == "copycat" {
            value += 4.0 * ctx.counts.get("cat").copied().unwrap_or(0) as f64;
        }
        if name == "ancient_lizard_blade" {
            let duplicates = ctx
                .counts
                .iter()
                .filter(|(other, count)| **count >= 2 && other.as_str() != "empty")
                .count();
            value += (9.0 - 2.0 * duplicates as f64).max(0.0) * remaining;
        }
        let count = |other: &str| ctx.counts.get(other).copied().unwrap_or(0);
        if name == "triple_coins" {
            value += 2.0 * count("coin") as f64;
        }
        if name == "rusty_gear" {
            let mut duplicate_value = 0.0;
            for symbol in ctx.deck {
                let other = self.symbol_name(ctx, symbol);
                if ctx.counts.get(other).copied().unwrap_or(0) >= 2 {
                    duplicate_value += self.current_value(symbol, ctx).max(0.0);
                }
            }
            value += 0.5 * duplicate_value;
        }
        if name == "lefty_the_rabbit" {
            value += 0.5 * remaining;
            value += 0.25 * (count("rabbit") + count("rabbit_fluff")) as f64;
        }
        if name == "shrine" {
            let organisms: usize = ctx
                .counts
                .iter()
                .filter(|(other, _)| self.group_has(other, "organism"))
                .map(|(_, amount)| *amount)
                .sum();
            value += 1.5 * organisms as f64;
        }
        if name == "lemon" {
            value += 20usize.saturating_sub(ctx.deck.len()) as f64;
        }
        if name == "chili_powder" {
            let peppers: usize = [
                "black_pepper",
                "blue_pepper",
                "brown_pepper",
                "cyan_pepper",
                "gray_pepper",
                "green_pepper",
                "lime_pepper",
                "pink_pepper",
                "purple_pepper",
                "red_pepper",
                "white_pepper",
                "yellow_pepper",
            ]
            .iter()
            .map(|pepper| ctx.items.get(*pepper).copied().unwrap_or(0))
            .sum();
            value += (2.0 * peppers as f64).max(2.0);
        }
        if name == "bag_of_holding" {
            value += 5.0;
        }
        if name == "x_ray_machine" {
            value += 4.0;
        }
        if name == "dark_humor" {
            let dark: usize = ctx
                .counts
                .iter()
                .filter(|(other, _)| self.group_has(other, "darkhumor"))
                .map(|(_, amount)| *amount)
                .sum();
            value += 2.0 + 1.5 * count("comedian") as f64 + 0.5 * dark as f64;
        }
        if name == "time_machine" {
            value += 3.0 * count("coal") as f64;
        }
        if name == "pink_pepper" {
            value += 2.0 * remaining;
        }
        if name == "gray_pepper" {
            value += 2.0;
        }
        if name == "lime_pepper" {
            value += 3.0;
        }
        if name == "clear_sky" {
            value += 4.0 * (count("sun") + count("moon")) as f64;
        }
        if name == "coin_on_a_string" {
            // Consumes every coin for three coins each: a one-shot conversion.
            value += 3.0 * count("coin") as f64;
        }
        if name == "flush" {
            let suits: usize = ["clubs", "diamonds", "hearts", "spades"]
                .iter()
                .map(|suit| count(suit))
                .sum();
            value += 12.0 * suits as f64;
        }
        if name == "lint_roller" {
            value += 12.0 * count("rabbit_fluff") as f64;
        }
        value
    }

    pub fn item_combo(&self, name: &str, ctx: &EvalContext) -> f64 {
        let suits: usize = self
            .suits
            .iter()
            .map(|suit| ctx.counts.get(suit).copied().unwrap_or(0))
            .sum();
        if name == self.flush.item {
            return self.flush.payout_item * suits as f64 + self.flush.premium;
        }
        if name == self.flush.essence {
            return self.flush.payout_essence * (suits as f64).min(self.flush.essence_cap)
                + self.flush.premium;
        }
        let rate: f64 = self
            .hover
            .generators
            .iter()
            .map(|(generator, weight)| {
                ctx.counts.get(generator).copied().unwrap_or(0) as f64 * weight
            })
            .sum();
        if name == self.hover.item {
            return 10.0 * (1.0 + self.hover.payout_item * rate);
        }
        if name == self.hover.essence {
            return self.hover.payout_essence
                * (rate * self.hover.essence_cap).min(self.hover.essence_cap);
        }
        self.item_effect_value(name, ctx)
    }

    pub fn estimate(&self, name: &str, ctx: &EvalContext) -> Estimate {
        let Some(info) = self.symbols.get(name) else {
            return Estimate::default();
        };
        let counts = &ctx.counts;
        let items = &ctx.items;
        let physical = ctx.physical;
        let rent = ctx.rent;
        let count = |other: &str| counts.get(other).copied().unwrap_or(0);

        let payout = self.suit_payout(ctx);
        if self.suits.iter().any(|suit| suit == name) && payout > 0.0 {
            let effective: f64 = counts
                .iter()
                .map(|(other, value)| {
                    *value as f64 * self.slots_value(other, counts, physical, items)
                })
                .sum();
            let slots = 0.0;
            let penalty = 0.25 * (effective + slots - 20.0).max(0.0);
            let empty_bonus = if ctx.empty_compensation {
                0.25 * (20.0 - (effective + slots)).max(0.0)
            } else {
                0.0
            };
            let value = payout + self.flush.premium;
            return Estimate {
                score: value - penalty + empty_bonus,
                gross: value,
                penalty,
                empty_bonus,
                slots,
                effective,
                growth: false,
            };
        }

        if let Some(rule) = self.curses.get(name) {
            let value = self.curse_score(rule, ctx);
            return self.result(name, value, ctx, false);
        }

        if name == "wildcard" {
            let mut values: Vec<f64> = ctx
                .deck
                .iter()
                .map(|symbol| self.current_value(symbol, ctx).max(0.0))
                .collect();
            let value = if values.is_empty() {
                self.wildcard.floor
            } else {
                values.sort_by(|left, right| right.partial_cmp(left).unwrap());
                let mut expected = self.wildcard.max_weight * values[0];
                if values.len() > 1 {
                    expected += (1.0 - self.wildcard.max_weight)
                        * values[1..].iter().sum::<f64>()
                        / (values.len() - 1) as f64;
                }
                expected.max(self.wildcard.floor)
            };
            return self.result(name, value, ctx, false);
        }

        if name == "hustler" {
            let remaining = self.remaining_spins(rent);
            let value = (self.hustler.income_per_spin
                * self.capsule_multiplier(items)
                * remaining
                * self.hustler.remaining_discount
                - self.hustler.upfront_cost)
                .max(0.0);
            return self.result(name, value, ctx, false);
        }

        if name == "king_midas" {
            let mut value = 1.0 + self.hover_payout(items);
            let pirates = count("pirate");
            let modifiers = self
                .king_midas
                .coin_modifiers
                .iter()
                .filter(|item| items.get(*item).copied().unwrap_or(0) > 0)
                .count();
            let remaining = self.remaining_spins(rent);
            if pirates > 0 {
                value = value.max(
                    self.king_midas.pirate_base
                        + self.king_midas.pirate_per_remaining * remaining,
                );
            } else if modifiers >= 2 {
                value = value.max(
                    self.king_midas.two_modifier_base
                        + self.king_midas.two_modifier_per_remaining * remaining,
                );
            } else if modifiers == 1 {
                value += 1.0;
            }
            return self.result(name, value, ctx, false);
        }

        if name == "key" {
            let value = self.key_value(counts);
            return self.result(name, value, ctx, false);
        }

        if name == "magic_key" {
            return self.result(name, 3.0, ctx, false);
        }

        if name == "midas_bomb" {
            let physical_now = ctx.deck.len();
            let low_quality = ctx
                .deck
                .iter()
                .filter(|symbol| self.current_value(symbol, ctx) <= 1.5)
                .count();
            let value = if physical_now >= 24 && low_quality >= 6 {
                8.0 + 0.3 * low_quality as f64
            } else {
                0.5
            };
            return self.result(name, value, ctx, false);
        }

        if let Some(rule) = self.one_shot.get(name) {
            let value = self.one_shot_value(rule, name, ctx);
            return self.result(name, value, ctx, false);
        }

        let curve = self.rent_value(ctx.data, name, rent);
        let curve_growth = curve.is_some();
        let mut base = match curve {
            Some(value) => {
                (value + self.item_addition(name, items)) * self.item_multiplier(name, items)
            }
            None => (info.base + self.item_addition(name, items)) * self.item_multiplier(name, items),
        };
        if self.void.contains(name) && physical < 20 {
            base = 0.0;
        }
        let mut value = if base > 0.0 {
            base * self.expected_boost(name, counts, items)
        } else {
            base
        };

        if let Some((_, death)) = CHESTS.iter().find(|(chest, _)| *chest == name) {
            if count("key") > 0 || count("magic_key") > 0 {
                let bonus = if count("key") > 0 {
                    5.0 + 0.5 * death
                } else {
                    3.0 + 0.3 * death
                };
                value = value.max(bonus);
            }
        }

        if name == "bounty_hunter" && items.get("zaroffs_contract").copied().unwrap_or(0) > 0 {
            let humans: usize = counts
                .iter()
                .filter(|(other, _)| self.group_has(other, "human"))
                .map(|(_, value)| *value)
                .sum();
            value = value.max(3.0 + 25.0 * self.prob(humans));
        }
        if name == "coal" {
            let future = if rent <= 150 {
                4.0
            } else if rent <= 300 {
                3.0
            } else if rent <= 450 {
                2.0
            } else {
                1.0
            };
            value = value.max(future);
        }
        if name == "matryoshka_doll_1" {
            let remaining = self.remaining_spins(rent);
            value = (1.0 + remaining / 20.0).clamp(2.0, 4.0);
        }
        if name == "cow" && count("cat") > 0 {
            value = value.max(4.0);
        }
        if name == "gambler" {
            let dice = count("d3") > 0 || count("d5") > 0 || count("midas_bomb") > 0;
            value = value.max(if dice { 3.0 } else { 2.5 });
        }
        if name == "golem"
            && (count("miner") > 0
                || count("dwarf") > 0
                || items.get("mining_pick").copied().unwrap_or(0) > 0
                || items.get("borax_kyle").copied().unwrap_or(0) > 0
                || items.get("borax_kyle_essence").copied().unwrap_or(0) > 0)
        {
            value = value.max(3.0);
        }
        if name == "beehive" && count("bear") > 0 {
            value = value.max(7.0);
        }
        if name == "mine" && count("archaeologist") == 0 {
            if items.get("mining_pick").copied().unwrap_or(0) > 0 {
                value += 2.0;
            }
            if count("golem") > 0 {
                value += 1.0;
            }
        }
        if name == "chicken" {
            let synergy = items.get("chicken_coop").copied().unwrap_or(0) > 0
                || items.get("egg_carton").copied().unwrap_or(0) > 0
                || count("egg") > 0
                || count("golden_egg") > 0
                || count("chick") > 0;
            if synergy {
                value = value.max(4.0);
            } else {
                value = value.min(1.2);
            }
        }
        if name == "bartender" {
            if count("dwarf") > 0 {
                value += 4.0;
            } else if rent >= 225 {
                value = value.min(1.8);
            }
        }
        if name == "dwarf" && count("bartender") > 0 {
            value += 5.0;
        }

        // A food can be consumed once. With multiple eater types use the best
        // estimated route, avoiding duplicate payout for the same food.
        let mut food_extra = 0.0f64;
        for (eater, rule) in &self.feeders {
            let owned = count(eater);
            if owned == 0 {
                continue;
            }
            let Some(pair) = self.pairs.get(&format!("{name}/{eater}")) else {
                continue;
            };
            let n = owned.min(3);
            let mut extra = pair.expected_eater[n];
            if rule.value_multiple != 0.0 {
                extra += self.prob(n) * (base - info.raw_base) * rule.value_multiple;
            }
            let eater_multiplier = self.item_multiplier(eater, items);
            let route = extra * eater_multiplier
                + pair.expected_death[n] * self.item_multiplier(name, items);
            food_extra = food_extra.max(route.max(0.0));
        }
        value += food_extra;
        let mut growth = curve_growth || self.growth_food(name, counts);

        if let Some(rule) = self.feeders.get(name) {
            let old = count(name);
            let delta = self.prob(old + 1) - self.prob(old);
            let eater_factor = self.item_multiplier(name, items);
            let mut matched = 0usize;
            for (food, owned) in counts {
                if *owned == 0 {
                    continue;
                }
                let Some(pair) = self.pairs.get(&format!("{food}/{name}")) else {
                    continue;
                };
                value += delta
                    * (pair.eater_bonus * eater_factor
                        + pair.death_bonus * self.item_multiplier(food, items))
                    * *owned as f64;
                matched += owned;
            }
            if matched > 0 && rule.growth {
                growth = true;
            }
        }

        if let Some(rule) = self.boosters.get(name) {
            if name != "buffing_powder" {
                for symbol in ctx.deck {
                    let other = self.symbol_name(ctx, symbol);
                    if self.matches(other, &rule.rule) {
                        let existing =
                            self.instance_value(symbol, ctx) * self.expected_boost(other, counts, items);
                        value += self.prob(1) * (rule.multiplier - 1.0) * existing;
                    }
                }
            }
        }
        if let Some(mult) = match name {
            "bronze_arrow" => Some(2.0),
            "silver_arrow" => Some(3.0),
            "golden_arrow" => Some(4.0),
            _ => None,
        } {
            let directions = if items.get("quiver").copied().unwrap_or(0) > 0
                || items.get("quiver_essence").copied().unwrap_or(0) > 0
            {
                2.0
            } else {
                1.0
            };
            let ray = directions * self.arrow_hit_probability;
            for symbol in ctx.deck {
                let other = self.symbol_name(ctx, symbol);
                let existing = self.instance_value(symbol, ctx)
                    * self.expected_boost(other, counts, items);
                value += ray * (mult - 1.0) * existing.max(0.0);
                if other == "target" {
                    value += ray * 10.0;
                }
            }
        }

        if name == "robin_hood" {
            let arrows: usize = ["bronze_arrow", "silver_arrow", "golden_arrow"]
                .iter()
                .map(|arrow| count(arrow))
                .sum();
            value += 3.0 * self.prob(1) * arrows as f64;
        } else if matches!(name, "bronze_arrow" | "silver_arrow" | "golden_arrow") {
            value += 3.0 * self.prob(1) * count("robin_hood") as f64;
        }

        // Reverse adjacency effects: a doglike/bee-like/omelette-stuff symbol
        // gains its source's bonus for every adjacent source symbol.
        for (source, group, amount) in [
            ("dog", "doglikes", 2.0),
            ("omelette", "omelettestuff", 2.0),
            ("bee", "beelikes", 1.0),
        ] {
            if self.group_has(name, group) {
                let sources = count(source);
                if sources > 0 {
                    value += amount * self.prob(sources);
                }
            }
        }
        // The candidate as a source increases the value of every adjacent
        // matching symbol. Expected adjacent targets are about 4 * n / 19.
        for (source, group, amount) in [
            ("dog", "doglikes", 2.0),
            ("omelette", "omelettestuff", 2.0),
        ] {
            if name == source {
                let targets: usize = counts
                    .iter()
                    .filter(|(other, _)| self.group_has(other, group))
                    .map(|(_, value)| *value)
                    .sum();
                value += amount * 4.0 * targets as f64 / 19.0;
            }
        }
        if name == "bee" {
            let mut targets = 0usize;
            let mut total = 0.0;
            for symbol in ctx.deck {
                let other = self.symbol_name(ctx, symbol);
                if self.group_has(other, "beelikes") {
                    targets += 1;
                    total += self.instance_value(symbol, ctx).max(0.0);
                }
            }
            if targets > 0 {
                value += (total / targets as f64 + 1.0) * 4.0 * targets as f64 / 19.0;
            }
        }

        let same = count(name);
        if matches!(
            name,
            "diamond" | "watermelon" | "ruby" | "emerald" | "strawberry"
        ) {
            value += 2.0 * same as f64;
        } else if name == "cultist" {
            value += 2.0 * same as f64 + if same == 2 { 3.0 } else if same >= 3 { 1.0 } else { 0.0 };
        } else if name == "ninja" && items.get("cursed_katana").copied().unwrap_or(0) == 0 {
            value -= 2.0 * same as f64;
        }
        if name == "ninja" {
            value += 1.5 * count("mouse").min(3) as f64;
        } else if name == "mouse" {
            value += 1.5 * count("ninja").min(2) as f64;
        } else if name == "crab" {
            value += 6.0 * (4.0 / 19.0) * same as f64;
        }

        let hover = self.hover_payout(items);
        if hover > 0.0 {
            if let Some(rate) = self.hover.generators.get(name) {
                value += hover * rate;
            }
        }

        if name == "golden_arrow" {
            value = value.max(12.0);
        }
        let spiritbox = self.spiritbox_bonus(name, ctx);
        if spiritbox > 0.0 {
            value += spiritbox;
        }
        let association = self.growth_food_bonus(name, counts);
        if association > 0.0 {
            value = value.max(association);
        }
        if growth && !curve_growth {
            value = value.max(4.5);
        }
        self.result(name, value, ctx, growth)
    }
}

fn number_map(value: Option<&Value>) -> Vec<(String, f64)> {
    value
        .and_then(Value::as_object)
        .map(|object| {
            object
                .iter()
                .filter_map(|(key, value)| value.as_f64().map(|value| (key.clone(), value)))
                .collect()
        })
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn data() -> GameData {
        GameData::load(crate::default_data_dir()).expect("game data should load")
    }

    #[test]
    fn milk_is_worth_more_with_a_cat() {
        let data = data();
        let cat = data.symbol_id("cat").expect("cat");
        let mut deck = vec![Symbol::new(cat, 0)];
        let without = {
            let ctx = EvalContext::new(&data, &deck, &[], 25, false);
            data.policy.estimate("milk", &ctx)
        };
        deck.push(Symbol::new(cat, 0));
        let with = {
            let ctx = EvalContext::new(&data, &deck, &[], 25, false);
            data.policy.estimate("milk", &ctx)
        };
        assert!(with.score > without.score);
    }

    #[test]
    fn overflow_penalises_a_full_deck() {
        let data = data();
        let coin = data.symbol_id("coin").expect("coin");
        let deck: Vec<Symbol> = (0..20).map(|_| Symbol::new(coin, 0)).collect();
        let ctx = EvalContext::new(&data, &deck, &[], 100, false);
        let estimate = data.policy.estimate("coin", &ctx);
        assert!(estimate.penalty > 0.0);
        assert!(estimate.score < estimate.gross);
    }
}
