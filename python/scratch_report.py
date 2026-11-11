"""Build offline, sortable Chinese reports for the scratch experiment."""
from __future__ import annotations
import argparse
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def embedded(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def details_page(path: Path, title: str, rows: list[dict], names: dict[str, str]) -> None:
    template = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>__TITLE__</title><style>
body{font:15px system-ui,sans-serif;background:#f4f7fa;color:#172737;max-width:1200px;margin:30px auto;padding:0 20px}h1{font-size:23px}a{color:#106381}.box{background:white;padding:20px;border-radius:12px;margin:16px 0}label{margin-right:16px}input,select,button{padding:7px;border:1px solid #b9c8ce;border-radius:5px}table{border-collapse:collapse;width:100%}th,td{padding:9px;text-align:left;border-bottom:1px solid #dce5e7}th{background:#eff5f5}dialog{width:min(1100px,92vw);max-height:85vh;border:0;border-radius:10px;box-shadow:0 8px 40px #0006}dialog::backdrop{background:#0008}.muted{color:#65777e}.top{display:flex;justify-content:space-between;align-items:center}.scroll{overflow:auto;max-height:68vh}.win{color:#16704c;font-weight:bold}.loss{color:#9a3b38}
</style></head><body><p><a href="../报告.html">← 返回实验汇总</a></p><h1>__TITLE__</h1><div class="box">
<p class="muted">每局可查看按顺序记录的候选、选择、跳过、重掷和删除。名字优先使用本地关联表中的中文；没有匹配时保留英文。</p>
<label>种子 <input id="query" placeholder="输入种子"></label><label>结果 <select id="outcome"><option value="all">全部</option><option value="win">胜利</option><option value="loss">失败</option></select></label>
<label>排序 <select id="sort"><option value="seed">种子从小到大</option><option value="rents">交租次数从高到低</option><option value="spins">旋转次数从高到低</option><option value="remove">删除次数从高到低</option><option value="reroll">重掷次数从高到低</option></select></label>
<p id="count" class="muted"></p><table><thead><tr><th>种子</th><th>结果</th><th>交租</th><th>旋转</th><th>删除</th><th>重掷</th><th>详情</th></tr></thead><tbody id="body"></tbody></table>
<p><button id="prev">上一页</button> <span id="page"></span> <button id="next">下一页</button></p></div>
<dialog id="modal"><div class="top"><h2 id="modal-title"></h2><button id="close">关闭</button></div><div class="scroll"><table><thead><tr><th>旋转</th><th>阶段</th><th>候选</th><th>操作</th><th>选择或删除</th></tr></thead><tbody id="details"></tbody></table></div></dialog>
<script>const DATA=__DATA__;const NAMES=__NAMES__;
const phases={ready:'旋转前',spin_reward:'旋转后选符号',symbol_reward:'交租后选符号',item_reward:'选物品',essence_reward:'选精华'};
const acts={take:'选择',skip:'跳过',remove:'删除符号',reroll:'重掷'};let current=0,view=[];const size=50;
const byId=(x)=>document.getElementById(x);const name=(s)=>NAMES[s]||s||'—';
function cell(row,text,cls){const td=document.createElement('td');td.textContent=text;if(cls)td.className=cls;row.appendChild(td);return td}
function filter(){const q=byId('query').value.trim(),o=byId('outcome').value,s=byId('sort').value;view=DATA.filter(x=>(!q||String(x.seed).includes(q))&&(o==='all'||(o==='win'&&x.won)||(o==='loss'&&!x.won)));
const score=x=>s==='rents'?x.rents_paid:s==='spins'?x.spins:s==='remove'?(x.actions.remove||0):s==='reroll'?(x.actions.reroll||0):-x.seed;view.sort((a,b)=>score(b)-score(a)||a.seed-b.seed);current=0;draw()}
function details(x){byId('modal-title').textContent='种子 '+x.seed+' · '+(x.won?'胜利':'失败');const dest=byId('details');dest.replaceChildren();for(const d of x.decisions){const tr=document.createElement('tr');cell(tr,d.spin);cell(tr,phases[d.phase]||d.phase||'—');cell(tr,d.candidates.map(c=>name(c.name)).join('、')||'—');cell(tr,acts[d.action]||d.action);cell(tr,name(d.choice));dest.appendChild(tr)}byId('modal').showModal()}
function draw(){const dest=byId('body');dest.replaceChildren();const pages=Math.max(1,Math.ceil(view.length/size));current=Math.min(current,pages-1);for(const x of view.slice(current*size,(current+1)*size)){const tr=document.createElement('tr');cell(tr,x.seed);cell(tr,x.truncated?'未完成':x.won?'胜利':'失败',x.won?'win':'loss');cell(tr,x.rents_paid);cell(tr,x.spins);cell(tr,x.actions.remove||0);cell(tr,x.actions.reroll||0);const td=cell(tr,'');const b=document.createElement('button');b.textContent='查看';b.onclick=()=>details(x);td.appendChild(b);dest.appendChild(tr)}byId('count').textContent='符合条件 '+view.length+' 局';byId('page').textContent=(current+1)+' / '+pages;byId('prev').disabled=current===0;byId('next').disabled=current>=pages-1}
byId('query').oninput=filter;byId('outcome').onchange=filter;byId('sort').onchange=filter;byId('prev').onclick=()=>{current--;draw()};byId('next').onclick=()=>{current++;draw()};byId('close').onclick=()=>byId('modal').close();filter();</script></body></html>"""
    page = template.replace("__TITLE__", html.escape(title)).replace("__DATA__", embedded(rows)).replace("__NAMES__", embedded(names))
    path.write_text(page, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    root = args.root
    translated = json.loads((ROOT / "data" / "association_rules.json").read_text(encoding="utf-8"))["names"]
    names = {key: value.get("display", key) for key, value in translated.items()}
    summaries = {mode: json.loads((root / mode / "summary.json").read_text(encoding="utf-8"))
                 for mode in ("sparse", "legacy")}
    labels = {"sparse": "纯胜负奖赏", "legacy": "交租和收益提示", "previous_ppo": "之前的 PPO 模型"}
    stage_labels = {"initial": "初始模型", "middle": "训练 250 局后", "final": "训练 500 局后"}
    entries = []
    for mode, summary in summaries.items():
        for result in summary["evaluations"]:
            tag = result["label"]
            entries.append((mode, tag, f"{labels[mode]} · {stage_labels[tag]}", result))
    if summaries["legacy"].get("previous_ppo"):
        entries.append(("legacy", "previous_ppo", labels["previous_ppo"], summaries["legacy"]["previous_ppo"]))
    rows_html = []
    for mode, tag, title, result in entries:
        data_path = root / mode / f"{tag}_games.jsonl"
        rows = [json.loads(line) for line in data_path.read_text(encoding="utf-8").splitlines()]
        detail_path = root / mode / f"{tag}.html"
        details_page(detail_path, title, rows, names)
        actions = result.get("actions", {})
        rows_html.append("<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in [
            title, result["games"], result["wins"], f"{100 * result['win_rate']:.1f}%",
            f"{result['avg_rents']:.3f}", actions.get("remove", 0), actions.get("reroll", 0),
            result["truncated_games"]]) + f'<td><a href="{mode}/{tag}.html">查看 1000 局</a></td></tr>')
    page = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>从零训练实验</title><style>
body{font:15px system-ui,sans-serif;background:#f4f7fa;color:#172737;max-width:1200px;margin:30px auto;padding:0 20px}h1{font-size:25px}.box{background:white;padding:20px;border-radius:12px;margin:16px 0}table{border-collapse:collapse;width:100%}th,td{padding:9px;text-align:left;border-bottom:1px solid #dce5e7}th{background:#eff5f5}a{color:#106381}.muted{color:#65777e;line-height:1.8}</style></head><body>
<h1>进阶 20：从零训练实验</h1><div class="box"><p>两组模型均从随机权重开始，各训练 500 局；没有使用旧模型权重或候选的专家评分。每个阶段都用相同的 1000 个训练外种子评估。</p>
<p class="muted">纯胜负组：通关 1 分、失败 0 分。提示组：使用旧版交租和收益提示，指标更容易偏向活得更久。之前的 PPO 模型只用于对照，不参与从零训练。表格中的删除和重掷为这 1000 局里的总次数。</p>
<table><thead><tr><th>模型</th><th>局数</th><th>胜场</th><th>胜率</th><th>平均交租</th><th>删除</th><th>重掷</th><th>未完成</th><th>每局详情</th></tr></thead><tbody>__ROWS__</tbody></table>
<p class="muted">每组只有一次训练，1000 局评估只能作为初步观察。纯胜负组在 500 局训练中没有胜利，不能据此认为它已从胜利奖励中学到了赢法。</p></div></body></html>"""
    (root / "报告.html").write_text(page.replace("__ROWS__", "\n".join(rows_html)), encoding="utf-8")
    print(root / "报告.html")


if __name__ == "__main__":
    main()
