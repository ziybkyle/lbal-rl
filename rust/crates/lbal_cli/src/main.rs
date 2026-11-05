use lbal_core::{
    default_data_dir, run_episode, run_episode_with_seed_text, EpisodeResult, GameData, RunState,
    Phase,
};
use rayon::prelude::*;
use serde_json::json;
use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::path::PathBuf;
use std::time::Instant;

struct Args {
    games: usize,
    seed: u64,
    threads: usize,
    max_spins: u32,
    detail: bool,
    hist: bool,
    replay: Option<String>,
    replay_actions: Option<String>,
    replay_spins: u32,
    replay_json: Option<String>,
    action_json: Option<String>,
    results_json: Option<String>,
    seed_text: Option<String>,
    data_dir: PathBuf,
}

fn parse_args() -> Args {
    let mut args = Args {
        games: 10_000,
        seed: 42,
        threads: 0,
        max_spins: 20_000,
        detail: false,
        hist: false,
        replay: None,
        replay_actions: None,
        replay_spins: 5,
        replay_json: None,
        action_json: None,
        results_json: None,
        seed_text: None,
        data_dir: default_data_dir(),
    };
    let mut iter = env::args().skip(1);
    while let Some(arg) = iter.next() {
        match arg.as_str() {
            "--games" => args.games = iter.next().and_then(|v| v.parse().ok()).unwrap_or(args.games),
            "--seed" => args.seed = iter.next().and_then(|v| v.parse().ok()).unwrap_or(args.seed),
            "--threads" => args.threads = iter.next().and_then(|v| v.parse().ok()).unwrap_or(args.threads),
            "--max-spins" => {
                args.max_spins = iter.next().and_then(|v| v.parse().ok()).unwrap_or(args.max_spins)
            }
            "--replay" => args.replay = iter.next(),
            "--replay-actions" => args.replay_actions = iter.next(),
            "--replay-json" => args.replay_json = iter.next(),
            "--action-json" => args.action_json = iter.next(),
            "--results-json" => args.results_json = iter.next(),
            "--seed-text" => args.seed_text = iter.next(),
            "--data-dir" => {
                if let Some(value) = iter.next() {
                    args.data_dir = PathBuf::from(value);
                }
            }
            "--replay-spins" => {
                args.replay_spins = iter
                    .next()
                    .and_then(|v| v.parse().ok())
                    .unwrap_or(args.replay_spins)
            }
            "--detail" => args.detail = true,
            "--hist" => args.hist = true,
            "--help" | "-h" => {
                println!("usage: lbal_cli [--games N] [--seed N] [--seed-text TEXT] [--threads N] [--max-spins N] [--detail] [--hist] [--replay DIR] [--replay-actions DIR] [--replay-spins N] [--replay-json FILE] [--action-json FILE] [--results-json FILE] [--data-dir DIR]");
                std::process::exit(0);
            }
            other => {
                eprintln!("unknown argument: {other}");
                std::process::exit(2);
            }
        }
    }
    args
}

