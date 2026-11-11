use super::*;

#[path = "catalog_matrix_tests.rs"]
mod matrix;

fn data() -> GameData { GameData::load(crate::default_data_dir()).unwrap() }

fn fixed<'a>(data: &'a GameData, symbols: &[(&str,usize)]) -> RunState<'a> {
    let mut s = RunState::new(data,1);
    s.deck.clear(); s.board = Board::empty(); s.coins = 10000; s.spins_left = 1000;
    for &(name,slot) in symbols {
        let id = data.symbol_id(name).unwrap();
        let index = s.deck.len(); s.deck.push(Symbol::new(id,data.symbols[id].base_value));
        if slot < BOARD_SLOTS { s.board.slots[slot] = Some(index); }
    }
    s
}

fn names<'a>(s: &RunState<'a>) -> Vec<&'a str> {
    s.deck.iter().map(|sym| s.data.symbols[sym.type_id as usize].name.as_str()).collect()
}

#[test]
fn visible_newborn_rarity_modifiers_apply_once_and_hidden_ones_do_not() {
    let d = data();
    let mut s = fixed(&d,&[("shiny_pebble",1),("shiny_pebble",6),("rabbit_fluff",20)]);
    s.spin_rarity_factor = 1.2;
    let mut income = 0;
    let mut full = false;
    s.apply_spawned_effects(&[0,1,2],&[None;BOARD_SLOTS],&[0;BOARD_SLOTS],
        &HashMap::new(),0,&mut full,&mut income);
    assert!((s.spin_rarity_factor - 1.2 * 1.1 * 1.1).abs() < 1e-12);
}

#[test]
fn seed_1234567_boss_draw_matches_native_after_two_income_payments() {
    let d = data();
    let mut s = fixed(&d,&[("diver",6)]);
    s.rng = RngStreams::from_seed_text("1234567");
    s.boss_fight = true;
    s.boss_hp = 1500;
    s.apply_boss_damage(118);
    s.boss_hp -= 118;
    assert!(s.pending_fine_prints.is_empty());
    s.apply_boss_damage(82);
    s.boss_hp -= 82;
    assert!(s.fine_prints.is_empty());
    assert_eq!(s.pending_fine_prints,vec![(19,Some("diver".into())),(21,None),(4,None)]);
    assert!(!names(&s).contains(&"dud"), "Dud must not precede the chosen symbol");
    s.phase = Phase::SpinReward { offer:vec![d.symbol_id("highlander").unwrap()] };
    s.choose_spin(0);
    assert_eq!(s.fine_prints,vec![19,21,4]);
    assert_eq!(names(&s),vec!["diver","highlander","dud"]);
    assert!(s.fp_affects(19,"diver"));
    assert!(!s.fp_affects(19,"archaeologist"));
    let mut expected = s.rng.fineprint().clone();
    let details = s.fine_print_details();
    assert_eq!(details["damage_to_next"],100);
    assert_eq!(s.rng.fineprint().randf(),expected.randf(),"UI reads must not advance RNG");
}

#[test]
fn chinese_clause37_keeps_later_fineprints_and_rng_in_sync_with_native() {
    let d = data();
    let mut s = fixed(&d,&[("diver",0),("diamond",1),("dwarf",2)]);
    s.items_held.push(d.item_index["green_pepper"]);
    s.rng = RngStreams::from_seed_text("1234567");
    s.boss_fight = true;
    s.boss_hp = 1500;
    for damage in [118,82,69,67,58,65] {
        s.apply_boss_damage(damage);
        s.boss_hp -= damage;
        s.activate_pending_fine_prints();
    }
    assert_eq!(s.fine_prints,vec![19,21,4,28,26,37,27,17,20]);
    assert!(!s.fine_print_icons.contains_key(&37));
    assert_eq!(s.fine_print_icons.get(&27).map(String::as_str),Some("diamond"));
    assert_eq!(s.fine_print_icons.get(&17).map(String::as_str),Some("green_pepper"));
    // Exact state captured from the Steam seed mod's saved rng_state.json.
    assert_eq!(s.rng.fineprint().state_pair(),[-4671625087459783101,2885390081777926815]);
}

#[test]
fn missing_dynamic_target_does_not_disable_all_capsules() {
    let d = data();
    let mut s = fixed(&d,&[("reroll_capsule",6)]);
    s.fine_prints.push(37);
    assert!(!s.fp_affects(37,"reroll_capsule"));
    let before = s.reroll_tokens;
    s.resolve_spin_effects();
    assert!(s.deck.is_empty());
    assert!(s.reroll_tokens > before);
    assert_eq!(s.destroyed_history,vec![d.symbol_id("reroll_capsule").unwrap() as u16]);
}

#[test]
fn periodic_dud_overflow_waits_for_the_following_spin() {
    let d = data();
    let entries:Vec<_> = (0..26).map(|i|("coin",i)).collect();
    let mut s = fixed(&d,&entries);
    s.total_spins = 14;
    s.spin();s.skip_spin();
    assert_eq!(s.total_spins,15);
    s.spin();
    let dud=s.deck.len()-1;
    assert_eq!(s.data.symbols[s.deck[dud].type_id as usize].name,"dud");
    assert_eq!(s.board.deferred.iter().flatten().copied().collect::<Vec<_>>(),vec![dud]);
    assert!(!s.board.slots.contains(&Some(dud)),"new overflow Dud must not join this spin's shuffle");
    s.skip_spin();s.spin();
    assert!(s.board.deferred.iter().all(Vec::is_empty));
    assert!(s.board.reels.iter().flatten().any(|entry| *entry==Some(dud)));
}

#[test]
fn rent_offer_precedes_frozen_pizza_bonus_after_essence_choice() {
    let d = data();
    let mut s = fixed(&d,&[("coin",0)]);
    s.rents_paid=8;
    s.pending_essence_offers=1;
    s.pending_extra_symbol_offers=1;
    s.phase=Phase::EssenceReward {offer:Vec::new()};
    s.skip_essence();
    let Phase::SymbolReward {offer} = &s.phase else {panic!("rent offer must be first")};
    assert!(offer.iter().any(|&id| d.symbols[id].rarity=="rare"));
    assert!(offer.iter().all(|&id| d.symbols[id].rarity!="common"));
    assert_eq!(s.pending_extra_symbol_offers,1);
    s.skip_symbol();
    assert!(matches!(s.phase,Phase::SpinReward {..}));
    s.skip_spin();
    assert!(matches!(s.phase,Phase::ItemReward {..}));
}

#[test]
fn new_scalers_do_not_inherit_another_instances_growth() {
    let d = data();
    for name in ["diver","archaeologist","mrs_fruit","pirate","dove","pear","amethyst"] {
        let mut s = fixed(&d,&[(name,6)]);
        s.deck[0].bonus = 8;
        s.add_symbols(name,1);
        assert_eq!(s.deck[0].bonus,8,"{name}");
        assert_eq!(s.deck[1].bonus,0,"new {name} must have independent growth");
    }
}

#[test]
fn eldritch_growth_uses_both_histories_and_applies_to_future_instances() {
    let d = data(); let mut s = fixed(&d,&[("eldritch_beast",6)]);
    s.destroyed_history.push(d.symbol_id("cultist").unwrap() as u16);
    s.removed_history.push(d.symbol_id("witch").unwrap() as u16);
    s.destroyed_history.push(d.symbol_id("spirit").unwrap() as u16);
    s.add_symbols("eldritch_beast",1);
    assert_eq!(s.fossil_progress(),2);
    for symbol in &s.deck {
        assert_eq!(symbol.bonus,0);
        assert_eq!(s.symbol_rule_bonus(symbol.type_id as usize),2);
    }
}

#[test]
fn shared_pearl_grows_diver_and_geologist_in_both_column_orders() {
    let d = data();
    for (left,right) in [("diver","archaeologist"),("archaeologist","diver")] {
        let mut s = fixed(&d,&[(left,6),(right,7),("pearl",1)]);
        s.items_held.push(d.item_index["black_pepper"]);
        s.essences_held.push(d.essence_index["void_portal_essence"]);
        assert_eq!(s.resolve_spin_effects(),7,"reference: 3+3+pearl's own 1");
        assert_eq!(s.deck.len(),2);
        assert_eq!(s.deck[0].bonus,1); assert_eq!(s.deck[1].bonus,1);
        assert!(s.destroyed_history.is_empty());
        assert_eq!(s.removed_history,vec![d.symbol_id("pearl").unwrap() as u16]);
        assert!(s.has_essence("void_portal_essence"));
    }
}

#[test]
fn shared_pearl_removal_can_grow_geologist_even_with_a_dove() {
    let d = data();
    let mut s = fixed(&d,&[("diver",6),("archaeologist",7),("pearl",1),("dove",2)]);
    s.resolve_spin_effects();
    assert_eq!(s.deck[0].bonus,1); assert_eq!(s.deck[1].bonus,1);
    assert_eq!(s.deck[2].bonus,0,"a dove does not prevent a removal");
    assert!(s.destroyed_history.is_empty());
}

