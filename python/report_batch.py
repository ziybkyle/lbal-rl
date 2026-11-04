"""Create human-readable local reports from the original JSONL records."""
import argparse
from collections import Counter
import html
import json
from pathlib import Path


STYLE = """<style>body{font:15px/1.55 system-ui,'Microsoft YaHei',sans-serif;margin:24px auto;
max-width:1450px;padding:0 20px;color:#dfe8f2;background:#10151d}a{color:#8cc8ff}
table{border-collapse:collapse;width:100%;background:#19222e}td,th{border:1px solid #344253;
padding:7px;text-align:left;vertical-align:top}th{background:#233348}tr:nth-child(even){background:#1d2937}
.note{background:#263241;padding:12px;border-radius:6px}small{color:#aab9c9}details{min-width:200px}
code{color:#c9ddf1}h1,h2{color:#eaf3fc}</style>"""

CHOICE_UI = """<style>
.controls{display:flex;flex-wrap:wrap;align-items:end;gap:14px;margin:18px 0}
.controls label{display:grid;gap:4px;color:#c7d6e6}.controls input,.controls select{
 background:#19222e;color:#edf5fc;border:1px solid #53687e;border-radius:4px;padding:7px}
.choice-table th button{font:inherit;color:inherit;background:none;border:0;padding:0;text-align:left;
 cursor:pointer;white-space:nowrap}.choice-table th button:hover{text-decoration:underline}
.choice-table th[aria-sort='ascending'] button:after{content:' ▲'}
.choice-table th[aria-sort='descending'] button:after{content:' ▼'}
.choice-table td{white-space:nowrap}.choice-table tr[hidden]{display:none}
</style>"""

CHOICE_SCRIPT = """<script>
(() => {
  const table = document.getElementById('choice-table');
  const body = table.tBodies[0];
  const rows = Array.from(body.rows);
  const heads = Array.from(table.tHead.rows[0].cells);
  const search = document.getElementById('choice-search');
  const category = document.getElementById('choice-category');
  const stageMode = document.getElementById('stage-sort-mode');
  const count = document.getElementById('visible-count');
  let activeColumn = -1;
  let direction = 'descending';
  function filter() {
    const term = search.value.trim().toLocaleLowerCase();
    let shown = 0;
    for (const row of rows) {
      const visible = (!category.value || row.dataset.category === category.value) &&
        (!term || row.cells[1].textContent.toLocaleLowerCase().includes(term));
      row.hidden = !visible;
      if (visible) shown++;
    }
    count.textContent = '显示 ' + shown + ' / ' + rows.length + ' 项';
  }
  function sortRows(index, dir) {
    const head = heads[index];
    const numeric = head.dataset.type === 'number' || head.dataset.type === 'stage';
    const key = head.dataset.type === 'stage' ? stageMode.value : 'sort';
    const sign = dir === 'ascending' ? 1 : -1;
    const sorted = Array.from(body.rows).map((row, order) => ({row, order}));
    sorted.sort((a, b) => {
      const left = a.row.cells[index].dataset[key];
      const right = b.row.cells[index].dataset[key];
      const emptyLeft = left === undefined || left === '';
      const emptyRight = right === undefined || right === '';
      if (emptyLeft || emptyRight) return emptyLeft === emptyRight ? a.order - b.order : (emptyLeft ? 1 : -1);
      const diff = numeric ? Number(left) - Number(right) : left.localeCompare(right, 'zh-CN', {numeric:true});
      return diff ? sign * diff : a.order - b.order;
    });
    for (const entry of sorted) body.appendChild(entry.row);
    for (const h of heads) h.removeAttribute('aria-sort');
    head.setAttribute('aria-sort', dir);
    activeColumn = index;
    direction = dir;
  }
  heads.forEach((head, index) => head.querySelector('button').addEventListener('click', () => {
    const next = activeColumn === index ? (direction === 'ascending' ? 'descending' : 'ascending') :
      (head.dataset.type === 'text' ? 'ascending' : 'descending');
    sortRows(index, next);
  }));
  search.addEventListener('input', filter);
  category.addEventListener('change', filter);
  stageMode.addEventListener('change', () => {
    if (activeColumn >= 0 && heads[activeColumn].dataset.type === 'stage') sortRows(activeColumn, direction);
  });
  filter();
})();
</script>"""
LABELS = {"win": "胜利", "loss": "失败", "invalid": "异常，未计入胜负",
          "decision_limit": "达到决策上限，未计入胜负"}
ACTION_LABELS = {"spin": "转动", "remove": "打开删除", "skip": "跳过", "deny": "取消/拒绝",
                 "confirm": "确认", "pay": "交租", "reroll": "重掷", "toggle_item": "启用/禁用物品",
                 "destroy_item": "消耗物品", "other_button": "其他按钮"}