fn replay(data: &GameData, directory: &str, max_spins: u32, replay_json: Option<&str>) {
    let mut records: Vec<serde_json::Value> = Vec::new();
    let mut files: Vec<PathBuf> = fs::read_dir(directory)
        .unwrap_or_else(|error| panic!("cannot read {directory}: {error}"))
        .filter_map(|entry| entry.ok().map(|entry| entry.path()))
        .filter(|path| {
            path.file_name()
                .and_then(|name| name.to_str())
                .map(|name| name.starts_with("game_") && name.ends_with(".jsonl"))
                .unwrap_or(false)
        })
        .collect();
    files.sort();
    let mut compared = 0usize;
    let mut exact = 0usize;
    let mut abs_diff = 0i64;
    let mut shown = 0usize;
    let mut missing = 0usize;
    let mut random_boards = 0usize;
    let mut seq_exact = 0usize;
    for path in &files {
        let Ok(text) = fs::read_to_string(path) else {
            continue;
        };
        let mut previous_earned = 0i64;
        let mut previous_spins = 0u64;
        let mut previous_names: Option<Vec<String>> = None;
        for line in text.lines() {
            let Ok(value) = serde_json::from_str::<serde_json::Value>(line) else {
                continue;
            };
            let Some(state) = value.get("state") else {
                continue;
            };
            let Some(info) = state.get("info") else {
                continue;
            };
            let spins = info.get("spins").and_then(|v| v.as_u64()).unwrap_or(0);
            if spins == 0 {
                continue;
            }
            if spins > max_spins as u64 {
                break;
            }
            let mut record_names: Vec<String> = state
                .get("deck")
                .and_then(|v| v.as_array())
                .map(|items| {
                    items
                        .iter()
                        .filter_map(|item| item.get("name").and_then(|v| v.as_str()))
                        .map(str::to_string)
                        .collect()
                })
                .unwrap_or_default();
            record_names.sort();
            let deck_changed = previous_names
                .as_ref()
                .map(|previous| previous != &record_names)
                .unwrap_or(false);
            previous_names = Some(record_names);
            let earned = info.get("earned").and_then(|v| v.as_i64()).unwrap_or(0);
            let earned_delta = earned - previous_earned;
            previous_earned = earned;
            if spins <= previous_spins {
                continue;
            }
            previous_spins = spins;
            let deck_entries: Vec<(String, i32, u32, u32)> = state
                .get("deck")
                .and_then(|v| v.as_array())
                .map(|items| {
                    items
                        .iter()
                        .filter_map(|item| {
                            let name = item.get("name")?.as_str()?.to_string();
                            let features = item.get("features").and_then(|v| v.as_array());
                            let current = features
                                .and_then(|values| values.first())
                                .and_then(|v| v.as_f64())
                                .unwrap_or(1.0);
                            let bonus = features
                                .and_then(|values| values.get(1))
                                .and_then(|v| v.as_f64())
                                .unwrap_or(0.0);
                            let multiplier = features
                                .and_then(|values| values.get(2))
                                .and_then(|v| v.as_f64())
                                .unwrap_or(1.0);
                            let value = ((current + bonus) * multiplier).round() as i32;
                            // Features are captured after the spin incremented
                            // the counter; the spin itself starts one lower.
                            let displayed = features
                                .and_then(|values| values.get(8))
                                .and_then(|v| v.as_f64())
                                .unwrap_or(1.0);
                            let displayed = (displayed as u32).saturating_sub(1);
                            let given = features
                                .and_then(|values| values.get(4))
                                .and_then(|v| v.as_f64())
                                .unwrap_or(0.0);
                            Some((name, value, displayed, given as u32))
                        })
                        .collect()
                })
                .unwrap_or_default();
            let deck_names: Vec<String> = deck_entries
                .iter()
                .map(|(name, _, _, _)| name.clone())
                .collect();
            let events: Vec<i32> = info
                .get("events")
                .and_then(|v| v.as_array())
                .map(|items| {
                    items
                        .iter()
                        .filter(|event| {
                            event.get("kind").and_then(|v| v.as_str()) == Some("coin_sum")
                                && !event
                                    .get("rent_payment")
                                    .and_then(|v| v.as_bool())
                                    .unwrap_or(false)
                        })
                        .filter_map(|event| event.get("amount").and_then(|v| v.as_i64()))
                        .map(|amount| amount as i32)
                        .collect()
                })
                .unwrap_or_default();
            // `earned` ignores negative coin events; use the full coin_sum
            // sequence when the trace has it.
            let delta: i64 = if info.get("events").is_some() {
                events.iter().map(|amount| *amount as i64).sum()
            } else {
                earned_delta
            };
            let item_names: Vec<String> = state
                .get("items")
                .and_then(|v| v.as_array())
                .map(|items| {
                    items
                        .iter()
                        .filter_map(|item| {
                            item.get("name")
                                .or_else(|| item.get("type"))
                                .and_then(|v| v.as_str())
                        })
                        .map(str::to_string)
                        .collect()
                })
                .unwrap_or_default();
            let board_names: Vec<String> = state
                .get("board")
                .and_then(|v| v.as_array())
                .map(|items| {
                    items
                        .iter()
                        .filter_map(|item| item.as_str())
                        .map(str::to_string)
                        .collect()
                })
                .unwrap_or_default();
            let file_name = path.file_name().and_then(|n| n.to_str()).unwrap_or("?");
            let main_symbol = value
                .get("chosen")
                .and_then(|chosen| chosen.get("name"))
                .and_then(|v| v.as_str())
                .or_else(|| info.get("main_symbol").and_then(|v| v.as_str()));
            let rent_due = info.get("rent_due").and_then(|v| v.as_i64());
            let rents_paid = info.get("rents_paid").and_then(|v| v.as_u64());
            let has_random = board_names.iter().any(|name| {
                matches!(
                    name.as_str(),
                    "d3" | "d5" | "goose" | "oyster" | "egg" | "seed" | "chick" | "chicken"
                )
            });
            if has_random {
                random_boards += 1;
                records.push(json!({
                    "file": file_name,
                    "spin": spins,
                    "status": "random",
                    "main_symbol": main_symbol,
                    "rent_due": rent_due,
                    "rents_paid": rents_paid,
                }));
                continue;
            }
            let Some((ours, symbol_income)) =
                RunState::replay_income(data, &deck_entries, &item_names, &board_names)
            else {
                missing += 1;
                records.push(json!({
                    "file": file_name,
                    "spin": spins,
                    "status": "missing",
                    "main_symbol": main_symbol,
                    "rent_due": rent_due,
                    "rents_paid": rents_paid,
                }));
                continue;
            };
            compared += 1;
            let is_exact = ours as i64 == delta;
            if is_exact {
                exact += 1;
            } else {
                abs_diff += (ours as i64 - delta).abs();
                if shown < 12 {
                    shown += 1;
                    println!(
                        "mismatch {} spin={} game_delta={} rust={} items={:?} board={:?}",
                        file_name, spins, delta, ours, item_names, board_names
                    );
                }
            }
            let mut ordered = symbol_income.clone();
            ordered.sort_by_key(|(slot, _)| (slot % 5, slot / 5));
            let symbol_trace: Vec<serde_json::Value> = ordered
                .iter()
                .map(|(slot, value)| {
                    json!({
                        "slot": slot,
                        "name": board_names.get(*slot).cloned().unwrap_or_default(),
                        "value": value,
                    })
                })
                .collect();
            let events_match = item_names.is_empty()
                && events.len() == ordered.len()
                && events
                    .iter()
                    .zip(ordered.iter())
                    .all(|(event, (_, value))| *event == *value);
            if events_match {
                seq_exact += 1;
            }
            records.push(json!({
                "file": file_name,
                "spin": spins,
                "status": "compared",
                "main_symbol": main_symbol,
                "rent_due": rent_due,
                "rents_paid": rents_paid,
                "game_delta": delta,
                "rust": ours,
                "exact": is_exact,
                "items": &item_names,
                "deck": &deck_names,
                "board": &board_names,
                "deck_changed": deck_changed,
                "events": &events,
                "symbol_income": symbol_trace,
                "events_match": events_match,
            }));
        }
    }
    if let Some(path) = replay_json {
        let text = serde_json::to_string_pretty(&records)
            .unwrap_or_else(|error| format!("{{\"error\":\"{error}\"}}"));
        if let Err(error) = fs::write(path, text) {
            eprintln!("failed to write replay json {path}: {error}");
        }
    }
    println!(
        "replay files={} compared={} exact={} ({:.1}%) mean_abs_diff={:.2} skipped={} random_boards={} slot_seq_exact={}",
        files.len(),
        compared,
        exact,
        100.0 * exact as f64 / compared.max(1) as f64,
        abs_diff as f64 / (compared - exact).max(1) as f64,
        missing,
        random_boards,
        seq_exact
    );
}

