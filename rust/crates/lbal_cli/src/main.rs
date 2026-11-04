use lbal_core::{default_data_dir, run_episode, EpisodeResult, GameData, RunState};
use rayon::prelude::*;
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
    replay_spins: u32,
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
        replay_spins: 5,
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
                println!("usage: lbal_cli [--games N] [--seed N] [--threads N] [--max-spins N] [--detail] [--hist] [--replay DIR] [--replay-spins N] [--data-dir DIR]");
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

fn replay(data: &GameData, directory: &str, max_spins: u32) {
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
    for path in &files {
        let Ok(text) = fs::read_to_string(path) else {
            continue;
        };
        let mut previous_earned = 0i64;
        let mut previous_spins = 0u64;
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
            let earned = info.get("earned").and_then(|v| v.as_i64()).unwrap_or(0);
            let delta = earned - previous_earned;
            previous_earned = earned;
            if spins <= previous_spins {
                continue;
            }
            previous_spins = spins;
            let deck_names: Vec<String> = state
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
            let item_names: Vec<String> = state
                .get("items")
                .and_then(|v| v.as_array())
                .map(|items| {
                    items
                        .iter()
                        .filter_map(|item| item.get("type").and_then(|v| v.as_str()))
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
            let has_random = board_names.iter().any(|name| {
                matches!(
                    name.as_str(),
                    "d3" | "d5" | "goose" | "oyster" | "egg" | "seed" | "chick" | "chicken"
                )
            });
            if has_random {
                random_boards += 1;
                continue;
            }
            let Some(ours) = RunState::replay_income(data, &deck_names, &item_names, &board_names)
            else {
                missing += 1;
                continue;
            };
            compared += 1;
            if ours as i64 == delta {
                exact += 1;
            } else {
                abs_diff += (ours as i64 - delta).abs();
                if shown < 12 {
                    shown += 1;
                    println!(
                        "mismatch {} spin={} game_delta={} rust={} items={:?} board={:?}",
                        path.file_name().and_then(|n| n.to_str()).unwrap_or("?"),
                        spins,
                        delta,
                        ours,
                        item_names,
                        board_names
                    );
                }
            }
        }
    }
    println!(
        "replay files={} compared={} exact={} ({:.1}%) mean_abs_diff={:.2} skipped={} random_boards={}",
        files.len(),
        compared,
        exact,
        100.0 * exact as f64 / compared.max(1) as f64,
        abs_diff as f64 / (compared - exact).max(1) as f64,
        missing,
        random_boards
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
    if let Some(directory) = &args.replay {
        replay(&data, directory, args.replay_spins);
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
}