#[test]
fn newborn_pearl_can_feed_both_consumers() {
    let d = data();
    let mut s = fixed(&d,&[("diver",6),("archaeologist",7)]);
    s.essences_held.push(d.essence_index["telescope_essence"]);
    s.queued_spin_symbols.push(d.symbol_id("pearl").unwrap());
    s.resolve_spin_effects();
    assert_eq!(s.deck.len(),2); assert_eq!(s.deck[0].bonus,1); assert_eq!(s.deck[1].bonus,1);
    assert_eq!(s.removed_history.len(),1); assert!(s.destroyed_history.is_empty());
}

#[test]
fn removed_oyster_spawns_a_pearl_that_can_be_removed_this_spin() {
    let mut d = data(); let oyster = d.symbol_id("oyster").unwrap(); d.symbols[oyster].values[0] = 0.0;
    let mut s = fixed(&d,&[("diver",6),("oyster",7)]);
    s.essences_held.push(d.essence_index["telescope_essence"]);
    assert_eq!(s.resolve_spin_effects(),6);
    assert_eq!(names(&s),vec!["diver"]); assert_eq!(s.deck[0].bonus,2);
    assert_eq!(s.removed_history.len(),2); assert!(s.destroyed_history.is_empty());
}

#[test]
fn manual_oyster_removal_and_removal_token_symbols_use_removal_callbacks() {
    let d = data();
    for name in ["oyster","pufferfish","jellyfish","sand_dollar"] {
        let mut s = fixed(&d,&[(name,6)]); s.removal_tokens = 1;
        let coins = s.coins; s.remove_symbol(0);
        assert!(s.destroyed_history.is_empty()); assert_eq!(s.removed_history.len(),1);
        match name {
            "oyster" => assert_eq!(names(&s),vec!["pearl"]),
            "pufferfish" => assert_eq!(s.reroll_tokens,1),
            "jellyfish" => assert_eq!(s.removal_tokens,1),
            "sand_dollar" => assert_eq!(s.coins-coins,10), _ => unreachable!(),
        }
    }
}

#[test]
fn permanent_essence_bonuses_also_apply_to_future_symbols() {
    let d = data();
    let cases = [
        ("cleaning_rag_essence","pearl",5,0,2.0),
        ("triple_coins_essence","coin",3,2,1.0),
        ("ricky_the_banana_essence","banana",3,2,1.0),
        ("fruit_basket_essence","cherry",5,0,2.0),
        ("holy_water_essence","hex_of_tedium",6,0,2.0),
    ];
    for (essence,name,n,bonus,mult) in cases {
        let board: Vec<_> = (0..n).map(|slot| (name,slot)).collect();
        let mut s = fixed(&d,&board); s.essences_held.push(d.essence_index[essence]);
        s.resolve_spin_effects(); assert!(!s.has_essence(essence),"{essence}");
        s.add_symbols(name,1); let sym = s.deck.last().unwrap();
        assert_eq!(sym.bonus,0);
        assert_eq!(s.symbol_rule_bonus(sym.type_id as usize),bonus,"{essence}");
        assert_eq!(s.symbol_rule_multiplier(sym.type_id as usize),mult,"{essence}");
    }
}

#[test]
fn event_essences_do_not_expire_on_empty_spins() {
    let d = data();
    for name in ["coffee_essence","devils_deal_essence","lucky_dice_essence","reroll_essence",
        "lockpick_essence","time_machine_essence","happy_hour_essence","lunchbox_essence",
        "adoption_papers_essence","symbol_bomb_small_essence","symbol_bomb_big_essence",
        "symbol_bomb_very_big_essence","symbol_bomb_quantum_essence","shattered_mirror_essence",
        "frozen_pizza_essence","bag_of_holding_essence","chili_powder_essence","capsule_machine_essence"] {
        let mut s = fixed(&d,&[]); s.essences_held.push(d.essence_index[name]);
        for _ in 0..35 { s.resolve_spin_effects(); }
        assert!(s.has_essence(name),"idle spins must not consume {name}");
        assert_eq!(s.essence_saved.get(&d.essence_index[name]).copied().unwrap_or(0),0,"{name}");
    }
}

#[test]
fn reroll_essence_rerolls_only_low_dice_and_expires_on_tenth_actual_reroll() {
    let d = data(); let mut s = fixed(&d,&[]); let id = d.essence_index["reroll_essence"];
    s.essences_held.push(id);
    assert_eq!(s.roll_die("d3",3,Some(2)),2);
    assert_eq!(s.roll_die("d5",5,Some(3)),3); assert_eq!(s.reroll_essence_rerolls,0);
    for _ in 0..9 { assert_eq!(s.roll_die("d3",3,Some(1)),3); }
    assert!(s.has_essence("reroll_essence")); assert_eq!(s.reroll_essence_rerolls,9);
    assert_eq!(s.roll_die("d5",5,Some(2)),5); assert!(!s.has_essence("reroll_essence"));
}

#[test]
fn lucky_dice_essence_is_max_plus_one_and_counts_both_dice() {
    let d = data(); let mut s = fixed(&d,&[("d3",6),("d5",7)]);
    s.essences_held.push(d.essence_index["lucky_dice_essence"]);
    for i in 0..5 {
        assert_eq!(s.resolve_spin_effects(),10);
        assert_eq!(s.has_essence("lucky_dice_essence"),i < 4);
    }
}

#[test]
fn essence_charges_count_player_additions_and_not_skips_or_spins() {
    let d = data(); let mut s = fixed(&d,&[]);
    let id = d.essence_index["symbol_bomb_small_essence"]; s.essences_held.push(id);
    s.resolve_spin_effects(); assert_eq!(s.removal_tokens,0);
    s.phase = Phase::SpinReward { offer: vec![d.symbol_id("coin").unwrap()] }; s.skip_spin();
    assert_eq!(s.removal_tokens,0); assert!(s.has_essence("symbol_bomb_small_essence"));
    s.add_symbols("coin",1); assert_eq!(s.removal_tokens,0);
    s.add_symbols("coin",1); assert_eq!(s.removal_tokens,1); assert!(!s.has_essence("symbol_bomb_small_essence"));
}

#[test]
fn popsicle_resets_lunchbox_progress_and_pays_on_both_triggers() {
    let d = data(); let mut s = fixed(&d,&[]); s.items_held.push(d.item_index["popsicle"]);
    let id = d.essence_index["lunchbox_essence"]; s.essences_held.push(id); let coins = s.coins;
    s.add_symbols("cherry",7); assert_eq!(s.coins-coins,33);
    assert!(s.has_essence("lunchbox_essence")); assert_eq!(s.essence_saved[&id],0);
    s.add_symbols("cherry",1); assert_eq!(s.coins-coins,33);
    s.add_symbols("cherry",6); assert_eq!(s.coins-coins,66); assert!(!s.has_essence("lunchbox_essence"));
}

#[test]
fn instant_ramen_essence_waits_for_an_item_skip() {
    let d = data(); let mut s = fixed(&d,&[]); s.grant_essence(d.essence_index["bag_of_holding_essence"]);
    assert_eq!(s.pending_bonus_items,0); s.resolve_spin_effects(); assert!(s.has_essence("bag_of_holding_essence"));
    s.phase = Phase::ItemReward { offer: vec![d.item_index["pool_ball"]] }; s.skip_item();
    assert!(!s.has_essence("bag_of_holding_essence")); assert_eq!(s.pending_bonus_items,1);
    assert!(matches!(s.phase,Phase::ItemReward { .. }));
    s.choose_item(0); assert_eq!(s.pending_bonus_items,0); assert!(matches!(s.phase,Phase::ItemReward { .. }));
    s.choose_item(0); assert_eq!(s.phase,Phase::Ready);
}

#[test]
fn lockpick_essence_opens_chests_and_counts_deaths() {
    let d = data(); let mut s = fixed(&d,&[("lockbox",6),("safe",7),("treasure_chest",8)]);
    s.essences_held.push(d.essence_index["lockpick_essence"]);
    assert_eq!(s.resolve_spin_effects(),(1+15)+(1+30)+(2+50));
    assert!(s.deck.is_empty()); assert_eq!(s.destroyed_history.len(),3); assert!(!s.has_essence("lockpick_essence"));
}

#[test]
fn time_machine_essence_counts_two_coal_transformations() {
    let d = data(); let mut s = fixed(&d,&[("coal",6),("coal",7)]);
    s.essences_held.push(d.essence_index["time_machine_essence"]);
    assert_eq!(s.resolve_spin_effects(),10); assert_eq!(names(&s),vec!["diamond","diamond"]);
    assert!(!s.has_essence("time_machine_essence")); assert!(s.destroyed_history.is_empty());
}

#[test]
fn rabbit_and_wine_mature_only_once() {
    let d = data();
    for (name,count,bonus) in [("rabbit",10,2),("wine",8,1)] {
        let mut s = fixed(&d,&[(name,6)]); s.deck[0].times_coins_given = count-1;
        s.resolve_spin_effects(); assert_eq!(s.deck[0].bonus,bonus);
        for _ in 0..40 { s.resolve_spin_effects(); }
        assert_eq!(s.deck[0].bonus,bonus,"{name} must not mature repeatedly");
    }
}