fn offer_position_symbol(data: &GameData, offer: &[usize], name: &str) -> Option<usize> {
    offer
        .iter()
        .position(|id| data.symbols.get(*id).map(|def| def.name.as_str()) == Some(name))
}

fn offer_position_item(data: &GameData, offer: &[usize], name: &str) -> Option<usize> {
    offer
        .iter()
        .position(|id| data.items.get(*id).map(|def| def.name.as_str()) == Some(name))
}

fn offer_position_essence(data: &GameData, offer: &[usize], name: &str) -> Option<usize> {
    offer
        .iter()
        .position(|id| data.essences.get(*id).map(|def| def.name.as_str()) == Some(name))
}

/// Replays the recorded decisions of a real run through the Rust state machine.
///
/// The trace's board snapshots are taken after a spin, so they cannot be used
/// to reconstruct the spin itself. Driving the real action sequence instead
/// lets Rust deal the same board from the same seed and compare both the
/// layout and the income of every spin.
fn replay_actions(data: &GameData, directory: &str, max_spins: u32, out: Option<&str>) {
    let mut files: Vec<PathBuf> = fs::read_dir(directory)
        .unwrap_or_else(|error| panic!("cannot read {directory}: {error}"))
        .filter_map(|entry| entry.ok().map(|entry| entry.path()))
        .filter(|path| {
            path.file_name()
                .and_then(|name| name.to_str())
                .map(|name| name.starts_with("game_") && name.ends_with(".jsonl"))
                .unwrap_or(false)
        })
        .collect();
    files.sort();
    let mut records: Vec<serde_json::Value> = Vec::new();
    let mut spins_compared = 0usize;
    let mut income_exact = 0usize;
    let mut board_exact = 0usize;
    let mut action_issues = 0usize;
    let mut offer_checked = 0usize;
    let mut offer_exact = 0usize;
    let mut deck_checked = 0usize;
    let mut deck_exact = 0usize;
    let mut deck_order_exact = 0usize;
    let mut item_checked = 0usize;
    let mut item_exact = 0usize;
    let mut swallowed_actions = 0usize;
    for path in &files {
        let Ok(text) = fs::read_to_string(path) else {
            continue;
        };
        let mut seed = String::new();
        let mut decisions: Vec<serde_json::Value> = Vec::new();
        for line in text.lines() {
            let Ok(value) = serde_json::from_str::<serde_json::Value>(line) else {
                continue;
            };
            match value.get("record").and_then(|v| v.as_str()) {
                Some("episode_start") => {
                    seed = value
                        .get("seed")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string();
                }
                Some("decision") => decisions.push(value),
                _ => {}
            }
        }
        if seed.is_empty() {
            continue;
        }
        let mut state = RunState::new_with_seed_text(data, &seed);
        let mut previous_earned = 0i64;
        let mut previous_spins = 0u64;
        let mut step = 0usize;
        let file_name = path.file_name().and_then(|n| n.to_str()).unwrap_or("?");
        for (decision_index, value) in decisions.iter().enumerate() {
            let Some(state_json) = value.get("state") else {
                continue;
            };
            let Some(info) = state_json.get("info") else {
                continue;
            };
            let spins = info.get("spins").and_then(|v| v.as_u64()).unwrap_or(0);
            if spins == 0 {
                continue;
            }
            if spins > max_spins as u64 {
                break;
            }
            step += 1;
            let earned = info.get("earned").and_then(|v| v.as_i64()).unwrap_or(0);
            let earned_delta = earned - previous_earned;
            previous_earned = earned;
            let new_spin = spins > previous_spins;
            previous_spins = previous_spins.max(spins);
            // `earned` only accumulates positive coin events; the balance
            // delta also includes negative ones (crow, hex of thievery, ...).
            let event_sum: i64 = info
                .get("events")
                .and_then(|v| v.as_array())
                .map(|events| {
                    events
                        .iter()
                        .filter(|event| {
                            event.get("kind").and_then(|v| v.as_str()) == Some("coin_sum")
                                && !event
                                    .get("rent_payment")
                                    .and_then(|v| v.as_bool())
                                    .unwrap_or(false)
                        })
                        .filter_map(|event| event.get("amount").and_then(|v| v.as_i64()))
                        .sum()
                })
                .unwrap_or(earned_delta);
            let delta = if spins > 0 && info.get("events").is_some() {
                event_sum
            } else {
                earned_delta
            };

            // The real game may spin without asking for an action (for
            // example the first spin, or an auto-resolved prompt).
            let mut auto_spins = 0usize;
            while state.total_spins < spins as u32 && state.phase != Phase::Done {
                match state.phase.clone() {
                    Phase::Ready => {
                        state.spin();
                        auto_spins += 1;
                    }
                    Phase::SpinReward { .. } => state.skip_spin(),
                    Phase::SymbolReward { .. } => state.skip_symbol(),
                    Phase::ItemReward { .. } => state.skip_item(),
                    Phase::EssenceReward { .. } => state.skip_essence(),
                    Phase::Done => break,
                }
            }

            let email = info.get("email").and_then(|v| v.as_str()).unwrap_or("");
            let phase_kind = match &state.phase {
                Phase::Ready => "ready",
                Phase::SpinReward { .. } => "symbol",
                Phase::SymbolReward { .. } => "symbol_bonus",
                Phase::ItemReward { .. } => "item",
                Phase::EssenceReward { .. } => "essence",
                Phase::Done => "done",
            };
            let rust_offer_names: Vec<String> = match &state.phase {
                Phase::SpinReward { offer } | Phase::SymbolReward { offer } => offer
                    .iter()
                    .filter_map(|id| data.symbols.get(*id).map(|def| def.name.clone()))
                    .collect(),
                Phase::ItemReward { offer } => offer
                    .iter()
                    .filter_map(|id| data.items.get(*id).map(|def| def.name.clone()))
                    .collect(),
                Phase::EssenceReward { offer } => offer
                    .iter()
                    .filter_map(|id| data.essences.get(*id).map(|def| def.name.clone()))
                    .collect(),
                _ => Vec::new(),
            };
            let real_offer_names: Vec<String> = state_json
                .get("candidates")
                .and_then(|v| v.as_array())
                .map(|items| {
                    items
                        .iter()
                        .filter(|candidate| {
                            candidate.get("kind").and_then(|v| v.as_u64()) == Some(0)
                                && candidate.get("name").and_then(|v| v.as_str()) != Some("skip")
                        })
                        .filter_map(|candidate| {
                            candidate.get("name").and_then(|v| v.as_str()).map(str::to_string)
                        })
                        .collect()
                })
                .unwrap_or_default();
            let offer_is_checked =
                matches!(email, "add_tile" | "add_item") && !real_offer_names.is_empty();
            let offer_ok = {
                let mut real_sorted = real_offer_names.clone();
                real_sorted.sort();
                let mut rust_sorted = rust_offer_names.clone();
                rust_sorted.sort();
                real_sorted == rust_sorted
            };
            if offer_is_checked {
                offer_checked += 1;
                if offer_ok {
                    offer_exact += 1;
                }
            }
            let mut real_deck_all: Vec<String> = state_json
                .get("deck")
                .and_then(|v| v.as_array())
                .map(|items| {
                    items
                        .iter()
                        .filter_map(|item| item.get("name").and_then(|v| v.as_str()))
                        .map(str::to_string)
                        .collect()
                })
                .unwrap_or_default();
            let real_deck_order = real_deck_all.clone();
            real_deck_all.sort();
            let mut rust_deck_all: Vec<String> = state
                .deck
                .iter()
                .filter_map(|symbol| data.symbols.get(symbol.type_id as usize))
                .map(|def| def.name.clone())
                .collect();
            let rust_deck_order = rust_deck_all.clone();
            rust_deck_all.sort();
            let deck_ok = real_deck_all == rust_deck_all;
            let deck_order_ok = real_deck_order == rust_deck_order;
            deck_checked += 1;
            if deck_ok {
                deck_exact += 1;
            }
            if deck_order_ok {
                deck_order_exact += 1;
            }
            // Real items are stacked: one frame entry per type with the
            // stack size in `features[0]`.  Compare per-type counts.
            let mut real_item_counts: BTreeMap<String, i64> = BTreeMap::new();
            if let Some(items) = state_json.get("items").and_then(|v| v.as_array()) {
                for item in items {
                    let Some(name) = item.get("name").and_then(|v| v.as_str()) else {
                        continue;
                    };
                    if name.ends_with("_essence") {
                        continue;
                    }
                    let count = item
                        .get("features")
                        .and_then(|v| v.as_array())
                        .and_then(|v| v.first())
                        .and_then(|v| v.as_f64())
                        .unwrap_or(1.0) as i64;
                    *real_item_counts.entry(name.to_string()).or_insert(0) += count.max(1);
                }
            }
            let mut rust_item_counts: BTreeMap<String, i64> = BTreeMap::new();
            for id in &state.items_held {
                if let Some(def) = data.items.get(*id) {
                    *rust_item_counts.entry(def.name.clone()).or_insert(0) += 1;
                }
            }
            let items_ok = real_item_counts == rust_item_counts;
            item_checked += 1;
            if items_ok {
                item_exact += 1;
            }
            let mut record = json!({
                "file": file_name,
                "step": step,
                "spin": spins,
                "email": email,
                "phase_kind": phase_kind,
                "real_offer": &real_offer_names,
                "rust_offer": &rust_offer_names,
                "offer_ok": offer_ok,
                "offer_checked": offer_is_checked,
                "deck_ok": deck_ok,
                "deck_order_ok": deck_order_ok,
                "items_ok": items_ok,
                "chosen": value.get("chosen").and_then(|v| v.get("name")).and_then(|v| v.as_str()),
                "auto_spins": auto_spins,
            });
            if !deck_ok || !deck_order_ok {
                record["deck_order_mismatch"] = json!({
                    "real": &real_deck_order,
                    "rust": &rust_deck_order,
                });
            }
            if !deck_ok {
                let mut missing = real_deck_all.clone();
                for name in &rust_deck_all {
                    if let Some(index) = missing.iter().position(|entry| entry == name) {
                        missing.remove(index);
                    }
                }
                let mut extra = rust_deck_all.clone();
                for name in &real_deck_all {
                    if let Some(index) = extra.iter().position(|entry| entry == name) {
                        extra.remove(index);
                    }
                }
                record["deck_mismatch"] = json!({"missing": missing, "extra": extra});
            }
            if !items_ok {
                record["items_mismatch"] =
                    json!({"real": real_item_counts, "rust": rust_item_counts});
            }
            if new_spin {
                spins_compared += 1;
                let income_ok = state.last_income as i64 == delta;
                if income_ok {
                    income_exact += 1;
                }
                let real_board: Vec<String> = state_json
                    .get("board")
                    .and_then(|v| v.as_array())
                    .map(|items| {
                        items
                            .iter()
                            .filter_map(|item| item.as_str())
                            .map(str::to_string)
                            .collect()
                    })
                    .unwrap_or_default();
                let mut mismatch: Vec<serde_json::Value> = Vec::new();
                for (slot, name) in real_board.iter().enumerate().take(20) {
                    let ours = state.last_board.get(slot).copied().unwrap_or(-1);
                    let ours_name = if ours >= 0 {
                        data.symbols
                            .get(ours as usize)
                            .map(|def| def.name.clone())
                            .unwrap_or_else(|| "empty".to_string())
                    } else {
                        "empty".to_string()
                    };
                    if ours_name != *name {
                        mismatch.push(json!({
                            "slot": slot, "real": name, "rust": ours_name,
                        }));
                    }
                }
                if mismatch.is_empty() {
                    board_exact += 1;
                }
                let mut ordered = state.last_symbol_income.clone();
                ordered.sort_by_key(|(slot, _)| (slot % 5, slot / 5));
                let symbol_trace: Vec<serde_json::Value> = ordered
                    .iter()
                    .map(|(slot, value)| {
                        let ours = state.last_board.get(*slot).copied().unwrap_or(-1);
                        let name = if ours >= 0 {
                            data.symbols
                                .get(ours as usize)
                                .map(|def| def.name.clone())
                                .unwrap_or_default()
                        } else {
                            String::new()
                        };
                        json!({"slot": slot, "name": name, "value": value})
                    })
                    .collect();
                let items: Vec<String> = state_json
                    .get("items")
                    .and_then(|v| v.as_array())
                    .map(|items| {
                        items
                            .iter()
                            .filter_map(|item| item.get("name").and_then(|v| v.as_str()))
                            .map(str::to_string)
                            .collect()
                    })
                    .unwrap_or_default();
                record["income_ok"] = json!(income_ok);
                record["real_delta"] = json!(delta);
                record["rust_income"] = json!(state.last_income);
                record["symbol_income"] = json!(symbol_trace);
                record["item_gains"] = json!(state.last_item_gains);
                record["extra_income"] = json!(state.last_extra_income);
                record["rng_log"] = json!(state.last_effect_rng);
                record["reel_shuffle_log"] = json!(state.last_reel_shuffle);
                record["effect_shuffle_log"] = json!(state.last_effect_shuffle);
                record["items"] = json!(items);
                record["board_mismatch"] = json!(mismatch);
            }

            let chosen = value.get("chosen").cloned().unwrap_or(serde_json::Value::Null);
            let chosen_name = chosen.get("name").and_then(|v| v.as_str()).unwrap_or("");
            let chosen_reply = chosen.get("reply").and_then(|v| v.as_str()).unwrap_or("");
            let deck_gamblers = |snapshot: &serde_json::Value| -> Option<usize> {
                let deck = snapshot.get("deck")?.as_array()?;
                Some(
                    deck.iter()
                        .filter(|entry| {
                            entry.get("name").and_then(|v| v.as_str()) == Some("gambler")
                        })
                        .count(),
                )
            };
            let gamblers_before = deck_gamblers(state_json);
            let gamblers_after = decisions
                .get(decision_index + 1)
                .and_then(|next| next.get("state"))
                .and_then(deck_gamblers);
            // The real popup silently drops a click while its animation timer
            // is running. Its own trace shows that the chosen gambler never
            // reached the deck, so replay must not add one either.
            let swallowed = chosen_reply != "skip"
                && chosen_name == "gambler"
                && gamblers_after.is_some()
                && gamblers_after == gamblers_before;
            let mut status = String::from("ok");
            match email {
                "add_tile" | "add_tile_prompt" => match state.phase.clone() {
                    Phase::SpinReward { offer } => {
                        if swallowed {
                            state.swallow_reward();
                            swallowed_actions += 1;
                            status = "swallowed".to_string();
                        } else if chosen_reply == "skip" {
                            state.skip_spin();
                        } else if let Some(index) =
                            offer_position_symbol(data, &offer, chosen_name)
                        {
                            state.choose_spin(index);
                        } else {
                            status = format!("missing_offer:{chosen_name}");
                        }
                    }
                    Phase::SymbolReward { offer } => {
                        if swallowed {
                            state.swallow_reward();
                            swallowed_actions += 1;
                            status = "swallowed".to_string();
                        } else if chosen_reply == "skip" {
                            state.skip_symbol();
                        } else if let Some(index) =
                            offer_position_symbol(data, &offer, chosen_name)
                        {
                            state.choose_symbol(index);
                        } else {
                            status = format!("missing_offer:{chosen_name}");
                        }
                    }
                    Phase::ItemReward { offer } => {
                        if chosen_reply == "skip" {
                            state.skip_item();
                        } else if let Some(index) =
                            offer_position_item(data, &offer, chosen_name)
                        {
                            state.choose_item(index);
                        } else {
                            status = format!("missing_offer:{chosen_name}");
                        }
                    }
                    Phase::EssenceReward { offer } => {
                        if chosen_reply == "skip" {
                            state.skip_essence();
                        } else if let Some(index) =
                            offer_position_essence(data, &offer, chosen_name)
                        {
                            state.choose_essence(index);
                        } else {
                            status = format!("missing_offer:{chosen_name}");
                        }
                    }
                    Phase::Ready => {
                        if chosen_reply != "skip" {
                            status = "no_reward".to_string();
                        }
                    }
                    Phase::Done => status = "done".to_string(),
                },
                "add_item" => match state.phase.clone() {
                    Phase::ItemReward { offer } => {
                        if chosen_reply == "skip" {
                            state.skip_item();
                        } else if let Some(index) =
                            offer_position_item(data, &offer, chosen_name)
                        {
                            state.choose_item(index);
                        } else {
                            status = format!("missing_offer:{chosen_name}");
                        }
                    }
                    // The real game sends essence picks through the item
                    // mailbox with forced essence rarities.
                    Phase::EssenceReward { offer } => {
                        if chosen_reply == "skip" {
                            state.skip_essence();
                        } else if let Some(index) =
                            offer_position_essence(data, &offer, chosen_name)
                        {
                            state.choose_essence(index);
                        } else {
                            status = format!("missing_offer:{chosen_name}");
                        }
                    }
                    Phase::Ready => {
                        if chosen_reply != "skip" {
                            status = "no_reward".to_string();
                        }
                    }
                    _ => status = "phase_mismatch".to_string(),
                },
                "removal_token_prompt" => {
                    if state.removal_tokens >= state.removal_cost() {
                        if let Some(index) = state
                            .deck
                            .iter()
                            .position(|symbol| {
                                data.symbols[symbol.type_id as usize].name == chosen_name
                            })
                        {
                            state.remove_symbol(index);
                        } else {
                            status = format!("missing_deck:{chosen_name}");
                        }
                    } else {
                        status = "no_token".to_string();
                    }
                }
                "" => {
                    if chosen_reply == "spin" {
                        if state.phase == Phase::Ready {
                            state.spin();
                        } else {
                            status = format!("spin_phase:{:?}", state.phase);
                        }
                    } else if chosen_reply == "use_removal" {
                        status = "auto".to_string();
                    } else {
                        status = "ui".to_string();
                    }
                }
                "rent_due" | "rent_increase" | "swap_prompt_1" | "oil_can_prompt"
                | "comfy_pillow_prompt" => status = "auto".to_string(),
                _ => status = format!("unsupported:{email}"),
            }
            let issue = status.starts_with("missing")
                || status.starts_with("spin_phase")
                || status == "no_token"
                || status.starts_with("unsupported");
            if issue {
                action_issues += 1;
            }
            record["action_status"] = json!(status);
            record["phase"] = json!(format!("{:?}", state.phase));
            records.push(record);
        }
    }
    if let Some(path) = out {
        let text = serde_json::to_string_pretty(&records)
            .unwrap_or_else(|error| format!("{{\"error\":\"{error}\"}}"));
        if let Err(error) = fs::write(path, text) {
            eprintln!("failed to write action replay json {path}: {error}");
        }
    }
    println!(
        "action replay files={} spins={} income_exact={} ({:.1}%) board_exact={} ({:.1}%) offer_exact={}/{} ({:.1}%) deck_exact={}/{} ({:.1}%) deck_order={}/{} item_exact={}/{} ({:.1}%) swallowed={} action_issues={}",
        files.len(),
        spins_compared,
        income_exact,
        100.0 * income_exact as f64 / spins_compared.max(1) as f64,
        board_exact,
        100.0 * board_exact as f64 / spins_compared.max(1) as f64,
        offer_exact,
        offer_checked,
        100.0 * offer_exact as f64 / offer_checked.max(1) as f64,
        deck_exact,
        deck_checked,
        100.0 * deck_exact as f64 / deck_checked.max(1) as f64,
        deck_order_exact,
        deck_checked,
        item_exact,
        item_checked,
        100.0 * item_exact as f64 / item_checked.max(1) as f64,
        swallowed_actions,
        action_issues
    );
}

