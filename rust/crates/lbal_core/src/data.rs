use crate::policy::PolicyData;
use serde::Deserialize;
use serde_json::Value;
use std::collections::HashMap;
use std::fs;
use std::path::Path;
use std::path::PathBuf;

/// Game data directory.
///
/// Resolution order: `LBAL_DATA_DIR`, then `game/JSON` at the repository root
/// (the `godot_kernel/rust` layout) or inside the shared `rust` directory, and
/// finally `game/JSON` relative to the current working directory.
pub fn default_data_dir() -> PathBuf {
    if let Some(directory) = std::env::var_os("LBAL_DATA_DIR") {
        return PathBuf::from(directory);
    }
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let candidates = [
        manifest.join("../../../game/JSON"),
        manifest.join("../../game/JSON"),
        PathBuf::from("game/JSON"),
    ];
    candidates
        .iter()
        .find(|directory| directory.join("Symbols - JSON.json").is_file())
        .cloned()
        .unwrap_or_else(|| candidates[0].clone())
}

/// Rent schedule for floor 20. The first rent is 25, then the base schedule
/// from the game's `update_rent_values` with the floor-20 increments applied.
pub const RENT_SCHEDULE_FLOOR_20: [i32; 12] = [
    25, 50, 100, 150, 225, 300, 375, 450, 600, 650, 700, 777,
];

