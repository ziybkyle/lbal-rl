//! Read-only observations of the real counters used by item effects.
//! Exporting these values must never advance a counter, trigger an effect or
//! consume RNG. An absent progress target is not a zero-length countdown.
use super::RunState;
use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
pub struct ManagedProgress {
    pub kind: &'static str,
    pub current: i32,
    pub target: i32,
    pub remaining: i32,
}

#[derive(Debug, Clone, Serialize)]
pub struct ManagedItemObservation {
    pub saved_value: i32,
    pub cashout_coins: Option<i32>,
    pub age: i32,
    pub stored_symbols: Option<i32>,
    pub consumed_count: i32,
    pub protection_limit: Option<i32>,
    pub progress: Option<ManagedProgress>,
}

impl<'a> RunState<'a> {
    pub fn managed_item_observation(&self, essence: bool, id: usize) -> ManagedItemObservation {
        let def = if essence { &self.data.essences[id] } else { &self.data.items[id] };
        let value = |n: usize| def.values.get(n).copied().unwrap_or(0.0) as i32;
        let saved = if essence { self.essence_saved.get(&id) } else { self.item_saved.get(&id) }
            .copied().unwrap_or(0);
        let age = if essence { 0 } else { self.item_ages.get(&id).copied().unwrap_or(0) };
        let mut result = ManagedItemObservation {
            saved_value: saved, cashout_coins: None, age, stored_symbols: None,
            consumed_count: if essence { self.essence_destroy_counters.get(&id).copied().unwrap_or(0) } else { 0 },
            protection_limit: if essence { self.essence_protection_limit() } else { None },
            progress: None,
        };
        let progress = if essence {
            match def.name.as_str() {
                "reroll_essence" => Some(("rerolls", self.reroll_essence_rerolls, value(2))),
                "conveyor_belt_essence" => Some(("spawns", self.spawner_spawns, value(0))),
                "swapping_device_essence" => Some(("swaps", self.swapping_essence_swaps, value(0))),
                "coin_on_a_string_essence" | "egg_carton_essence" | "fish_bowl_essence" |
                "flush_essence" | "lint_roller_essence" | "barrel_o_dwarves_essence" => Some(("removals", saved, value(0))),
                "symbol_bomb_small_essence" | "symbol_bomb_big_essence" | "symbol_bomb_very_big_essence" |
                "symbol_bomb_quantum_essence" | "shattered_mirror_essence" | "frozen_pizza_essence" |
                "lunchbox_essence" | "adoption_papers_essence" => Some(("additions", saved, value(0))),
                "lockpick_essence" | "happy_hour_essence" | "mining_pick_essence" |
                "mobius_strip_essence" | "void_party_essence" | "shrine_essence" => Some(("destructions", saved, value(0))),
                "time_machine_essence" => Some(("transformations", saved, value(0))),
                "lucky_dice_essence" | "checkered_flag_essence" => Some(("payouts", saved, value(0))),
                "piggy_bank_essence" | "booster_pack_essence" | "golden_carrot_essence" |
                "lucky_carrot_essence" => Some(("spins", saved, value(1))),
                "horseshoe_essence" | "pool_ball_essence" | "bowling_ball_essence" |
                "four_leaf_clover_essence" | "cardboard_box_essence" | "dishwasher_essence" |
                "treasure_map_essence" | "lucky_seven_essence" | "telescope_essence" |
                "protractor_essence" | "oil_can_essence" => Some(("spins", saved, value(0))),
                _ => None,
            }
        } else {
            match def.name.as_str() {
                "piggy_bank" | "swear_jar" => {
                    // This is the payout of one legal use, not a prediction of
                    // future deposits and not multiplied by the stack twice.
                    result.cashout_coins = Some(((saved as f64 * def.values.get(1).copied().unwrap_or(0.0)).round() as i32).max(0));
                    None
                }
                "egg_carton" => { result.stored_symbols = Some(self.stored_eggs); None }
                "fish_bowl" => { result.stored_symbols = Some(self.stored_goldfish); None }
                "treasure_map" | "blue_suits" | "red_suits" => Some(("spins", age, value(0))),
                "cardboard_box" => Some(("spins", age, value(1))),
                "dishwasher" | "shattered_mirror" => Some(("spins", saved, value(1))),
                "frozen_pizza" => Some(("spins", saved, value(0))),
                "oil_can" => Some(("spins", saved, (value(0)-1).max(1))),
                "lefty_the_rabbit" => Some(("spins", self.lefty_saved, value(0))),
                "telescope" => Some(("spins", self.telescope_saved, value(0))),
                "protractor" => Some(("spins", self.protractor_saved, value(0))),
                "credit_card" => Some(("spins", self.credit_card_saved, value(0))),
                "compost_heap" => Some(("destructions", self.compost_destroyed as i32, value(0))),
                _ => None,
            }
        };
        result.progress = progress.map(|(kind,current,target)| ManagedProgress {
            kind, current, target: target.max(1), remaining: (target.max(1)-current).max(0),
        });
        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{default_data_dir, GameData};

    #[test]
    fn only_symbol_offers_can_reroll_and_illegal_calls_preserve_rng_and_tokens() {
        use crate::Phase;
        let data = GameData::load(default_data_dir()).unwrap();
        let mut state = RunState::new(&data, 41);
        state.reroll_tokens = 20;
        for phase in [Phase::Ready, Phase::Done, Phase::ItemReward { offer: vec![0,1,2] },
                      Phase::EssenceReward { offer: vec![0,1,2] }] {
            state.phase = phase;
            let before = format!("{:?}", state.phase);
            let rng_before = (state.rng.item_pick_event, state.rng.item_rarity_ctr, state.rng.essence_pick_event);
            assert!(!state.can_reroll());
            state.reroll_reward();
            assert_eq!(format!("{:?}", state.phase), before);
            assert_eq!(state.reroll_tokens, 20);
            assert_eq!((state.rng.item_pick_event, state.rng.item_rarity_ctr, state.rng.essence_pick_event), rng_before);
        }
        for phase in [Phase::SpinReward { offer: vec![0,1,2] }, Phase::SymbolReward { offer: vec![0,1,2] }] {
            state.phase = phase;
            let before = state.reroll_tokens;
            assert!(state.can_reroll());
            state.reroll_reward();
            assert_eq!(state.reroll_tokens, before-state.reroll_cost());
        }
    }

    #[test]
    fn savings_quote_matches_actual_manual_use_and_stacks() {
        let data = GameData::load(default_data_dir()).unwrap();
        for name in ["piggy_bank", "swear_jar"] {
            let mut state = RunState::new(&data, 41);
            let id = data.item_index[name];
            state.items_held.extend([id,id]);
            state.item_saved.insert(id, 200);
            let before = state.coins;
            let observed = state.managed_item_observation(false,id);
            assert_eq!(observed.saved_value,200);
            assert!(observed.progress.is_none());
            assert!(state.use_item(id));
            assert_eq!(Some(state.coins-before),observed.cashout_coins);
            assert_eq!(state.items_held.iter().filter(|held| **held==id).count(),1);
        }
    }

    #[test]
    fn essence_counters_use_real_event_units_and_thresholds() {
        let data = GameData::load(default_data_dir()).unwrap();
        let mut state = RunState::new(&data, 41);
        let coin = data.essence_index["coin_on_a_string_essence"];
        state.essences_held.push(coin);state.essence_saved.insert(coin,9);
        let p = state.managed_item_observation(true,coin).progress.unwrap();
        assert_eq!((p.kind,p.current,p.target,p.remaining),("removals",9,10,1));
        let reroll = data.essence_index["reroll_essence"];
        state.reroll_essence_rerolls=9;
        let p=state.managed_item_observation(true,reroll).progress.unwrap();
        assert_eq!((p.current,p.target,p.remaining),(9,10,1));
        let belt=data.essence_index["conveyor_belt_essence"];
        state.spawner_spawns=3;
        assert_eq!(state.managed_item_observation(true,belt).progress.unwrap().current,3);
        let dice=data.essence_index["lucky_dice_essence"];
        state.essence_saved.insert(dice,5);
        assert_eq!(state.managed_item_observation(true,dice).progress.unwrap().kind,"payouts");
    }

    #[test]
    fn disabled_items_keep_counters_and_observations_do_not_advance_them() {
        let data=GameData::load(default_data_dir()).unwrap();
        let mut state=RunState::new(&data,41);
        let id=data.item_index["credit_card"];
        state.disabled_items.push(id);state.credit_card_saved=6;
        let a=state.managed_item_observation(false,id);
        let b=state.managed_item_observation(false,id);
        assert_eq!(serde_json::to_value(a).unwrap(),serde_json::to_value(b).unwrap());
        assert_eq!(state.credit_card_saved,6);
        let egg=data.item_index["egg_carton"];state.stored_eggs=17;
        assert_eq!(state.managed_item_observation(false,egg).stored_symbols,Some(17));
        let fish=data.item_index["fish_bowl"];state.stored_goldfish=23;
        assert_eq!(state.managed_item_observation(false,fish).stored_symbols,Some(23));
    }
}
