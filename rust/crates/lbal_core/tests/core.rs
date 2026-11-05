use lbal_core::{run_episode, GameData, Phase, RunState, Symbol, RENT_SCHEDULE_FLOOR_20};

fn data() -> GameData {
    GameData::load(lbal_core::default_data_dir()).expect("game data should load")
}

fn act(state: &mut RunState) {
    match state.phase.clone() {
        Phase::SpinReward { .. } => state.choose_spin(0),
        Phase::SymbolReward { .. } => state.choose_symbol(0),
        Phase::ItemReward { .. } => state.choose_item(0),
        Phase::EssenceReward { .. } => state.choose_essence(0),
        Phase::Ready => {
            state.spin();
        }
        Phase::Done => {}
    }
}

#[test]
fn data_counts() {
    let data = data();
    assert_eq!(data.symbols.len(), 168);
    assert_eq!(data.items.len(), 114);
    assert_eq!(data.essences.len(), 113);
    assert_eq!(data.fine_print_count, 37);
    assert!(data.symbol("coin").is_some());
    assert!(data.symbol("matryoshka_doll_2").is_some());
}

#[test]
fn rent_schedule_is_floor_20() {
    assert_eq!(RENT_SCHEDULE_FLOOR_20.len(), 12);
    assert_eq!(RENT_SCHEDULE_FLOOR_20[0], 25);
    assert_eq!(RENT_SCHEDULE_FLOOR_20[6], 375);
    assert_eq!(RENT_SCHEDULE_FLOOR_20[11], 777);
}

#[test]
fn same_seed_is_reproducible() {
    let data = data();
    let a = run_episode(&data, 12345, 10_000);
    let b = run_episode(&data, 12345, 10_000);
    assert_eq!(a.won, b.won);
    assert_eq!(a.rents_paid, b.rents_paid);
    assert_eq!(a.coins, b.coins);
    assert_eq!(a.total_spins, b.total_spins);
    assert_eq!(a.deck_len, b.deck_len);
}

#[test]
fn different_seeds_usually_differ() {
    let data = data();
    let results: Vec<_> = (1..=20)
        .map(|seed| run_episode(&data, seed, 10_000))
        .collect();
    let distinct = results
        .iter()
        .map(|result| (result.total_spins, result.rents_paid, result.coins))
        .collect::<std::collections::HashSet<_>>();
    assert!(distinct.len() > 1, "all seeds produced identical episodes");
}

#[test]
fn hundred_games_run_without_panic() {
    let data = data();
    for seed in 0..100 {
        let result = run_episode(&data, seed, 10_000);
        assert!(result.total_spins <= 10_000);
        assert!(result.rents_paid <= 25);
    }
}

#[test]
fn initial_deck_has_eight_symbols_with_duds() {
    let data = data();
    let state = RunState::new(&data, 1);
    let names: Vec<&str> = state
        .deck
        .iter()
        .map(|symbol| data.symbols[symbol.type_id as usize].name.as_str())
        .collect();
    assert_eq!(names.len(), 8);
    for expected in ["coin", "cherry", "pearl", "flower", "cat"] {
        assert!(names.contains(&expected), "missing {expected}");
    }
    assert_eq!(names.iter().filter(|name| **name == "dud").count(), 3);
}

#[test]
fn skipping_every_reward_cannot_pay_first_rent() {
    let data = data();
    let mut state = RunState::new(&data, 7);
    for _ in 0..5 {
        state.spin();
        while matches!(state.phase, Phase::SpinReward { .. }) {
            state.skip_spin();
        }
    }
    assert_eq!(state.rents_paid, 0);
    assert!(state.done());
}

#[test]
fn first_rent_is_paid_after_five_spins() {
    let data = data();
    let mut state = RunState::new(&data, 7);
    let mut guard = 0;
    while state.rents_paid < 1 && !state.done() && guard < 200 {
        act(&mut state);
        guard += 1;
    }
    assert_eq!(state.rents_paid, 1);
    assert_eq!(state.spins_left, 5);
    assert!(matches!(state.phase, Phase::SymbolReward { .. }));
}

#[test]
fn first_spin_reward_is_three_commons() {
    let data = data();
    let mut state = RunState::new(&data, 7);
    state.spin();
    let Phase::SpinReward { offer } = &state.phase else {
        panic!("expected a regular spin reward");
    };
    assert_eq!(offer.len(), 3);
    for type_id in offer {
        assert_eq!(data.symbols[*type_id].rarity, "common");
    }
}