#[derive(Debug, Clone)]
pub struct Def {
    pub name: String,
    pub base_value: i32,
    pub values: Vec<f64>,
    pub rarity: String,
    pub groups: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct RawDef {
    #[serde(default)]
    value: Option<Value>,
    #[serde(default)]
    values: Option<Vec<f64>>,
    #[serde(default)]
    rarity: Option<String>,
    #[serde(default)]
    groups: Option<Vec<String>>,
}

fn value_to_i32(value: Option<&Value>) -> i32 {
    match value {
        Some(Value::Number(number)) => number.as_i64().unwrap_or(0) as i32,
        Some(Value::String(text)) => text.parse().unwrap_or(0),
        _ => 0,
    }
}

fn parse_def(name: &str, value: &Value) -> Result<Def, String> {
    let raw: RawDef = serde_json::from_value(value.clone())
        .map_err(|error| format!("failed to parse {name}: {error}"))?;
    let values = raw.values.unwrap_or_default();
    let mut base_value = value_to_i32(raw.value.as_ref());
    // `values` holds effect parameters, not a base value. Only fall back when
    // the JSON has no `value` field at all.
    if raw.value.is_none() {
        if let Some(first) = values.first() {
            base_value = *first as i32;
        }
    }
    Ok(Def {
        name: name.to_string(),
        base_value,
        values,
        rarity: raw.rarity.unwrap_or_else(|| "none".to_string()),
        groups: raw.groups.unwrap_or_default(),
    })
}

fn load_defs(path: &Path) -> Result<Vec<Def>, String> {
    let text = fs::read_to_string(path)
        .map_err(|error| format!("failed to read {}: {error}", path.display()))?;
    let root: Value = serde_json::from_str(&text)
        .map_err(|error| format!("failed to parse {}: {error}", path.display()))?;
    let object = root
        .as_object()
        .ok_or_else(|| format!("{} is not a JSON object", path.display()))?;
    let mut defs = Vec::with_capacity(object.len());
    for (name, value) in object {
        defs.push(parse_def(name, value)?);
    }
    Ok(defs)
}

fn index_by_name(defs: &[Def]) -> HashMap<String, usize> {
    defs.iter()
        .enumerate()
        .map(|(index, def)| (def.name.clone(), index))
        .collect()
}

fn load_filling_order(path: &Path) -> HashMap<String, usize> {
    let Ok(text) = fs::read_to_string(path) else {
        return HashMap::new();
    };
    let Ok(root) = serde_json::from_str::<Value>(&text) else {
        return HashMap::new();
    };
    let Some(order) = root.get("filling_common_order").and_then(Value::as_array) else {
        return HashMap::new();
    };
    order
        .iter()
        .enumerate()
        .filter_map(|(position, name)| name.as_str().map(|name| (name.to_string(), position)))
        .collect()
}

#[derive(Debug, Clone)]
pub struct FinePrintDef {
    pub values: Vec<f64>,
    pub reliant_types: Vec<String>,
    pub reliant_groups: Vec<String>,
    pub difficulty: i32,
}

fn load_filling_scores(path: &Path) -> HashMap<String, f64> {
    let Ok(text) = fs::read_to_string(path) else {
        return HashMap::new();
    };
    let Ok(root) = serde_json::from_str::<Value>(&text) else {
        return HashMap::new();
    };
    let Some(scores) = root.get("filling_symbol_scores").and_then(Value::as_object) else {
        return HashMap::new();
    };
    scores
        .iter()
        .filter_map(|(name, value)| value.as_f64().map(|score| (name.clone(), score)))
        .collect()
}

fn load_feed_pairs(path: &Path) -> HashMap<String, Vec<(String, f64)>> {
    let Ok(text) = fs::read_to_string(path) else {
        return HashMap::new();
    };
    let Ok(root) = serde_json::from_str::<Value>(&text) else {
        return HashMap::new();
    };
    let Some(pairs) = root.get("feed_pairs").and_then(Value::as_object) else {
        return HashMap::new();
    };
    let mut result = HashMap::new();
    for (food, entries) in pairs {
        let Some(entries) = entries.as_array() else {
            continue;
        };
        let mut feeds = Vec::new();
        for entry in entries {
            let Some(pair) = entry.as_array() else {
                continue;
            };
            if pair.len() != 2 {
                continue;
            }
            if let (Some(eater), Some(bonus)) = (pair[0].as_str(), pair[1].as_f64()) {
                feeds.push((eater.to_string(), bonus));
            }
        }
        if !feeds.is_empty() {
            result.insert(food.clone(), feeds);
        }
    }
    result
}

fn load_early_item_scores(path: &Path) -> HashMap<String, f64> {
    let Ok(text) = fs::read_to_string(path) else {
        return HashMap::new();
    };
    let Ok(root) = serde_json::from_str::<Value>(&text) else {
        return HashMap::new();
    };
    let Some(scores) = root.get("early_item_scores").and_then(Value::as_object) else {
        return HashMap::new();
    };
    scores
        .iter()
        .filter_map(|(name, value)| value.as_f64().map(|score| (name.clone(), score)))
        .collect()
}

fn load_fine_prints(path: &Path) -> HashMap<u32, FinePrintDef> {
    let Ok(text) = fs::read_to_string(path) else {
        return HashMap::new();
    };
    let Ok(root) = serde_json::from_str::<Value>(&text) else {
        return HashMap::new();
    };
    let Some(object) = root.as_object() else {
        return HashMap::new();
    };
    let mut result = HashMap::new();
    for (key, def) in object {
        let Ok(number) = key.parse::<u32>() else {
            continue;
        };
        // Base data stores a single string (or null); mods may store arrays.
        let string_list = |field: &str| -> Vec<String> {
            match def.get(field) {
                Some(Value::String(value)) => vec![value.clone()],
                Some(Value::Array(items)) => items
                    .iter()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect(),
                _ => Vec::new(),
            }
        };
        let values = def
            .get("values")
            .and_then(Value::as_array)
            .map(|items| items.iter().filter_map(Value::as_f64).collect())
            .unwrap_or_default();
        result.insert(
            number,
            FinePrintDef {
                values,
                reliant_types: string_list("reliant_types"),
                reliant_groups: string_list("reliant_groups"),
                difficulty: def.get("difficulty").and_then(Value::as_i64).unwrap_or(0) as i32,
            },
        );
    }
    result
}

/// 玩家关联资料（data/association_rules.json）：正向 pairs、负向 red_pairs、
/// 自身复制增益 self_positive。灰色与 lunchbox 关联按导出方约定忽略。
#[derive(Debug, Clone)]
pub struct AssocRules {
    pub links: HashMap<String, Vec<(String, bool)>>,
    pub self_positive: std::collections::HashSet<String>,
    /// 每个正向关联的加分（LBAL_ASSOC_A 可覆盖）。
    pub a: f64,
    /// 每个负向关联的减分（LBAL_ASSOC_B 可覆盖）。
    pub b: f64,
}

impl Default for AssocRules {
    fn default() -> Self {
        Self {
            links: HashMap::new(),
            self_positive: std::collections::HashSet::new(),
            // 2026-11-06 网格搜索（每组 1~2 万局）：无封顶后 a=1.2、b≈2~3
            // 稳定在 20.8%~20.9%；a=2.0 开始过头回落。取平台中值。
            a: env_f64("LBAL_ASSOC_A", 1.2),
            b: env_f64("LBAL_ASSOC_B", 3.0),
        }
    }
}

fn env_f64(key: &str, fallback: f64) -> f64 {
    std::env::var(key)
        .ok()
        .and_then(|text| text.parse::<f64>().ok())
        .unwrap_or(fallback)
}

fn load_association_rules(path: &Path) -> AssocRules {
    let Ok(text) = fs::read_to_string(path) else {
        return AssocRules::default();
    };
    let Ok(root) = serde_json::from_str::<Value>(&text) else {
        return AssocRules::default();
    };
    let mut rules = AssocRules::default();
    if let Some(list) = root.get("self_positive").and_then(Value::as_array) {
        for name in list.iter().filter_map(Value::as_str) {
            rules.self_positive.insert(name.to_string());
        }
    }
    let mut push_pair = |left: String, right: String, positive: bool, rules: &mut AssocRules| {
        if left == right || left == "lunchbox" || right == "lunchbox" {
            return;
        }
        rules
            .links
            .entry(left.clone())
            .or_default()
            .push((right.clone(), positive));
        rules.links.entry(right).or_default().push((left, positive));
    };
    for (key, positive) in [("pairs", true), ("red_pairs", false)] {
        let Some(list) = root.get(key).and_then(Value::as_array) else {
            continue;
        };
        for pair in list.iter().filter_map(Value::as_array) {
            if pair.len() != 2 {
                continue;
            }
            let (Some(left), Some(right)) = (pair[0].as_str(), pair[1].as_str()) else {
                continue;
            };
            push_pair(left.to_string(), right.to_string(), positive, &mut rules);
        }
    }
    rules
}

#[derive(Debug, Clone)]
pub struct GameData {
    pub symbols: Vec<Def>,
    pub symbol_index: HashMap<String, usize>,
    pub items: Vec<Def>,
    pub item_index: HashMap<String, usize>,
    pub essences: Vec<Def>,
    pub essence_index: HashMap<String, usize>,
    pub fine_print_count: usize,
    pub symbol_values: HashMap<String, f64>,
    pub one_shot: HashMap<String, f64>,
    pub rent_curves: HashMap<String, Vec<(f64, f64)>>,
    pub filling_order: HashMap<String, usize>,
    pub filling_scores: HashMap<String, f64>,
    pub feed_pairs: HashMap<String, Vec<(String, f64)>>,
    pub early_item_scores: HashMap<String, f64>,
    pub assoc: AssocRules,
    pub fine_prints: HashMap<u32, FinePrintDef>,
    pub policy: PolicyData,
}

#[derive(Debug, Default)]
struct Valuation {
    base: HashMap<String, f64>,
    one_shot: HashMap<String, f64>,
    rent_curves: HashMap<String, Vec<(f64, f64)>>,
}

fn load_valuation(path: &Path) -> Valuation {
    let Ok(text) = fs::read_to_string(path) else {
        return Valuation::default();
    };
    let Ok(root) = serde_json::from_str::<Value>(&text) else {
        return Valuation::default();
    };
    let mut valuation = Valuation::default();
    if let Some(symbols) = root.get("symbols").and_then(Value::as_object) {
        for (name, def) in symbols {
            if let Some(value) = def.get("base").and_then(Value::as_f64) {
                valuation.base.insert(name.clone(), value);
            }
        }
    }
    if let Some(one_shot) = root.get("one_shot").and_then(Value::as_object) {
        for (name, def) in one_shot {
            if let Some(value) = def.get("value").and_then(Value::as_f64) {
                valuation.one_shot.insert(name.clone(), value);
            }
        }
    }
    if let Some(curves) = root.get("rent_curves").and_then(Value::as_object) {
        for (name, points) in curves {
            let Some(points) = points.as_array() else {
                continue;
            };
            let mut curve = Vec::with_capacity(points.len());
            for point in points {
                let Some(pair) = point.as_array() else {
                    continue;
                };
                if pair.len() != 2 {
                    continue;
                }
                let rent = pair[0].as_f64().unwrap_or(0.0);
                let value = pair[1].as_f64().unwrap_or(0.0);
                curve.push((rent, value));
            }
            valuation.rent_curves.insert(name.clone(), curve);
        }
    }
    valuation
}

impl GameData {
    /// Load the game's JSON definitions from `game/JSON`.
    pub fn load(directory: impl AsRef<Path>) -> Result<Self, String> {
        let directory = directory.as_ref();
        let symbols = load_defs(&directory.join("Symbols - JSON.json"))?;
        let items = load_defs(&directory.join("Items - JSON.json"))?;
        let essences = load_defs(&directory.join("Essences - JSON.json"))?;
        let fine_print_text = fs::read_to_string(directory.join("Fine Print - JSON.json"))
            .map_err(|error| format!("failed to read fine print: {error}"))?;
        let fine_print_json: Value = serde_json::from_str(&fine_print_text)
            .map_err(|error| format!("failed to parse fine print: {error}"))?;
        let fine_print_count = fine_print_json
            .as_object()
            .map(|object| object.len())
            .unwrap_or(0);
        let fine_prints = load_fine_prints(&directory.join("Fine Print - JSON.json"));
        let valuation = directory
            .parent()
            .and_then(Path::parent)
            .map(|root| load_valuation(&root.join("data").join("symbol_values.json")))
            .unwrap_or_default();
        let priors_path = directory.parent().and_then(Path::parent).map(|root| {
            root.join("data").join("baseline_priors.json")
        });
        let filling_order = priors_path
            .as_deref()
            .map(load_filling_order)
            .unwrap_or_default();
        let filling_scores = priors_path
            .as_deref()
            .map(load_filling_scores)
            .unwrap_or_default();
        let feed_pairs = priors_path
            .as_deref()
            .map(load_feed_pairs)
            .unwrap_or_default();
        let early_item_scores = priors_path
            .as_deref()
            .map(load_early_item_scores)
            .unwrap_or_default();
        let assoc = directory
            .parent()
            .and_then(Path::parent)
            .map(|root| load_association_rules(&root.join("data").join("association_rules.json")))
            .unwrap_or_default();
        let policy = directory
            .parent()
            .and_then(Path::parent)
            .map(|root| PolicyData::load(&root.join("data").join("symbol_values.json")))
            .unwrap_or_default();
        Ok(Self {
            symbol_index: index_by_name(&symbols),
            item_index: index_by_name(&items),
            essence_index: index_by_name(&essences),
            symbols,
            items,
            essences,
            fine_print_count,
            symbol_values: valuation.base,
            one_shot: valuation.one_shot,
            rent_curves: valuation.rent_curves,
            filling_order,
            filling_scores,
            feed_pairs,
            early_item_scores,
            assoc,
            fine_prints,
            policy,
        })
    }

    pub fn symbol(&self, name: &str) -> Option<&Def> {
        self.symbol_index.get(name).map(|index| &self.symbols[*index])
    }

    pub fn symbol_id(&self, name: &str) -> Option<usize> {
        self.symbol_index.get(name).copied()
    }
}
