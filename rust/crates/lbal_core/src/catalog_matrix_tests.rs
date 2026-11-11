// Behavior matrices for catalog entries sharing the same kind of effect.
// Each row is a concrete rule example, including values from the encyclopedia.
use super::*;

#[test]
fn ordinary_item_flat_and_multiplier_examples() {
    let d = data();
    for (item,symbol,expected) in [
        ("birdhouse","owl",2),("happy_hour","beer",2),("kyle_the_kernite","ore",2),
        ("anthropology_degree","bounty_hunter",2),("ritual_candle","witch",3),
        ("holy_water","hex_of_tedium",4),("rain_cloud","rain",3),
        ("chicken_coop","golden_egg",6),("nori_the_rabbit","rabbit",2),
        ("pizza_the_cat","cat",2),("quigley_the_wolf","wolf",3),
        ("ricky_the_banana","banana",3),("fruit_basket","cherry",2),
        ("cleaning_rag","pearl",2),("triple_coins","coin",3),
        ("maxwell_the_bear","bear",3),("oswald_the_monkey","monkey",2),
        ("dwarven_anvil","dwarf",2),("jackolantern","toddler",2),
        ("black_cat","cat",2),("lucky_carrot","rabbit",3),("golden_carrot","rabbit",5),
        ("wanted_poster","thief",-1),
    ] {
        let mut s = fixed(&d,&[(symbol,6)]); s.items_held.push(d.item_index[item]);
        assert_eq!(s.resolve_spin_effects(),expected,"{item} with {symbol}");
    }
    for (item,expected) in [("pool_ball",1),("horseshoe",2),("bowling_ball",3),("four_leaf_clover",4)] {
        let mut s = fixed(&d,&[]); s.items_held.push(d.item_index[item]);
        assert_eq!(s.resolve_spin_effects(),expected,"{item}");
    }
}

#[test]
fn pre_spin_storage_and_removal_item_examples() {
    let d = data();
    for (item,symbol,one_time,stored_income) in [
        ("coin_on_a_string","coin",3,1),("egg_carton","egg",0,1),
        ("fish_bowl","goldfish",0,1),("flush","clubs",12,0),("lint_roller","rabbit_fluff",12,0),
    ] {
        let mut s = fixed(&d,&[(symbol,6)]); s.items_held.push(d.item_index[item]);
        s.consume_symbols_pre_spin(); assert!(s.deck.is_empty(),"{item}");
        assert_eq!(s.pre_spin_income,one_time); assert_eq!(s.resolve_spin_effects(),one_time+stored_income,"{item}");
        assert_eq!(s.removed_history.len(),1); assert!(s.destroyed_history.is_empty());
    }
    let mut s = fixed(&d,&[]); s.items_held.push(d.item_index["egg_carton"]); s.add_symbols("egg",9);
    s.consume_symbols_pre_spin(); assert_eq!(s.resolve_spin_effects(),6,"egg carton has a six-coin cap");
}

#[test]
fn item_choices_and_delayed_manual_uses() {
    let d = data();
    for (item,age,first,second) in [("blue_suits",7,"clubs","spades"),("red_suits",7,"hearts","diamonds"),("treasure_map",20,"key","treasure_chest")] {
        let mut s = fixed(&d,&[]); let id = d.item_index[item]; s.items_held.push(id);
        assert!(!s.can_use_managed_item(id)); s.item_ages.insert(id,age-1); s.tick_items();
        assert!(s.use_item(id)); assert_eq!(s.deck.len(),2);
        assert!(names(&s).contains(&first)); assert!(names(&s).contains(&second));
    }
    let mut s = fixed(&d,&[]); let id = d.item_index["barrel_o_dwarves"]; s.items_held.push(id); assert!(s.use_item(id));
    assert_eq!(names(&s),vec!["dwarf";7]);
    let mut s = fixed(&d,&[]); let id = d.item_index["cardboard_box"]; s.items_held.push(id);
    for _ in 0..9 { s.tick_items(); } assert_eq!(s.removal_tokens,0); s.tick_items(); assert_eq!(s.removal_tokens,1);
}

