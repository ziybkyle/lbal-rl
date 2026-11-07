use crate::data::{Def, GameData};
use crate::policy::EvalContext;
use crate::state::{Phase, RunState};
use std::collections::HashMap;

#[derive(Debug, Clone, Copy, Default)]
pub struct EpisodeResult {
    pub seed: u64,
    pub won: bool,
    pub rents_paid: u32,
    pub coins: i32,
    pub total_spins: u32,
    pub deck_len: usize,
    pub final_rent: i32,
}

const REMOVAL_ORDER: [&str; 5] = ["cherry", "flower", "coin", "cat", "pearl"];

fn rarity_rank(def: &Def) -> i32 {
    match def.rarity.as_str() {
        "very_rare" => 4,
        "rare" => 3,
        "uncommon" => 2,
        "common" => 1,
        _ => 0,
    }
}

fn feed_bonus(
    data: &GameData,
    name: &str,
    counts: &HashMap<String, usize>,
    weight: f64,
) -> f64 {
    let mut bonus = 0.0;
    if let Some(feeds) = data.feed_pairs.get(name) {
        for (eater, amount) in feeds {
            if counts.get(eater).copied().unwrap_or(0) > 0 {
                bonus += (amount * 0.35).min(3.5);
            }
        }
    }
    for (food, feeds) in &data.feed_pairs {
        let owned = counts.get(food).copied().unwrap_or(0);
        if owned == 0 {
            continue;
        }
        if let Some((_, amount)) = feeds.iter().find(|(eater, _)| eater == name) {
            bonus += (amount * 0.25 * owned as f64).min(4.0);
        }
    }
    bonus * weight
}

fn banned_in_filling(name: &str, counts: &HashMap<String, usize>) -> bool {
    (name == "cow" && counts.get("cat").copied().unwrap_or(0) == 0)
        || (name == "coal" && counts.get("coal").copied().unwrap_or(0) >= 2)
}

/// 玩家关联资料的评分提示：每个已持有的正向关联 +a、负向关联 −b，
/// 不设封顶。默认 a=1.2、b=3.0（网格搜索所得），可用
/// LBAL_ASSOC_A / LBAL_ASSOC_B 覆盖。
fn assoc_adjustment(
    data: &GameData,
    candidate: &str,
    counts: &HashMap<String, usize>,
    held: &HashMap<String, usize>,
) -> f64 {
    let mut pos = 0.0f64;
    let mut neg = 0.0f64;
    for (other, positive) in data.assoc.links.get(candidate).map(Vec::as_slice).unwrap_or(&[]) {
        if other == "dog"
            && !matches!(
                candidate,
                "beastmaster" | "comedian" | "quigley_the_wolf" | "quigley_the_wolf_essence"
            )
        {
            continue;
        }
        if candidate == "copycat_essence" && other == "cat" && counts.get("cat").copied().unwrap_or(0) < 5 {
            continue;
        }
        let owned =
            counts.get(other).copied().unwrap_or(0) > 0 || held.get(other).copied().unwrap_or(0) > 0;
        if !owned {
            continue;
        }
        if *positive {
            pos += 1.0;
        } else {
            neg += 1.0;
        }
    }
    if data.assoc.self_positive.contains(candidate)
        && counts.get(candidate).copied().unwrap_or(0) >= 2
    {
        pos += 1.0;
    }
    data.assoc.a * pos - data.assoc.b * neg
}

fn filling_has_chest(counts: &HashMap<String, usize>) -> bool {
    ["lockbox", "safe", "treasure_chest", "mega_chest"]
        .iter()
        .any(|chest| counts.get(*chest).copied().unwrap_or(0) > 0)
}

fn selection_value(
    data: &GameData,
    type_id: usize,
    counts: &HashMap<String, usize>,
    ctx: &EvalContext,
) -> f64 {
    let def = &data.symbols[type_id];
    let assoc = assoc_adjustment(data, &def.name, counts, &ctx.items);
    if let Some(guide) = data.filling_scores.get(&def.name) {
        let mut score = *guide;
        if def.name == "key" && !filling_has_chest(counts) {
            score = score.min(1.0);
        }
        return score + feed_bonus(data, &def.name, counts, 1.0) + assoc;
    }
    data.policy
        .estimate(&def.name, ctx)
        .score
        .clamp(0.0, 6.5)
        + assoc
}

