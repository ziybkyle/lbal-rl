//! Python bindings for the Rust simulator.
//!
//! The environment exposes a JSON observation and integer actions so a Python
//! policy can drive full games without reimplementing the rules.

use lbal_core::run::{expert_gym_action, expert_offer_scores, ExpertMemory};
use lbal_core::{default_data_dir, GameData, Phase, RunState};
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use serde_json::{json, Value};
use std::sync::OnceLock;

const COMPACT_MAGIC: i32 = 0x4C42_414C;

fn push_i32(out: &mut Vec<u8>, value: i32) {
    out.extend_from_slice(&value.to_le_bytes());
}

/// Compact little-endian i32 observation for training loops.
/// Header (18 fields): magic, phase, coins, rent_due, rents_paid, spins_left,
/// total_spins, last_income, removal/reroll/essence tokens, boss flag/hp,
/// deck_len, item_count, candidate_count, done, won. Then deck (4 per symbol),
/// items (kind, id), candidates (kind, id), and 20 board type ids.
fn compact_bytes(state: &RunState<'_>) -> Vec<u8> {
    let (phase_code, candidates): (i32, Vec<(i32, i32)>) = match &state.phase {
        Phase::Ready => (0, Vec::new()),
        Phase::SpinReward { offer } | Phase::SymbolReward { offer } => (
            if matches!(state.phase, Phase::SpinReward { .. }) {
                1
            } else {
                2
            },
            offer.iter().map(|id| (0, *id as i32)).collect(),
        ),
        Phase::ItemReward { offer } => {
            (3, offer.iter().map(|id| (1, *id as i32)).collect())
        }
        Phase::EssenceReward { offer } => {
            (4, offer.iter().map(|id| (2, *id as i32)).collect())
        }
        Phase::Done => (5, Vec::new()),
    };
    let items: Vec<(i32, i32)> = state
        .items_held
        .iter()
        .map(|id| (0, *id as i32))
        .chain(
            state
                .essences_held
                .iter()
                .map(|id| (1, *id as i32)),
        )
        .collect();
    let mut out = Vec::with_capacity(18 * 4 + state.deck.len() * 16 + items.len() * 8 + candidates.len() * 8 + 80);
    for value in [
        COMPACT_MAGIC,
        phase_code,
        state.coins,
        state.rent_due(),
        state.rents_paid as i32,
        state.spins_left,
        state.total_spins as i32,
        state.last_income,
        state.removal_tokens,
        state.reroll_tokens,
        state.essence_tokens,
        state.boss_fight_active() as i32,
        state.boss_hp(),
        state.deck.len() as i32,
        items.len() as i32,
        candidates.len() as i32,
        matches!(state.phase, Phase::Done) as i32,
        state.won as i32,
    ] {
        push_i32(&mut out, value);
    }
    for symbol in &state.deck {
        push_i32(&mut out, symbol.type_id as i32);
        push_i32(&mut out, symbol.bonus);
        push_i32(&mut out, symbol.multiplier);
        push_i32(&mut out, symbol.times_displayed as i32);
    }
    for (kind, id) in items {
        push_i32(&mut out, kind);
        push_i32(&mut out, id);
    }
    for (kind, id) in candidates {
        push_i32(&mut out, kind);
        push_i32(&mut out, id);
    }
    for id in state.last_board.iter() {
        push_i32(&mut out, *id as i32);
    }
    out
}

static DEFAULT_DATA: OnceLock<Result<GameData, String>> = OnceLock::new();

fn load_data(custom: Option<String>) -> PyResult<&'static GameData> {
    if let Some(directory) = custom {
        let data = GameData::load(&directory)
            .map_err(|error| PyRuntimeError::new_err(format!("failed to load data: {error}")))?;
        return Ok(Box::leak(Box::new(data)));
    }
    let result = DEFAULT_DATA.get_or_init(|| GameData::load(default_data_dir()));
    match result {
        Ok(data) => Ok(data),
        Err(error) => Err(PyRuntimeError::new_err(format!(
            "failed to load data: {error}"
        ))),
    }
}

fn phase_name(phase: &Phase) -> &'static str {
    match phase {
        Phase::Ready => "ready",
        Phase::SpinReward { .. } => "spin_reward",
        Phase::SymbolReward { .. } => "symbol_reward",
        Phase::ItemReward { .. } => "item_reward",
        Phase::EssenceReward { .. } => "essence_reward",
        Phase::Done => "done",
    }
}