#[test]
fn ordinary_pepper_conditions_and_chili_count() {
    let d = data();
    for (item,board,expected) in [
        ("blue_pepper",vec![("coin",6)],4), ("cyan_pepper",vec![("coin",6)],4),
        ("red_pepper",vec![("coin",6),("pearl",7)],7),
        ("green_pepper",vec![("coin",6),("coin",7),("coin",1)],6),
        ("purple_pepper",vec![("coin",6),("coin",7),("coin",1)],8),
    ] {
        let mut s = fixed(&d,&board); s.items_held.push(d.item_index[item]); assert_eq!(s.resolve_spin_effects(),expected,"{item}");
    }
    let board: Vec<_> = (0..20).map(|slot| ("coin",slot)).collect();
    let mut s = fixed(&d,&board); s.items_held.push(d.item_index["yellow_pepper"]); assert_eq!(s.resolve_spin_effects(),22);
    let mut s = fixed(&d,&[]); s.items_held.push(d.item_index["brown_pepper"]); let before = s.coins;
    s.add_symbols("coin",1); assert_eq!(s.coins-before,1);
}

#[test]
fn essence_passive_timers_and_token_payouts() {
    let d = data();
    for (name,spins,each) in [("horseshoe_essence",30,2),("pool_ball_essence",60,1),
        ("bowling_ball_essence",20,3),("four_leaf_clover_essence",15,4)] {
        let mut s = fixed(&d,&[]); s.essences_held.push(d.essence_index[name]);
        for i in 0..spins { assert_eq!(s.resolve_spin_effects(),each,"{name}"); assert_eq!(s.has_essence(name),i < spins-1); }
        assert_eq!(s.resolve_spin_effects(),0);
    }
    for (name,spins,removal,essence) in [("cardboard_box_essence",12,3,0),("dishwasher_essence",18,0,3)] {
        let mut s = fixed(&d,&[]); s.essences_held.push(d.essence_index[name]);
        for _ in 0..spins { s.resolve_spin_effects(); }
        assert_eq!((s.removal_tokens,s.essence_tokens),(removal,essence)); assert!(!s.has_essence(name));
    }
    for (name,spins) in [("golden_carrot_essence",7),("lucky_carrot_essence",15)] {
        let mut s = fixed(&d,&[]); s.essences_held.push(d.essence_index[name]);
        for _ in 0..spins { s.resolve_spin_effects(); }
        assert!(!s.has_essence(name)); assert_eq!(s.symbol_forced_queue,vec![(Rarity::VeryRare,true)]);
    }
}

#[test]
fn pre_spin_essences_pay_for_every_matching_symbol() {
    let d = data();
    for (name,symbol,threshold,per) in [
        ("coin_on_a_string_essence","coin",10,10),("fish_bowl_essence","goldfish",10,10),
        ("egg_carton_essence","egg",12,12),("flush_essence","clubs",5,20),
        ("lint_roller_essence","rabbit_fluff",6,20),("barrel_o_dwarves_essence","dwarf",7,17),
    ] {
        let mut s = fixed(&d,&[]); let id = d.essence_index[name]; s.essences_held.push(id);
        s.essence_saved.insert(id,threshold-1); s.add_symbols(symbol,2); s.consume_symbols_pre_spin();
        assert_eq!(s.pre_spin_income,2*per,"{name}"); assert!(s.deck.is_empty()); assert!(!s.has_essence(name));
    }
}

