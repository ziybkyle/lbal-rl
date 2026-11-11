//! Optional settlement records. These observers must not draw RNG or change rewards.
use super::{RunState, BOARD_COLS, BOARD_SLOTS};
use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
pub struct SymbolPayment {
    pub name: String,
    pub stage: &'static str,
    pub sum: i32,
    pub multiplier: f64,
    pub coins: i32,
    pub state: &'static str,
    pub order: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct SlotSettlement {
    pub slot: usize,
    pub row: usize,
    pub column: usize,
    pub initial: Option<String>,
    pub final_symbol: Option<String>,
    pub payments: Vec<SymbolPayment>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ExtraPayment {
    pub kind: &'static str,
    pub name: String,
    pub coins: i32,
}

#[derive(Debug, Clone, Serialize)]
pub struct ItemGrant {
    pub source: String,
    pub name: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct SpinDiagnostics {
    pub spin: u32,
    pub income: i32,
    pub symbol_total: i32,
    pub other_trigger_income: i32,
    pub slots: Vec<SlotSettlement>,
    pub extras: Vec<ExtraPayment>,
    pub item_grants: Vec<ItemGrant>,
}

impl<'a> RunState<'a> {
    pub(super) fn start_spin_diagnostics(&mut self) {
        if !self.diagnostics_enabled { self.last_spin_diagnostics = None; return; }
        let slots = (0..BOARD_SLOTS).map(|slot| SlotSettlement {
            slot, row: slot / BOARD_COLS + 1, column: slot % BOARD_COLS + 1,
            initial: self.board.slots[slot].and_then(|i| self.deck.get(i))
                .map(|s| self.data.symbols[s.type_id as usize].name.clone()),
            final_symbol: None, payments: Vec::new(),
        }).collect();
        self.last_spin_diagnostics = Some(SpinDiagnostics { spin: self.total_spins + 1, income: 0,
            symbol_total: 0, other_trigger_income: 0, slots, extras: Vec::new(), item_grants: Vec::new() });
    }

    pub(super) fn trace_payment(&mut self, slot: usize, id: usize, stage: &'static str,
                               sum: i32, multiplier: f64, coins: i32, state: &'static str, order: usize) {
        if let Some(trace) = self.last_spin_diagnostics.as_mut() {
            trace.slots[slot].payments.push(SymbolPayment { name: self.data.symbols[id].name.clone(),
                stage, sum, multiplier, coins, state, order });
        }
    }

    pub(super) fn trace_extra(&mut self, kind: &'static str, name: &str, coins: i32) {
        if coins == 0 { return; }
        if let Some(trace) = self.last_spin_diagnostics.as_mut() {
            trace.extras.push(ExtraPayment { kind, name: name.to_string(), coins });
        }
    }

    pub(super) fn trace_item_grant(&mut self, source: &str, id: usize) {
        if let Some(trace) = self.last_spin_diagnostics.as_mut() {
            trace.item_grants.push(ItemGrant { source: source.to_string(), name: self.data.items[id].name.clone() });
        }
    }

    pub(super) fn finish_spin_diagnostics(&mut self, income: i32) {
        if let Some(trace) = self.last_spin_diagnostics.as_mut() {
            trace.income = income;
            for entry in &mut trace.slots {
                entry.final_symbol = self.board.slots[entry.slot].and_then(|i| self.deck.get(i))
                    .map(|s| self.data.symbols[s.type_id as usize].name.clone());
                entry.payments.sort_by(|a,b| a.stage.cmp(b.stage).then(a.order.cmp(&b.order)));
            }
            trace.symbol_total = trace.slots.iter().flat_map(|s| &s.payments).map(|p| p.coins).sum();
            let named: i32 = trace.extras.iter().map(|e| e.coins).sum();
            // This remainder is honestly labeled. It includes coin effects
            // without an individual attribution, rather than inventing a source.
            trace.other_trigger_income = income - trace.symbol_total - named;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Board,GameData,Phase,Symbol,default_data_dir};

    fn fixed<'a>(data: &'a GameData, names: &[(&str,usize)]) -> RunState<'a> {
        let mut state=RunState::new(data,41);
        state.deck.clear();state.board=Board::empty();state.coins=10000;state.spins_left=1000;
        state.diagnostics_enabled=true;
        for &(name,slot) in names {
            let id=data.symbol_id(name).unwrap();let index=state.deck.len();
            state.deck.push(Symbol::new(id,data.symbols[id].base_value));
            state.board.slots[slot]=Some(index);
        }
        state
    }

    #[test]
    fn twenty_positions_and_basic_coins_match_actual_settlement() {
        let data=GameData::load(default_data_dir()).unwrap();
        let mut state=fixed(&data,&[("coin",0),("coin",19)]);
        let income=state.resolve_spin_effects();let trace=state.last_spin_diagnostics.as_ref().unwrap();
        assert_eq!(trace.slots.len(),20);assert_eq!(income,2);assert_eq!(trace.symbol_total,2);
        assert_eq!(trace.slots[0].payments[0].coins,1);assert_eq!(trace.slots[19].payments[0].coins,1);
        assert_eq!(trace.other_trigger_income,0);
        assert!(trace.slots[10].payments.is_empty());
    }

    #[test]
    fn hustler_pool_ball_source_and_distinct_exit_pools_are_real() {
        let data=GameData::load(default_data_dir()).unwrap();
        let mut state=fixed(&data,&[("hustler",0),("diver",6),("pearl",7)]);
        state.resolve_spin_effects();
        assert!(state.destroyed_symbol_history().contains(&(data.symbol_id("hustler").unwrap() as u16)));
        assert!(state.removed_symbol_history().contains(&(data.symbol_id("pearl").unwrap() as u16)));
        assert!(!state.destroyed_symbol_history().contains(&(data.symbol_id("pearl").unwrap() as u16)));
        let trace=state.last_spin_diagnostics.as_ref().unwrap();
        assert!(trace.item_grants.iter().any(|g| g.source=="hustler" && g.name=="pool_ball"));
    }

    #[test]
    fn coconut_and_its_newborns_keep_separate_payment_records() {
        let data=GameData::load(default_data_dir()).unwrap();
        let mut state=fixed(&data,&[("monkey",6),("coconut",7)]);
        let income=state.resolve_spin_effects();let trace=state.last_spin_diagnostics.as_ref().unwrap();
        let payments:Vec<_>=trace.slots.iter().flat_map(|s| &s.payments).collect();
        assert!(payments.iter().any(|p| p.name=="coconut" && p.stage=="initial"));
        assert!(payments.iter().any(|p| p.name=="coconut_half" && p.stage=="newborn"));
        assert_eq!(trace.income,income);
        assert_eq!(trace.symbol_total+trace.extras.iter().map(|p| p.coins).sum::<i32>()+trace.other_trigger_income,income);
    }

    #[test]
    fn optional_diagnostics_preserve_rng_choices_and_actual_income() {
        let data=GameData::load(default_data_dir()).unwrap();
        fn step(state: &mut RunState) {
            match state.phase {
                Phase::Ready => {state.spin();},
                Phase::SpinReward{..}=>state.choose_spin(0),
                Phase::SymbolReward{..}=>state.choose_symbol(0),
                Phase::ItemReward{..}=>state.choose_item(0),
                Phase::EssenceReward{..}=>state.choose_essence(0),
                Phase::Done=>{},
            }
        }
        for seed in 0..24 {
            let mut plain=RunState::new(&data,seed);let mut recorded=RunState::new(&data,seed);
            recorded.diagnostics_enabled=true;
            for _ in 0..240 {
                assert_eq!(plain.phase,recorded.phase,"seed={seed}");
                assert_eq!(plain.coins,recorded.coins,"seed={seed}");
                assert_eq!(plain.items_held,recorded.items_held,"seed={seed}");
                assert_eq!(format!("{:?}",plain.deck),format!("{:?}",recorded.deck),"seed={seed}");
                if matches!(plain.phase,Phase::Done){break;}
                step(&mut plain);step(&mut recorded);
            }
        }
    }
}