fn observation_for(state: &RunState<'_>) -> String {
    let expert_scores = expert_offer_scores(state);
    let offer: Vec<Value> = match &state.phase {
            Phase::SpinReward { offer } | Phase::SymbolReward { offer } => offer
                .iter()
                .enumerate()
                .map(|(index, type_id)| {
                    let def = &state.data.symbols[*type_id];
                    json!({
                        "index": index,
                        "type_id": type_id,
                        "name": def.name,
                        "rarity": def.rarity,
                        "base_value": def.base_value,
                        "kind": "symbol",
                        "expert_score": expert_scores.get(index).copied().unwrap_or(0.0),
                    })
                })
                .collect(),
            Phase::ItemReward { offer } => offer
                .iter()
                .enumerate()
                .map(|(index, type_id)| {
                    let def = &state.data.items[*type_id];
                    json!({
                        "index": index,
                        "type_id": type_id,
                        "name": def.name,
                        "rarity": def.rarity,
                        "kind": "item",
                        "expert_score": expert_scores.get(index).copied().unwrap_or(0.0),
                    })
                })
                .collect(),
            Phase::EssenceReward { offer } => offer
                .iter()
                .enumerate()
                .map(|(index, essence_id)| {
                    let def = &state.data.essences[*essence_id];
                    json!({
                        "index": index,
                        "type_id": essence_id,
                        "name": def.name,
                        "rarity": def.rarity,
                        "kind": "essence",
                    })
                })
                .collect(),
            _ => Vec::new(),
        };
        let deck: Vec<Value> = state
            .deck
            .iter()
            .enumerate()
            .map(|(index, symbol)| {
                let def = &state.data.symbols[symbol.type_id as usize];
                json!({
                    "index": index,
                    "type_id": symbol.type_id,
                    "name": def.name,
                    "rarity": def.rarity,
                    "bonus": symbol.bonus,
                    "multiplier": symbol.multiplier,
                    "times_displayed": symbol.times_displayed,
                    "can_remove": state.can_remove_symbol(index),
                })
            })
            .collect();
        let mut items: Vec<Value> = state
            .items_held
            .iter()
            .map(|type_id| {
                let def = &state.data.items[*type_id];
                json!({
                    "type_id": type_id,
                    "name": def.name,
                    "rarity": def.rarity,
                    "kind": "item",
                })
            })
            .collect();
        items.extend(state.essences_held.iter().map(|essence_id| {
            let def = &state.data.essences[*essence_id];
            json!({
                "type_id": essence_id,
                "name": def.name,
                "rarity": def.rarity,
                "kind": "essence",
            })
        }));
        let managed_items: Vec<Value> = state.management_stacks().into_iter().map(|(essence, id, disabled, count)| {
            let def = if essence { &state.data.essences[id] } else { &state.data.items[id] };
            json!({"kind": if essence {"essence"} else {"item"}, "type_id": id, "name": def.name,
                "disabled": disabled, "count": count, "can_toggle": state.can_toggle_managed(essence, id),
                "can_use": !essence && state.can_use_managed_item(id)})
        }).collect();
        let board: Vec<i64> = state.last_board.iter().map(|id| *id as i64).collect();
        json!({
            "phase": phase_name(&state.phase),
            "done": matches!(state.phase, Phase::Done),
            "won": state.won,
            "failure_reason": state.failure_reason,
            "managed_items": managed_items,
            "coins": state.coins,
            "rents_paid": state.rents_paid,
            "rent_due": state.rent_due(),
            "spins_left": state.spins_left,
            "total_spins": state.total_spins,
            "removal_tokens": state.removal_tokens,
            "reroll_tokens": state.reroll_tokens,
            "essence_tokens": state.essence_tokens,
            "last_income": state.last_income,
            "removal_cost": state.removal_cost(),
            "reroll_cost": state.reroll_cost(),
            "can_remove": state.removal_tokens >= state.removal_cost(),
            "can_reroll": state.reroll_tokens >= state.reroll_cost(),
            "boss_fight": state.boss_fight_active(),
            "boss_hp": state.boss_hp(),
            "must_take_symbol": state.must_take_symbol(),
            "deck": deck,
            "items": items,
            "board": board,
            "offer": offer,
        })
    .to_string()
}