#[test]
fn count_and_clump_essences_trigger_only_at_their_threshold() {
    let mut d = data();
    for name in ["egg","chick","chicken","oyster","goose"] { let id = d.symbol_id(name).unwrap(); for v in &mut d.symbols[id].values { *v = 0.0; } }
    for (name,symbol,n,bonus,mult) in [
        ("blue_suits_essence","clubs",4,1,1.0),("red_suits_essence","hearts",4,1,1.0),
        ("birdhouse_essence","goose",3,2,1.0),("kyle_the_kernite_essence","ore",3,2,1.0),
        ("goldilocks_essence","bear",3,1,1.0),("undertaker_essence","spirit",4,2,1.0),
        ("ritual_candle_essence","cultist",6,0,2.0),("anthropology_degree_essence","bounty_hunter",9,0,1.5),
        ("black_cat_essence","cat",13,0,13.0),
    ] {
        let board: Vec<_> = (0..n).map(|slot| (symbol,slot)).collect(); let mut s = fixed(&d,&board);
        s.essences_held.push(d.essence_index[name]); s.resolve_spin_effects(); assert!(!s.has_essence(name),"{name}");
        let id = d.symbol_id(symbol).unwrap(); assert_eq!(s.symbol_rule_bonus(id),bonus,"{name}"); assert_eq!(s.symbol_rule_multiplier(id),mult,"{name}");
    }
    let mut s = fixed(&d,&[("cat",6),("cat",7),("cat",1)]); s.essences_held.push(d.essence_index["lucky_cat_essence"]);
    s.resolve_spin_effects(); assert_eq!(s.permanent_rarity_factor,2.5); assert!(!s.has_essence("lucky_cat_essence"));
    let mut s = fixed(&d,&[("dog",6),("wolf",7),("dog",1)]); s.essences_held.push(d.essence_index["quigley_the_wolf_essence"]);
    s.resolve_spin_effects(); assert_eq!(names(&s),vec!["dog","wolf","dog"]);
    s.resolve_spin_effects(); assert_eq!(names(&s),vec!["wolf";3]); assert_eq!(s.symbol_rule_bonus(d.symbol_id("wolf").unwrap()),2);
}

#[test]
fn essence_pepper_conditions() {
    let d = data();
    for (name,board,expected) in [
        ("blue_pepper_essence",vec![("coin",6)],31), ("cyan_pepper_essence",vec![("coin",6)],31),
        ("red_pepper_essence",vec![("coin",6),("pearl",7)],52),
        ("green_pepper_essence",vec![("coin",6),("coin",7),("coin",1)],33),
        ("purple_pepper_essence",vec![("coin",6),("coin",7),("coin",1)],53),
    ] {
        let mut s = fixed(&d,&board); s.essences_held.push(d.essence_index[name]); assert_eq!(s.resolve_spin_effects(),expected,"{name}"); assert!(!s.has_essence(name));
    }
}

#[test]
fn standalone_symbols_with_no_conditional_effect_pay_their_base_value() {
    let d = data();
    for name in ["beer","candy","cheese","cherry","coin","flower","milk","pearl","orange","sapphire","wolf","apple","golden_egg","honey","martini","highlander","matryoshka_doll_5"] {
        let mut s = fixed(&d,&[(name,6)]); let id = d.symbol_id(name).unwrap();
        assert_eq!(s.resolve_spin_effects(),d.symbols[id].base_value,"{name}");
    }
}

#[test]
fn geometric_and_counted_symbol_examples() {
    let d = data();
    for (symbol,n,expected) in [("cultist",3,6),("ninja",3,0),("diamond",3,21),("watermelon",3,18),
        ("emerald",2,8),("ruby",2,8),("strawberry",2,8)] {
        let board: Vec<_> = (0..n).map(|slot| (symbol,slot)).collect(); let mut s = fixed(&d,&board);
        assert_eq!(s.resolve_spin_effects(),expected,"{symbol}");
    }
    let mut s = fixed(&d,&[("anchor",0)]); assert_eq!(s.resolve_spin_effects(),5);
    let mut s = fixed(&d,&[("anchor",6)]); assert_eq!(s.resolve_spin_effects(),1);
    let mut s = fixed(&d,&[("crab",0),("crab",4)]); assert_eq!(s.resolve_spin_effects(),8);
    let mut s = fixed(&d,&[("crab",0),("crab",9)]); assert_eq!(s.resolve_spin_effects(),2);
}

