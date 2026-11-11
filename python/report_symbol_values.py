"""Produce an inspectable inventory and precomputed food/feeder table."""
import html
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def render():
    data = json.loads((ROOT / "data" / "symbol_values.json").read_text(encoding="utf8"))
    symbols = data["symbols"]
    raw_symbols = json.loads((ROOT / "game" / "JSON" / "Symbols - JSON.json").read_text(encoding="utf8"))
    rules = json.loads((ROOT / "data" / "association_rules.json").read_text(encoding="utf8"))
    label = lambda name: symbols.get(name, {}).get("name_zh") or name
    label_any = lambda name: rules["names"].get(name, {}).get("display") or label(name)
    esc = lambda value: html.escape(str(value), quote=True)
    status = {"base_and_supported_synergies": "基础及列出配合", "partially_modeled": "部分近似",
              "base_only": "仅基础值", "utility_proxy": "功能代理分", "internal": "内部符号",
              "special_combo": "同花/系绳组合", "one_shot": "一次性"}
    def description(name, obj):
        values = raw_symbols.get(name, {}).get("values", [])
        def replace(match):
            tag = match.group(1)
            if tag.startswith("value_") and tag[6:].isdigit():
                index = int(tag[6:]) - 1
                return str(values[index]) if 0 <= index < len(values) else "[数值待核对]"
            if tag.startswith("icon_"):
                key = tag[5:]
                return "金币" if key == "coin" else label(key)
            if tag.startswith("group_") or tag.startswith("last_"):
                return "[" + tag + "]"
            return ""
        return re.sub(r"<([^>]*)>", replace, obj.get("description") or "")
    curves = data.get("rent_curves", {})
    curse_values = data.get("curse_values", {})
    growth_assoc = data.get("growth_associations", {})
    def curve_text(name):
        return "、".join(f"租金{int(rent)}：{value:g}" for rent, value in curves[name]) if name in curves else ""
    def special_text(name):
        if name in curves:
            return "租金线性成长：" + curve_text(name)
        if name in curse_values:
            rule = curse_values[name]
            return "诅咒净期望：" + ("1.2" if rule.get("kind") == "thievery" else
                                      f"基础{rule.get('base', 2.5):g}，租金150及以前更高，之后衰减")
        if name == "hustler":
            return "台球每回合 +1，先扣 7，按剩余旋转 ×0.6 折现；扭蛋机可翻倍台球数量"
        if name == "wildcard":
            return "相邻最高当前价值 ×0.9，下限 8"
        if name == "king_midas":
            return "有海盗时最高优先；两种金币倍率可拿；系绳硬币 4 / 精华 11"
        if name == "golden_arrow":
            return "攻略 4.125X 公式，抓取分下限 12（原 40 已下调）"
        if name == "midas_bomb":
            return "默认 0.5；牌组≥24 且低质量≥6 时才作为清洁工具"
        if name == "diamond":
            return "基础 5；每多一张 +2（第二张 7，第三张 9）"
        if name == "watermelon":
            return "基础 4；每多一张 +2（第二张 6）"
        if name in ("ruby", "emerald", "strawberry"):
            return "基础 3；第二张 5（每多一张 +2）"
        if name == "highlander":
            return "固定 6"
        if name == "card_shark":
            return "固定 3 价值纯白板"
        if name == "golem":
            return "裸抓 0.5；有地质学家按成长关联 16；有矿工/矮人/矿镐/硼砂可到 3"
        if name == "rabbit":
            return "按攻略按 3 价值；月光/胡萝卜/诺里另算"
        if name == "wine":
            return "按攻略按 3 价值；矮人/矮人砧喂食另算"
        if name == "coal":
            return "按未来钻石：租金≤150 为 4，≤300 为 3，≤450 为 2，之后 1"
        if name == "matryoshka_doll_1":
            return "按剩余回合平均成长：1+剩余/20，限制在 2～4"
        if name == "cow":
            return "有猫时至少 4；无猫不进入满格候选"
        if name == "gambler":
            return "隐性白板 2.5；有 d3/d5/迈达斯炸弹时 3"
        if name == "beehive":
            return "有熊时按攻略 7；蜜蜂/厨师另算"
        if name == "bounty_hunter":
            return "有扎罗夫合约时 3+25×人类相邻概率，小偷额外 20"
        if name == "ninja":
            return "每只老鼠 +1.5；多忍者每只 -2"
        if name == "mouse":
            return "每只忍者 +1.5；奶酪 20 喂食另算"
        if name in data.get("one_shot", {}):
            rule = data["one_shot"][name]
            kind = rule.get("kind", "cash")
            scale = data.get("one_shot_horizon", {}).get("filling_scale", 0.5)
            if kind == "adjacent":
                text = f"一次性：只放大相邻符号一回合；填格期空位≥3 时再 ×{scale:g}"
            elif name == "chemical_seven":
                text = (f"一次性：7 金币 + 幸运七；填格期空位≥3 约 {rule.get('value', 0) * scale:.1f}")
            else:
                text = (f"一次性：等效抓取分 {rule.get('value', 0):g}；"
                        f"填格期空位≥3 约 {rule.get('value', 0) * scale:.1f}")
            if rule.get("skip_value"):
                text += f"；若只剩跳过，可用原值 {rule['skip_value']:g} 判断不跳过"
            if rule.get("capsule") and rule.get("kind") != "adjacent":
                text += "；牌组满 20 后额外 +1 抓位"
            return text
        if name == "key":
            return "有箱子时 5 + 0.5×平均箱子死亡收益；无箱子不拿"
        if name == "magic_key":
            return "固定 3"
        if name in ("lockbox", "safe", "treasure_chest", "mega_chest"):
            return "固定实际价值 " + str(symbols[name]["base"])
        if name == "tomb":
            return "固定 4"
        if name in ("urn", "big_urn"):
            return "按基础价值"
        if name == "big_ore":
            return "固定 2；与地质学家关联时 12"
        if name in data.get("spiritbox", {}).get("symbols", []):
            return "持有流氓/盗墓人时额外加成"
        return ""
    rows = []
    for name, obj in sorted(symbols.items(), key=lambda x: (x[1]["rarity"] is None, x[0])):
        sources = "、".join(label(x) for x in obj["feeders"]) or "—"
        boosters = "、".join(label(x) for x in obj["boosters"]) or "—"
        desc = description(name, obj)
        rows.append(f"<tr><td>{esc(label(name))}<br><small>{esc(name)}</small></td><td>{esc(obj['rarity'] or '内部')}</td>"
                    f"<td>{obj['base']:.2f}</td><td>{esc(special_text(name))}</td>"
                    f"<td>{status[obj['status']]}</td><td>{esc(sources)}</td>"
                    f"<td>{esc(boosters)}</td><td>{esc(desc)}</td></tr>")
    pairs = []
    for key, pair in sorted(data["pairs"].items()):
        food, eater = key.split("/", 1)
        base = symbols[food]["base"]
        vals = []
        for n in (1, 2, 3):
            value = base + pair["expected_extra"][str(n)]
            if pair["growth"]:
                value = max(4.5, value)
            vals.append(f"{value:.2f}")
        note = "永久成长：最低按 4.5 估分" if pair["growth"] else "一次性喂食估算"
        pairs.append(f"<tr><td>{esc(label(food))} <small>{esc(food)}</small></td>"
                     f"<td>{esc(label(eater))} <small>{esc(eater)}</small></td><td>{base:.2f}</td>"
                     + "".join(f"<td>{v}</td>" for v in vals) + f"<td>{note}</td></tr>")
    rent_rows = "".join(
        f"<tr><td>{esc(label(name))}<br><small>{esc(name)}</small></td>"
        f"<td>{esc(curve_text(name))}</td><td>{esc('线性插值；见政策说明')}</td></tr>"
        for name in curves)
    curse_rows = "".join(
        f"<tr><td>{esc(label(name))}<br><small>{esc(name)}</small></td>"
        f"<td>{esc(rule.get('kind', ''))}</td>"
        f"<td>{esc('1.2' if rule.get('kind') == 'thievery' else '随租金和牌组平均价值变化')}</td></tr>"
        for name, rule in curse_values.items())
    special_rows = "".join(
        f"<tr><td>{esc(label(name))}<br><small>{esc(name)}</small></td><td>{esc(special_text(name))}</td></tr>"
        for name in ["hustler", "wildcard", "king_midas", "golden_arrow", "midas_bomb",
                     "key", "magic_key", "lockbox", "safe", "treasure_chest", "mega_chest",
                     "tomb", "urn", "big_urn", "big_ore", "diamond", "watermelon", "ruby",
                     "emerald", "strawberry", "highlander", "card_shark", "golem", "rabbit",
                     "wine", "coal", "matryoshka_doll_1", "cow", "gambler", "beehive",
                     "bounty_hunter", "ninja", "mouse"])
    one_shot_names = list(data.get("one_shot", {})) + ["hustler"]
    one_shot_rows = "".join(
        f"<tr><td>{esc(label(name))}<br><small>{esc(name)}</small></td>"
        f"<td>{esc(special_text(name))}</td>"
        f"<td>{esc(data.get('one_shot', {}).get(name, {}).get('kind', 'pool_ball'))}</td></tr>"
        for name in one_shot_names)
    growth_rows = "".join(
        f"<tr><td>{esc(label(core))}<br><small>{esc(core)}</small></td>"
        f"<td>{esc('、'.join(f'{label(food)} {score:g}' for food, score in foods.items()))}</td></tr>"
        for core, foods in growth_assoc.items())
    addition_rows = "".join(
        f"<tr><td>{esc(label_any(name))}<br><small>{esc(name)}</small></td>"
        f"<td>{esc(rule.get('types') or rule.get('groups') or [])}</td><td>{rule['amount']}</td></tr>"
        for name, rule in data.get("item_additions", {}).items())
    multiplier_rows = "".join(
        f"<tr><td>{esc(label_any(name))}<br><small>{esc(name)}</small></td>"
        f"<td>{esc(rule.get('types') or rule.get('groups') or [])}</td><td>{rule['multiplier']}</td></tr>"
        for name, rule in data.get("item_multipliers", {}).items())
    passive_rows = "".join(
        f"<tr><td>{esc(label_any(name))}<br><small>{esc(name)}</small></td><td>{value:g}</td></tr>"
        for name, value in data.get("item_passive", {}).items())
    combos = data["special_combos"]
    suit_names = "、".join(label_any(name) for name in combos["suits"])
    flush, hover = combos["flush"], combos["hover"]
    generators = "、".join(f"{label_any(name)}（约 {weight:.0%}/回合）"
                           for name, weight in hover["generators"].items())
    combo_rows = "".join([
        f"<tr><td>同花</td><td>{esc(label_any(flush['item']))}<br><small>{esc(flush['item'])}</small></td>"
        f"<td>每次旋转前收走全部花色符号（{esc(suit_names)}），每个花色 {flush['payout_item']} 金币；"
        "花色不占出场格，按一次性金币计。</td></tr>",
        f"<tr><td>同花精华</td><td>{esc(label_any(flush['essence']))}<br><small>{esc(flush['essence'])}</small></td>"
        f"<td>每次旋转前收走全部花色符号，每个 {flush['payout_essence']} 金币；"
        f"收走 {flush['essence_cap']} 个后销毁，最多 {flush['payout_essence'] * flush['essence_cap']} 金币。</td></tr>",
        f"<tr><td>系绳硬币</td><td>{esc(label_any(hover['item']))}<br><small>{esc(hover['item'])}</small></td>"
        f"<td>自身每回合 1 金币；每次旋转前收走悬浮金币，每个 {hover['payout_item']} 金币。"
        f"当前已建模的生成来源：{esc(generators)}。</td></tr>",
        f"<tr><td>系绳硬币精华</td><td>{esc(label_any(hover['essence']))}<br><small>{esc(hover['essence'])}</small></td>"
        f"<td>每次旋转前收走悬浮金币，每个 {hover['payout_essence']} 金币；"
        f"收走 {hover['essence_cap']} 个后销毁，最多 {hover['payout_essence'] * hover['essence_cap']} 金币。</td></tr>",
    ])
    base_only = sorted(name for name, obj in symbols.items() if obj["status"] == "base_only")
    base_only_text = "、".join(f"{label(name)}（{name}）" for name in base_only)
    partial_count = sum(1 for obj in symbols.values() if obj["status"] == "partially_modeled")
    one_shot_count = sum(1 for obj in symbols.values() if obj["status"] == "one_shot")
    page = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>符号估值和配合覆盖</title>'
            '<style>body{font:15px/1.55 system-ui,"Microsoft YaHei";max-width:1450px;margin:24px auto;'
            'padding:0 20px;background:#10151d;color:#dfe8f2}a{color:#8cc8ff}small{color:#aab9c9}'
            'table{border-collapse:collapse;width:100%;background:#19222e}td,th{border:1px solid #344253;'
            'padding:7px;text-align:left;vertical-align:top}th{background:#233348}.note{background:#263241;padding:12px}'
            'input{background:#19222e;color:#fff;border:1px solid #53687e;padding:8px;width:300px}</style>'
            '<body><h1>符号估值和配合覆盖</h1><p class="note">这是一份可检查的基线近似表，不是游戏全部规则的精确计算。'
            'v12 按 85% 胜率攻略逐项校准了基础值：钻石 5、西瓜 4、高地剑 6、纸牌高手 3、裸石巨人 0.5、'
            '兔子/葡萄酒 3、煤炭的未来钻石价值、金箭头 4.125X 公式等；同时保留用户此前明确给出的宝箱值和成长曲线。'
            '基础值和已列出的配合可能仍忽略生成物、局面、禁用物品和额外效果；“仅基础值”明确表示特殊效果尚未建模。'
            f'“部分近似”目前有 {partial_count} 个符号；一次性符号 {one_shot_count} 个，按一次性折算分估值；内部符号一般不直接出现在普通选择中。'
            '本页同时列出本轮按用户经验重估的成长曲线、诅咒净期望、宝箱/坟墓/骨灰瓮固定值和间接关联表。</p>'
            '<p><a href="#rent-list">成长曲线与诅咒</a> · <a href="#special-list">特殊固定值</a> · '
            '<a href="#one-shot-list">一次性符号</a> · '
            '<a href="#growth-list">成长核心的间接食物</a> · <a href="#combo-list">同花与系绳硬币</a> · '
            '<a href="#unmodeled">尚未完整建模的效果</a> · <a href="#symbol-list">查看逐个符号</a> · '
            '<a href="#pair-list">查看 1～3 个喂食卡的预计算表</a></p>'
            '<h2 id="rent-list">成长曲线与诅咒</h2><p class="note">成长曲线是“抓取价值”，不是符号印刷的每回合金币；'
            '在曲线锚点之间线性插值。诅咒不再一律按 3 分：偷窃魔法按 1.2，破坏/抽吸/囤积在租金 150 以前更高，'
            '之后下降；破坏和囤积在较晚阶段进入删除目标。</p>'
            '<table><tr><th>成长符号</th><th>锚点</th><th>说明</th></tr>' + rent_rows + '</table>'
            '<table><tr><th>诅咒魔法</th><th>类型</th><th>估值口径</th></tr>' + curse_rows + '</table>'
            '<h2 id="special-list">特殊固定值</h2><p class="note">这些数值来自用户经验和 85% 胜率攻略的逐项校准，'
            '冲突时以 85% 攻略优先：宝箱基础值改为 1/1/2/3、通配符按用户经验保留 8 分下限、金箭头改用 4.125X 公式并设 12 分下限；用户此前给出的成长曲线没有与攻略具体数值直接冲突，继续保留。'
            '钥匙只在场上有箱子时进入高优先级；租金 777 前最后 5 个旋转停止拿成长核心及其喂食符号。</p>'
            '<table><tr><th>符号</th><th>本次重估规则</th></tr>' + special_rows + '</table>'
            '<h2 id="one-shot-list">一次性符号的相对估值</h2><p class="note">'
            '一次性金币按四次旋转摊算，功能扭蛋按统一代理分估值。'
            '再乘当前租金的剩余回合系数（约 0.4～1.0）；牌组满 20 后，扭蛋类额外 +1 抓位（攻略：50 后和满格阶段扭蛋抓位大幅上升）。'
            '增益扭蛋只放大相邻符号一回合，按当前场面周围符号的平均价值估算；七号化学物额外计算幸运七物品，'
            '扭蛋机和扭蛋机精华会放大扭蛋效果。填格阶段若还有至少 3 个空位，再乘 0.5。'
            '财富扭蛋、七号化学物和三种虚空符号可用一次性金币判断是否值得跳过。</p>'
            '<table><tr><th>符号</th><th>实际效果/重估</th><th>类型</th></tr>'
            + one_shot_rows + '</table>'
            '<h2 id="growth-list">成长核心的间接食物</h2><p class="note">这些不是简单的一对一喂食表，而是“核心已存在时”'
            '该符号的抓取价值。例如矿井和石巨人会生成矿石，矿石又能被地质学家吃掉，所以它们被列为地质学家的间接食物。'
            '同一符号同时符合多个核心时取最高值，不重复叠加。</p>'
            '<table><tr><th>成长核心</th><th>间接食物及估值（从高到低）</th></tr>' + growth_rows + '</table>'
            '<h2 id="combo-list">同花与系绳硬币</h2><p class="note">花色和悬浮金币会在旋转前被收走并直接结算，'
            '不需要出现在场上，因此按一次性金币估值，不占用出场格；它们也比“跳过”更有价值。'
            '当前策略只在持有对应物品或精华时启用这些组合加分。</p>'
            '<table><tr><th>组合</th><th>物品/精华</th><th>估分方式</th></tr>' + combo_rows + '</table>'
            '<h2 id="unmodeled">尚未完整建模的效果</h2><p class="note">'
            '“部分近似”包括生成类符号的产物、转化、计时/自消除、胶囊内容、骰子重掷期望、'
            '宝箱/矿石/种子/蛋/宝石的后续收益，以及部分箭头和物品相互作用；这些只按简化值或已列出的配对计入。'
            '“仅基础值”表示下列符号的特殊效果尚未计入：</p>'
            f'<p class="note"><b>仅基础值：</b>{esc(base_only_text)}</p>'
            '<h2 id="symbol-list">逐个符号</h2><p>表中“可被谁喂食”与“哪些符号可增益它”只列已实现的部分。</p>'
            '<input id="search" placeholder="搜索中文或英文名称"><table id="symbols"><thead><tr><th>名称</th>'
            '<th>品质</th><th>印刷基础/代理分（不是最终抓取分）</th><th>本次重估</th><th>覆盖程度</th><th>可被谁喂食</th>'
            '<th>已建模的增益符号</th><th>游戏描述（部分组名保留代码名称）</th></tr></thead><tbody>'
            + "".join(rows) + '</tbody></table>'
            '<h2 id="item-list">物品联动与被动收益</h2><p class="note">物品联动按“已有该物品时”的每回合增量估算；'
            '被动物品按剩余旋转折现。它们会同时影响符号选择，而不只是物品本身的抓位。</p>'
            '<table><tr><th>物品</th><th>匹配类型/组</th><th>每回合额外金币</th></tr>' + addition_rows + '</table>'
            '<table><tr><th>物品</th><th>匹配类型/组</th><th>倍率</th></tr>' + multiplier_rows + '</table>'
            '<table><tr><th>被动物品</th><th>每回合金币</th></tr>' + passive_rows + '</table>'
            '<h2 id="pair-list">1～3 个喂食卡的预计算表</h2><p class="note">假设不超过 20 个符号，'
            '随机排列，1、2、3 个相同喂食卡至少有一个相邻的概率约为 28.95%、49.71%、64.55%。'
            '表中未计其他物品、倍率、溢出和多个不同喂食者的竞争。'
            '消耗性食物的数字描述它在场时的当回合估分，不能把一次性奖励当成永久收入。'
            '永久成长的 4.5 是策略偏好，不是实际金币期望。</p>'
            '<table><tr><th>食物</th><th>喂食卡</th><th>基础</th><th>1 个</th><th>2 个</th><th>3 个</th><th>说明</th></tr>'
            + "".join(pairs) + '</table><script>const input=document.getElementById("search");'
            'input.addEventListener("input",()=>{const q=input.value.trim().toLowerCase();'
            'for(const row of document.querySelectorAll("#symbols tbody tr"))row.hidden=!row.textContent.toLowerCase().includes(q)});</script>'
            '</body></html>')
    target = ROOT / "evaluations" / "符号增量估值与覆盖.html"
    target.parent.mkdir(exist_ok=True)
    target.write_text(page, encoding="utf8")
    print(f"Wrote {len(rows)} symbols and {len(pairs)} food/feeder combinations")


if __name__ == "__main__":
    render()