#[test]
fn pear_and_amethyst_do_not_grow_without_a_symbol_buff() {
    let d = data();
    for name in ["pear","amethyst"] {
        let mut s = fixed(&d,&[(name,6)]);
        for _ in 0..10 { s.resolve_spin_effects(); } assert_eq!(s.deck[0].bonus,0);
    }
}

#[test]
fn capsule_machine_essence_applies_to_all_capsules_this_spin_and_expires() {
    let d = data(); let mut s = fixed(&d,&[("lucky_capsule",6),("lucky_capsule",7)]);
    s.items_held.push(d.item_index["capsule_machine"]); s.essences_held.push(d.essence_index["capsule_machine_essence"]);
    assert_eq!(s.resolve_spin_effects(),120); assert!(!s.has_essence("capsule_machine_essence"));
    s.add_symbols("lucky_capsule",1); assert_eq!(s.resolve_spin_effects(),20);
}

#[test]
fn chemical_seven_is_not_a_capsule_machine_target() {
    let d = data(); let mut s = fixed(&d,&[("chemical_seven",6)]);
    s.items_held.push(d.item_index["capsule_machine"]); s.essences_held.push(d.essence_index["capsule_machine_essence"]);
    assert_eq!(s.resolve_spin_effects(),7); assert!(s.has_essence("capsule_machine_essence"));
}

#[test]
fn newborn_token_capsules_and_money_capsules_resolve_this_spin() {
    let d = data();
    for (name,coins,reroll,removal,essence) in [
        ("lucky_capsule",20,0,0,0),("reroll_capsule",0,2,0,0),("removal_capsule",0,0,2,0),
        ("essence_capsule",-12,0,0,2),("tedium_capsule",10,0,0,0)] {
        let mut s = fixed(&d,&[]); s.items_held.push(d.item_index["capsule_machine"]);
        s.queued_spin_symbols.push(d.symbol_id(name).unwrap());
        assert_eq!(s.resolve_spin_effects(),coins,"{name}"); assert!(s.deck.is_empty(),"{name}");
        assert_eq!((s.reroll_tokens,s.removal_tokens,s.essence_tokens),(reroll,removal,essence));
        assert_eq!(s.destroyed_history.len(),1);
    }
}

#[test]
fn dove_protects_newborn_capsules_before_their_payouts() {
    let d = data();
    for name in ["lucky_capsule","reroll_capsule","removal_capsule","essence_capsule","chemical_seven"] {
        let mut s = fixed(&d,&[("dove",6)]); s.essences_held.push(d.essence_index["telescope_essence"]);
        s.queued_spin_symbols.push(d.symbol_id(name).unwrap());
        s.resolve_spin_effects(); assert!(names(&s).contains(&name),"{name}");
        assert!(s.destroyed_history.is_empty()); assert_eq!((s.reroll_tokens,s.removal_tokens,s.essence_tokens),(0,0,0));
        assert!(!s.has_item("lucky_seven"));
    }
}

#[test]
fn resurrection_has_a_per_symbol_limit_and_can_repeat_in_the_same_spin() {
    let mut d = data(); let id = d.item_index["mobius_strip"]; d.items[id].values[0] = 100.0;
    let mut s = fixed(&d,&[("lucky_capsule",6),("lucky_capsule",7)]); s.items_held.push(id);
    assert_eq!(s.resolve_spin_effects(),160,"two lineages, 8 payouts of 10 each");
    assert_eq!(s.destroyed_history.len(),16); assert!(s.deck.is_empty());
}

#[test]
fn undertaker_prevents_spirit_expiry() {
    let d = data(); let mut s = fixed(&d,&[("spirit",6)]); s.items_held.push(d.item_index["undertaker"]);
    s.deck[0].times_coins_given = 3;
    for _ in 0..6 { s.resolve_spin_effects(); }
    assert_eq!(names(&s),vec!["spirit"]); assert!(s.destroyed_history.is_empty());
}

#[test]
fn frying_pan_essence_buffs_current_and_future_omelettes_without_double_bonus() {
    let mut d = data(); let egg = d.symbol_id("egg").unwrap(); d.symbols[egg].values[0] = 0.0;
    let mut s = fixed(&d,&[("egg",6),("omelette",7)]); s.essences_held.push(d.essence_index["frying_pan_essence"]);
    assert_eq!(s.resolve_spin_effects(),31,"egg 1 + two omelettes (3+2)*3");
    assert_eq!(names(&s),vec!["omelette","omelette"]);
    s.add_symbols("omelette",1); assert_eq!(s.symbol_rule_multiplier(d.symbol_id("omelette").unwrap()),3.0);
}

#[test]
fn xray_essence_applies_to_all_ores_including_big_ore() {
    let d = data(); let mut s = fixed(&d,&[("miner",6),("ore",7),("big_ore",1)]);
    s.essences_held.push(d.essence_index["x_ray_machine_essence"]);
    s.resolve_spin_effects(); assert!(!s.has_essence("x_ray_machine_essence"));
    assert_eq!(names(&s).iter().filter(|&&n| n == "diamond").count(),3);
}

#[test]
fn consumer_essence_does_not_fire_when_another_symbol_kills_its_food() {
    let mut d = data(); let hex = d.symbol_id("hex_of_destruction").unwrap(); d.symbols[hex].values[0] = 100.0;
    let mut s = fixed(&d,&[("hex_of_destruction",0),("candy",1),("toddler",19)]);
    s.essences_held.push(d.essence_index["jackolantern_essence"]); s.resolve_spin_effects();
    assert!(s.has_essence("jackolantern_essence"));
    assert!(!names(&s).contains(&"candy"));
}

#[test]
fn arrow_quiver_essence_expires_when_an_arrow_hits_nothing() {
    let d = data(); let mut s = fixed(&d,&[("bronze_arrow",6)]);
    s.essences_held.push(d.essence_index["quiver_essence"]); s.resolve_spin_effects();
    assert!(!s.has_essence("quiver_essence"));
}

#[test]
fn banana_feeds_both_monkey_and_fruit_lady_in_either_order() {
    let d = data();
    for (left,right) in [("mrs_fruit","monkey"),("monkey","mrs_fruit")] {
        let mut s = fixed(&d,&[(left,6),(right,7),("banana",1)]);
        s.essences_held.push(d.essence_index["oswald_the_monkey_essence"]);
        s.resolve_spin_effects();
        let fruit = s.deck.iter().find(|sym| d.symbols[sym.type_id as usize].name == "mrs_fruit").unwrap();
        assert_eq!(fruit.bonus,1);
        assert!(!s.has_essence("oswald_the_monkey_essence"));
        assert_eq!(s.symbol_rule_multiplier(d.symbol_id("monkey").unwrap()),2.0);
        assert_eq!(s.destroyed_history.iter().filter(|&&id| id == d.symbol_id("banana").unwrap() as u16).count(),1);
        assert!(names(&s).contains(&"banana_peel"));
    }
}

#[test]
fn full_board_peach_seed_banana_chain_stays_in_the_only_empty_position() {
    let mut d = data();
    for def in &mut d.symbols { if def.name != "banana" { def.groups.retain(|g| g != "plant"); } }
    let mut board: Vec<_> = (0..20).map(|slot| ("coin",slot)).collect();
    board[6] = ("mrs_fruit",6); board[7] = ("peach",7);
    let mut s = fixed(&d,&board); s.items_held.push(d.item_index["watering_can"]);
    s.resolve_spin_effects();
    assert_eq!(s.deck.iter().find(|sym| d.symbols[sym.type_id as usize].name == "mrs_fruit").unwrap().bonus,2);
    let index = s.board.slots[7].unwrap(); assert_eq!(d.symbols[s.deck[index].type_id as usize].name,"banana_peel");
    for name in ["peach","banana"] { assert!(s.destroyed_history.contains(&(d.symbol_id(name).unwrap() as u16))); }
    assert!(!names(&s).contains(&"seed")); assert_eq!(s.board.occupied(),20);
}

#[test]
fn full_board_peach_seed_coconut_chain_leaves_second_half_offscreen() {
    let mut d = data();
    for def in &mut d.symbols { if def.name != "coconut" { def.groups.retain(|g| g != "plant"); } }
    let mut board: Vec<_> = (0..20).map(|slot| ("coin",slot)).collect();
    board[6] = ("mrs_fruit",6); board[7] = ("peach",7);
    let mut s = fixed(&d,&board); s.items_held.push(d.item_index["watering_can"]);
    s.resolve_spin_effects();
    assert_eq!(s.deck.iter().find(|sym| d.symbols[sym.type_id as usize].name == "mrs_fruit").unwrap().bonus,3);
    assert_eq!(names(&s).iter().filter(|&&n| n == "coconut_half").count(),1);
    let half = s.deck.iter().position(|sym| d.symbols[sym.type_id as usize].name == "coconut_half").unwrap();
    assert!(s.board_slot_of(half).is_none()); assert_eq!(s.board.occupied(),19);
}