fn deck_counts(data: &GameData, deck: &[crate::state::Symbol]) -> HashMap<String, usize> {
    let mut counts: HashMap<String, usize> = HashMap::new();
    for symbol in deck {
        *counts
            .entry(data.symbols[symbol.type_id as usize].name.clone())
            .or_default() += 1;
    }
    counts
}

fn best_full_offer(
    state: &RunState,
    offer: &[usize],
    rent: i32,
    counts: &HashMap<String, usize>,
) -> Option<usize> {
    let ctx = EvalContext::new(state.data, &state.deck, &state.items_held, rent, false);
    let target = if rent <= 50 {
        1.0
    } else if rent <= 150 {
        2.0
    } else if rent <= 375 {
        3.0
    } else {
        4.0
    };
    let mut best_allowed: Option<(usize, f64, i32)> = None;
    let mut best_any: Option<(usize, f64, i32)> = None;
    let mut one_shot_available = false;
    for (index, type_id) in offer.iter().enumerate() {
        let def = &state.data.symbols[*type_id];
        let estimate = state.data.policy.estimate(&def.name, &ctx);
        let score =
            estimate.score + assoc_adjustment(state.data, &def.name, counts, &ctx.items);
        let rank = rarity_rank(def);
        let better = |current: &Option<(usize, f64, i32)>| match current {
            None => true,
            Some((_, current_score, current_rank)) => {
                score > *current_score + 1e-9
                    || ((score - *current_score).abs() <= 1e-9 && rank > *current_rank)
            }
        };
        if better(&best_any) {
            best_any = Some((index, score, rank));
        }
        if state
            .data
            .policy
            .allows_full(&def.name, counts, rent, state.spins_left)
        {
            if state.data.policy.one_shot_skip(&def.name) >= target {
                one_shot_available = true;
            }
            if better(&best_allowed) {
                best_allowed = Some((index, score, rank));
            }
        }
    }
    if let Some((index, score, _)) = best_allowed {
        if score >= target || one_shot_available || state.must_take_symbol() {
            Some(index)
        } else {
            None
        }
    } else if state.must_take_symbol() {
        best_any.map(|(index, _, _)| index)
    } else {
        None
    }
}

fn best_offer(state: &RunState) -> Option<usize> {
    let offer = match &state.phase {
        Phase::SpinReward { offer } | Phase::SymbolReward { offer } => offer,
        _ => return None,
    };
    let rent = state.rent_due();
    let deck_len = state.deck.len();
    let counts = deck_counts(state.data, &state.deck);
    if deck_len >= 20 {
        return best_full_offer(state, offer, rent, &counts);
    }
    let mut best_allowed: Option<(usize, i32, f64)> = None;
    let mut best_any: Option<(usize, i32, f64)> = None;
    let ctx = EvalContext::new(state.data, &state.deck, &state.items_held, rent, true);
    for (index, type_id) in offer.iter().enumerate() {
        let def = &state.data.symbols[*type_id];
        let value = selection_value(state.data, *type_id, &counts, &ctx);
        let score = if value > 0.0 { value } else { -100.0 };
        let rank = rarity_rank(def);
        let better = |current: &Option<(usize, i32, f64)>| match current {
            None => true,
            Some((_, current_rank, current_score)) => {
                rank > *current_rank || (rank == *current_rank && score > *current_score)
            }
        };
        if better(&best_any) {
            best_any = Some((index, rank, score));
        }
        if !banned_in_filling(&def.name, &counts) && better(&best_allowed) {
            best_allowed = Some((index, rank, score));
        }
    }
    let choice = match best_allowed.or(best_any) {
        Some((index, _, _)) => index,
        None => return None,
    };
    if state.must_take_symbol() {
        Some(choice)
    } else if best_allowed.is_some() {
        Some(choice)
    } else {
        // Every offer is banned; only force-take sources override the skip.
        None
    }
}