#[test]
fn slow_symbol_and_countdown_examples() {
    let d = data();
    for (symbol,n,expected) in [("crow",4,vec![2,2,2,-1]),("robin_hood",4,vec![-4,-4,-4,21]),
        ("snail",4,vec![0,0,0,5]),("sloth",2,vec![0,4]),("turtle",3,vec![0,0,4]),
        ("magpie",4,vec![-1,-1,-1,8]),("owl",3,vec![1,1,2])] {
        let mut s = fixed(&d,&[(symbol,6)]); let incomes: Vec<_> = (0..n).map(|_| s.resolve_spin_effects()).collect();
        assert_eq!(incomes,expected,"{symbol}");
    }
    let mut s = fixed(&d,&[("crow",6)]); s.items_held.push(d.item_index["checkered_flag"]);
    assert_eq!((0..4).map(|_| s.resolve_spin_effects()).collect::<Vec<_>>(),vec![2,2,2,-1]);
    let mut s = fixed(&d,&[("present",6)]); s.deck[0].times_displayed = 11;
    assert_eq!(s.resolve_spin_effects(),10); assert!(s.deck.is_empty());
    let mut s = fixed(&d,&[("bubble",6)]); s.deck[0].times_coins_given = 2;
    assert_eq!(s.resolve_spin_effects(),2); assert!(s.deck.is_empty());
    let mut s = fixed(&d,&[("bar_of_soap",6)]); for _ in 0..3 { s.resolve_spin_effects(); }
    assert!(!names(&s).contains(&"bar_of_soap")); assert_eq!(names(&s).iter().filter(|&&n| n == "bubble").count(),2);
    let mut s = fixed(&d,&[("mine",6)]); for _ in 0..4 { s.resolve_spin_effects(); }
    assert!(!names(&s).contains(&"mine")); assert_eq!(names(&s).iter().filter(|&&n| n == "ore").count(),4); assert!(s.has_item("mining_pick"));
}

#[test]
fn probability_producer_and_hex_examples() {
    let mut d = data();
    for name in ["goose","cow","beehive","oyster","tomb","bartender","hex_of_midas","hex_of_draining","hex_of_thievery","hex_of_emptiness","hex_of_hoarding"] {
        let id = d.symbol_id(name).unwrap(); d.symbols[id].values[0] = 100.0;
    }
    for (name,spawn) in [("goose","golden_egg"),("cow","milk"),("beehive","honey"),("oyster","pearl"),("tomb","spirit"),("hex_of_midas","coin")] {
        let mut s = fixed(&d,&[(name,6)]); s.resolve_spin_effects(); assert!(names(&s).contains(&spawn),"{name}");
    }
    let mut s = fixed(&d,&[("bartender",6)]); s.resolve_spin_effects(); assert!(s.deck.iter().any(|sym| s.has_group(sym.type_id as usize,"booze")));
    let mut s = fixed(&d,&[("hex_of_draining",6),("coin",7)]); assert_eq!(s.resolve_spin_effects(),3);
    let mut s = fixed(&d,&[("hex_of_thievery",6)]); assert_eq!(s.resolve_spin_effects(),-3);
    let mut s = fixed(&d,&[("hex_of_emptiness",6)]); s.resolve_spin_effects(); assert!(s.force_skip_reward);
    let mut s = fixed(&d,&[("hex_of_hoarding",6)]); s.resolve_spin_effects(); assert!(s.force_take_symbol);
}