fn apply_action(state: &mut RunState<'_>, action: i64) {
    match state.phase.clone() {
        Phase::Ready => {
            state.spin();
        }
        Phase::SpinReward { .. } => {
            if action < 0 {
                state.skip_spin();
            } else {
                state.choose_spin(action as usize);
            }
        }
        Phase::SymbolReward { .. } => {
            if action < 0 {
                state.skip_symbol();
            } else {
                state.choose_symbol(action as usize);
            }
        }
        Phase::ItemReward { .. } => {
            if action < 0 {
                state.skip_item();
            } else {
                state.choose_item(action as usize);
            }
        }
        Phase::EssenceReward { .. } => {
            if action < 0 {
                state.skip_essence();
            } else {
                state.choose_essence(action as usize);
            }
        }
        Phase::Done => {}
    }
}

#[pyclass]
struct Env {
    state: RunState<'static>,
    teacher: ExpertMemory,
}

impl Env {
    fn observation(&self) -> String {
        observation_for(&self.state)
    }
}

#[pymethods]
impl Env {
    #[new]
    #[pyo3(signature = (seed=42, data_dir=None, seed_text=None))]
    fn new(seed: u64, data_dir: Option<String>, seed_text: Option<String>) -> PyResult<Self> {
        let data = load_data(data_dir)?;
        let state = match seed_text {
            Some(text) => RunState::new_with_seed_text(data, &text),
            None => RunState::new(data, seed),
        };
        Ok(Self {
            state,
            teacher: ExpertMemory::default(),
        })
    }

    /// 手工策略教师在 gym 动作空间中的一步决策（供行为克隆收集示范）。
    fn expert_action(&mut self) -> u64 {
        expert_gym_action(&self.state, &mut self.teacher) as u64
    }

    /// Re-roll the current reward if a token is available.
    fn reroll(&mut self) -> bool {
        if self.state.reroll_tokens >= self.state.reroll_cost() {
            self.state.reroll_reward();
            true
        } else {
            false
        }
    }

    /// Remove a deck symbol during a reward phase if a token is available.
    fn remove(&mut self, index: usize) -> bool {
        if self.state.removal_tokens >= self.state.removal_cost()
            && self.state.can_remove_symbol(index)
        {
            self.state.remove_symbol(index);
            true
        } else {
            false
        }
    }

    fn toggle_managed(&mut self, essence: bool, id: usize) -> bool { self.state.toggle_managed(essence, id) }
    fn use_item(&mut self, id: usize) -> bool { self.state.use_item(id) }

    /// Restart with a new seed and return the initial observation.
    fn reset(&mut self, seed: u64) -> String {
        self.state = RunState::new(self.state.data, seed);
        self.teacher = ExpertMemory::default();
        self.observation()
    }

    /// Restart with a BetterLandlord seed string.
    fn reset_text(&mut self, seed_text: String) -> String {
        self.state = RunState::new_with_seed_text(self.state.data, &seed_text);
        self.teacher = ExpertMemory::default();
        self.observation()
    }

    /// Return the current observation without advancing the game.
    fn observe(&self) -> String {
        self.observation()
    }

    /// Return the packed little-endian i32 observation (see `compact_bytes`).
    fn observe_compact(&self, py: Python<'_>) -> Py<PyBytes> {
        PyBytes::new(py, &compact_bytes(&self.state)).unbind()
    }

    /// Advance one decision. `action < 0` means skip the current reward.
    fn step(&mut self, action: i64) -> String {
        apply_action(&mut self.state, action);
        self.observation()
    }
}

/// A batch of independent run states. `step_batch` resolves every environment
/// in parallel with Rayon and returns one observation string per slot.
#[pyclass]
struct BatchEnv {
    states: Vec<RunState<'static>>,
}

#[pymethods]
impl BatchEnv {
    #[new]
    #[pyo3(signature = (count, seed=42, data_dir=None))]
    fn new(count: usize, seed: u64, data_dir: Option<String>) -> PyResult<Self> {
        let data = load_data(data_dir)?;
        let states = (0..count)
            .map(|index| RunState::new(data, seed.wrapping_add(index as u64)))
            .collect();
        Ok(Self { states })
    }

    fn len(&self) -> usize {
        self.states.len()
    }

    /// Restart every environment from `seed`, `seed+1`, ... and return observations.
    fn reset_all(&mut self, seed: u64) -> Vec<String> {
        let data = self.states.first().map(|state| state.data);
        if let Some(data) = data {
            self.states = (0..self.states.len())
                .map(|index| RunState::new(data, seed.wrapping_add(index as u64)))
                .collect();
        }
        self.observe_all()
    }