fn item_rarity(def: &Def) -> i32 {
    match def.rarity.as_str() {
        "very_rare" => 4,
        "rare" => 3,
        "uncommon" => 2,
        "common" => 1,
        _ => 0,
    }
}

fn best_item(state: &RunState) -> usize {
    let Phase::ItemReward { offer } = &state.phase else {
        return 0;
    };
    let rent = state.rent_due();
    let ctx = EvalContext::new(state.data, &state.deck, &state.items_held, rent, false);
    let counts = deck_counts(state.data, &state.deck);
    let mut best = 0;
    let mut best_key = (i32::MIN, f64::NEG_INFINITY);
    for (index, item_id) in offer.iter().enumerate() {
        let def = &state.data.items[*item_id];
        let combo = state.data.policy.item_combo(&def.name, &ctx).max(0.0);
        let early = state.data.early_item_scores.get(&def.name).copied().unwrap_or(0.0);
        let assoc = assoc_adjustment(state.data, &def.name, &counts, &ctx.items);
        let key = (item_rarity(def), combo.max(early) + assoc);
        if key > best_key {
            best_key = key;
            best = index;
        }
    }
    best
}
fn removal_target(state: &RunState) -> Option<usize> {
    let protected = |type_id: usize| {
        let def = &state.data.symbols[type_id];
        def.name == "dud"
            || (def.name == "coin" && state.has_fine_print(20))
            || (def.name == "egg" && state.has_fine_print(21))
            || (def.groups.iter().any(|group| group == "void")
                && state.fp_affects(36, &def.name))
    };
    for name in REMOVAL_ORDER {
        let Some(type_id) = state.data.symbol_id(name) else {
            continue;
        };
        if protected(type_id) {
            continue;
        }
        if let Some(index) = state
            .deck
            .iter()
            .position(|symbol| symbol.type_id as usize == type_id)
        {
            return Some(index);
        }
    }
    state.deck.iter().position(|symbol| {
        let def = &state.data.symbols[symbol.type_id as usize];
        !protected(symbol.type_id as usize)
            && state
                .data
                .symbol_values
                .get(&def.name)
                .copied()
                .unwrap_or(def.base_value as f64)
                < 1.5
    })
}

/// 手工策略对当前每个候选的估值（行为克隆的特权特征）：填充期用
/// `selection_value`，满 20 张后用估值+关联分，物品用 combo/early+关联分。
pub fn expert_offer_scores(state: &RunState) -> Vec<f64> {
    match &state.phase {
        Phase::SpinReward { offer } | Phase::SymbolReward { offer } => {
            let counts = deck_counts(state.data, &state.deck);
            let filling = state.deck.len() < 20;
            let ctx = EvalContext::new(
                state.data,
                &state.deck,
                &state.items_held,
                state.rent_due(),
                filling,
            );
            offer
                .iter()
                .map(|type_id| {
                    let def = &state.data.symbols[*type_id];
                    if filling {
                        selection_value(state.data, *type_id, &counts, &ctx)
                    } else {
                        state.data.policy.estimate(&def.name, &ctx).score
                            + assoc_adjustment(state.data, &def.name, &counts, &ctx.items)
                    }
                })
                .collect()
        }
        Phase::ItemReward { offer } => {
            let counts = deck_counts(state.data, &state.deck);
            let ctx =
                EvalContext::new(state.data, &state.deck, &state.items_held, state.rent_due(), false);
            offer
                .iter()
                .map(|item_id| {
                    let def = &state.data.items[*item_id];
                    let combo = state.data.policy.item_combo(&def.name, &ctx).max(0.0);
                    let early = state.data.early_item_scores.get(&def.name).copied().unwrap_or(0.0);
                    combo.max(early) + assoc_adjustment(state.data, &def.name, &counts, &ctx.items)
                })
                .collect()
        }
        Phase::EssenceReward { offer } => vec![0.0; offer.len()],
        _ => Vec::new(),
    }
}

/// 教师（手工策略）跨决策的小状态，供行为克隆逐决策取示范用。
#[derive(Debug, Default, Clone)]
pub struct ExpertMemory {
    pub rerolled_rent: Option<u32>,
    pub pending_removal: bool,
}