#[test]
fn consumer_symbol_and_support_examples() {
    let d = data();
    for (source,victim,expected) in [("goldfish","bubble",18),("cat","milk",11),("mouse","cheese",22),
        ("bear","honey",45),("dwarf","beer",12),("monkey","banana",9),
        ("toddler","candy",8),("hooligan","urn",15)] {
        let mut s = fixed(&d,&[(source,6),(victim,7)]); let income = s.resolve_spin_effects();
        assert_eq!(income,expected,"{source} consumes {victim}"); assert!(!names(&s).contains(&victim));
    }
    for (source,target,mult) in [("beastmaster","rabbit",2),("chef","honey",2),("dame","sapphire",2),
        ("joker","clubs",2),("comedian","monkey",3),("bee","flower",2)] {
        let mut s = fixed(&d,&[(source,6),(target,7)]); let a = d.symbol_id(source).unwrap(); let b = d.symbol_id(target).unwrap();
        let extra = if source == "bee" { 1 } else { 0 };
        assert_eq!(s.resolve_spin_effects(),d.symbols[a].base_value + extra + d.symbols[b].base_value*mult,"{source} -> {target}");
    }
    let mut s = fixed(&d,&[("king_midas",6)]); s.essences_held.push(d.essence_index["telescope_essence"]);
    assert_eq!(s.resolve_spin_effects(),4); assert!(names(&s).contains(&"coin"));
    let mut s = fixed(&d,&[("light_bulb",6),("pearl",7)]);
    for _ in 0..5 { assert_eq!(s.resolve_spin_effects(),3); }
    assert_eq!(names(&s),vec!["pearl"]);
}

#[test]
fn capsule_and_destruction_special_examples() {
    let d = data();
    let mut s = fixed(&d,&[("hustler",6)]); assert_eq!(s.resolve_spin_effects(),-6); assert!(s.has_item("pool_ball")); assert!(s.deck.is_empty());
    let mut s = fixed(&d,&[("item_capsule",6)]); s.resolve_spin_effects(); assert_eq!(s.items_held.len(),1); assert_eq!(d.items[s.items_held[0]].rarity,"common");
    let mut s = fixed(&d,&[("rarity_capsule",6)]); s.resolve_spin_effects(); let offer = s.roll_spin_reward();
    assert!(offer.iter().any(|&id| matches!(s.effective_symbol_rarity(id),"rare"|"very_rare")));
    for (victim,spawn,n) in [("urn","spirit",1),("big_urn","spirit",2),("tomb","spirit",4),("pinata","candy",7),("peach","seed",1)] {
        let mut altered = data();
        if victim == "tomb" { let id = altered.symbol_id("tomb").unwrap(); altered.symbols[id].values[0] = 0.0; }
        let seed = altered.symbol_id("seed").unwrap(); altered.symbols[seed].values[0] = 0.0;
        let mut s = fixed(&altered,&[(if victim == "pinata" || victim == "peach" { "mrs_fruit" } else { "hooligan" },6),(victim,7)]);
        // Piñata is toddler food, not fruit-lady food.
        if victim == "pinata" { s.deck[0].type_id = altered.symbol_id("toddler").unwrap() as u16; }
        s.resolve_spin_effects();
        let created = names(&s).iter().filter(|&&name| name == spawn).count()
            + s.destroyed_history.iter().filter(|&&id| altered.symbols[id as usize].name == spawn).count();
        assert_eq!(created,n,"{victim}; newborns may themselves be consumed in this spin");
    }
}

#[test]
fn fifth_ace_counts_each_opposite_suit_and_ninja_mouse_buffs_every_matching_symbol_once() {
    let d = data(); let mut s = fixed(&d,&[("clubs",6),("hearts",7),("hearts",1)]);
    s.items_held.push(d.item_index["fifth_ace"]); assert_eq!(s.resolve_spin_effects(),12);
    let mut s = fixed(&d,&[("ninja",6),("mouse",7),("mouse",1)]);
    s.items_held.push(d.item_index["ninja_and_mouse"]); assert_eq!(s.resolve_spin_effects(),24);
    let mut s = fixed(&d,&[("ninja",6),("ninja",7),("mouse",1)]);
    s.items_held.push(d.item_index["ninja_and_mouse"]); assert_eq!(s.resolve_spin_effects(),18);
}