#[test]
fn newborn_spirit_receives_adjacent_witch_multiplier_on_a_full_board() {
    let d = data(); let mut board: Vec<_> = (0..20).map(|slot| ("coin",slot)).collect();
    board[5] = ("hooligan",5); board[6] = ("urn",6); board[7] = ("witch",7);
    let mut s = fixed(&d,&board);
    let income = s.resolve_spin_effects();
    let spirit = s.board.slots[6].unwrap(); assert_eq!(d.symbols[s.deck[spirit].type_id as usize].name,"spirit");
    assert_eq!(income,40,"17 coins + hooligan (2+6) + urn 1 + witch 2 + newborn spirit (6*2)");
}

#[test]
fn seed_probability_bonuses_are_added_as_percentage_points() {
    let d = data(); let mut s = fixed(&d,&[("seed",6),("rain",7),("farmer",1),("sun",5)]);
    assert_eq!(s.seed_growth_chance(0,1,1),175.0,"25+50+50+50");
    s.items_held.push(d.item_index["fertilizer"]); assert_eq!(s.seed_growth_chance(0,1,1),225.0);
    s.items_held.push(d.item_index["watering_can"]); assert_eq!(s.seed_growth_chance(0,1,1),325.0);
}

#[test]
fn fertilizer_essence_covers_all_growing_seeds_this_spin() {
    let d = data(); let mut s = fixed(&d,&[("seed",6),("seed",7)]);
    s.items_held.push(d.item_index["watering_can"]); s.essences_held.push(d.essence_index["fertilizer_essence"]);
    s.resolve_spin_effects(); assert_eq!(names(&s),vec!["watermelon","watermelon"]);
    assert!(!s.has_essence("fertilizer_essence"));
}

#[test]
fn credit_card_changes_the_actual_post_spin_offer_and_reroll_width() {
    let d = data();
    for (ordinary,essence,width) in [(true,false,20),(false,true,30),(true,true,47)] {
        let mut s = fixed(&d,&[]); s.reroll_tokens = 1;
        // Later-rent pools have enough distinct candidates for both bonuses.
        // The opening common-only offer can legitimately exhaust its pool.
        s.total_spins = 20; s.rents_paid = 4;
        if essence { s.essences_held.push(d.essence_index["credit_card_essence"]); }
        if ordinary { s.items_held.push(d.item_index["credit_card"]); s.credit_card_saved = 6; }
        s.spin();
        let Phase::SpinReward { offer } = &s.phase else { panic!("expected post-spin offer"); };
        assert_eq!(offer.len(),width);
        s.reroll_reward(); let Phase::SpinReward { offer } = &s.phase else { unreachable!(); };
        assert_eq!(offer.len(),width,"reroll must preserve credit-card width");
    }
}

#[test]
fn rarity_guarantees_survive_rerolling_the_same_offer() {
    let d = data(); let mut s = fixed(&d,&[]); s.reroll_tokens = 20;
    s.symbol_forced_queue.push((Rarity::VeryRare,true)); let offer = s.roll_spin_reward(); s.phase = Phase::SpinReward { offer };
    for _ in 0..20 {
        let Phase::SpinReward { offer } = &s.phase else { unreachable!(); };
        assert!(offer.iter().any(|&id| s.effective_symbol_rarity(id) == "very_rare"));
        s.reroll_reward();
    }
    assert!(s.symbol_forced_queue.is_empty(),"rerolls do not add guarantees to future offers");
}

#[test]
fn symbol_bomb_booster_and_group_offer_restrictions_survive_reroll() {
    let d = data();
    for (item,n) in [("symbol_bomb_small",2),("symbol_bomb_big",4),("symbol_bomb_very_big",8),("symbol_bomb_quantum",9)] {
        let mut s = fixed(&d,&[]); let id = d.item_index[item]; s.items_held.push(id);
        s.passed_symbols.insert(d.symbol_id("cat").unwrap()); assert!(s.use_item(id));
        assert_eq!(s.pending_bonus_symbols.len(),n-1);
    }
    let mut s = fixed(&d,&[]); s.reroll_tokens = 1; s.items_held.push(d.item_index["booster_pack"]);
    assert!(s.use_item(d.item_index["booster_pack"]));
    s.reroll_reward(); let Phase::SpinReward { offer } = &s.phase else { unreachable!(); };
    assert!(offer.iter().all(|id| *id == offer[0])); assert_eq!(s.effective_symbol_rarity(offer[0]),"common");
    let mut s = fixed(&d,&[]); s.reroll_tokens = 1; s.items_held.push(d.item_index["lunchbox"]);
    assert!(s.use_item(d.item_index["lunchbox"])); s.reroll_reward();
    let Phase::SpinReward { offer } = &s.phase else { unreachable!(); };
    assert!(offer.iter().all(|&id| s.has_group(id,"food")));
}

#[test]
fn shattered_mirror_essence_reduces_offer_width_until_seventh_addition() {
    let d = data(); let mut s = fixed(&d,&[]); s.essences_held.push(d.essence_index["shattered_mirror_essence"]);
    assert_eq!(s.roll_spin_reward().len(),2); let coins = s.coins;
    s.add_symbols("coin",6); assert_eq!(s.roll_spin_reward().len(),2);
    s.add_symbols("coin",1); assert_eq!(s.coins-coins,77); assert_eq!(s.roll_spin_reward().len(),3);
}

#[test]
fn chili_powder_pays_only_for_owned_peppers() {
    let d = data(); let mut s = fixed(&d,&[]); s.items_held.push(d.item_index["chili_powder"]);
    assert_eq!(s.resolve_spin_effects(),0);
    s.items_held.push(d.item_index["brown_pepper"]); assert_eq!(s.resolve_spin_effects(),2);
}

#[test]
fn golem_and_dolls_spawn_their_successor_when_destroyed_by_another_symbol() {
    let mut d = data(); let hex = d.symbol_id("hex_of_destruction").unwrap(); d.symbols[hex].values[0] = 100.0;
    for (name,spawn,n) in [("golem","ore",5),("matryoshka_doll_1","matryoshka_doll_2",1),
        ("matryoshka_doll_2","matryoshka_doll_3",1),("matryoshka_doll_3","matryoshka_doll_4",1),("matryoshka_doll_4","matryoshka_doll_5",1)] {
        let mut s = fixed(&d,&[("hex_of_destruction",6),(name,7)]); s.resolve_spin_effects();
        assert!(!names(&s).contains(&name)); assert_eq!(names(&s).iter().filter(|&&v| v == spawn).count(),n,"{name}");
    }
}

#[test]
fn managed_rent_failure_waits_for_bank_use_and_checks_the_balance_again() {
    let d = data();
    for name in ["piggy_bank", "swear_jar"] {
        let mut s = fixed(&d,&[]); s.manual_decisions = true; s.coins = 10; s.spins_left = 1;
        let id = d.item_index[name]; s.items_held.push(id); s.item_saved.insert(id,30);
        s.spin();
        assert!(s.rent_pending, "{name}"); assert!(!s.done()); assert!(s.has_item(name));
        assert_eq!(s.rents_paid,0); assert_eq!(s.total_spins,1);
        assert!(s.can_use_managed(false,id)); assert!(s.use_managed(false,id));
        assert!(s.coins >= s.rent_due()); assert_eq!(s.rents_paid,0);
        let out = s.spin(); assert!(out.rent_paid); assert_eq!(s.rents_paid,1);
        assert_eq!(s.total_spins,1,"confirming rent does not consume an extra spin");
        assert!(!s.rent_pending); assert!(!s.has_item(name));
    }
}

#[test]
fn managed_rent_can_use_two_banks_in_sequence_or_choose_not_to_use_them() {
    let d = data(); let piggy = d.item_index["piggy_bank"]; let swear = d.item_index["swear_jar"];
    let mut s = fixed(&d,&[]); s.manual_decisions = true; s.coins = 0; s.spins_left = 1;
    s.items_held.extend([piggy,swear]); s.item_saved.insert(piggy,4); s.item_saved.insert(swear,4);
    s.spin(); assert!(s.rent_pending);
    assert!(s.use_item(piggy)); assert!(s.coins < s.rent_due());
    assert!(s.use_item(swear)); assert!(s.coins >= s.rent_due());
    s.spin(); assert_eq!(s.rents_paid,1);
    let mut s = fixed(&d,&[]); s.manual_decisions = true; s.coins = 0; s.spins_left = 1;
    s.items_held.push(piggy); s.item_saved.insert(piggy,30); s.spin(); assert!(s.rent_pending);
    s.spin(); assert!(s.done()); assert!(!s.won); assert!(s.has_item("piggy_bank"));
    assert_eq!(s.failure_reason,Some("unpaid_rent")); assert_eq!(s.total_spins,1);
}