fn main() {
    let args = parse_args();
    let data = match GameData::load(&args.data_dir) {
        Ok(data) => data,
        Err(error) => {
            eprintln!("failed to load game data: {error}");
            std::process::exit(1);
        }
    };
    if let Some(seed_text) = &args.seed_text {
        let result = run_episode_with_seed_text(&data, seed_text, args.max_spins);
        println!(
            "seed_text={} won={} rents={} coins={} spins={} deck={} final_rent={}",
            seed_text,
            result.won,
            result.rents_paid,
            result.coins,
            result.total_spins,
            result.deck_len,
            result.final_rent
        );
        return;
    }
    if let Some(directory) = &args.replay {
        replay(&data, directory, args.replay_spins, args.replay_json.as_deref());
        return;
    }
    if let Some(directory) = &args.replay_actions {
        replay_actions(
            &data,
            directory,
            args.replay_spins,
            args.action_json.as_deref(),
        );
        return;
    }
    let mut builder = rayon::ThreadPoolBuilder::new();
    if args.threads > 0 {
        builder = builder.num_threads(args.threads);
    }
    let pool = builder.build().expect("failed to build thread pool");
    let start = Instant::now();
    let results: Vec<EpisodeResult> = pool.install(|| {
        (0..args.games)
            .into_par_iter()
            .map(|index| run_episode(&data, args.seed.wrapping_add(index as u64), args.max_spins))
            .collect()
    });
    let elapsed = start.elapsed().as_secs_f64();
    let wins = results.iter().filter(|result| result.won).count();
    let rents: f64 =
        results.iter().map(|result| result.rents_paid as f64).sum::<f64>() / args.games as f64;
    let spins: f64 =
        results.iter().map(|result| result.total_spins as f64).sum::<f64>() / args.games as f64;
    let deck: f64 =
        results.iter().map(|result| result.deck_len as f64).sum::<f64>() / args.games as f64;
    let final_rent: f64 =
        results.iter().map(|result| result.final_rent as f64).sum::<f64>() / args.games as f64;
    println!(
        "games={} threads={} elapsed={elapsed:.3}s games_per_sec={:.1}",
        args.games,
        pool.current_num_threads(),
        args.games as f64 / elapsed
    );
    println!(
        "wins={wins} losses={} win_rate={:.2}% avg_rents={rents:.2} avg_spins={spins:.1} avg_deck={deck:.1} avg_final_rent={final_rent:.1}",
        args.games - wins,
        100.0 * wins as f64 / args.games.max(1) as f64
    );
    if args.hist {
        let mut buckets = [0usize; 13];
        for result in &results {
            let index = (result.rents_paid as usize).min(12);
            buckets[index] += 1;
        }
        for (rents, count) in buckets.iter().enumerate() {
            println!(
                "rents={rents:2} games={count:7} share={:.4}",
                *count as f64 / args.games.max(1) as f64
            );
        }
    }
    if args.detail {
        for result in results.iter().take(10) {
            println!(
                "seed={} won={} rents={} coins={} spins={} deck={} final_rent={}",
                result.seed,
                result.won,
                result.rents_paid,
                result.coins,
                result.total_spins,
                result.deck_len,
                result.final_rent
            );
        }
    }
    if let Some(path) = &args.results_json {
        let rows: Vec<serde_json::Value> = results
            .iter()
            .map(|result| {
                json!({
                    "seed": result.seed,
                    "won": result.won,
                    "rents_paid": result.rents_paid,
                    "coins": result.coins,
                    "total_spins": result.total_spins,
                    "deck_len": result.deck_len,
                    "final_rent": result.final_rent,
                })
            })
            .collect();
        match serde_json::to_string_pretty(&rows) {
            Ok(text) => {
                if let Err(error) = fs::write(path, text) {
                    eprintln!("failed to write results json {path}: {error}");
                }
            }
            Err(error) => eprintln!("failed to serialize results: {error}"),
        }
    }
}