#[test]
fn dwarf_uses_its_ten_times_food_payout_on_anvil_ores() {
    let mut d = data(); for def in &mut d.symbols { if def.name != "pearl" { def.groups.retain(|g| g != "gem"); } }
    let mut s = fixed(&d,&[("dwarf",6),("ore",7)]); s.items_held.push(d.item_index["dwarven_anvil"]);
    assert_eq!(s.resolve_spin_effects(),24,"(dwarf 1 + ore 1 * 10) * 2 + ore 1 + newborn pearl 1");
    let mut s = fixed(&d,&[("dwarf",6),("ore",7)]); s.items_held.push(d.item_index["dwarven_anvil"]);
    s.essences_held.push(d.essence_index["dwarven_anvil_essence"]); assert_eq!(s.resolve_spin_effects(),35);
    assert!(!s.has_essence("dwarven_anvil_essence"));
}

#[test]
fn probabilistic_destroy_items_and_xray_floor() {
    let mut d = data(); d.items[d.item_index["lockpick"]].values[0] = 100.0;
    d.items[d.item_index["grave_robber"]].values[0] = 100.0;
    let mut s = fixed(&d,&[("safe",6)]); s.items_held.push(d.item_index["lockpick"]);
    assert_eq!(s.resolve_spin_effects(),31); assert!(s.deck.is_empty());
    let mut s = fixed(&d,&[("big_urn",6)]); s.items_held.push(d.item_index["grave_robber"]);
    s.resolve_spin_effects(); assert_eq!(names(&s),vec!["spirit","spirit"]);
    let mut s = fixed(&d,&[("miner",6),("big_ore",7)]); s.items_held.push(d.item_index["x_ray_machine"]);
    s.resolve_spin_effects(); let gems: Vec<_> = s.deck.iter().filter(|sym| s.has_group(sym.type_id as usize,"gem")).collect();
    assert_eq!(gems.len(),2); assert!(gems.iter().all(|sym| matches!(s.effective_symbol_rarity(sym.type_id as usize),"rare"|"very_rare")));
}

#[test]
fn lucky_cat_pebble_and_fluff_rarity_factors() {
    let d = data(); let mut s = fixed(&d,&[("cat",6),("cat",7),("shiny_pebble",1),("rabbit_fluff",2)]);
    s.items_held.push(d.item_index["lucky_cat"]);
    let entries = [(0,1,1),(1,2,1),(2,1,0),(3,2,0)];
    assert!((s.compute_rarity_factor(&entries) - 1.3_f64.powi(2)*1.1*1.2).abs() < 1e-10);
}

#[test]
fn dice_reroll_and_lucky_dice_item_examples() {
    let d = data(); let mut s = fixed(&d,&[]); s.items_held.push(d.item_index["reroll"]);
    s.reroll_d3_roll = Some(2); s.reroll_d5_roll = Some(4);
    assert_eq!(s.roll_die("d3",3,Some(1)),2); assert_eq!(s.roll_die("d3",3,Some(2)),2);
    assert_eq!(s.roll_die("d5",5,Some(2)),4); assert_eq!(s.roll_die("d5",5,Some(3)),3);
    let mut s = fixed(&d,&[("d3",6),("d5",7)]); s.items_held.push(d.item_index["lucky_dice"]);
    for _ in 0..5 { assert_eq!(s.resolve_spin_effects(),8); }
}

#[test]
fn quiver_protractor_frozen_pizza_and_tortoise_hare_items() {
    let d = data(); let mut s = fixed(&d,&[("bronze_arrow",6)]); s.items_held.push(d.item_index["quiver"]);
    let pre = s.preregister_symbol_effects(&[(0,1,1)]); assert_eq!(pre[&0].arrow_directions.len(),2); assert_ne!(pre[&0].arrow_directions[0],pre[&0].arrow_directions[1]);
    let mut s = fixed(&d,&[("sun",0),("flower",19)]); s.items_held.push(d.item_index["protractor"]); s.protractor_saved = 2;
    assert_eq!(s.resolve_spin_effects(),8);
    let mut s = fixed(&d,&[]); s.items_held.push(d.item_index["frozen_pizza"]);
    s.resolve_spin_effects(); assert_eq!(s.pending_extra_symbol_offers,0); s.resolve_spin_effects(); assert_eq!(s.pending_extra_symbol_offers,1);
    let mut s = fixed(&d,&[("rabbit",0),("turtle",4)]); s.items_held.push(d.item_index["turtle_and_rabbit"]);
    assert_eq!(s.resolve_spin_effects(),78); assert!(!s.has_item("turtle_and_rabbit"));
    let mut s = fixed(&d,&[("sun",0),("flower",19)]); s.essences_held.push(d.essence_index["protractor_essence"]);
    for _ in 0..4 { assert_eq!(s.resolve_spin_effects(),8); } assert!(!s.has_essence("protractor_essence")); assert_eq!(s.resolve_spin_effects(),4);
}