#[test]
fn managed_coffee_and_devils_deal_are_choices_at_the_rent_check() {
    let d = data();
    for name in ["coffee", "devils_deal"] {
        let mut s = fixed(&d,&[]); s.manual_decisions = true; s.coins = 0; s.spins_left = 1;
        let id = d.item_index[name]; s.items_held.push(id);
        assert!(!s.can_use_managed(false,id),"rent-only action must not be available early");
        s.spin(); assert!(s.rent_pending); assert!(s.has_item(name)); assert!(!s.done());
        assert!(s.use_managed(false,id)); assert!(!s.has_item(name)); assert!(!s.rent_pending);
        assert_eq!(s.total_spins,1);
        if name == "coffee" { assert_eq!(s.spins_left,1); assert_eq!(s.rents_paid,0); }
        else {
            assert_eq!(s.rents_paid,1); assert_eq!(names(&s),vec!["dud";6]);
            assert!(s.deck.iter().all(|symbol| symbol.times_displayed == 0));
        }
    }
}

#[test]
fn managed_pillows_allow_accepting_or_declining_without_disabling_the_item() {
    let d = data();
    for essence in [false,true] {
        let name = if essence { "comfy_pillow_essence" } else { "comfy_pillow" };
        let id = if essence { d.essence_index[name] } else { d.item_index[name] };
        for accept in [false,true] {
            let mut s = fixed(&d,&[]); s.manual_decisions = true; s.coins = 100; s.spins_left = if essence { 2 } else { 1 };
            if essence { s.essences_held.push(id); } else { s.items_held.push(id); }
            assert!(s.can_use_managed(essence,id));
            if accept {
                assert!(s.use_managed(essence,id)); assert_eq!(s.total_spins,0); assert_eq!(s.rents_paid,1);
                if essence { assert!(!s.has_essence(name)); } else { assert!(s.has_item(name)); }
                s.choose_symbol(0);
                let Phase::ItemReward { offer } = &s.phase else { panic!("missing pillow item offer"); };
                assert!(offer.iter().any(|&id| if essence { d.items[id].rarity == "very_rare" }
                    else { matches!(d.items[id].rarity.as_str(),"rare" | "very_rare") }));
            } else {
                s.spin(); assert_eq!(s.total_spins,1); assert!(s.item_offer_floor.is_none());
                if essence { assert!(s.has_essence(name)); assert_eq!(s.rents_paid,0); }
                else { assert!(s.has_item(name)); assert_eq!(s.rents_paid,1); }
            }
        }
    }
}

#[test]
fn managed_pillow_respects_popsicle_and_cannot_be_used_in_the_boss_fight() {
    let d = data(); let id = d.essence_index["comfy_pillow_essence"];
    let mut s = fixed(&d,&[]); s.manual_decisions = true; s.coins = 100; s.spins_left = 2;
    s.essences_held.push(id); s.items_held.push(d.item_index["popsicle"]);
    assert!(s.use_managed(true,id)); assert!(s.has_essence("comfy_pillow_essence"));
    s.phase = Phase::Ready; s.boss_fight = true; s.spins_left = 2;
    assert!(!s.can_use_managed(true,id)); assert!(!s.use_managed(true,id));
}

#[test]
fn newborns_trigger_clump_items_without_counting_offscreen_copies_or_repeating_buffs() {
    let d = data();
    for (coin_slots,expected) in [(vec![6,7],6),(vec![6,7,2],8)] {
        let board: Vec<_> = (0..20).filter(|&slot| slot != 1)
            .map(|slot| (if coin_slots.contains(&slot) { "coin" } else { "dud" },slot)).collect();
        let mut s = fixed(&d,&board); s.items_held.push(d.item_index["rusty_gear"]);
        s.queued_spin_symbols.push(d.symbol_id("coin").unwrap());
        assert_eq!(s.resolve_spin_effects(),expected);
    }
    let mut s = fixed(&d,&[("coin",0),("coin",19)]); s.items_held.push(d.item_index["rusty_gear"]);
    s.queued_spin_symbols.push(d.symbol_id("coin").unwrap());
    assert_eq!(s.resolve_spin_effects(),3,"three scattered coins cannot all form one clump");
}

#[test]
fn fifth_newborn_gem_triggers_cleaning_rag_essence_in_the_current_spin() {
    let d = data();
    let board: Vec<_> = (0..20).filter(|&slot| slot != 7)
        .map(|slot| (if [0,1,5,6].contains(&slot) { "pearl" } else { "coin" },slot)).collect();
    let mut s = fixed(&d,&board); s.essences_held.push(d.essence_index["cleaning_rag_essence"]);
    s.queued_spin_symbols.push(d.symbol_id("pearl").unwrap());
    assert_eq!(s.resolve_spin_effects(),25); assert!(!s.has_essence("cleaning_rag_essence"));
    s.add_symbols("pearl",1); assert_eq!(s.symbol_rule_multiplier(d.symbol_id("pearl").unwrap()),2.0);
}

#[test]
fn dove_can_protect_a_light_bulb_when_a_newborn_gem_uses_its_last_charge() {
    let d = data(); let mut s = fixed(&d,&[("light_bulb",6),("dove",7)]); s.deck[0].saved = 4;
    s.essences_held.push(d.essence_index["telescope_essence"]);
    s.queued_spin_symbols.push(d.symbol_id("pearl").unwrap());
    s.resolve_spin_effects(); assert!(names(&s).contains(&"light_bulb"));
    assert_eq!(s.deck[1].bonus,1); assert!(s.destroyed_history.is_empty());
}

#[test]
fn quigley_essence_converts_existing_dogs_on_the_following_spin_and_keeps_type_change_income() {
    let d = data(); let mut s = fixed(&d,&[("dog",6),("dog",7),("dog",1)]);
    s.essences_held.push(d.essence_index["quigley_the_wolf_essence"]);
    assert_eq!(s.resolve_spin_effects(),3); assert_eq!(names(&s),vec!["dog";3]);
    assert!(!s.has_essence("quigley_the_wolf_essence"));
    assert_eq!(s.resolve_spin_effects(),15); assert_eq!(names(&s),vec!["wolf";3]);
    assert_eq!(s.resolve_spin_effects(),12);
    let mut s = fixed(&d,&[]); s.items_held.push(d.item_index["quigley_the_wolf"]);
    s.queued_spin_symbols.push(d.symbol_id("dog").unwrap());
    assert_eq!(s.resolve_spin_effects(),4); assert_eq!(names(&s),vec!["wolf"]);
}

#[test]
fn living_soap_cannot_use_a_slot_freed_later_by_a_toddler() {
    let d = data();
    let board: Vec<_> = (0..20).map(|slot|
        (match slot { 3 => "toddler", 6 => "bar_of_soap", 8 => "bubble", _ => "dud" },slot)).collect();
    let mut s = fixed(&d,&board);
    s.deck[6].times_coins_given = 1;
    assert_eq!(s.resolve_spin_effects(),10,"one eaten bubble, not two: toddler 7 + soap 1 + bubble 2");
    assert_eq!(s.destroyed_history,vec![d.symbol_id("bubble").unwrap() as u16]);
    assert_eq!(s.board.occupied(),19);
    assert!(s.board.slots[8].is_none());
    let newborn = s.deck.iter().position(|sym| sym.type_id as usize == d.symbol_id("bubble").unwrap()).unwrap();
    assert!(s.board_slot_of(newborn).is_none(),"the new bubble is stored off screen");
    assert_eq!(s.deck[newborn].times_coins_given,0);
}

#[test]
fn dying_soap_can_place_its_bubble_in_its_own_freed_slot() {
    let d = data();
    let board: Vec<_> = (0..20).map(|slot| (if slot == 6 { "bar_of_soap" } else { "dud" },slot)).collect();
    let mut s = fixed(&d,&board);
    s.deck[6].times_coins_given = 2;
    assert_eq!(s.resolve_spin_effects(),3);
    assert_eq!(s.board.occupied(),20);
    let newborn = s.board.slots[6].unwrap();
    assert_eq!(s.data.symbols[s.deck[newborn].type_id as usize].name,"bubble");
    assert_eq!(s.deck[newborn].times_coins_given,1);
}

#[test]
fn natural_generators_do_not_claim_slots_freed_by_a_later_destruction_batch() {
    // Reference probes in reference_generations.json, on the same full boards.
    for (producer,consumer,food,income) in [
        ("king_midas","toddler","candy",9),
        ("beehive","bear","honey",48),
        ("cow","cat","milk",14),
        ("oyster","archaeologist","pearl",5),
    ] {
        let mut d = data();
        let producer_id = d.symbol_id(producer).unwrap();
        if producer != "king_midas" { d.symbols[producer_id].values[0] = 100.0; }
        let board: Vec<_> = (0..20).map(|slot|
            (match slot { 3 => consumer, 6 => producer, 8 => food, _ => "dud" },slot)).collect();
        let mut s = fixed(&d,&board);
        assert_eq!(s.resolve_spin_effects(),income,"{producer}");
        assert_eq!(s.board.occupied(),19,"{producer}");
        assert!(s.board.slots[8].is_none(),"{producer}");
        let newborn_type = if producer == "king_midas" { "coin" } else { food };
        let newborn = s.deck.iter().position(|symbol| d.symbols[symbol.type_id as usize].name == newborn_type).unwrap();
        assert!(s.board_slot_of(newborn).is_none(),"{producer}");
        assert_eq!(s.deck[newborn].times_coins_given,0,"{producer}");
    }
}