def esc(value):
    return html.escape(str(value), quote=True)


def action_name(c):
    name = c.get("name", "")
    if name in ("destroy_item", "toggle_item") and c.get("target_item_display"):
        return ACTION_LABELS[name] + "：" + c["target_item_display"]
    value = ACTION_LABELS.get(name) or c.get("display_name") or name
    if name.startswith("column_"):
        value = "选择第 " + str(int(name.split("_")[1]) + 1) + " 列"
    return str(value)


def display_items(items):
    result = []
    for item in items:
        label = item.get("display_name") or item.get("name", "")
        features = item.get("features", [])
        if len(features) > 3 and features[3]:
            label += "（已禁用）"
        result.append(label)
    return "、".join(result) or "无"


def page(title, content):
    return f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>{esc(title)}</title>{STYLE}<body>{content}</body></html>'


def removal_choices(records):
    """Player-selected removal-token targets, in decision order."""
    result = []
    for record in records:
        if record.get("record") != "decision" or record["chosen"].get("kind") != 2:
            continue
        info = record["state"]["info"]
        result.append({"name": action_name(record["chosen"]), "step": record["step"] + 1,
                       "spin": info.get("spins", ""), "rent": info.get("rent_due", "")})
    return result


def render_game(directory: Path, result: dict):
    path = directory / result["record_file"]
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rent_rows = "".join(f"<tr><td>{e['count']}</td><td>{e['previous_due']}</td><td>{e['spin']}</td></tr>"
                        for e in result.get("rent_events", []))
    rows = []
    offers = []
    for r in records:
        if r["record"] != "decision":
            continue
        state, chosen = r["state"], r["chosen"]
        info = state["info"]
        choices = "、".join(esc(action_name(c)) for c in state["candidates"]) or "无"
        counts = Counter(d.get("display_name") or d.get("name", "") for d in state["deck"])
        deck = "、".join(f"{esc(name)}×{count}" for name, count in sorted(counts.items())) or "无"
        items = esc(display_items(state["items"]))
        reasons = esc(r.get("explanation", ""))
        chosen_text = esc(action_name(chosen))
        cards = [c for c in state["candidates"] if c["kind"] == 0 and 0 <= c["type_id"] < 410]
        if cards:
            labels = []
            kinds = set()
            for card in cards:
                category = "符号" if card["type_id"] < 168 else "物品" if card["type_id"] < 282 else "精华"
                kinds.add(category)
                label = esc(action_name(card))
                if card["index"] == chosen["index"]:
                    label = f"<strong>{label} ✓</strong>"
                labels.append(label)
            category = " / ".join(k for k in ("符号", "物品", "精华") if k in kinds)
            offers.append(f"<tr><td>{len(offers)+1}</td><td>{esc(info.get('spins',''))}</td>"
                          f"<td>{esc(info.get('rent_due',''))}</td><td>{category}</td>"
                          f"<td>{'、'.join(labels)}</td><td><b>{chosen_text}</b><br><small>{reasons}</small></td>"
                          f"<td><a href='#decision-{r['step']+1}'>查看决策详情</a></td></tr>")
        details = f"<details><summary>候选与牌组</summary><p>候选：{choices}</p><p>储物间：{deck}</p><p>物品：{items}</p></details>"
        rows.append(f"<tr id='decision-{r['step']+1}'><td>{r['step']+1}</td><td>{esc(info.get('spins',''))}</td>"
                    f"<td>{esc(info.get('rent_due',''))}</td><td>{esc(info.get('spins_left',''))}</td>"
                    f"<td>{esc(info.get('coins',''))}</td><td>{esc(info.get('deck_len',''))}</td>"
                    f"<td>{chosen_text}<br><small>{reasons}</small></td><td>{details}</td></tr>")
    error = f"<p class='note'>异常：{esc(result['error'])}</p>" if result.get("error") else ""
    removals = removal_choices(records)
    removal_rows = "".join(f"<tr><td>{i}</td><td>{esc(r['name'])}</td><td>{esc(r['spin'])}</td>"
                           f"<td>{esc(r['rent'])}</td><td>{esc(r['step'])}</td></tr>"
                           for i, r in enumerate(removals, 1))
    title = f"单局记录 {result['seed']}"
    content = (f"<p><a href='report.html'>← 返回本批汇总</a></p><h1>{esc(title)}</h1>"
               f"<p>结果：<b>{esc(LABELS.get(result['outcome'], result['outcome']))}</b> · 进阶 20 · "
               f"转动 {esc(result.get('spins'))} 次 · 决策 {result['decisions']} 次 · 耗时 {result['seconds']} 秒</p>{error}"
               "<p class='note'>交租和状态来自游戏程序记录。转动过程可能自动处理确认、收入等事件；"
               "本页展示的是玩家需要做选择的时刻，完整事件及原始状态保存在同名 JSONL 中。</p>"
               "<h2 id='offer-history'>每次选牌记录</h2><p>按符号、物品和精华的候选画面出现顺序排列；"
               "✓ 标记实际选中的候选。跳过或重掷会显示在“实际选择”列，重掷后的候选画面另算一次。"
               "点击“查看决策详情”，再展开“候选与牌组”，可查看当时的储物间和物品。</p>"
               "<table><tr><th>选牌序号</th><th>转动次数</th><th>目标租金</th><th>类型</th>"
               "<th>当次全部候选</th><th>实际选择及理由</th><th>详细局面</th></tr>"
               + ("".join(offers) or "<tr><td colspan='7'>无选牌记录</td></tr>") + "</table>"
               "<h2>成功交租</h2><table><tr><th>次数</th><th>租金</th><th>转动次数</th></tr>"
               + (rent_rows or "<tr><td colspan='3'>无</td></tr>") + "</table>"
               "<h2>删除符号顺序</h2><p>按使用删除机会时的选择顺序记录；不包含被吃掉、自消除或其他自动移除。</p>"
               "<table><tr><th>顺序</th><th>选择删除的符号</th><th>转动次数</th><th>目标租金</th><th>决策序号</th></tr>"
               + (removal_rows or "<tr><td colspan='5'>无</td></tr>") + "</table>"
               "<h2>逐次决策</h2><table><tr><th>决策</th><th>转动</th><th>目标租金</th>"
               "<th>剩余转动</th><th>余额</th><th>符号数</th><th>选择和基线理由</th><th>局面详情</th></tr>"
               + "".join(rows) + "</table>")
    (directory / (path.stem + ".html")).write_text(page(title, content), encoding="utf-8")
    return records


