// Shared event handling for symbols created while a spin is resolving.
// Included by state.rs so these helpers use exactly the same rule state.
impl<'a> RunState<'a> {
    fn counted_symbol_bonus(&self, type_id: usize, counts: &HashMap<u16,i32>) -> i32 {
        let def = &self.data.symbols[type_id];
        let n = counts.get(&(type_id as u16)).copied().unwrap_or(0);
        match def.name.as_str() {
            "diamond"|"watermelon" => def.values[0] as i32 * (n-1).max(0),
            "cultist" => def.values[0] as i32 * (n-1).max(0) + if n >= def.values[2] as i32 { def.values[1] as i32 } else { 0 },
            "emerald"|"ruby"|"strawberry" if n >= def.values[1] as i32 => def.values[0] as i32,
            "ninja" => {
                let bonus = self.items_held.iter().filter(|id| self.data.items[**id].name == "cursed_katana")
                    .map(|id| self.data.items[*id].values[0] as i32).sum::<i32>();
                (bonus-def.values[0] as i32) * (n-1).max(0)
            }
            "clubs"|"diamonds"|"hearts"|"spades" => {
                let total: i32 = counts.iter().filter(|(id,_)| self.has_group(**id as usize,"suit")).map(|(_,n)| *n).sum();
                if total >= def.values[2] as i32 { def.values[1] as i32 } else { 0 }
            }
            _ => 0,
        }
    }

    fn seed_growth_chance(&self, index: usize, x: usize, y: usize) -> f64 {
        let type_id = self.deck[index].type_id as usize;
        let mut chance = self.data.symbols[type_id].values[0];
        for &id in &self.items_held {
            if matches!(self.data.items[id].name.as_str(),"watering_can"|"fertilizer") { chance += self.data.items[id].values[0]; }
        }
        for n in self.neighbor_indexes(x,y,index) {
            let def = &self.data.symbols[self.deck[n].type_id as usize];
            if matches!(def.name.as_str(),"rain"|"sun"|"farmer") { chance += def.values[1]; }
        }
        chance
    }

    fn queue_revivals(&mut self, index: usize, queue: &mut Vec<usize>, lineage: &mut HashMap<usize,[u8;2]>) {
        let symbol = self.deck[index];
        for item_id in self.items_held.clone() {
            let item = &self.data.items[item_id];
            let (axis,group) = match item.name.as_str() { "void_party" => (0,"void"), "mobius_strip" => (1,"box"), _ => continue };
            let limit = item.values[1] as u8;
            if self.has_group(symbol.type_id as usize,group) && symbol.revivals[axis] < limit
                && self.rng.unit()*100.0 < item.values[0] {
                let mut counters = symbol.revivals;
                counters[axis] += 1;
                lineage.insert(queue.len(),counters);
                queue.push(symbol.type_id as usize);
            }
        }
    }