#[test]
fn pending_destruction_delays_the_short_generation_queue_until_the_victim_is_cleared() {
    for (producer,income,occupied) in [("king_midas",10,20),("bar_of_soap",17,19)] {
        let d = data();
        let board: Vec<_> = (0..20).map(|slot|
            (match slot { 0 => "toddler", 1 => "candy", 4 => producer, _ => "dud" },slot)).collect();
        let mut s = fixed(&d,&board);
        assert_eq!(s.resolve_spin_effects(),income,"{producer}");
        assert_eq!(s.board.occupied(),occupied,"{producer}");
        if producer == "king_midas" {
            let index=s.board.slots[1].unwrap();
            assert_eq!(d.symbols[s.deck[index].type_id as usize].name,"coin");
        } else {
            assert!(s.board.slots[1].is_none(),"the new bubble was eaten in this spin");
            assert_eq!(s.destroyed_history.len(),2);
        }
    }
}

#[test]
fn a_full_board_coconut_batch_keeps_one_half_offscreen_while_the_visible_half_is_eaten() {
    for (consumer,income) in [("monkey",22),("mrs_fruit",7)] {
        let d = data();
        let board: Vec<_> = (0..20).map(|slot|
            (match slot { 6 => consumer, 7 => "coconut", _ => "dud" },slot)).collect();
        let mut s = fixed(&d,&board);
        assert_eq!(s.resolve_spin_effects(),income,"{consumer}");
        assert_eq!(s.destroyed_history.len(),2);
        assert!(s.board.slots[7].is_none());
        let halves: Vec<_> = s.deck.iter().enumerate().filter(|(_,symbol)|
            d.symbols[symbol.type_id as usize].name == "coconut_half").collect();
        assert_eq!(halves.len(),1);
        assert!(s.board_slot_of(halves[0].0).is_none());
        assert_eq!(halves[0].1.times_coins_given,0);
    }
}

#[test]
fn periodic_dud_uses_the_current_spin_rng_before_soap_and_future_card_placement() {
    // Compact reproduction of the user's paired Steam/Rust run. The hidden
    // offscreen order first diverged on spin 31, while its visible layout and
    // income still matched. On spins 32/33 it changed toddler payouts by -6/+7.
    let actions = [0,4,0,4,0,3,0,4,0,3,4,0,4,0,1,0,3,0,3,0,3,3,0,1,0,1,
        0,5,0,3,0,3,0,3,3,128,0,1,0,5,0,5,0,4,28,128,0,3,0,3,3,5,0,3,
        0,3,0,5,0,5,0,3,0,5,0,3,3,0,3,0,4,0,1,0,5];
    for correct_essence in [false,true] {
        let d = data();let mut s = RunState::new_with_seed_text(&d,"1234567");
        s.manual_decisions = true;
        for (step,&action) in actions.iter().enumerate() {
            let action = if correct_essence && step == 49 { 4 } else { action };
            match action {
                0 => {
                    s.spin();
                    if s.total_spins == 32 { assert_eq!(s.last_income,36); }
                    if s.total_spins == 33 { assert_eq!(s.last_income,33); }
                }
                1 => match s.phase {
                    Phase::SpinReward{..} => s.skip_spin(),
                    Phase::SymbolReward{..} => s.skip_symbol(),
                    _ => panic!("unexpected skip phase"),
                },
                3..=18 => match s.phase {
                    Phase::SpinReward{..} => s.choose_spin(action-3),
                    Phase::SymbolReward{..} => s.choose_symbol(action-3),
                    Phase::ItemReward{..} => s.choose_item(action-3),
                    Phase::EssenceReward{..} => s.choose_essence(action-3),
                    _ => panic!("unexpected offer phase"),
                },
                28 => { s.remove_symbol(9); }
                128 => { assert!(s.toggle_managed(false,d.item_index["grave_robber"])); }
                _ => panic!("unexpected action"),
            }
        }
        assert_eq!(s.total_spins,33);assert_eq!(s.rents_paid,5);
        assert_eq!(s.phase,Phase::Ready);
        assert_eq!(s.coins,if correct_essence { 261 } else { 243 });
        assert_eq!(s.has_essence("chicken_coop_essence"),!correct_essence);
        assert!(!s.has_essence("void_portal_essence"),"the selected void essence already triggered");
    }
}

fn paired_seed_1234567_through_spin37<'a>(d: &'a GameData) -> RunState<'a> {
    // Steam spins 1..37 and all 90 user actions, including manual bomb use.
    let actions = [0,4,0,4,0,3,0,4,0,3,4,0,4,0,1,0,3,0,3,0,3,3,0,1,0,1,
        0,5,0,3,0,3,0,3,3,128,0,1,0,5,0,5,0,4,28,128,0,3,0,4,3,5,0,3,
        0,3,0,5,0,5,0,3,0,5,0,3,3,0,3,0,4,0,1,0,5,0,1,0,5,0,5,3,5,388,
        5,1,5,5,0,5];
    let mut s=RunState::new_with_seed_text(d,"1234567");
    s.manual_decisions=true;
    for action in actions {
        match action {
            0=>{s.spin();},
            1=>match s.phase {Phase::SpinReward{..}=>s.skip_spin(),Phase::SymbolReward{..}=>s.skip_symbol(),_=>panic!("skip phase")},
            3..=18=>match s.phase {
                Phase::SpinReward{..}=>s.choose_spin(action-3),Phase::SymbolReward{..}=>s.choose_symbol(action-3),
                Phase::ItemReward{..}=>s.choose_item(action-3),Phase::EssenceReward{..}=>s.choose_essence(action-3),_=>panic!("offer phase")},
            28=>{s.remove_symbol(9);},
            128=>{assert!(s.toggle_managed(false,d.item_index["grave_robber"]));},
            388=>{assert!(s.use_managed(false,d.item_index["symbol_bomb_big"]));},
            _=>panic!("unexpected action"),
        }
    }
    s
}

#[test]
fn ore_gem_draws_precede_a_later_urn_roll_in_the_paired_seed_1234567_game() {
    let d=data();let s=paired_seed_1234567_through_spin37(&d);
    assert_eq!(s.total_spins,37);assert_eq!(s.rents_paid,6);assert_eq!(s.phase,Phase::Ready);
    assert_eq!(s.last_income,121);assert_eq!(s.coins,185);
    assert_eq!(s.destroyed_history.len(),44);
    assert!(!names(&s).contains(&"void_stone"));
    assert_eq!(names(&s).iter().filter(|&&n|n=="pearl").count(),2);
    assert_eq!(s.data.symbols[s.deck[s.board.slots[5].unwrap()].type_id as usize].name,"pearl");
    assert_eq!(s.data.symbols[s.deck[s.board.slots[11].unwrap()].type_id as usize].name,"shiny_pebble");
}

#[test]
fn ore_rng_order_follows_the_victim_for_miners_dwarves_and_geologists() {
    // Six independent fixed-board reference probes, in reference_cases.json.
    for consumer in ["dwarf","miner","archaeologist"] {
        for urn_first in [false,true] {
            let d=data();
            let (ore,source,urn)=if urn_first {(13,12,5)}else{(5,10,13)};
            let board:Vec<_>=(0..20).map(|slot| (if slot==0 {"d3"}else if slot==ore {"big_ore"}
                else if slot==source {consumer}else if slot==urn {"urn"}else{"dud"},slot)).collect();
            let mut s=fixed(&d,&board);s.rents_paid=6;
            s.rng=RngStreams::from_seed_text("1234567");s.rng.begin_spin(37);s.rng.effect().begin_log();
            s.items_held.extend([d.item_index["grave_robber"],d.item_index["kyle_the_kernite"]]);
            if consumer=="dwarf" {s.items_held.push(d.item_index["dwarven_anvil"]);}
            let income=s.resolve_spin_effects();
            let symbols=names(&s);
            assert!(!symbols.contains(&"big_ore"));assert!(symbols.contains(&"urn"));
            assert_eq!(symbols.contains(&"pearl"),!urn_first,"{consumer}/{urn_first}");
            assert_eq!(symbols.contains(&"void_stone"),urn_first,"{consumer}/{urn_first}");
            assert_eq!(symbols.contains(&"shiny_pebble"),consumer!="archaeologist");
            assert_eq!(income,match consumer {"dwarf"=>71,"miner"=>30,_=>13},"{consumer}/{urn_first}");
        }
    }
}

#[test]
fn a_protected_or_unconsumed_ore_does_not_draw_death_gems() {
    for protected in [false,true] {
        let d=data();
        let board:Vec<_>=(0..20).map(|slot| (match slot {
            0=>"d3",5=>"big_ore",6 if protected=>"dove",10 if protected=>"miner",13=>"urn",_=>"dud"},slot)).collect();
        let mut s=fixed(&d,&board);s.rents_paid=6;
        s.rng=RngStreams::from_seed_text("1234567");s.rng.begin_spin(37);s.rng.effect().begin_log();
        s.items_held.push(d.item_index["grave_robber"]);
        s.resolve_spin_effects();
        assert!(names(&s).contains(&"big_ore"));
        assert!(!names(&s).iter().any(|n|matches!(*n,"pearl"|"shiny_pebble"|"void_stone")));
        assert_eq!(s.last_effect_rng.len(),2,"only the die and grave robber draw, protected={protected}");
    }
}