def add_choice_stats(stats, records, result):
    if result["outcome"] not in ("win", "loss"):
        return
    for record in records:
        if record["record"] != "decision":
            continue
        state = record["state"]
        rent = state["info"].get("rent_due")
        rent_key = str(int(rent)) if isinstance(rent, (int, float)) and float(rent).is_integer() else str(rent)
        chosen = record["chosen"]
        seen = set()
        for candidate in state["candidates"]:
            type_id = candidate["type_id"]
            if candidate["kind"] != 0 or not 0 <= type_id < 410:
                continue
            category = "符号" if type_id < 168 else ("物品" if type_id < 282 else "精华")
            key = (category, candidate["name"])
            if key in seen:
                continue
            seen.add(key)
            row = stats.setdefault(key, {"display": candidate.get("display_name") or candidate["name"],
                                         "offers": 0, "picks": 0, "picked_games": set(),
                                         "winning_games": set(), "stages": {}})
            row["offers"] += 1
            stage = row["stages"].setdefault(rent_key, [0, 0])
            stage[0] += 1
            if chosen["kind"] == 0 and chosen["type_id"] == type_id:
                row["picks"] += 1
                stage[1] += 1
                row["picked_games"].add(result["seed"])
                if result["outcome"] == "win":
                    row["winning_games"].add(result["seed"])