    /// Restart one environment with a new seed and return its observation.
    fn reset_at(&mut self, index: usize, seed: u64) -> PyResult<String> {
        let state = self
            .states
            .get_mut(index)
            .ok_or_else(|| PyRuntimeError::new_err("batch index out of range"))?;
        let data = state.data;
        *state = RunState::new(data, seed);
        Ok(observation_for(state))
    }

    fn observe(&self, index: usize) -> PyResult<String> {
        self.states
            .get(index)
            .map(observation_for)
            .ok_or_else(|| PyRuntimeError::new_err("batch index out of range"))
    }

    fn observe_all(&self) -> Vec<String> {
        use rayon::prelude::*;
        self.states.par_iter().map(observation_for).collect()
    }

    fn observe_compact(&self, py: Python<'_>, index: usize) -> PyResult<Py<PyBytes>> {
        self.states
            .get(index)
            .map(|state| PyBytes::new(py, &compact_bytes(state)).unbind())
            .ok_or_else(|| PyRuntimeError::new_err("batch index out of range"))
    }

    fn reset_all_compact(&mut self, py: Python<'_>, seed: u64) -> Vec<Py<PyBytes>> {
        use rayon::prelude::*;
        let data = self.states.first().map(|state| state.data);
        if let Some(data) = data {
            self.states = (0..self.states.len())
                .map(|index| RunState::new(data, seed.wrapping_add(index as u64)))
                .collect();
        }
        let buffers: Vec<Vec<u8>> = py.detach(|| {
            self.states.par_iter().map(compact_bytes).collect()
        });
        buffers
            .into_iter()
            .map(|buffer| PyBytes::new(py, &buffer).unbind())
            .collect()
    }

    fn reset_at_compact(
        &mut self,
        py: Python<'_>,
        index: usize,
        seed: u64,
    ) -> PyResult<Py<PyBytes>> {
        let state = self
            .states
            .get_mut(index)
            .ok_or_else(|| PyRuntimeError::new_err("batch index out of range"))?;
        let data = state.data;
        *state = RunState::new(data, seed);
        Ok(PyBytes::new(py, &compact_bytes(state)).unbind())
    }

    fn observe_compact_all(&self, py: Python<'_>) -> Vec<Py<PyBytes>> {
        use rayon::prelude::*;
        let buffers: Vec<Vec<u8>> = py.detach(|| {
            self.states.par_iter().map(compact_bytes).collect()
        });
        buffers
            .into_iter()
            .map(|buffer| PyBytes::new(py, &buffer).unbind())
            .collect()
    }

    fn step(&mut self, index: usize, action: i64) -> PyResult<String> {
        let state = self
            .states
            .get_mut(index)
            .ok_or_else(|| PyRuntimeError::new_err("batch index out of range"))?;
        apply_action(state, action);
        Ok(observation_for(state))
    }

    fn step_batch_compact(
        &mut self,
        py: Python<'_>,
        actions: Vec<i64>,
    ) -> PyResult<Vec<Py<PyBytes>>> {
        if actions.len() != self.states.len() {
            return Err(PyRuntimeError::new_err(
                "one action per environment is required",
            ));
        }
        use rayon::prelude::*;
        let buffers: Vec<Vec<u8>> = py.detach(|| {
            self.states
                .par_iter_mut()
                .zip(actions.par_iter())
                .map(|(state, action)| {
                    apply_action(state, *action);
                    compact_bytes(state)
                })
                .collect()
        });
        Ok(buffers
            .into_iter()
            .map(|buffer| PyBytes::new(py, &buffer).unbind())
            .collect())
    }

    fn step_batch(&mut self, actions: Vec<i64>) -> PyResult<Vec<String>> {
        if actions.len() != self.states.len() {
            return Err(PyRuntimeError::new_err(
                "one action per environment is required",
            ));
        }
        use rayon::prelude::*;
        Ok(self
            .states
            .par_iter_mut()
            .zip(actions.par_iter())
            .map(|(state, action)| {
                apply_action(state, *action);
                observation_for(state)
            })
            .collect())
    }
}

#[pymodule]
fn lbal_py(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<Env>()?;
    module.add_class::<BatchEnv>()?;
    Ok(())
}