#[test]
fn pinata_overflow_and_long_reel_stops_match_the_next_paired_spin() {
    let d=data();let mut s=paired_seed_1234567_through_spin37(&d);
    s.spin();assert_eq!(s.total_spins,38);assert_eq!(s.last_income,114);assert_eq!(s.coins,298);
    assert_eq!(s.board.reels.iter().map(Vec::len).collect::<Vec<_>>(),vec![5;5]);
    assert_eq!(s.board.deferred.iter().map(Vec::len).collect::<Vec<_>>(),vec![0,0,0,0,2]);
    assert_eq!(s.deck.len(),26);
    s.skip_spin();s.spin();
    assert_eq!(s.total_spins,39);assert_eq!(s.last_income,30);assert_eq!(s.coins,327);
    let expected=["crow","candy","cherry","candy","urn","rain","candy","golden_egg","dud","candy",
        "flower","pearl","dud","candy","pearl","cat","toddler","dwarf","dog","shiny_pebble"];
    for (slot,expected) in expected.iter().enumerate() {
        assert_eq!(d.symbols[s.deck[s.board.slots[slot].unwrap()].type_id as usize].name,*expected,"slot {slot}");
    }
    assert_eq!(s.destroyed_history.len(),48);
    assert_eq!(s.board.positions[0],vec![3,4,-1,0,1,2]);
}

#[test]
fn overflow_batches_match_five_native_reference_probes() {
    // The first overflow and the remaining batch use separate draws against
    // the same candidate list. See reference_batches.json for native results.
    for (count,grow,column) in [(1,0,0),(2,0,0),(7,0,0),(7,2,1),(7,2,4)] {
        let mut board=Board::empty();
        for slot in 0..20 {board.slots[slot]=Some(slot);}
        board.adopt_slots();
        for reel in 0..5 {
            let empty=board.reels[reel].iter().position(Option::is_none).unwrap();
            board.reels[reel][empty]=Some(20+reel);
        }
        for index in 0..grow {board.reels[column].push(Some(25+index));board.positions[column].push(-1);}
        let first=25+grow;
        let indices:Vec<_>=(first..first+count).collect();
        let mut rng=RngStreams::from_seed_text("1234567");rng.begin_spin(38);
        board.add_symbols(&indices,rng.reel());
        let mut expected:[Vec<usize>;5]=Default::default();
        if grow>0 && column==4 {expected[4]=indices;}
        else {expected[2].push(first);if count>1 {expected[3]=(first+1..first+count).collect();}}
        assert_eq!(board.deferred,expected,"batch={count} grow={grow} column={column}");
        assert_eq!(board.occupied(),20);
        assert_eq!(board.reels.iter().map(Vec::len).sum::<usize>(),25+grow);
    }
}

#[test]
fn deferred_icons_do_not_change_the_reels_until_next_spin_and_can_be_removed() {
    let mut board=Board::empty();
    for slot in 0..20 {board.slots[slot]=Some(slot);}
    board.adopt_slots();
    for reel in 0..5 {
        let empty=board.reels[reel].iter().position(Option::is_none).unwrap();
        board.reels[reel][empty]=Some(20+reel);
    }
    let mut rng=RngStreams::from_seed_text("1234567");rng.begin_spin(38);
    board.add_symbols(&[25,26],rng.reel());
    board.add_symbol(27,rng.reel());
    assert_eq!(board.reels.iter().map(Vec::len).sum::<usize>(),25);
    assert_eq!(board.deferred.iter().map(Vec::len).sum::<usize>(),3);
    board.remove_deck_indices(&[2,25]);
    assert_eq!(board.deferred.iter().flatten().copied().collect::<Vec<_>>().len(),2);
    assert!(board.deferred.iter().flatten().all(|&i|i==24 || i==25));
    board.flush_deferred();
    assert_eq!(board.reels.iter().map(Vec::len).sum::<usize>(),27);
    assert!(board.deferred.iter().all(Vec::is_empty));
}

#[test]
fn feeding_uses_the_foods_temporary_value_in_both_column_orders() {
    // Eight fixed boards run independently in the native reference.
    for (consumer,food,item) in [("dwarf","wine","dwarven_anvil"),("monkey","coconut_half","oswald_the_monkey")] {
        for (source,powder) in [(6,8),(8,6),(6,2),(8,2)] {
            let d=data();
            let board:Vec<_>=(0..20).map(|slot| (if slot==source {consumer}else if slot==7 {food}
                else if slot==powder {"buffing_powder"}else{"dud"},slot)).collect();
            let mut s=fixed(&d,&board);s.items_held.push(d.item_index[item]);
            if food=="wine" {s.deck[7].bonus=1;}
            let expected=match (consumer,powder==2) {("dwarf",false)=>128,("dwarf",true)=>250,(_,false)=>54,_=>104};
            assert_eq!(s.resolve_spin_effects(),expected,"{consumer} source={source} powder={powder}");
        }
    }
}

#[test]
fn newly_generated_coconut_halves_feed_with_the_chefs_buff_once() {
    for (source,chef) in [(6,8),(8,6)] {
        let d=data();
        let board:Vec<_>=(0..20).map(|slot| (if slot==source {"monkey"}else if slot==7 {"coconut"}
            else if slot==chef {"chef"}else{"dud"},slot)).collect();
        let mut s=fixed(&d,&board);s.items_held.push(d.item_index["oswald_the_monkey"]);
        assert_eq!(s.resolve_spin_effects(),69,"reference: monkey 62 + coconut 1 + visible half 4 + chef 2");
        assert_eq!(s.destroyed_history.iter().map(|&id|d.symbols[id as usize].name.as_str()).collect::<Vec<_>>(),vec!["coconut","coconut_half"]);
        assert_eq!(names(&s).iter().filter(|&&n|n=="coconut_half").count(),1,"one uneaten offscreen half remains");
    }
}

#[test]
fn paired_seed_1234567_powder_wine_and_dwarf_match_through_spin42() {
    let d=data();let mut s=paired_seed_1234567_through_spin37(&d);
    s.spin();s.skip_spin();s.spin();assert_eq!(s.coins,327);
    s.skip_spin();s.spin();assert_eq!(s.total_spins,40);assert_eq!(s.last_income,46);assert_eq!(s.coins,372);
    s.choose_spin(0); // buffing powder
    s.remove_symbol(1);s.remove_symbol(0); // cherry and coin
    s.spin();assert_eq!(s.total_spins,41);assert_eq!(s.last_income,285);assert_eq!(s.coins,656);
    s.choose_spin(2); // removal capsule
    s.spin();assert_eq!(s.total_spins,42);assert_eq!(s.last_income,39);assert_eq!(s.coins,694);
    assert_eq!(s.destroyed_history.len(),57);
    assert_eq!(s.removed_history.len(),3);
    assert!(matches!(s.phase,Phase::SpinReward{..}));
}

#[test]
fn removed_newborn_pearl_keeps_registered_gem_multipliers() {
    // Native fixed boards pay the removed pearl 2, not 1, in both column orders.
    for (buff,expected) in [("light_bulb",8),("dame",9)] {
        for (diver,source) in [(6,8),(8,6)] {
            let mut d=data();let oyster=d.symbol_id("oyster").unwrap();d.symbols[oyster].values[0]=0.0;
            let board:Vec<_>=(0..20).filter(|&slot|slot!=2).map(|slot|
                (if slot==diver {"diver"}else if slot==source {buff}else if slot==7 {"oyster"}else{"dud"},slot)).collect();
            let mut s=fixed(&d,&board);
            assert_eq!(s.resolve_spin_effects(),expected,"{buff}, diver={diver}");
            assert_eq!(s.removed_history.len(),2);
            assert!(s.destroyed_history.is_empty());
            let diver=s.deck.iter().find(|symbol|d.symbols[symbol.type_id as usize].name=="diver").unwrap();
            assert_eq!(diver.bonus,2);
            if buff=="light_bulb" {
                assert_eq!(s.deck.iter().find(|symbol|d.symbols[symbol.type_id as usize].name==buff).unwrap().saved,1);
            }
        }
    }
}

#[test]
fn last_bulb_charge_counts_a_newborn_pearl_even_after_removal() {
    for (diver,source) in [(6,8),(8,6)] {
        let mut d=data();let oyster=d.symbol_id("oyster").unwrap();d.symbols[oyster].values[0]=0.0;
        let board:Vec<_>=(0..20).filter(|&slot|slot!=2).map(|slot|
            (if slot==diver {"diver"}else if slot==source {"light_bulb"}else if slot==7 {"oyster"}else{"dud"},slot)).collect();
        let mut s=fixed(&d,&board);let bulb=s.board.slots[source].unwrap();s.deck[bulb].saved=4;
        assert_eq!(s.resolve_spin_effects(),8,"diver={diver}");
        assert!(!names(&s).contains(&"light_bulb"));
        assert_eq!(s.destroyed_history,vec![d.symbol_id("light_bulb").unwrap() as u16]);
        assert_eq!(s.removed_history.len(),2);
    }
}