def render_choice_stats(directory, stats, valid_games):
    stages = sorted({int(k) for r in stats.values() for k in r["stages"] if k.isdigit()})
    head = "".join(f'<th data-type="stage"><button type="button">{stage}</button></th>' for stage in stages)
    rows = []
    for (category, name), row in sorted(stats.items(), key=lambda e: (e[0][0], -e[1]["offers"], e[0][1])):
        games = len(row["picked_games"])
        wins = len(row["winning_games"])
        win_rate = f"{wins/games*100:.1f}% ({wins}/{games})" if games else "—"
        cells = []
        for stage in stages:
            offers, picks = row["stages"].get(str(stage), [0, 0])
            cells.append(f'<td data-picks="{picks}" data-offers="{offers}" data-rate="{picks/offers}">'
                         f'{picks}/{offers}<br><small>{picks/offers*100:.1f}%</small></td>' if offers else "<td>—</td>")
        win_sort = f' data-sort="{wins/games}"' if games else ""
        rows.append(f'<tr data-category="{esc(category)}"><td data-sort="{esc(category)}">{esc(category)}</td>'
                    f'<td data-sort="{esc(row["display"])}">{esc(row["display"])}<br><small>{esc(name)}</small></td>'
                    f'<td data-sort="{row["offers"]}">{row["offers"]}</td>'
                    f'<td data-sort="{row["picks"]}">{row["picks"]}</td>'
                    f'<td data-sort="{row["picks"]/row["offers"]}">{row["picks"]/row["offers"]*100:.1f}%</td>'
                    f'<td{win_sort}>{win_rate}</td>' + "".join(cells) + "</tr>")
    content = ("<p><a href='report.html'>← 返回本批汇总</a></p><h1>候选和选择统计</h1>"
               f"<p>进阶 20 · 有效局 {valid_games}</p>"
               "<p class='note'>出现次数是决策画面出现该候选的次数，同一画面同名只算一次；"
               "选择次数是实际选中次数，跳过和重掷后的新画面也分别记录。各租金列显示「选择次数/出现次数」及选择率。"
               "选中局胜率按至少选择过一次的有效局计算，并非该符号导致胜利的因果效果。"
               "开局自带、自动生成、自动处理的内容不算手动选择；异常局不计入本表。</p>"
               "<p>点击列名排序，再点一次反转；各租金列可选择按选择率、选择次数或出现次数排序，"
               "没有数据的项始终排在最后。表格较宽时可横向滚动。</p>" + CHOICE_UI +
               "<div class='controls'><label>搜索名称<input id='choice-search' type='search' placeholder='中文或英文名称'></label>"
               "<label>类别<select id='choice-category'><option value=''>全部</option><option>符号</option>"
               "<option>物品</option><option>精华</option></select></label>"
               "<label>租金列排序依据<select id='stage-sort-mode'><option value='rate'>选择率</option>"
               "<option value='picks'>选择次数</option><option value='offers'>出现次数</option></select></label>"
               "<span id='visible-count' aria-live='polite'></span></div>"
               "<div style='overflow:auto'><table id='choice-table' class='choice-table'><thead><tr>"
               "<th data-type='text'><button type='button'>类别</button></th>"
               "<th data-type='text'><button type='button'>名称</button></th>"
               "<th data-type='number'><button type='button'>候选出现</button></th>"
               "<th data-type='number'><button type='button'>选择</button></th>"
               "<th data-type='number'><button type='button'>选择率</button></th>"
               "<th data-type='number'><button type='button'>选中局胜率</button></th>" + head +
               "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>" + CHOICE_SCRIPT)
    (directory / "choice_stats.html").write_text(page("候选和选择统计", content), encoding="utf-8")


def render_batch(directory: Path):
    summary = json.loads((directory / "batch_summary.json").read_text(encoding="utf-8"))
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    source = directory / "summary.jsonl"
    results = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()] if source.exists() else []
    rows = []
    choice_stats = {}
    for n, result in enumerate(results, 1):
        records = render_game(directory, result)
        add_choice_stats(choice_stats, records, result)
        link = esc(Path(result["record_file"]).stem + ".html")
        removals = removal_choices(records)
        removal_text = " → ".join(esc(r["name"]) for r in removals) or "无"
        rows.append(f"<tr><td><a href='{link}'>{n}</a><br><a href='{link}#offer-history'>选牌记录</a></td>"
                    f"<td><code>{esc(result['seed'])}</code></td>"
                    f"<td>{esc(LABELS.get(result['outcome'], result['outcome']))}</td>"
                    f"<td>{esc(result.get('spins'))}</td><td>{esc(result.get('rents_paid'))}</td>"
                    f"<td>{esc(result.get('rent_due'))}</td><td>{esc(result.get('balance'))}</td>"
                    f"<td>{esc(result.get('seconds'))}</td><td>{removal_text}</td></tr>")
    rate = summary.get("valid_win_rate")
    rate_text = "无有效局" if rate is None else f"{rate*100:.2f}%"
    content = (f"<h1>进阶 20 · 对局统计</h1><p>策略：{esc(manifest['policy'])} · "
               f"批次种子：{manifest['seed']} · 有效局胜率：<b>{rate_text}</b> · "
               f"有效局 {summary['valid_games']} · 排除局 {summary['excluded_games']}</p>"
               "<p><a href='choice_stats.html'>查看符号、物品和精华的分租金阶段选择统计</a></p>"
               "<p class='note'>胜率仅统计正常胜负的对局；异常或达到上限的对局单独列出。"
               "简单基线和少量测试局不能代表模型训练效果。点击局号查看候选、选择和当时阵容。"
               "删除顺序只记录使用删除机会时选择的符号，不包含自动消除或移除。</p>"
               "<table><tr><th>局号</th><th>种子</th><th>结果</th><th>转动次数</th><th>成功交租</th>"
               "<th>最终租金</th><th>最终余额</th><th>秒</th><th>删除符号顺序</th></tr>" + "".join(rows) + "</table>"
               "<p><small>每局 JSONL 保存完整决策帧和自动事件；manifest.json 保存游戏、策略及关联数据的哈希。</small></p>")
    (directory / "report.html").write_text(page("进阶 20 · 对局统计", content), encoding="utf-8")
    render_choice_stats(directory, choice_stats, summary["valid_games"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    render_batch(parser.parse_args().directory)