#[test]
fn growth_consumption_and_black_pepper_essence_examples() {
    let d = data(); let mut s = fixed(&d,&[("cat",6),("milk",7)]);
    s.essences_held.push(d.essence_index["pizza_the_cat_essence"]); assert_eq!(s.resolve_spin_effects(),16);
    assert!(!s.has_essence("pizza_the_cat_essence")); assert_eq!(s.symbol_rule_multiplier(d.symbol_id("cat").unwrap()),1.5);
    let mut s = fixed(&d,&[("lucky_capsule",6),("lucky_capsule",7)]); s.essences_held.push(d.essence_index["black_pepper_essence"]);
    assert_eq!(s.resolve_spin_effects(),40); assert!(!s.has_essence("black_pepper_essence"));
    let mut s = fixed(&d,&[("buffing_powder",6),("pear",7)]); s.items_held.push(d.item_index["capsule_machine"]);
    assert_eq!(s.resolve_spin_effects(),8,"pear (base 1 + permanent growth 1) * 4"); assert_eq!(s.deck[0].bonus,1,"one source symbol supplies a x4 buff");
    let mut s = fixed(&d,&[("frozen_fossil",6)]); s.deck[0].times_displayed = 4;
    s.removed_history = vec![d.symbol_id("cultist").unwrap() as u16;3]; s.resolve_spin_effects();
    assert_eq!(names(&s),vec!["eldritch_beast"]); assert_eq!(s.symbol_rule_bonus(s.deck[0].type_id as usize),3);
}

#[test]
fn arrow_target_payout_is_multiplied_and_dice_can_kill_a_gambler() {
    let d = data();
    for (arrow,mult) in [("bronze_arrow",2),("silver_arrow",3),("golden_arrow",4)] {
        let mut board: Vec<_> = (0..20).map(|slot| ("target",slot)).collect(); board[6] = (arrow,6);
        let mut s = fixed(&d,&board); let income = s.resolve_spin_effects(); let dead = s.destroyed_history.len() as i32;
        assert!(dead > 0); let base = d.symbols[d.symbol_id("target").unwrap()].base_value;
        assert_eq!(income,(19-dead)*base+dead*(base+10)*mult,"{arrow}");
    }
    let mut saw_death = false;
    for seed in 0..32 {
        let mut s = fixed(&d,&[("gambler",6),("d3",7)]); s.rng = RngStreams::from_seed_u64(seed);
        let income = s.resolve_spin_effects();
        if !names(&s).contains(&"gambler") {
            saw_death = true; assert_eq!(income,d.symbols[d.symbol_id("gambler").unwrap()].base_value+2+1);
        }
    }
    assert!(saw_death);
}

#[test]
fn dark_humor_buffs_its_extra_targets_and_disabling_restores_comedian_rarity() {
    let d = data(); let mut s = fixed(&d,&[("comedian",6),("beer",7)]);
    let item = d.item_index["dark_humor"]; s.items_held.push(item);
    let comedian = d.symbol_id("comedian").unwrap(); let beer = d.symbol_id("beer").unwrap();
    assert_eq!(s.effective_symbol_rarity(comedian),"uncommon");
    assert_eq!(s.resolve_spin_effects(),d.symbols[comedian].base_value+3*d.symbols[beer].base_value);
    assert!(s.toggle_managed(false,item)); assert_eq!(s.effective_symbol_rarity(comedian),"rare");
}