    fn finish_charged_event_essences(&mut self) {
        for name in ["lockpick_essence","happy_hour_essence","time_machine_essence","lucky_dice_essence"] {
            let Some(id) = self.data.essence_index.get(name).copied().filter(|id|
                self.essences_held.contains(id) && !self.spin_essence_triggers.contains(id)) else { continue; };
            let values = self.data.essences[id].values.clone();
            if self.essence_saved.get(&id).copied().unwrap_or(0) >= values[0] as i32 {
                if name == "happy_hour_essence" { self.multiply_permanent_group("booze",values[1]); }
                self.try_destroy_essence(id);
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn resolve_newborn_intrinsic(
        &mut self, index: usize, states: &mut HashMap<usize,SpawnedValue>,
        survivor: &mut HashMap<usize,(usize,i32,f64,i32)>,
        gone: &mut HashSet<usize>, income: &mut i32, queued: &mut Vec<usize>,
    ) {
        let Some(slot) = self.board_slot_of(index) else { return; };
        let (x,y) = (slot % BOARD_COLS,slot / BOARD_COLS);
        let mut pre = self.preregister_symbol_effects(&[(index,x,y)]);
        let pre = pre.remove(&index).unwrap_or_default();
        let type_id = self.deck[index].type_id as usize;
        let def = &self.data.symbols[type_id];
        let name = def.name.clone();
        let values = def.values.clone();
        let base_before = def.base_value;
        let permanent_before = self.permanent_mult_for(def);
        let v0 = values.first().copied().unwrap_or(0.0);
        let mut flat = 0;
        let mut destroy = false;
        let allowed = !self.fp_affects(13,&name);
        let suppressed_hex = self.has_group(type_id,"hex") && (self.has_item("holy_water") || self.holy_water_active);
        if suppressed_hex { return; }
        let disposable = self.has_group(type_id,"capsule") || matches!(name.as_str(),"chemical_seven"|"midas_bomb");
        if disposable && name != "buffing_powder" && !self.can_destroy_spawn(index,states,survivor,gone,income) { return; }
        match name.as_str() {
            "egg"|"chick"|"chicken" => {
                let (bonus,born,egg,chick) = self.grow_chicken_and_lay(index);
                flat += bonus;
                queued.extend(born);
                if egg && chick {
                    if let Some(id) = self.data.essence_index.get("chicken_coop_essence").copied()
                        .filter(|id| self.essences_held.contains(id) && !self.spin_essence_triggers.contains(id)) {
                        self.add_permanent_group_bonus("chickenstuff",self.data.essences[id].values[0] as i32);
                        self.try_destroy_essence(id);
                    }
                }
            }
            "coal" => {
                if self.deck[index].times_displayed as i32 >= self.timed_destroy_threshold("coal",v0 as i32) {
                    if let Some(id) = self.data.symbol_id("diamond") {
                        self.deck[index].type_id = id as u16;
                        self.deck[index].times_displayed = 0;
                        if let Some(ess) = self.data.essence_index.get("time_machine_essence").copied().filter(|id| self.essences_held.contains(id)) {
                            *self.essence_saved.entry(ess).or_default() += 1;
                        }
                    }
                }
            }
            "golem"|"present"|"matryoshka_doll_1"|"matryoshka_doll_2"|"matryoshka_doll_3"|"matryoshka_doll_4" => {
                destroy = self.deck[index].times_displayed as i32 >= self.timed_destroy_threshold(&name,v0 as i32);
            }
            "frozen_fossil" => {
                destroy = self.deck[index].times_displayed as i32 >= self.timed_destroy_threshold(&name,v0 as i32)
                    - self.fossil_progress() * values[1] as i32;
            }
            "dud" => destroy = self.deck[index].times_displayed >= v0 as u32,
            "void_creature"|"void_fruit"|"void_stone" => destroy = self.empty_neighbor_count(x,y) <= values[1] as i32,
            "bubble"|"spirit"|"bar_of_soap" => {
                destroy = self.deck[index].times_coins_given + 1 >= v0 as u32;
                if name == "bar_of_soap" { if let Some(id) = self.data.symbol_id("bubble") { queued.push(id); } }
            }
            "mine" => {
                if let Some(id) = self.data.symbol_id("ore") { queued.push(id); }
                destroy = self.deck[index].times_coins_given + 1 >= v0 as u32;
            }
            "king_midas" => { if let Some(id) = self.data.symbol_id("coin") { queued.push(id); } }
            "beehive"|"cow"|"goose"|"oyster"|"tomb"|"bartender"|"hex_of_midas" => {
                if allowed && self.rng.unit() * 100.0 < v0 * self.spawn_multiplier() {
                    let born = match name.as_str() {
                        "beehive" => self.data.symbol_id("honey"), "cow" => self.data.symbol_id("milk"),
                        "goose" => self.data.symbol_id("golden_egg"), "oyster" => self.data.symbol_id("pearl"),
                        "tomb" => self.data.symbol_id("spirit"), "hex_of_midas" => self.data.symbol_id("coin"),
                        _ => self.pick_group("booze"),
                    };
                    if let Some(id) = born { queued.push(id); self.note_spawner_spawns(1); }
                }
            }
            "rabbit" => {
                if self.deck[index].saved == 0 && self.deck[index].times_coins_given + 1 >= values[1] as u32 {
                    self.deck[index].bonus += v0 as i32; self.deck[index].saved = 1;
                }
                let chance: f64 = self.items_held.iter().filter(|id| self.data.items[**id].name == "shedding_season")
                    .map(|id| self.data.items[*id].values[0]).sum();
                if chance > 0.0 && allowed && self.rng.unit() * 100.0 < chance * self.spawn_multiplier() {
                    if let Some(id) = self.data.symbol_id("rabbit_fluff") { queued.push(id); self.note_spawner_spawns(1); }
                }
            }
            "wine" => {
                if self.deck[index].saved == 0 && self.deck[index].times_coins_given + 1 >= values[1] as u32 {
                    self.deck[index].bonus += v0 as i32; self.deck[index].saved = 1;
                }
            }
            "d3"|"d5" => {
                let roll = self.roll_die(&name,values[1] as i32,pre.dice);
                flat += roll;
                if roll == 1 {
                    let gamblers: Vec<usize> = self.board.slots.iter().flatten().copied().filter(|&n|
                        self.data.symbols[self.deck[n].type_id as usize].name == "gambler").collect();
                    for n in gamblers {
                        if self.can_destroy_spawn(n,states,survivor,gone,income) {
                            if let Some(s) = states.get_mut(&n) { s.exit = SymbolExit::Destroyed; } else { gone.insert(n); }
                        }
                    }
                }
                if self.has_spin_essence("lucky_dice_essence") {
                    let id = self.data.essence_index["lucky_dice_essence"];
                    flat += self.data.essences[id].values[1] as i32;
                    *self.essence_saved.entry(id).or_default() += 1;
                }
            }
            "snail"|"sloth"|"turtle"|"owl"|"magpie"|"crow"|"robin_hood" => {
                let flagged = self.has_group(type_id,"slow");
                let reduction = if flagged { self.items_held.iter().filter(|id| self.data.items[**id].name == "checkered_flag")
                    .map(|id| self.data.items[*id].values[0] as u32).sum() } else { 0 };
                if (flagged && self.has_spin_essence("checkered_flag_essence"))
                    || self.deck[index].times_displayed >= (values[1] as u32).saturating_sub(reduction).max(1) {
                    flat += v0 as i32; self.deck[index].times_displayed = 0;
                    if flagged && base_before + flat > 0 {
                        if let Some(id) = self.data.essence_index.get("checkered_flag_essence").copied().filter(|id| self.essences_held.contains(id)) {
                            *self.essence_saved.entry(id).or_default() += 1;
                        }
                    }
                }
            }
            "anchor" if (x == 0 || x == BOARD_COLS-1) && (y == 0 || y == BOARD_ROWS-1) => flat += v0 as i32,
            "seed" => {
                let chance_items = self.items_held.iter().filter(|id| self.data.items[**id].name == "fertilizer")
                    .map(|id| self.data.items[*id].values[0] / 100.0).sum::<f64>();
                if self.has_spin_essence("watering_can_essence") {
                    flat += self.data.essences[self.data.essence_index["watering_can_essence"]].values[0] as i32;
                }
                if !self.has_fine_print(34) && allowed && self.rng.unit() * 100.0 < self.seed_growth_chance(index,x,y) {
                    let floor = if self.has_spin_essence("fertilizer_essence") { Rarity::VeryRare }
                        else if chance_items > 0.0 { Rarity::Rare } else if self.has_item("compost_heap") { Rarity::Uncommon } else { Rarity::Common };
                    if let Some(id) = self.pick_group_min("plant",floor) {
                        flat += self.deck[index].current_value(self.data);
                        self.deck[index].type_id = id as u16;
                        for name in ["watering_can_essence","fertilizer_essence"] {
                            if let Some(ess) = self.data.essence_index.get(name).copied().filter(|id|
                                self.essences_held.contains(id) && !self.spin_essence_triggers.contains(id)) { self.try_destroy_essence(ess); }
                        }
                    }
                }
            }
            "hex_of_thievery" if allowed && self.rng.unit()*100.0 < v0 => *income -= values[1] as i32,
            "hex_of_emptiness"|"hex_of_hoarding" => {
                if allowed && self.spins_left != 1 && self.rng.unit()*100.0 < v0 {
                    if name == "hex_of_emptiness" && !self.force_take_symbol { self.force_skip_reward = true; }
                    if name == "hex_of_hoarding" && !self.force_skip_reward { self.force_take_symbol = true; }
                }
            }
            "hex_of_tedium" => self.spin_rarity_penalty /= v0,
            "hex_of_destruction" => {
                if allowed && self.rng.unit()*100.0 < v0 {
                    if let Some(n) = pre.destruction_target {
                        if self.can_destroy_spawn(n,states,survivor,gone,income) {
                            self.note_consumption(index,n);
                            if let Some(s) = states.get_mut(&n) { s.exit = SymbolExit::Destroyed; } else { gone.insert(n); }
                        }
                    }
                }
            }
            "hex_of_draining" => {
                if allowed && self.rng.unit()*100.0 < v0 {
                    if let Some(n) = pre.draining_target { Self::multiply_spawn_target(states,survivor,gone,n,states.contains_key(&n),0.0,income); }
                }
            }
            "bronze_arrow"|"silver_arrow"|"golden_arrow" => {
                let offsets = [(-1,-1),(0,-1),(1,-1),(1,0),(1,1),(0,1),(-1,1),(-1,0)];
                let mut hit = HashSet::new();
                for direction in pre.arrow_directions {
                    let (dx,dy) = offsets[direction-1];
                    let (mut cx,mut cy) = (x as i32+dx,y as i32+dy);
                    while cx >= 0 && cy >= 0 && cx < BOARD_COLS as i32 && cy < BOARD_ROWS as i32 {
                        if let Some(n) = self.board.slots[cy as usize*BOARD_COLS+cx as usize] {
                            if hit.insert(n) {
                                let gain = self.grow_when_buffed(n);
                                Self::add_spawn_flat(states,survivor,gone,n,states.contains_key(&n),gain,income);
                                Self::multiply_spawn_target(states,survivor,gone,n,states.contains_key(&n),v0,income);
                                if self.data.symbols[self.deck[n].type_id as usize].name == "target" && self.can_destroy_spawn(n,states,survivor,gone,income) {
                                    self.note_consumption(index,n);
                                    if let Some(s) = states.get_mut(&n) { s.exit = SymbolExit::Destroyed; } else { gone.insert(n); }
                                }
                            }
                        }
                        cx += dx; cy += dy;
                    }
                }
                if hit.is_empty() {
                    if let Some(id) = self.data.essence_index.get("quiver_essence").copied().filter(|id|
                        self.essences_held.contains(id) && !self.spin_essence_triggers.contains(id)) { self.try_destroy_essence(id); }
                }
            }
            "midas_bomb" => {
                for n in self.neighbor_indexes(x,y,index) {
                    if self.can_destroy_spawn(n,states,survivor,gone,income) {
                        Self::multiply_spawn_target(states,survivor,gone,n,states.contains_key(&n),v0,income);
                        if let Some(s) = states.get_mut(&n) { s.exit = SymbolExit::Destroyed; } else { gone.insert(n); }
                    }
                }
                destroy = true;
            }
            "buffing_powder"|"chemical_seven"|"lucky_capsule"|"tedium_capsule"|"hustler"|
            "time_capsule"|"item_capsule"|"removal_capsule"|"reroll_capsule"|"essence_capsule"|"rarity_capsule" => destroy = true,
            _ => {}
        }
        let def = &self.data.symbols[self.deck[index].type_id as usize];
        if let Some(state) = states.get_mut(&index) {
            state.sum = def.base_value + self.deck[index].bonus + self.permanent_bonus_for(def) + flat;
            state.mult *= self.permanent_mult_for(def) / permanent_before.max(f64::MIN_POSITIVE);
        }
        if destroy && self.can_destroy_spawn(index,states,survivor,gone,income) {
            if let Some(s) = states.get_mut(&index) { s.exit = SymbolExit::Destroyed; }
        }
    }
}