fn paired_seed_1234567_through_spin51<'a>(d:&'a GameData) -> RunState<'a> {
    let mut s=paired_seed_1234567_through_spin37(d);
    s.spin();s.skip_spin();s.spin();s.skip_spin();s.spin();s.choose_spin(0);
    s.remove_symbol(1);s.remove_symbol(0);s.spin();s.choose_spin(2);s.spin();
    assert_eq!(s.total_spins,42);assert_eq!(s.coins,694);
    for action in [3,0,5,0,4,3,0,2,4,0,3,0,1,0,5,0,2,3,0,5,0] {
        match action {
            0=>{s.spin();},
            1=>s.skip_spin(),
            2=>s.reroll_reward(),
            3..=18=>match s.phase {
                Phase::SpinReward{..}=>s.choose_spin(action-3),Phase::SymbolReward{..}=>s.choose_symbol(action-3),
                Phase::ItemReward{..}=>s.choose_item(action-3),Phase::EssenceReward{..}=>s.choose_essence(action-3),
                _=>panic!("unexpected offer phase")},
            _=>panic!("unexpected action"),
        }
    }
    assert_eq!(s.total_spins,51);assert_eq!(s.last_income,39);assert_eq!(s.coins,687);
    assert_eq!(s.destroyed_history.len(),65);assert_eq!(s.removed_history.len(),7);
    assert!(!names(&s).contains(&"light_bulb"));
    assert!(s.board.slots[13].is_none());
    assert_eq!(s.deck.iter().find(|symbol|d.symbols[symbol.type_id as usize].name=="diver").unwrap().bonus,4);
    assert!(matches!(s.phase,Phase::SpinReward{..}));
    s
}

#[test]
fn paired_seed_1234567_removed_pearl_and_bulb_match_through_spin51() {
    let d=data();paired_seed_1234567_through_spin51(&d);
}

#[test]
fn full_board_peppers_count_generated_food_before_it_is_eaten_once() {
    // Twelve independent native fixed-board probes. Food briefly fills the
    // board, then toddler empties a slot again: the full-board reward sticks.
    for layout in 0..3 {
        for item in [false,true] {
            for essence in [false,true] {
                let d=data();
                let board:Vec<_>=(0..20).filter(|&slot|match layout {
                    0=>slot!=14 && slot!=19,1=>true,_=>slot>=10 || slot==5,
                }).map(|slot|(match slot {5=>"pinata",11=>"toddler",_=>"dud"},slot)).collect();
                let mut s=fixed(&d,&board);
                if item {s.items_held.push(d.item_index["yellow_pepper"]);}
                if essence {s.essences_held.push(d.essence_index["yellow_pepper_essence"]);}
                let expected=if layout==0 {17}else if layout==1 {15}else {21};
                // The never-full board may place candies differently under a
                // different seed; compare against an identical unmodified run.
                let mut control=fixed(&d,&board);
                let baseline=control.resolve_spin_effects();
                if layout<2 {assert_eq!(baseline,expected);}
                let gain=if layout<2 {i32::from(item)*2+i32::from(essence)*20}else {0};
                assert_eq!(s.resolve_spin_effects(),baseline+gain,"layout={layout}, item={item}, essence={essence}");
                assert!(s.board.occupied()<20,"the board ends with empty slots");
                assert_eq!(s.has_essence("yellow_pepper_essence"),essence && layout==2);
            }
        }
    }
}

fn paired_seed_1234567_through_spin55<'a>(d:&'a GameData) -> RunState<'a> {
    let mut s=paired_seed_1234567_through_spin51(d);
    for action in [3,0,5,4,5,136,0,5,0,5,0] {
        match action {
            0=>{s.spin();},
            3..=18=>match s.phase {
                Phase::SpinReward{..}=>s.choose_spin(action-3),Phase::SymbolReward{..}=>s.choose_symbol(action-3),
                Phase::ItemReward{..}=>s.choose_item(action-3),Phase::EssenceReward{..}=>s.choose_essence(action-3),
                _=>panic!("unexpected offer phase")},
            136=>{assert!(s.toggle_managed(true,d.essence_index["lockpick_essence"]));},
            _=>panic!("unexpected action"),
        }
    }
    assert_eq!(s.total_spins,55);assert_eq!(s.last_income,67);assert_eq!(s.coins,417);
    assert_eq!(s.destroyed_history.len(),69);assert_eq!(s.removed_history.len(),8);
    assert_eq!(s.board.occupied(),19);
    assert!(s.last_item_gains.contains(&("yellow_pepper".to_owned(),2)));
    s
}

#[test]
fn paired_seed_1234567_yellow_pepper_matches_a_temporarily_full_spin55() {
    let d=data();paired_seed_1234567_through_spin55(&d);
}

#[test]
fn coal_transformation_keeps_first_die_roll_and_refreshes_ore_randomness() {
    // Four native fixed boards isolate coal maturity from spirit expiration.
    for expire in [false,true] {
        for mature in [false,true] {
            let d=data();let board:Vec<_>=(0..20).map(|slot|(match slot {
                4=>"spirit",6=>"coal",9=>"d5",18=>"dwarf",19=>"big_ore",_=>"dud"},slot)).collect();
            let mut s=fixed(&d,&board);s.rents_paid=10;
            s.deck[4].times_coins_given=if expire {3}else {2};s.deck[6].times_displayed=if mature {19}else {18};
            s.items_held.extend([d.item_index["dwarven_anvil"],d.item_index["kyle_the_kernite"]]);
            s.rng=RngStreams::from_seed_text("1234567");s.rng.begin_spin(78);
            assert_eq!(s.resolve_spin_effects(),match (expire,mature) {
                (false,false)=>76,(false,true)=>80,(true,false)=>79,(true,true)=>82},"expire={expire}, mature={mature}");
            assert_eq!(names(&s).contains(&"diamond"),mature);
            assert_eq!(names(&s).contains(&"ruby"),!mature);
            assert_eq!(names(&s).contains(&"pearl"),mature);
            let die=s.board.slots[9].unwrap();
            assert_eq!(d.symbols[s.deck[die].type_id as usize].name,"d5");
            assert_eq!(s.deck[die].times_displayed,1,"refreshing does not add extra turns");
            assert!(s.last_symbol_income.contains(&(9,3)),"the original die result must remain 3");
        }
    }
}

#[test]
fn coal_refresh_respects_adjacent_dice_lucky_dice_and_an_earlier_ore() {
    for (die,ore,consumer,lucky,expected) in [(7,19,18,false,82),(7,19,18,true,83),(9,5,10,false,81)] {
        let d=data();let board:Vec<_>=(0..20).map(|slot|
            (if slot==4 {"spirit"}else if slot==6 {"coal"}else if slot==die {"d5"}
             else if slot==ore {"big_ore"}else if slot==consumer {"dwarf"}else{"dud"},slot)).collect();
        let mut s=fixed(&d,&board);s.rents_paid=10;s.deck[4].times_coins_given=2;s.deck[6].times_displayed=19;
        s.items_held.extend([d.item_index["dwarven_anvil"],d.item_index["kyle_the_kernite"]]);
        if lucky {s.items_held.push(d.item_index["lucky_dice"]);}
        s.rng=RngStreams::from_seed_text("1234567");s.rng.begin_spin(78);
        assert_eq!(s.resolve_spin_effects(),expected,"die={die}, ore={ore}, lucky={lucky}");
        assert!(s.last_symbol_income.contains(&(die,if lucky {5}else {3})));
    }
}

#[test]
fn paired_seed_1234567_coal_transformation_and_ore_match_through_spin78() {
    let d=data();let mut s=paired_seed_1234567_through_spin55(&d);
    for action in [4,0,5,19,136,0,3,0,4,0,4,0,2,5,0,4,3,0,2,3,0,4,0,4,0,5,0,5,
        0,3,0,5,28,0,5,0,5,5,5,22,0,5,0,1,0,3,25,0,3,0,1,0,5,0,1,0] {
        match action {
            0=>{s.spin();},
            1=>s.skip_spin(),2=>s.reroll_reward(),
            3..=18=>match s.phase {
                Phase::SpinReward{..}=>s.choose_spin(action-3),Phase::SymbolReward{..}=>s.choose_symbol(action-3),
                Phase::ItemReward{..}=>s.choose_item(action-3),Phase::EssenceReward{..}=>s.choose_essence(action-3),
                _=>panic!("unexpected offer phase")},
            19..=127=>{s.remove_symbol(action-19);},
            136=>{assert!(s.toggle_managed(true,d.essence_index["lockpick_essence"]));},
            _=>panic!("unexpected action"),
        }
    }
    assert_eq!(s.total_spins,78);assert_eq!(s.last_income,132);assert_eq!(s.coins,795);
    assert_eq!(s.destroyed_history.len(),97);assert_eq!(s.removed_history.len(),21);
    assert_eq!(d.symbols[s.deck[s.board.slots[4].unwrap()].type_id as usize].name,"pearl");
    assert_eq!(d.symbols[s.deck[s.board.slots[19].unwrap()].type_id as usize].name,"shiny_pebble");
}
