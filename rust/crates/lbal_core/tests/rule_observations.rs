use lbal_core::{GameData, RunState};

#[test]
fn pre_spin_rule_facts_follow_active_stacks_and_landlord_blocking() {
    let data = GameData::load(lbal_core::default_data_dir()).unwrap();
    let mut state = RunState::new(&data, 42);
    let coin = data.symbol_index["coin"];
    let string = data.item_index["coin_on_a_string"];
    let essence = data.essence_index["coin_on_a_string_essence"];
    assert_eq!(state.pre_spin_symbol_observation(coin)["removed"], false);
    state.items_held.extend([string, string]);
    state.essences_held.push(essence);
    let fact = state.pre_spin_symbol_observation(coin);
    assert_eq!(fact["removed"], true);
    assert_eq!(fact["cash_per_symbol"], 16);
    assert_eq!(fact["consumer_count"], 3);
    state.fine_prints.push(20);
    let blocked = state.pre_spin_symbol_observation(coin);
    assert_eq!(blocked["removed"], false);
    assert_eq!(blocked["cash_per_symbol"], 0);
    state.fine_prints.clear();
    assert!(state.toggle_managed(false, string));
    assert_eq!(state.pre_spin_symbol_observation(coin)["cash_per_symbol"], 10);
}

#[test]
fn stored_symbols_are_distinct_from_fixed_cash_consumers() {
    let data = GameData::load(lbal_core::default_data_dir()).unwrap();
    let mut state = RunState::new(&data, 42);
    let egg = data.symbol_index["egg"];
    let goldfish = data.symbol_index["goldfish"];
    state.items_held.push(data.item_index["egg_carton"]);
    state.items_held.push(data.item_index["fish_bowl"]);
    let fact = state.pre_spin_symbol_observation(egg);
    assert_eq!(fact["removed"], true);
    assert_eq!(fact["stored_as_egg"], true);
    assert_eq!(fact["cash_per_symbol"], 0);
    assert_eq!(state.pre_spin_symbol_observation(goldfish)["stored_as_goldfish"], true);
    state.fine_prints.push(21);
    assert_eq!(state.pre_spin_symbol_observation(egg)["removed"], false);
    assert_eq!(state.pre_spin_symbol_observation(egg)["stored_as_egg"], false);
    assert_eq!(state.pre_spin_symbol_observation(goldfish)["removed"], true);
}