#[test]
fn first_reward_is_three_uncommons() {
    let data = data();
    let mut state = RunState::new(&data, 7);
    let mut guard = 0;
    while state.rents_paid < 1 && guard < 200 {
        act(&mut state);
        guard += 1;
    }
    while matches!(state.phase, Phase::SpinReward { .. }) {
        state.skip_spin();
    }
    let Phase::SymbolReward { offer } = &state.phase else {
        panic!("expected a symbol reward");
    };
    assert_eq!(offer.len(), 3);
    for type_id in offer {
        assert_eq!(data.symbols[*type_id].rarity, "uncommon");
    }
}

#[test]
fn comrade_tokens_start_on_third_payment() {
    let data = data();
    let mut state = RunState::new(&data, 7);
    let mut guard = 0;
    while state.rents_paid < 3 && !state.done() && guard < 500 {
        act(&mut state);
        guard += 1;
    }
    if state.rents_paid >= 3 {
        assert!(state.reroll_tokens >= 1);
        assert!(state.removal_tokens >= 1);
    }
}

#[test]
fn dud_is_added_on_fifteenth_spin() {
    let data = data();
    let mut state = RunState::new(&data, 3);
    let mut guard = 0;
    while state.total_spins < 15 && !state.done() && guard < 500 {
        act(&mut state);
        guard += 1;
    }
    if state.done() {
        return;
    }
    while !matches!(state.phase, Phase::Ready) && !state.done() {
        act(&mut state);
    }
    if state.done() {
        return;
    }
    state.spin();
    let duds = state
        .deck
        .iter()
        .filter(|symbol| data.symbols[symbol.type_id as usize].name == "dud")
        .count();
    assert!(duds >= 4, "expected a new dud after fifteen spins, got {duds}");
}

#[test]
fn symbol_reward_is_followed_by_item_reward() {
    let data = data();
    let mut state = RunState::new(&data, 7);
    let mut guard = 0;
    while state.rents_paid < 1 && guard < 200 {
        act(&mut state);
        guard += 1;
    }
    while matches!(state.phase, Phase::SpinReward { .. }) {
        state.skip_spin();
    }
    assert!(matches!(state.phase, Phase::SymbolReward { .. }));
    state.choose_symbol(0);
    let Phase::ItemReward { offer } = &state.phase else {
        panic!("expected an item reward");
    };
    assert_eq!(offer.len(), 3);
}

#[test]
fn fine_print_24_raises_reroll_cost() {
    let data = data();
    let mut state = RunState::new(&data, 9);
    state.spin();
    assert!(matches!(state.phase, Phase::SpinReward { .. }));
    state.fine_prints.push(24);
    state.reroll_tokens = 1;
    state.reroll_reward();
    assert_eq!(state.reroll_tokens, 1, "cost should be 2 with clause 24");
    state.reroll_tokens = 2;
    state.reroll_reward();
    assert_eq!(state.reroll_tokens, 0);
}

#[test]
fn fine_print_25_raises_removal_cost() {
    let data = data();
    let mut state = RunState::new(&data, 9);
    let before = state.deck.len();
    state.fine_prints.push(25);
    state.removal_tokens = 1;
    state.remove_symbol(0);
    assert_eq!(state.deck.len(), before, "cost should be 2 with clause 25");
    state.removal_tokens = 2;
    state.remove_symbol(0);
    assert_eq!(state.deck.len(), before - 1);
    assert_eq!(state.removal_tokens, 0);
}

fn names(values: &[&str]) -> Vec<String> {
    values.iter().map(|value| value.to_string()).collect()
}

fn entries(data: &GameData, values: &[&str]) -> Vec<(String, i32, u32, u32)> {
    values
        .iter()
        .map(|value| {
            let id = data.symbol_id(value).expect("symbol");
            (value.to_string(), data.symbols[id].base_value, 0, 0)
        })
        .collect()
}

#[test]
fn spin_costs_one_coin() {
    let data = data();
    let mut state = RunState::new(&data, 11);
    let dud = data.symbol_id("dud").expect("dud");
    state.deck.clear();
    for _ in 0..5 {
        state.deck.push(Symbol::new(dud, 0));
    }
    state.coins = 1;
    state.spin();
    assert_eq!(state.coins, 0);
    while matches!(state.phase, Phase::SpinReward { .. }) {
        state.skip_spin();
    }
    state.spin();
    assert_eq!(state.coins, 0);
}