/// 手工策略在 gym 动作空间（0=旋转 1=跳过 2=重掷 3+候选位置 19+牌组下标）
/// 中的一步决策，语义与 `play_episode` 一致：选完符号/跳过后若有移除代币
/// 就先移除，租金 ≥6 后每次符号奖励重掷一次。
pub fn expert_gym_action(state: &RunState, mem: &mut ExpertMemory) -> u32 {
    match &state.phase {
        Phase::Ready => 0,
        Phase::SpinReward { .. } | Phase::SymbolReward { .. } => {
            if mem.pending_removal {
                mem.pending_removal = false;
                if state.removal_tokens >= state.removal_cost() {
                    if let Some(index) = removal_target(state) {
                        return 19 + index as u32;
                    }
                }
            }
            if matches!(state.phase, Phase::SymbolReward { .. })
                && state.rents_paid >= 6
                && state.reroll_tokens >= state.reroll_cost()
                && mem.rerolled_rent != Some(state.rents_paid)
            {
                mem.rerolled_rent = Some(state.rents_paid);
                return 2;
            }
            mem.pending_removal = true;
            match best_offer(state) {
                Some(position) => 3 + position as u32,
                None => 1,
            }
        }
        Phase::ItemReward { .. } => 3 + best_item(state) as u32,
        Phase::EssenceReward { .. } => 3,
        Phase::Done => 0,
    }
}

pub fn run_episode(data: &GameData, seed: u64, max_spins: u32) -> EpisodeResult {
    play_episode(RunState::new(data, seed), seed, max_spins)
}

pub fn run_episode_with_seed_text(
    data: &GameData,
    seed_text: &str,
    max_spins: u32,
) -> EpisodeResult {
    let state = RunState::new_with_seed_text(data, seed_text);
    let seed = state.rng.landlord_seed as u64;
    play_episode(state, seed, max_spins)
}

fn play_episode(mut state: RunState, seed: u64, max_spins: u32) -> EpisodeResult {
    let mut rerolled_rent = 0u32;
    while !state.done() && state.total_spins < max_spins {
        match state.phase.clone() {
            Phase::SpinReward { .. } => {
                match best_offer(&state) {
                    Some(choice) => state.choose_spin(choice),
                    None => state.skip_spin(),
                }
                if state.removal_tokens >= state.removal_cost() {
                    if let Some(index) = removal_target(&state) {
                        state.remove_symbol(index);
                    }
                }
            }
            Phase::SymbolReward { .. } => {
                if state.rents_paid >= 6
                    && state.reroll_tokens >= state.reroll_cost()
                    && rerolled_rent != state.rents_paid
                {
                    rerolled_rent = state.rents_paid;
                    state.reroll_reward();
                }
                match best_offer(&state) {
                    Some(choice) => state.choose_symbol(choice),
                    None => state.skip_symbol(),
                }
                if state.removal_tokens >= state.removal_cost() {
                    if let Some(index) = removal_target(&state) {
                        state.remove_symbol(index);
                    }
                }
            }
            Phase::ItemReward { .. } => {
                let choice = best_item(&state);
                state.choose_item(choice);
            }
            Phase::EssenceReward { .. } => {
                state.choose_essence(0);
            }
            Phase::Ready => {
                state.spin();
            }
            Phase::Done => break,
        }
    }
    if !state.done() {
        state.won = false;
    }
    let final_rent = state.rent_due();
    EpisodeResult {
        seed,
        won: state.won,
        rents_paid: state.rents_paid,
        coins: state.coins,
        total_spins: state.total_spins,
        deck_len: state.deck.len(),
        final_rent,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::Symbol;

    fn data() -> GameData {
        GameData::load(crate::default_data_dir()).expect("game data should load")
    }

    #[test]
    fn void_symbols_are_protected_by_fine_print_36() {
        let data = data();
        let mut state = RunState::new(&data, 3);
        let void = data.symbol_id("void_stone").expect("void stone");
        state.deck.clear();
        state.deck.push(Symbol::new(void, 0));
        state.fine_prints.push(36);
        assert!(removal_target(&state).is_none());
    }
}