#[test]
fn twelfth_rent_starts_the_boss_fight() {
    let data = data();
    let mut state = RunState::new(&data, 31);
    let dud = data.symbol_id("dud").expect("dud");
    state.deck.clear();
    for _ in 0..5 {
        state.deck.push(Symbol::new(dud, 0));
    }
    state.coins = 778;
    state.rents_paid = 11;
    state.spins_left = 1;
    state.spin();
    assert_eq!(state.rents_paid, 12);
    assert!(state.boss_fight_active());
    assert_eq!(state.boss_hp(), 1500);
    assert_eq!(state.rent_due(), 1000);
    assert_eq!(state.spins_left, 10);
}

#[test]
fn piggy_bank_is_smashed_to_pay_rent() {
    let data = data();
    let mut state = RunState::new(&data, 21);
    let dud = data.symbol_id("dud").expect("dud");
    state.deck.clear();
    for _ in 0..5 {
        state.deck.push(Symbol::new(dud, 0));
    }
    state.items_held.push(data.item_index["piggy_bank"]);
    state.coins = 24;
    state.spins_left = 1;
    state.spin();
    // Spin leaves 23 coins; the piggy bank saved 2 and pays round(2 * 2.5) = 5.
    assert_eq!(state.rents_paid, 1);
    assert_eq!(state.coins, 3);
    assert!(state
        .items_held
        .iter()
        .all(|id| data.items[*id].name != "piggy_bank"));
}

#[test]
fn guillotine_destroys_billionaires() {
    let data = data();
    let mut state = RunState::new(&data, 22);
    let dud = data.symbol_id("dud").expect("dud");
    let billionaire = data.symbol_id("billionaire").expect("billionaire");
    state.deck.clear();
    state
        .deck
        .push(Symbol::new(billionaire, data.symbols[billionaire].base_value));
    for _ in 0..4 {
        state.deck.push(Symbol::new(dud, 0));
    }
    state.items_held.push(data.item_index["guillotine"]);
    let outcome = state.spin();
    assert!(state
        .deck
        .iter()
        .all(|symbol| symbol.type_id as usize != billionaire));
    assert_eq!(outcome.income, 39);
}

#[test]
fn cardboard_box_grants_removal_token_every_ten_spins() {
    let data = data();
    let mut state = RunState::new(&data, 23);
    let dud = data.symbol_id("dud").expect("dud");
    state.deck.clear();
    for _ in 0..5 {
        state.deck.push(Symbol::new(dud, 0));
    }
    state.items_held.push(data.item_index["cardboard_box"]);
    state.coins = 1000;
    state.spins_left = 100;
    for _ in 0..10 {
        state.spin();
        while matches!(state.phase, Phase::SpinReward { .. }) {
            state.skip_spin();
        }
    }
    assert_eq!(state.rents_paid, 0);
    assert_eq!(state.removal_tokens, 1);
}

#[test]
fn replay_income_applies_joker_to_adjacent_suits() {
    let data = data();
    let deck = entries(&data, &["joker", "clubs"]);
    let mut board = names(&["empty"; 20]);
    board[0] = "joker".to_string();
    board[1] = "clubs".to_string();
    let joker = data.symbol_id("joker").expect("joker");
    let clubs = data.symbol_id("clubs").expect("clubs");
    let expected = data.symbols[joker].base_value + data.symbols[clubs].base_value * 2;
    assert_eq!(
        RunState::replay_income(&data, &deck, &[], &board).map(|(income, _)| income),
        Some(expected)
    );
}

#[test]
fn replay_income_matches_simple_board() {
    let data = data();
    let deck = entries(&data, &["coin", "cherry", "dud"]);
    let mut board = names(&["empty"; 20]);
    board[0] = "coin".to_string();
    board[7] = "cherry".to_string();
    assert_eq!(
        RunState::replay_income(&data, &deck, &[], &board).map(|(income, _)| income),
        Some(2)
    );
}

#[test]
fn replay_income_applies_bee_adjacency() {
    let data = data();
    let deck = entries(&data, &["bee", "flower"]);
    let mut board = names(&["empty"; 20]);
    board[0] = "bee".to_string();
    board[1] = "flower".to_string();
    // Flower is doubled by the bee, then gains the bee's +1: 1 * 2 + 1.
    assert_eq!(
        RunState::replay_income(&data, &deck, &[], &board).map(|(income, _)| income),
        Some(4)
    );
}

#[test]
fn replay_income_applies_dog_reverse_adjacency() {
    let data = data();
    let deck = entries(&data, &["dog", "toddler"]);
    let mut board = names(&["empty"; 20]);
    board[0] = "dog".to_string();
    board[1] = "toddler".to_string();
    // Toddler (doglikes) gains +2 from the adjacent dog.
    assert_eq!(
        RunState::replay_income(&data, &deck, &[], &board).map(|(income, _)| income),
        Some(4)
    );
}
